#!/usr/bin/env python3
"""Command-line entry point for the computer-use automation system.

Discovery is the only subcommand today; `replay` lands with the replay engine. The flags exist
to make the expensive thing explicit: a run costs money only when you leave off `--stub`, and
only ever opens a browser window when you ask for `--headed`.
"""

from __future__ import annotations

import argparse
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

from src.agent.llm import DEFAULT_MODEL, AnthropicLLMClient, LLMClient, StubLLMClient
from src.agent.loop import DiscoveryAgent, StopReason
from src.agent.prompt import build_system_prompt
from pydantic import ValidationError

from src.artifact.schema import ActionType, Artifact, RiskLevel, SurfaceType, Target
from src.artifact.store import load_artifact, save_artifact
from src.evidence import RunRecorder, redact, redact_url
from src.replay.engine import ReplayEngine, ReplayError
from src.escalation.operator import build_console
from src.escalation.request import InterventionRequest, StuckReason
from src.escalation.session import ControlViolation, EscalationHandler, SessionControl
from src.safety.guard import ConfirmationCallback, GuardedSurface, PolicyViolation
from src.safety.policy import DEFAULT_POLICY_PATH, load_policy
from src.surface.base import Surface
from src.surface.web import WebSurface

EPILOG = """\
PREREQUISITE: the target application must already be running. For the bundled mock bank:

    python mock_app/app.py          # serves http://127.0.0.1:5001

examples:
  # zero-cost dry run against a scripted model -- no API key needed, nothing billed
  python cli.py discover --goal "Look up member 12345 and read their balance" \\
      --url http://localhost:5001 --input member_id=12345 --stub \\
      --out artifacts/lookup_member_balance.json

  # real discovery (calls the Anthropic API and costs money)
  python cli.py discover --goal "Look up member 12345 and read their balance" \\
      --url http://localhost:5001 --input member_id=12345 \\
      --out artifacts/lookup_member_balance.json

Evidence for every run is written to evidence/<run_id>/. Values are redacted on the way to
disk: balances and identifiers are masked, and the artifact never stores values at all.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Discover and replay reusable UI automation capabilities.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    discover = subcommands.add_parser(
        "discover",
        help="drive the target with an LLM and record a reusable artifact",
        description="Explore a running application to discover a flow, and save it as an "
        "artifact that can later be replayed without a model.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    discover.add_argument("--goal", required=True, help="what to accomplish, in plain language")
    discover.add_argument(
        "--url", default="http://localhost:5001", help="entry URL of the running target app"
    )
    discover.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="an input parameter; repeatable. Values typed into the page are recorded as "
        "{{NAME}} so the artifact is reusable.",
    )
    discover.add_argument(
        "--out",
        default="artifacts/capability.json",
        help="where to write the artifact (default: %(default)s)",
    )
    discover.add_argument("--max-steps", type=int, default=15, help="step budget (default: 15)")
    discover.add_argument(
        "--timeout", type=float, default=180.0, help="wall-clock budget in seconds"
    )
    discover.add_argument(
        "--headed", action="store_true", help="show the browser window (default: headless)"
    )
    discover.add_argument(
        "--stub",
        action="store_true",
        help="use a scripted stub instead of the real API: zero cost, no key required",
    )
    discover.add_argument("--model", default=DEFAULT_MODEL, help="model id (default: %(default)s)")
    discover.add_argument("--verbose", "-v", action="store_true", help="log every action")

    replay = subcommands.add_parser(
        "replay",
        help="execute a saved artifact deterministically -- no model, no cost",
        description="Replay a recorded capability. There is no LLM in this path: every choice "
        "was made at discovery time and is read from the artifact.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    replay.add_argument("--artifact", required=True, help="path to the artifact JSON")
    replay.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="an input parameter; repeatable. Must cover every input the artifact declares.",
    )
    replay.add_argument(
        "--headed", action="store_true", help="show the browser window (default: headless)"
    )
    replay.add_argument(
        "--inject",
        default=None,
        metavar="STATE",
        help="append ?inject=STATE to the entry URL, to exercise the mock app's injectable "
        "error states (not_found, slow, popup, session_expired, server_error)",
    )
    replay.add_argument("--verbose", "-v", action="store_true", help="log every action")

    for sub in (discover, replay):
        sub.add_argument(
            "--policy",
            default=str(DEFAULT_POLICY_PATH),
            help="policy file enforced on every action (default: %(default)s)",
        )
        sub.add_argument(
            "--operator",
            default="none",
            choices=("cli", "auto-approve", "auto-abort", "none"),
            help="who answers when the run needs a human. 'cli' pauses and hands you the "
            "browser (use with --headed); 'none' (default) keeps the fail-closed behaviour of "
            "refusing anything that would need approval.",
        )
        sub.add_argument(
            "--unsafe",
            action="store_true",
            help="DEBUGGING ONLY: drive the browser with no policy enforcement at all",
        )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return run_discover(args) if args.command == "discover" else run_replay(args)


def build_escalation(args: argparse.Namespace, recorder: RunRecorder) -> EscalationHandler | None:
    """Assemble the handover machinery, or None when no operator is available.

    None is not a degraded mode: it is the documented fail-closed default. Without a console
    there is nobody to authorize a risky action or resolve a dead end, so those keep refusing.
    """
    console = build_console(args.operator, headed=args.headed)
    if console is None:
        return None
    if args.operator == "cli" and not args.headed:
        print(
            "note: --operator cli without --headed means there is no window to work in.\n"
            "      You can still resume or abort, but you cannot act on the page.",
            file=sys.stderr,
        )
    return EscalationHandler(console=console, control=SessionControl(), recorder=recorder)


def build_surface(
    args: argparse.Namespace, escalation: EscalationHandler | None = None
) -> Surface | None:
    """Construct the surface every caller will drive: policy-guarded unless explicitly disabled.

    Returns None when the policy could not be loaded, which is a refusal to run rather than a
    fallback to permissiveness.
    """
    inner = WebSurface(headless=not args.headed)
    if args.unsafe:
        print(
            "WARNING: --unsafe given. No allowlist, no risky-action policy, no decision log.\n"
            "         Every action goes straight to the browser. Debugging only -- never for a\n"
            "         run against anything you care about.",
            file=sys.stderr,
        )
        return inner

    try:
        policy = load_policy(args.policy)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        inner.close()
        return None

    print(
        f"policy {args.policy}: {len(policy.allowed_url_patterns)} url pattern(s), "
        f"{len(policy.allowed_actions)} action(s), risky={policy.risky_action_mode}"
    )
    if escalation is None:
        return GuardedSurface(inner, policy)

    # With an operator attached, CONFIRM mode routes to a real human decision instead of the
    # fail-closed refusal -- the guard's callback seam, unchanged.
    guarded = GuardedSurface(
        inner,
        policy,
        control=escalation.control,
        confirm=_confirm_via_operator(escalation, inner),
    )
    return guarded


def _confirm_via_operator(
    escalation: EscalationHandler, surface: Surface
) -> ConfirmationCallback:
    """Turn the guard's confirmation callback into a human decision.

    The operator's RESUME *is* the authorization: they were shown what the automation wants to
    do, they held the session while deciding, and handing control back is their approval.

    Reads the location from the concrete surface rather than the guarded one, deliberately: by
    the time the human is deciding, control has been ceded and the guard refuses automation
    calls -- and asking someone to authorize a money-moving click without telling them which
    page it is on is not asking them anything.
    """

    def current_url() -> str:
        try:
            return surface.current_url()
        except Exception:  # noqa: BLE001 - context for a human, never worth failing the run
            return ""

    def confirm(action: ActionType, target: str, risk: RiskLevel) -> bool:
        request = InterventionRequest(
            run_id=escalation.recorder.run_id if escalation.recorder else "unknown",
            capability_id="(authorization)",
            goal="authorize a risky action before it runs",
            reason=StuckReason.RISKY_ACTION_CONFIRMATION,
            action=str(action),
            url=current_url(),
            detail=f"the automation wants to perform a {risk} {action} on {target}",
        )
        return escalation.escalate(request, snapshot=current_url).resumed

    return confirm


def run_discover(args: argparse.Namespace) -> int:
    """Run one discovery, writing evidence throughout and an artifact at the end."""
    try:
        inputs = _parse_inputs(args.input)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not _is_reachable(args.url):
        print(
            f"error: nothing is serving {args.url}\n"
            "       start the target first, e.g. `python mock_app/app.py`",
            file=sys.stderr,
        )
        return 2

    recorder = RunRecorder.start("discovery")
    llm, model_id = _build_client(args)
    if llm is None:
        return 2

    print(f"run {recorder.run_id}  model={model_id}  evidence={recorder.dir}/")
    recorder.event(
        "run_started",
        goal=args.goal,
        url=redact_url(args.url),
        model=model_id,
        inputs={name: redact(value) for name, value in inputs.items()},
        max_steps=args.max_steps,
        headed=args.headed,
    )

    escalation = build_escalation(args, recorder)
    surface = build_surface(args, escalation)
    if surface is None:
        return 2
    target = Target(
        app_id=_app_id(args.url),
        entry_url=args.url,
        surface_type=SurfaceType.LEGACY_WEB,
    )

    def on_iteration(entry) -> None:
        """Write each iteration to evidence as it happens, with a screenshot of the result."""
        recorder.step_event(entry)
        for dropped in entry.pruned:
            # Its own event as well as the step record: a reviewer auditing proposal quality
            # should be able to grep one line per pruned candidate, not unpack nested arrays.
            recorder.event(
                "pruned_candidate",
                step_index=entry.step_index,
                iteration=entry.iteration,
                role=dropped.role,
                matched=dropped.matched,
            )
        recorder.screenshot(surface, f"{entry.iteration:02d}-{entry.action}")
        marker = "ok " if entry.recorded else "-- "
        print(f"  {marker}{entry.iteration:>2} {entry.action:<9} {entry.outcome}")

    agent = DiscoveryAgent(
        surface,
        llm,
        model_id=model_id,
        run_id=recorder.run_id,
        on_iteration=on_iteration,
        escalation=escalation,
    )

    try:
        try:
            result = agent.run(
                args.goal, target, inputs, max_steps=args.max_steps, timeout_s=args.timeout
            )
        except (PolicyViolation, ControlViolation) as exc:
            # The agent proposed something policy forbids, or acted out of turn. Deliberately
            # fatal rather than fed back as a retryable hint: a discovery run that quietly works
            # around the allowlist would record a flow nobody authorized.
            recorder.event("policy_violation", detail=str(exc))
            recorder.screenshot(surface, "policy-violation")
            recorder.finish(kind="discovery", status="policy_violation", note=str(exc))
            print(f"error: {exc}", file=sys.stderr)
            return 3
        if result.stop_reason is not StopReason.SUCCESS:
            # The failure screenshot is the whole reason a person opens an evidence directory.
            recorder.screenshot(surface, "failure")
    finally:
        surface.close()

    artifact_path: Path | None = None
    if result.artifact is not None:
        artifact_path = save_artifact(result.artifact, args.out)

    usage = getattr(llm, "usage", None)
    recorder.event(
        "run_finished",
        stop_reason=str(result.stop_reason),
        steps_recorded=result.steps_recorded,
        note=result.note,
        artifact=str(artifact_path) if artifact_path else None,
    )
    recorder.finish(
        kind="discovery",
        goal=args.goal,
        url=redact_url(args.url),
        model=model_id,
        stop_reason=str(result.stop_reason),
        outcome="success" if result.succeeded else "failed",
        steps=result.steps_recorded,
        iterations=len(result.transcript),
        note=result.note,
        artifact=str(artifact_path) if artifact_path else None,
        fallbacks_promoted=sum(1 for entry in result.transcript if entry.used_fallback),
        pruned_candidates=sum(len(entry.pruned) for entry in result.transcript),
        token_usage=(
            {
                "requests": usage.requests,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "estimated_cost_usd": usage.estimated_cost_usd,
            }
            if usage
            else None
        ),
    )

    _report(result, artifact_path, recorder, usage)
    return 0 if result.succeeded else 1


def run_replay(args: argparse.Namespace) -> int:
    """Execute one artifact and print its ReplayResult as JSON."""
    try:
        inputs = _parse_inputs(args.input)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        artifact = load_artifact(args.artifact)
    except (OSError, ValidationError) as exc:
        # A rejected artifact is a real answer: the schema refuses recordings that bake observed
        # data into targeting, so this is where that enforcement reaches an operator.
        print(f"error: {args.artifact} is not a loadable artifact:\n{exc}", file=sys.stderr)
        return 2

    # Probe before injecting. The check asks "is the target up?", and an injected state is a
    # property of the scenario, not of the server -- `--inject slow` delays every response past
    # the probe's timeout and would otherwise be reported as a target that is not running.
    if not _is_reachable(artifact.target.entry_url):
        print(
            f"error: nothing is serving {artifact.target.entry_url}\n"
            "       start the target first, e.g. `python mock_app/app.py`",
            file=sys.stderr,
        )
        return 2

    if args.inject:
        artifact = _with_injection(artifact, args.inject)

    recorder = RunRecorder.start("replay")
    escalation = build_escalation(args, recorder)
    surface = build_surface(args, escalation)
    if surface is None:
        return 2
    engine = ReplayEngine(recorder=recorder, escalation=escalation)
    try:
        result = engine.replay(artifact, inputs, surface)
    except ReplayError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ControlViolation as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except PolicyViolation as exc:
        # A refusal is not a replay outcome: the automation never got to find out what the
        # application would have said.
        print(f"error: {exc}", file=sys.stderr)
        return 3
    finally:
        surface.close()

    print(result.model_dump_json(indent=2))
    # A business outcome is a successful run, so it exits 0: the automation did its job and the
    # answer happened to be negative. Only a hard failure is a non-zero exit.
    return 1 if result.is_failure else 0


def _with_injection(artifact: Artifact, inject: str) -> Artifact:
    """Return a copy of the artifact whose entry URL carries ?inject=STATE.

    Rewrites the navigate steps too, since those hold their own URL, and re-validates the whole
    thing so an injected artifact is held to exactly the same schema rules as a recorded one.
    """
    payload = artifact.model_dump(mode="json")
    payload["target"]["entry_url"] = _append_query(payload["target"]["entry_url"], inject)
    for step in payload["steps"]:
        if step["action"] == "navigate" and step.get("value"):
            step["value"] = _append_query(step["value"], inject)
    return Artifact.model_validate(payload)


def _append_query(url: str, inject: str) -> str:
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}inject={inject}"


def _build_client(args: argparse.Namespace) -> tuple[LLMClient | None, str]:
    """Construct the stub or the real client. The real one is only ever built on request."""
    if args.stub:
        from src.agent.llm import Decision  # local: only the dry-run path needs it

        return StubLLMClient(_dry_run_script(args.url, args.input)), "stub"

    try:
        from dotenv import load_dotenv

        load_dotenv()  # entry point's job, not the client's
    except ImportError:
        pass

    try:
        return (
            AnthropicLLMClient(model=args.model, system_prompt=build_system_prompt()),
            args.model,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, args.model


def _dry_run_script(url: str, raw_inputs: list[str]):
    """A scripted flow for `--stub`, mirroring the mock app's search-and-read path.

    Hardcoded to the bundled mock app on purpose: the point of the dry run is to prove the
    plumbing -- CLI, evidence, redaction, artifact emission -- end to end at zero cost, not to
    pretend a model is thinking.
    """
    from src.agent.llm import ControlAction, Decision
    from src.artifact.schema import (
        ActionType,
        Checkpoint,
        CheckpointKind,
        Locator,
        LocatorBy,
        LocatorRule,
    )

    member_id = dict(pair.split("=", 1) for pair in raw_inputs).get("member_id", "12345")

    def locator(by: LocatorBy, value: str, rationale: str, *fallbacks: LocatorRule) -> Locator:
        return Locator(
            primary=LocatorRule(by=by, value=value),
            fallbacks=list(fallbacks),
            rationale=rationale,
        )

    return [
        Decision(
            thought="Open the member search screen.",
            action=ActionType.NAVIGATE,
            value=url,
            checkpoint=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Member Inquiry"),
        ),
        Decision(
            thought="The search field carries a visible label.",
            action=ActionType.TYPE,
            target=locator(
                LocatorBy.LABEL,
                "Member ID",
                "Visible labels are user-facing contract; this screen has no test ids.",
                LocatorRule(by=LocatorBy.CSS, value="form input[type=text]"),
            ),
            value=member_id,
        ),
        Decision(
            thought="Submit the search.",
            action=ActionType.CLICK,
            target=locator(
                LocatorBy.TEXT, "Search", "The submit button is identified by its caption."
            ),
            checkpoint=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Member Detail"),
        ),
        Decision(
            thought="The balance sits one table deep with no label of its own.",
            action=ActionType.READ,
            target=locator(
                LocatorBy.STRUCTURAL,
                "//td[normalize-space()='Savings Balance']/following-sibling::td[1]"
                "//tr[td[normalize-space()='Available']]/td[2]",
                "Reached via the 'Savings Balance' row heading, then the nested 'Available' row.",
            ),
            extract="savings_balance",
        ),
        Decision(
            thought="The detail page shows the member and their balance.",
            action=ControlAction.DONE,
            checkpoint=Checkpoint(
                kind=CheckpointKind.ALL_OF,
                children=[
                    Checkpoint(kind=CheckpointKind.URL_MATCHES, value="/member"),
                    Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Savings Balance"),
                ],
            ),
        ),
    ]


def _report(result, artifact_path: Path | None, recorder: RunRecorder, usage) -> None:
    """Print the run summary a person actually wants at the end."""
    print(f"\nstop reason : {result.stop_reason}")
    print(f"steps       : {result.steps_recorded} recorded / {len(result.transcript)} iterations")
    if result.note:
        print(f"note        : {result.note}")
    promoted = [entry for entry in result.transcript if entry.used_fallback]
    if promoted:
        print(f"fallbacks   : {len(promoted)} step(s) resolved via a fallback locator (drift)")
    pruned = sum(len(entry.pruned) for entry in result.transcript)
    if pruned:
        print(f"pruned      : {pruned} data-shaped locator candidate(s) dropped at assembly")
    if artifact_path:
        print(f"artifact    : {artifact_path}")
    print(f"evidence    : {recorder.dir}/")
    if usage and usage.requests:
        cost = usage.estimated_cost_usd
        cost_text = f"${cost:.4f}" if cost is not None else "unknown rate"
        print(
            f"tokens      : {usage.input_tokens} in / {usage.output_tokens} out "
            f"over {usage.requests} requests (~{cost_text})"
        )


def _parse_inputs(pairs: list[str]) -> dict[str, str]:
    inputs: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--input expects NAME=VALUE, got {pair!r}")
        name, _, value = pair.partition("=")
        if not name.strip():
            raise ValueError(f"--input needs a name, got {pair!r}")
        inputs[name.strip()] = value
    return inputs


def _is_reachable(url: str) -> bool:
    """Fail fast with a useful message rather than letting the browser time out."""
    try:
        urllib.request.urlopen(url, timeout=3).read()
        return True
    except (urllib.error.HTTPError,):
        return True  # an HTTP error is still a server answering
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return False


def _app_id(url: str) -> str:
    """Derive a stable app id from the entry URL's host."""
    from urllib.parse import urlparse

    return urlparse(url).hostname or "unknown-app"


if __name__ == "__main__":
    raise SystemExit(main())
