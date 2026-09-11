"""The deterministic replay engine: the production execution path.

Given an artifact and inputs, this re-runs a recorded flow with **no LLM in the decision loop**.
Every choice was made once, at discovery time, and written down; replay only binds parameters,
resolves locators, executes actions, and checks conditions. That is the whole point of the
artifact format -- discovery is the expensive, nondeterministic, billable phase, and it happens
once per capability rather than once per run.

ENFORCED BOUNDARY: nothing in `src/replay/` may import `src/agent/` or the anthropic SDK. Not by
convention -- `tests/test_replay.py` reads every module in this package and fails if either
appears. The guarantee that matters to an operator is that a replay cannot call a model, cannot
improvise, and cannot cost money, and a guarantee that rests on nobody adding an import is not
a guarantee.

Determinism also means no fixed sleeps anywhere: every wait is a condition with a timeout, so a
slow application is waited out while a stuck one is reported.
"""

from __future__ import annotations

import logging
import re
import time

from src.artifact.schema import (
    ActionType,
    Artifact,
    CheckpointKind,
    ErrorBucket,
    ErrorRule,
    Locator,
    LocatorBy,
    LocatorRule,
    RecoveryAction,
    Step,
    describe_checkpoint,
    template_params,
)
from src.evidence import RunRecorder, redact, redact_url
from src.replay.result import (
    FailureDetail,
    RecoveryEvent,
    ReplayResult,
    ReplayStatus,
)
from src.surface.base import Surface, SurfaceError

log = logging.getLogger(__name__)

MAX_RECOVERIES_PER_STEP = 2
MAX_RECOVERIES_PER_RUN = 5
"""Bounds on recovery, per step and per run.

Recovery exists for transient obstacles, and a transient obstacle clears. Something that needs a
third dismissal on one step, or a sixth across a run, is not transient -- it is a changed
application, and continuing to retry would turn a diagnosable failure into a hang.
"""

DISMISS_CAPTIONS = ("Continue", "OK", "Ok", "Close", "Dismiss", "Acknowledge")
"""Captions guessed at, in order, only when an error rule records no `recovery_target`.

The deterministic path is `ErrorRule.recovery_target`: a locator recorded at discovery time,
when an agent could actually see the button. This list is the fallback for rules authored
without one -- a run-time guess by an engine that cannot see the page, kept so such a rule
degrades rather than silently doing nothing. Taking this path is logged at WARNING, because a
replay that depends on a guess is not fully deterministic and a reviewer should know.
"""

SUCCESS_OUTCOME = "success"


class ReplayError(Exception):
    """A replay was rejected before execution began.

    Deliberately an exception rather than a `ReplayResult`: a missing input is not an outcome of
    running the automation, it is a malformed request that never ran. Returning HARD_FAILURE for
    it would put a caller's own bug in the same channel as "the bank's server broke", and those
    need different responses from different people.
    """


class MissingInputError(ReplayError):
    """A required input was not supplied, or a step references an input that was not."""


