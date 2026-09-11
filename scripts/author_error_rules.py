#!/usr/bin/env python3
"""Author the error rules on the member-balance capability.

WHY THIS IS A SEPARATE, DELIBERATE STEP. Discovery walked only the success path: it typed a
valid member id into a healthy application and never saw a "no such member" result, a
maintenance modal, an expired session, or an HTTP 500. It therefore could not record how those
conditions should be handled -- and inventing rules for states it never observed is exactly the
kind of plausible-sounding fabrication the artifact format exists to prevent.

So the error taxonomy is authored here instead, by a human, against the conditions the target
application is known to produce. That is a legitimate and expected part of promoting a
discovered flow into a production capability: discovery establishes *how to drive the app*, and
review establishes *what its answers mean*. Recording it in a script rather than a hand edit
keeps the authoring reviewable, re-runnable, and diffable alongside the artifact it produces.

Run:  python scripts/author_error_rules.py [--artifact PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.artifact.schema import (  # noqa: E402
    ActionType,
    Artifact,
    Checkpoint,
    CheckpointKind,
    ErrorBucket,
    ErrorRule,
    Locator,
    LocatorBy,
    LocatorRule,
    RecoveryAction,
)
from src.artifact.store import load_artifact, save_artifact  # noqa: E402

DEFAULT_ARTIFACT = "artifacts/lookup_member_balance.json"
NEW_VERSION = "1.1"

AUTHORING_NOTE = (
    " Error rules were authored post-discovery (see scripts/author_error_rules.py): the "
    "discovery run walked only the success path, so the handling of no-such-member, "
    "maintenance interstitials, slow responses, expired sessions and server errors was added "
    "by review against the conditions this application is known to produce."
)


def _text(value: str) -> Checkpoint:
    return Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value=value)


# The modal's dismiss control. Recorded as a locator so replay never has to guess which element
# closes the obstruction; the caption is the button's visible text, with a structural fallback
# for the overlay's own button in case the caption is reworded.
CONTINUE_BUTTON = Locator(
    primary=LocatorRule(by=LocatorBy.TEXT, value="Continue"),
    fallbacks=[LocatorRule(by=LocatorBy.CSS, value="input[value='Continue']")],
    rationale=(
        "The interstitial's only button, identified by its visible caption; the value-based "
        "CSS selector is the backstop if the overlay markup changes around it."
    ),
)


def no_such_member() -> ErrorRule:
    """A real answer, not a failure: the member does not exist.

    The application reports this as an ordinary HTTP 200 page, which is precisely why it needs
    a rule -- nothing about the response distinguishes it from success to an engine that only
    checks for errors.
    """
    return ErrorRule(
        detect=_text("No such member"),
        bucket=ErrorBucket.BUSINESS_OUTCOME,
        outcome="no_such_member",
    )


def session_expired() -> ErrorRule:
    """Needs a human. This is the escalation seam: no amount of retrying re-authenticates."""
    return ErrorRule(
        detect=_text("Your session has expired"),
        bucket=ErrorBucket.HARD_FAILURE,
    )


def server_error() -> ErrorRule:
    """The application broke. Retrying a 500 in a back office is how duplicate postings happen."""
    return ErrorRule(
        detect=_text("HTTP 500 - Internal Server Error"),
        bucket=ErrorBucket.HARD_FAILURE,
    )


def maintenance_interstitial() -> ErrorRule:
    """A transient obstruction: close it and carry on."""
    return ErrorRule(
        detect=_text("System Notice"),
        bucket=ErrorBucket.RECOVERABLE,
        action=RecoveryAction.DISMISS,
        recovery_target=CONTINUE_BUTTON,
    )


def response_not_arrived() -> ErrorRule:
    """The previous page is still showing, so the response is late rather than wrong.

    Detected by what is on screen -- still the search form -- because slowness itself is not
    visible on a page. This is the safety net for a delay that outlasts the step's own wait
    budget; within that budget the wait policy absorbs it and no rule is consulted at all.
    """
    return ErrorRule(
        detect=_text("Member Inquiry"),
        bucket=ErrorBucket.RECOVERABLE,
        action=RecoveryAction.WAIT,
    )


def rules_for(action: ActionType, index: int) -> list[ErrorRule]:
    """Which conditions can plausibly interrupt this step.

    Ordered definitive-first on purpose. Replay takes the first matching rule, so a page that
    is both "a business answer" and "not what this step expected" must be read as the answer,
    and a broken session must never be retried as though it were a slow one.
    """
    rules: list[ErrorRule] = []
    if action in (ActionType.CLICK, ActionType.READ):
        rules.append(no_such_member())
    rules += [session_expired(), server_error()]
    if action in (ActionType.CLICK, ActionType.READ):
        rules.append(maintenance_interstitial())
    if action is ActionType.CLICK:
        rules.append(response_not_arrived())
    return rules


def author(artifact: Artifact) -> Artifact:
    """Return the artifact with error rules attached and its version bumped."""
    payload = artifact.model_dump(mode="json")

    for step_payload, step in zip(payload["steps"], artifact.steps, strict=True):
        step_payload["on_error"] = [
            rule.model_dump(mode="json") for rule in rules_for(step.action, step.index)
        ]

    # The capability's contract changed -- callers can now receive no_such_member -- so the
    # recording's own version moves. schema_version does not: the format is unchanged.
    payload["version"] = NEW_VERSION
    if AUTHORING_NOTE.strip() not in payload["description"]:
        payload["description"] = payload["description"].rstrip(".") + "." + AUTHORING_NOTE

    # Re-validated, not just mutated: authored rules are held to exactly the same schema as
    # recorded ones.
    return Artifact.model_validate(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifact", default=DEFAULT_ARTIFACT)
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change without writing"
    )
    args = parser.parse_args()

    before = load_artifact(args.artifact)
    after = author(before)

    print(f"{args.artifact}: version {before.version} -> {after.version}")
    for step in after.steps:
        if not step.on_error:
            continue
        print(f"  step {step.index} ({step.action}):")
        for rule in step.on_error:
            target = (
                f" via {rule.recovery_target.primary.by}={rule.recovery_target.primary.value!r}"
                if rule.recovery_target
                else ""
            )
            detail = rule.outcome or (str(rule.action) + target if rule.action else "stop")
            print(f"    {rule.detect.value!r} -> {rule.bucket} ({detail})")

    if args.dry_run:
        print("\n(dry run; nothing written)")
        return 0

    path = save_artifact(after, args.artifact)
    load_artifact(path)  # prove the written file loads before claiming success
    print(f"\nwrote {path} and confirmed it loads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