class ReplayEngine:
    """Executes artifacts. Stateless between runs; `replay` owns everything it accumulates."""

    def __init__(
        self,
        *,
        recorder: RunRecorder | None = None,
        escalation: object | None = None,
        success_timeout_s: float = 10.0,
        poll_ms: int = 250,
        max_recoveries_per_step: int = MAX_RECOVERIES_PER_STEP,
        max_recoveries_per_run: int = MAX_RECOVERIES_PER_RUN,
    ) -> None:
        self.recorder = recorder
        """Evidence sink. Created per run when not supplied, so a caller that wants one
        directory for a batch of replays can pass its own."""
        self.escalation = escalation
        """An EscalationHandler, or None. Typed loosely on purpose: replay must not depend on
        the escalation package to run, and with no handler attached the engine behaves exactly
        as it did before -- a hard failure is returned, not referred to anybody."""
        self.success_timeout_s = success_timeout_s
        self.poll_ms = poll_ms
        self.max_recoveries_per_step = max_recoveries_per_step
        self.max_recoveries_per_run = max_recoveries_per_run

    # -- entry point --------------------------------------------------------------------

    def replay(
        self, artifact: Artifact, inputs: dict[str, str], surface: Surface
    ) -> ReplayResult:
        """Execute `artifact` against `surface`, binding `inputs`, and report what happened."""
        bound = self._validate_inputs(artifact, inputs)
        recorder = self.recorder or RunRecorder.start("replay")
        started = time.monotonic()

        recorder.event(
            "run_started",
            kind="replay",
            capability_id=artifact.capability_id,
            artifact_version=artifact.version,
            schema_version=artifact.schema_version,
            entry_url=redact_url(artifact.target.entry_url),
            inputs={name: redact(value) for name, value in bound.items()},
            steps=len(artifact.steps),
        )

        outputs: dict[str, str] = {}
        recoveries: list[RecoveryEvent] = []
        executed = 0

        for step in artifact.steps:
            step_recoveries = 0
            escalated_once = False
            while True:
                step_started = time.monotonic()
                attempt = self._run_step(step, bound, surface, outputs)
                elapsed_ms = int((time.monotonic() - step_started) * 1000)
                recorder.event(
                    "step",
                    step_index=step.index,
                    action=str(step.action),
                    locator_used=attempt.locator_used,
                    fallback_used=attempt.fallback_used,
                    checkpoint=describe_checkpoint(step.checkpoint),
                    checkpoint_passed=attempt.checkpoint_passed,
                    duration_ms=elapsed_ms,
                    ok=attempt.failure is None,
                    failure=attempt.failure,
                    extracted=attempt.extracted,
                )
                if attempt.failure is None:
                    executed += 1
                    break

                # Something went wrong. The artifact's own error rules decide what it means --
                # the engine never guesses, because guessing is what a model would do.
                rule = self._match_error_rule(step, surface)

                if rule is None:
                    if not escalated_once and self._refer_to_human(
                        artifact, step, attempt.failure or "", surface, recorder
                    ):
                        # A human dealt with it and handed the session back. Retry the step
                        # exactly once: if it fails again, the intervention did not work and
                        # asking a second time would just loop a person.
                        escalated_once = True
                        continue
                    return self._fail(
                        recorder, surface, step, attempt, started, outputs, recoveries, executed,
                        message=attempt.failure,
                    )

                detected = describe_checkpoint(rule.detect) or ""

                if rule.bucket is ErrorBucket.BUSINESS_OUTCOME:
                    recorder.event(
                        "business_outcome",
                        step_index=step.index,
                        detected=detected,
                        outcome=rule.outcome,
                    )
                    return self._business_outcome(
                        recorder, artifact, rule, started, outputs, recoveries, executed
                    )

                if rule.bucket is ErrorBucket.HARD_FAILURE:
                    recorder.event("hard_failure_rule", step_index=step.index, detected=detected)
                    if not escalated_once and self._refer_to_human(
                        artifact, step, f"a recorded hard-failure condition ({detected})",
                        surface, recorder,
                    ):
                        escalated_once = True
                        continue
                    return self._fail(
                        recorder, surface, step, attempt, started, outputs, recoveries, executed,
                        message=f"a recorded hard-failure condition was detected ({detected})",
                    )

                # RECOVERABLE, and bounded.
                step_recoveries += 1
                if (
                    step_recoveries > self.max_recoveries_per_step
                    or len(recoveries) >= self.max_recoveries_per_run
                ):
                    return self._fail(
                        recorder, surface, step, attempt, started, outputs, recoveries, executed,
                        message=(
                            f"recovery limit reached ({step_recoveries - 1} on this step, "
                            f"{len(recoveries)} in this run); the condition is not transient"
                        ),
                    )

                event = RecoveryEvent(
                    step_index=step.index,
                    detected=detected,
                    action=rule.action or RecoveryAction.RETRY,
                    attempt=step_recoveries,
                )
                recoveries.append(event)
                recorder.event("recovery", **event.model_dump(mode="json"))
                log.info("step %d: recovering via %s (attempt %d)", step.index, event.action, step_recoveries)
                self._recover(event.action, step, rule, surface)

        return self._finish(recorder, artifact, surface, started, outputs, recoveries, executed)

    # -- per step -----------------------------------------------------------------------

    def _run_step(
        self, step: Step, inputs: dict[str, str], surface: Surface, outputs: dict[str, str]
    ) -> _Attempt:
        """Bind, resolve, execute, wait, verify. Returns what happened, without raising."""
        attempt = _Attempt()

        # Declare before anything is resolved or executed. On a raw surface this is a no-op; on
        # a guarded one it is what lets policy treat a money-moving step differently from a read.
        surface.declare_step_risk(step.risk)

        locator = None
        if step.target is not None:
            locator = _bind_locator(step.target, inputs)
            handle = surface.find(locator)
            if handle is None:
                attempt.failure = (
                    f"no element matched {locator.primary.by}={locator.primary.value!r} "
                    "or any fallback"
                )
                return attempt
            attempt.locator_used = f"{handle.rule.by}={handle.rule.value}"
            attempt.fallback_used = handle.was_fallback
            if handle.was_fallback:
                # Drift signal. Surfaced, never hidden: the step passed, but on the recording's
                # second choice, which is one release away from passing on none of them.
                log.warning(
                    "step %d resolved via fallback %d (%s); the primary no longer matches",
                    step.index, handle.candidate_index, attempt.locator_used,
                )

        try:
            value = _bind_text(step.value, inputs)
            if step.action is ActionType.NAVIGATE:
                surface.open(value or "")
            elif step.action is ActionType.CLICK or step.action is ActionType.DISMISS:
                surface.click(locator)
            elif step.action is ActionType.TYPE:
                surface.type(locator, value or "")
            elif step.action is ActionType.READ:
                text = surface.read(locator)
                if step.extract:
                    outputs[step.extract] = text
                    attempt.extracted = text
            elif step.action is ActionType.WAIT:
                pass  # the wait policy below is the whole action
        except SurfaceError as exc:
            attempt.failure = str(exc)
            return attempt

        if step.wait.until is not None:
            if not surface.wait_for(step.wait.until, step.wait.timeout_s, step.wait.poll_ms):
                attempt.failure = (
                    f"the wait condition {describe_checkpoint(step.wait.until)} never became "
                    f"true within {step.wait.timeout_s}s"
                )
                return attempt

        if step.checkpoint is not None:
            passed = surface.wait_for(step.checkpoint, step.wait.timeout_s, step.wait.poll_ms)
            attempt.checkpoint_passed = passed
            if not passed:
                attempt.failure = (
                    f"the checkpoint {describe_checkpoint(step.checkpoint)} did not hold after "
                    "the action"
                )
        return attempt

    def _refer_to_human(
        self,
        artifact: Artifact,
        step: Step,
        detail: str,
        surface: Surface,
        recorder: RunRecorder,
    ) -> bool:
        """Offer a hard failure to a human. Returns True if they fixed it and resumed.

        Only reached when a console is attached. Without one the engine is unchanged: a hard
        failure is reported, not referred, because referring a failure to nobody is just a
        slower way of failing.
        """
        if self.escalation is None:
            return False

        from src.escalation.request import InterventionRequest, StuckReason

        shot = recorder.screenshot(surface, f"intervention-step-{step.index}")
        request = InterventionRequest(
            run_id=recorder.run_id,
            capability_id=artifact.capability_id,
            goal=artifact.provenance.goal,
            reason=StuckReason.UNRECOVERABLE_ERROR,
            step_index=step.index,
            action=str(step.action),
            url=_safe_url(surface),
            page_summary=_observed(surface),
            screenshot=str(shot) if shot else None,
            detail=detail,
        )
        response = self.escalation.escalate(request, snapshot=lambda: _observed(surface))
        return bool(response.resumed)

    def _match_error_rule(self, step: Step, surface: Surface) -> ErrorRule | None:
        """Return the first recorded error rule whose detector matches the live page.

        In order, and first match wins: the artifact's ordering is a recorded judgment about
        precedence -- "no such member" before "session expired" -- and reordering it here would
        silently change what a reviewed recording means.
        """
        for rule in step.on_error:
            try:
                if surface.check(rule.detect):
                    return rule
            except SurfaceError as exc:
                log.debug("error detector %s could not be evaluated: %s", rule.detect.kind, exc)
        return None

    def _recover(
        self, action: RecoveryAction, step: Step, rule: ErrorRule, surface: Surface
    ) -> None:
        """Apply a recovery, then let the caller retry the step.

        Nothing here advances the flow; recovery only clears an obstruction so the recorded step
        can be attempted again. That keeps the retry identical to the original -- which is what
        makes a recovered run as trustworthy as one that never needed recovering.
        """
        if action is RecoveryAction.DISMISS:
            self._dismiss(rule, surface)
        elif action is RecoveryAction.WAIT:
            # A condition, never a duration. With nothing to wait for there is nothing this can
            # legitimately do, so it degrades to a plain retry rather than sleeping.
            condition = step.wait.until or step.checkpoint
            if condition is not None:
                surface.wait_for(condition, step.wait.timeout_s, step.wait.poll_ms)
        # RETRY needs no action at all.

    def _dismiss(self, rule: ErrorRule, surface: Surface) -> None:
        """Close the obstruction the rule detected, by recorded target where one exists.

        Two paths, and which one ran is always logged, because they carry very different
        confidence. A `recovery_target` was recorded at discovery time by an agent that could
        see the button -- it is part of the reviewed contract. The caption heuristic is a guess
        made at run time by an engine that cannot see anything, kept only so that a rule
        authored without a target degrades instead of doing nothing at all.
        """
        if rule.recovery_target is not None:
            handle = surface.find(rule.recovery_target)
            if handle is not None:
                surface.click(rule.recovery_target)
                log.info(
                    "dismissed via recorded recovery_target (%s=%r)",
                    handle.rule.by, handle.rule.value,
                )
                return
            log.warning(
                "the recorded recovery_target (%s=%r) did not resolve; falling back to "
                "conventional captions, which is a guess",
                rule.recovery_target.primary.by, rule.recovery_target.primary.value,
            )

        candidates: list[Locator] = []
        if rule.detect.kind is CheckpointKind.ELEMENT_EXISTS and rule.detect.value:
            candidates.append(
                Locator(
                    primary=LocatorRule(by=LocatorBy.CSS, value=rule.detect.value),
                    rationale="the detector's own selector, which pointed at the obstruction",
                )
            )
        candidates += [
            Locator(
                primary=LocatorRule(by=LocatorBy.TEXT, value=caption),
                rationale="a conventional dismissal caption",
            )
            for caption in DISMISS_CAPTIONS
        ]

        for candidate in candidates:
            handle = surface.find(candidate)
            if handle is not None:
                surface.click(candidate)
                log.warning(
                    "dismissed by GUESSING at caption %r; record a recovery_target on this "
                    "error rule to make the recovery deterministic",
                    handle.rule.value,
                )
                return
        log.warning("no dismissal affordance found; the step will simply be retried")

    # -- endings ------------------------------------------------------------------------

    def _finish(
        self,
        recorder: RunRecorder,
        artifact: Artifact,
        surface: Surface,
        started: float,
        outputs: dict[str, str],
        recoveries: list[RecoveryEvent],
        executed: int,
    ) -> ReplayResult:
        """Every step ran; now decide whether the capability actually achieved its goal.

        Checked separately from the steps because passing every step is not the same as
        succeeding: a flow can execute perfectly and still land somewhere unintended, which is
        exactly why an artifact states its own end condition.
        """
        if surface.wait_for(artifact.success, self.success_timeout_s, self.poll_ms):
            result = ReplayResult(
                status=ReplayStatus.SUCCESS,
                outcome=SUCCESS_OUTCOME,
                outputs=outputs,
                run_id=recorder.run_id,
                duration_s=round(time.monotonic() - started, 3),
                steps_executed=executed,
                recoveries=recoveries,
            )
            self._record_result(recorder, result)
            return result

        expected = describe_checkpoint(artifact.success) or ""
        detail = FailureDetail(
            step_index=len(artifact.steps) - 1 if artifact.steps else 0,
            action="success_checkpoint",
            expected=expected,
            observed=_observed(surface),
            message=(
                "every step executed, but the capability's success checkpoint did not hold, so "
                "the goal was not achieved"
            ),
        )
        recorder.screenshot(surface, "failure")
        result = ReplayResult(
            status=ReplayStatus.HARD_FAILURE,
            outputs=outputs,
            failure=detail,
            run_id=recorder.run_id,
            duration_s=round(time.monotonic() - started, 3),
            steps_executed=executed,
            recoveries=recoveries,
        )
        self._record_result(recorder, result)
        return result

    def _business_outcome(
        self,
        recorder: RunRecorder,
        artifact: Artifact,
        rule: ErrorRule,
        started: float,
        outputs: dict[str, str],
        recoveries: list[RecoveryEvent],
        executed: int,
    ) -> ReplayResult:
        """Return a legitimate negative answer. A normal return, not an error path."""
        if rule.outcome not in artifact.outputs.outcome_values:
            # The contract promised a closed set; this breaks it. Reported rather than
            # suppressed, but still returned: the application really did say this.
            log.warning(
                "outcome %r is not in the capability's declared outcome_values %r",
                rule.outcome, artifact.outputs.outcome_values,
            )
        result = ReplayResult(
            status=ReplayStatus.BUSINESS_OUTCOME,
            outcome=rule.outcome,
            outputs=outputs,
            run_id=recorder.run_id,
            duration_s=round(time.monotonic() - started, 3),
            steps_executed=executed,
            recoveries=recoveries,
        )
        self._record_result(recorder, result)
        return result

    def _fail(
        self,
        recorder: RunRecorder,
        surface: Surface,
        step: Step,
        attempt: _Attempt,
        started: float,
        outputs: dict[str, str],
        recoveries: list[RecoveryEvent],
        executed: int,
        message: str | None,
    ) -> ReplayResult:
        """Stop on a hard failure, capturing the page while it is still in the failing state."""
        expected = (
            describe_checkpoint(step.checkpoint)
            or describe_checkpoint(step.wait.until)
            or f"{step.action} to succeed"
        )
        # Capture while the page is still failing; a screenshot taken later shows a different
        # screen, and a failure report of the wrong screen is worse than none.
        detail = FailureDetail(
            step_index=step.index,
            action=str(step.action),
            expected=expected,
            observed=_observed(surface),
            locator_used=attempt.locator_used,
            fallback_used=attempt.fallback_used,
            message=message or "the step did not succeed",
        )
        recorder.screenshot(surface, f"failure-step-{step.index}")
        result = ReplayResult(
            status=ReplayStatus.HARD_FAILURE,
            outputs=outputs,
            failure=detail,
            run_id=recorder.run_id,
            duration_s=round(time.monotonic() - started, 3),
            steps_executed=executed,
            recoveries=recoveries,
        )
        self._record_result(recorder, result)
        return result

    def _record_result(self, recorder: RunRecorder, result: ReplayResult) -> None:
        """Write the run's ending to evidence, redacted."""
        recorder.event(
            "run_finished",
            status=str(result.status),
            outcome=result.outcome,
            outputs=list(result.outputs),  # names only; values stay out of evidence entirely
            steps_executed=result.steps_executed,
            recoveries=len(result.recoveries),
            failure=result.failure.model_dump(mode="json") if result.failure else None,
        )
        recorder.finish(
            kind="replay",
            status=str(result.status),
            outcome=result.outcome,
            outputs=list(result.outputs),
            steps_executed=result.steps_executed,
            recoveries=len(result.recoveries),
            duration_s=result.duration_s,
        )

    # -- input binding ------------------------------------------------------------------

    def _validate_inputs(self, artifact: Artifact, inputs: dict[str, str]) -> dict[str, str]:
        """Check the request before touching a browser, and fail loudly if it is malformed.

        Up front rather than per step: discovering a missing parameter halfway through a flow
        would mean having already clicked things in a real back office.
        """
        missing = [
            param.name
            for param in artifact.inputs
            if param.required and param.name not in inputs
        ]
        if missing:
            raise MissingInputError(
                f"missing required input(s): {', '.join(sorted(missing))}; "
                f"this capability declares {[p.name for p in artifact.inputs]}"
            )

        declared = {param.name for param in artifact.inputs}
        for extra in set(inputs) - declared:
            log.info("ignoring input %r, which this capability does not declare", extra)

        referenced: set[str] = set()
        for step in artifact.steps:
            referenced.update(template_params(step.value))
            if step.target is not None:
                for rule in step.target.candidates():
                    referenced.update(template_params(rule.value))
        unknown = referenced - set(inputs)
        if unknown:
            raise MissingInputError(
                f"the artifact references input(s) that were not supplied: "
                f"{', '.join(sorted(unknown))}"
            )
        return dict(inputs)


class _Attempt:
    """Mutable per-attempt scratch space for one step execution."""

    def __init__(self) -> None:
        self.failure: str | None = None
        self.locator_used: str | None = None
        self.fallback_used: bool = False
        self.checkpoint_passed: bool | None = None
        self.extracted: str | None = None


_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _bind_text(text: str | None, inputs: dict[str, str]) -> str | None:
    """Substitute `{{param}}` placeholders with supplied values.

    Raises rather than leaving a placeholder in place: typing the literal string
    "{{member_id}}" into a live form is worse than not running at all.
    """
    if text is None:
        return None

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in inputs:
            raise MissingInputError(f"no value supplied for {{{{{name}}}}}")
        return inputs[name]

    return _TEMPLATE_RE.sub(substitute, text)


def _bind_locator(locator: Locator, inputs: dict[str, str]) -> Locator:
    """Bind templates inside every candidate, so a parameterized locator resolves at run time."""
    return Locator(
        primary=LocatorRule(
            by=locator.primary.by, value=_bind_text(locator.primary.value, inputs) or ""
        ),
        fallbacks=[
            LocatorRule(by=rule.by, value=_bind_text(rule.value, inputs) or "")
            for rule in locator.fallbacks
        ],
        rationale=locator.rationale,
    )


def _safe_url(surface: Surface) -> str:
    """The current URL, or a placeholder. Never raises: this runs on an already-failing path."""
    try:
        return surface.current_url()
    except SurfaceError:
        return ""


def _observed(surface: Surface | None) -> str:
    """A short, redacted summary of what the page is showing right now.

    Captured at the moment of failure because the state is gone a second later. Kept brief and
    masked: a failure report travels into tickets and chat, so it carries enough to recognize
    the screen and nothing that would leak an account.
    """
    if surface is None:
        return "(unavailable)"
    try:
        observation = surface.observe()
    except SurfaceError:
        return "(surface unavailable)"
    salient = ", ".join(
        f"{element.name or element.text!r}"
        for element in observation.elements[:6]
        if (element.name or element.text)
    )
    summary = (
        f"url={redact_url(observation.url)} title={observation.title!r} showing: {salient}"
    )
    return redact(summary)[:400]
