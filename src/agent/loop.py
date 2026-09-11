"""The discovery loop: observe, decide, act, verify, record.

The central rule of this module is that the artifact is built *incrementally from verified
actions*, never reconstructed from the model's transcript afterwards. Each iteration executes one
proposed action against the real surface, checks the proposed checkpoint against the real page,
and only then appends a `Step`. Anything the model said that did not survive contact with the
application is evidence, not automation.

That is what decouples the recording from the model. The transcript may contain wrong guesses,
retried locators, and reasoning that is merely plausible; the artifact contains only actions that
demonstrably worked, expressed in the artifact schema's own vocabulary.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, ValidationError

from src.agent.llm import ControlAction, Decision, LLMClient
from src.agent.prompt import build_user_prompt
from src.artifact.schema import (
    ActionType,
    Artifact,
    Checkpoint,
    InputParam,
    Locator,
    LocatorRule,
    OutputContract,
    OutputField,
    ParamType,
    Provenance,
    Step,
    Target,
    describe_checkpoint,
    looks_like_observed_data,
)
from src.surface.base import Observation, Surface, SurfaceError

log = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 15
DEFAULT_TIMEOUT_S = 120.0
DISCOVERED_OUTCOME_VALUES = ["success", "no_such_member"]
"""The closed set of business outcomes a discovered capability declares.

Discovery only ever walks the path that worked, so it cannot observe the negative answer -- but
the caller still has to be able to handle it, and replay needs the outcome declared before an
ErrorRule may return it. Declaring the known pair up front is what lets the error taxonomy be
wired in without re-recording: "no such member" is a real answer this application gives, not a
failure, and a capability that cannot express it would force replay to report a legitimate
result as a crash.

Fixed here because this target has one known negative outcome. A capability with different
business outcomes -- a rejected transfer, an ineligible account -- needs its own set, which is
why this is a named constant rather than a literal buried in the assembly code.
"""

MAX_CONSECUTIVE_FAILURES = 2
"""How many unproductive iterations in a row end the run.

Covers both a locator that will not resolve and a checkpoint that will not pass, because both
mean the same thing operationally: the model's last proposal did not work. Bounded low because a
model that has failed twice in a row on the same screen is looping, and every further turn costs
a request while making the same mistake.
"""


class PrunedCandidate(BaseModel):
    """One locator candidate the recorder dropped for naming page data instead of structure."""

    role: str = Field(description="'primary' or 'fallback N', as the model proposed it.")
    matched: str = Field(
        description="Which data shape it resembled. The value itself is deliberately absent, "
        "matching the validator's error messages: a pruning record must not become the leak "
        "the rule exists to prevent."
    )


@dataclass
class _Attempt:
    """The full outcome of executing one decision, including what evidence needs to see.

    A plain (step, failure) tuple carried enough for the loop but not enough for the record:
    which rule resolved, whether it was a fallback, and whether the checkpoint held are exactly
    the things a reviewer asks about afterwards, and they are only knowable here.
    """

    step: Step | None = None
    failure: str | None = None
    locator_by: str | None = None
    locator_value: str | None = None
    used_fallback: bool = False
    checkpoint_passed: bool | None = None
    extracted_value: str | None = None
    """Text a READ pulled off the page. Held in memory only -- the evidence writer redacts it
    before anything reaches disk, and the artifact never carries values at all."""
    pruned: list[PrunedCandidate] = field(default_factory=list)


class StopReason(StrEnum):
    """Why a discovery run ended. Every exit path names itself.

    Distinct values because the responses differ: SUCCESS yields an artifact, MAX_STEPS and
    TIMEOUT mean the budget was too small (or the flow longer than expected) and are worth
    retrying with more room, and DEAD_END means the agent could not proceed at all -- the only
    one of the four that warrants a human.
    """

    SUCCESS = "success"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    DEAD_END = "dead_end"


class TranscriptEntry(BaseModel):
    """One iteration, recorded as evidence.

    The transcript is where the model's reasoning lives -- including for actions that failed and
    were never recorded. Kept separate from the artifact so a reviewer can reconstruct *how* a
    recording was arrived at without the recording itself depending on it.
    """

    iteration: int
    url: str = Field(description="Where the surface was when the decision was made.")
    thought: str = Field(description="The model's stated reasoning. Evidence only.")
    action: str
    outcome: str = Field(description="What actually happened when the loop executed it.")
    recorded: bool = Field(
        default=False, description="Whether this iteration produced a Step in the artifact."
    )
    step_index: int | None = Field(
        default=None, description="Artifact step index, when this iteration produced one."
    )
    locator_by: str | None = Field(
        default=None, description="Strategy of the rule that actually resolved the element."
    )
    locator_value: str | None = Field(default=None, description="Argument of that rule.")
    used_fallback: bool = Field(
        default=False,
        description="Whether the model's primary locator failed and a fallback carried the "
        "step. Recorded per iteration because drift is only visible in the runs that still "
        "passed.",
    )
    checkpoint_passed: bool | None = Field(
        default=None,
        description="Whether the proposed checkpoint held. None when the action proposed none.",
    )
    duration_ms: int = Field(
        default=0, description="Wall-clock time for the whole iteration, decision included."
    )
    extracted_value: str | None = Field(
        default=None,
        description="Text a READ pulled off the page. In memory only: the evidence writer "
        "redacts it before it reaches disk, and the artifact never stores values at all.",
    )
    pruned: list[PrunedCandidate] = Field(
        default_factory=list,
        description="Locator candidates the recorder dropped as data-shaped. A non-empty list "
        "is a signal about proposal quality, so it is surfaced per step rather than discarded.",
    )
    checkpoint: str | None = Field(
        default=None,
        description="The checkpoint the model proposed, as 'kind=value'.\n\n"
        "Recorded because its absence blocked a real diagnosis: a run failed three "
        "'done' verifications and the evidence showed only that they failed, not what had "
        "been asserted, so the cause was unknowable after the fact.",
    )


class DiscoveryResult(BaseModel):
    """Everything a discovery run produced: the recording, the evidence, and why it stopped."""

    stop_reason: StopReason
    artifact: Artifact | None = Field(
        default=None, description="Present only on SUCCESS. A partial flow is not a capability."
    )
    transcript: list[TranscriptEntry] = Field(default_factory=list)
    steps_recorded: int = 0
    note: str = Field(
        default="", description="Human-readable detail about why the run ended as it did."
    )

    @property
    def succeeded(self) -> bool:
        return self.stop_reason is StopReason.SUCCESS


class DiscoveryAgent:
    """Drives a surface with an LLM to discover and record a reusable capability.

    Holds no state between runs: `run` owns everything it accumulates, so the same agent can
    record several capabilities without one leaking into the next.
    """

    def __init__(
        self,
        surface: Surface,
        llm: LLMClient,
        *,
        model_id: str = "stub",
        run_id: str | None = None,
        checkpoint_timeout_s: float = 10.0,
        poll_ms: int = 250,
        on_iteration: Callable[[TranscriptEntry], None] | None = None,
        escalation: object | None = None,
    ) -> None:
        self.surface = surface
        self.llm = llm
        self.on_iteration = on_iteration
        """Called after every iteration, successful or not.

        The hook exists so evidence can be written *as the run happens* -- a crashed or killed
        run still leaves a complete record up to the moment it died -- without the loop knowing
        anything about files, screenshots, or redaction.
        """
        self.escalation = escalation
        """An EscalationHandler, or None. Typed loosely so the agent does not depend on the
        escalation package to run; with none attached, a dead end ends the run as before."""
        self.model_id = model_id
        """Recorded as provenance.discovered_by, so artifacts can be traced to the model that
        wrote them -- and re-examined together if that model is later found to record badly."""
        self.run_id = run_id or f"disc-{datetime.now(UTC):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        self.checkpoint_timeout_s = checkpoint_timeout_s
        self.poll_ms = poll_ms
        self.current_goal = ""
        """The goal of the run in progress, so the escalation hook can describe what stopped."""

    # -- the loop -----------------------------------------------------------------------

    def run(
        self,
        goal: str,
        target: Target,
        inputs: dict[str, str],
        max_steps: int = DEFAULT_MAX_STEPS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> DiscoveryResult:
        """Discover a capability that accomplishes `goal`, and return it as an artifact.

        Three budgets bound the run -- iterations, wall clock, and consecutive failures -- and
        each has its own stop reason. They exist because an agent driving a real application can
        fail in three genuinely different ways: taking too many steps, hanging on a slow or
        broken target, or being unable to proceed at all.
        """
        self.current_goal = goal
        started = time.monotonic()
        steps: list[Step] = []
        transcript: list[TranscriptEntry] = []
        consecutive_failures = 0
        last_failure: str | None = None

        for iteration in range(max_steps):
            iteration_started = time.monotonic()
            if time.monotonic() - started >= timeout_s:
                return self._stop(
                    StopReason.TIMEOUT,
                    transcript,
                    steps,
                    f"wall-clock budget of {timeout_s}s exhausted after {len(steps)} steps",
                )

            observation = self._observe()
            decision = self.llm.decide(
                build_user_prompt(
                    goal, inputs, observation, steps, last_failure, entry_url=target.entry_url
                )
            )
            log.info("iteration %d: %s (%s)", iteration, decision.action, decision.thought)

            if decision.action is ControlAction.STUCK:
                self._record(transcript, iteration, observation, decision, "model reported stuck",
                    elapsed_s=time.monotonic() - iteration_started)
                # Escalation seam: a human would be brought in here, given the transcript and
                # the current observation. Deliberately not built yet -- see _on_dead_end.
                if self._on_dead_end(
                    "the model reported it could see no way forward",
                    observation, len(steps), str(decision.action),
                ):
                    consecutive_failures = 0
                    last_failure = (
                        "a human took over the session, did something, and handed control back; "
                        "look again -- the page may have changed"
                    )
                    continue
                return self._stop(
                    StopReason.DEAD_END, transcript, steps, "model reported it was stuck"
                )

            if decision.action is ControlAction.DONE:
                success = decision.checkpoint or (steps[-1].checkpoint if steps else None)
                if success is None:
                    self._record(transcript, iteration, observation, decision, "done, unverifiable",
                        elapsed_s=time.monotonic() - iteration_started)
                    self._on_dead_end(
                        "reported done without a success checkpoint",
                        observation, len(steps), "done",
                    )
                    return self._stop(
                        StopReason.DEAD_END,
                        transcript,
                        steps,
                        "the model reported done but supplied no success checkpoint, so the "
                        "run cannot be verified or replayed",
                    )
                if self.surface.wait_for(success, self.checkpoint_timeout_s, self.poll_ms):
                    self._record(transcript, iteration, observation, decision, "done, verified",
                        elapsed_s=time.monotonic() - iteration_started, checkpoint_passed=True)
                    return self._finish(goal, target, inputs, steps, success, transcript)

                # Claiming success is not achieving it. Treated as any other failed proposal.
                consecutive_failures += 1
                # Name the assertion that failed. The generic message left the model unable to
                # correct itself: it re-proposed a checkpoint three times without ever learning
                # which part of it the page disagreed with.
                last_failure = (
                    f"you reported done, but your success checkpoint "
                    f"({describe_checkpoint(success)}) is not true of the current page. Assert something "
                    f"the page actually shows now, or keep working."
                )
                self._record(transcript, iteration, observation, decision, "done, NOT verified",
                    elapsed_s=time.monotonic() - iteration_started, checkpoint_passed=False)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    if self._on_dead_end(last_failure, observation, len(steps), "done"):
                        consecutive_failures = 0
                        continue
                    return self._stop(StopReason.DEAD_END, transcript, steps, last_failure)
                continue

            attempt = self._execute(decision, inputs, next_index=len(steps))
            if attempt.step is None:
                consecutive_failures += 1
                last_failure = attempt.failure or "the action did not succeed"
                self._record(
                    transcript, iteration, observation, decision, last_failure,
                    attempt=attempt, elapsed_s=time.monotonic() - iteration_started,
                )
                log.warning("iteration %d failed: %s", iteration, last_failure)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    if self._on_dead_end(
                        last_failure, observation, len(steps), str(decision.action)
                    ):
                        consecutive_failures = 0
                        last_failure = (
                            "a human took over the session and handed control back; look again"
                        )
                        continue
                    return self._stop(
                        StopReason.DEAD_END,
                        transcript,
                        steps,
                        f"{consecutive_failures} consecutive failed attempts; last: {last_failure}",
                    )
                continue

            steps.append(attempt.step)
            consecutive_failures = 0
            last_failure = None
            self._record(
                transcript, iteration, observation, decision, "ok", recorded=True,
                attempt=attempt, elapsed_s=time.monotonic() - iteration_started,
            )

        return self._stop(
            StopReason.MAX_STEPS,
            transcript,
            steps,
            f"reached the {max_steps}-step budget with {len(steps)} steps recorded",
        )

    # -- one iteration ------------------------------------------------------------------

    def _observe(self) -> Observation:
        """Observe the surface, tolerating the case where nothing is open yet.

        The agent starts blind: on the first turn there is no page, and its first action is
        normally to navigate. Returning an empty observation rather than raising means that
        opening move is an ordinary decision instead of a special case in the loop.
        """
        try:
            return self.surface.observe()
        except SurfaceError:
            return Observation(url="about:blank", title="(nothing open yet)", elements=[])

    def _execute(self, decision: Decision, inputs: dict[str, str], next_index: int) -> _Attempt:
        """Execute one proposed action and verify it, reporting everything observed.

        Returns a failure string instead of raising: a locator that will not resolve is expected
        during discovery -- the model is guessing at a page it has only just seen -- and the
        useful response is to tell it what went wrong and let it try again, not to end the run.
        """
        action = decision.action
        if not isinstance(action, ActionType):  # defensive; control actions never reach here
            return _Attempt(failure=f"unexpected control action {action!r}")

        # The model's own risk assessment, declared to the surface before acting so a policy
        # wrapper can gate it. Treated as a claim to be checked, not as permission.
        self.surface.declare_step_risk(decision.risk)

        recorded_target: Locator | None = None
        used_fallback = False
        extracted_value: str | None = None
        pruned: list[PrunedCandidate] = []

        try:
            if action is ActionType.NAVIGATE:
                if not decision.value:
                    return _Attempt(failure="navigate requires a URL in 'value'")
                self.surface.open(decision.value)

            elif action is ActionType.WAIT:
                if decision.checkpoint is None:
                    return _Attempt(failure="wait requires a checkpoint to wait for")
                if not self.surface.wait_for(
                    decision.checkpoint, self.checkpoint_timeout_s, self.poll_ms
                ):
                    return _Attempt(failure="the condition never became true within the timeout")

            else:
                if decision.target is None:
                    return _Attempt(failure=f"{action} requires a target locator")

                # Resolve first, then act. The extra resolution buys two things worth more than
                # one round-trip: a non-exceptional failure signal to feed back to the model,
                # and the identity of the rule that actually matched, which is what gets
                # recorded.
                handle = self.surface.find(decision.target)
                if handle is None:
                    return _Attempt(failure=(
                        f"no element matched {decision.target.primary.by}="
                        f"{decision.target.primary.value!r} (or any fallback); "
                        "the element may be absent, hidden, or matched more than once"
                    ))
                recorded_target, pruned = _promote(decision.target, handle.candidate_index)
                used_fallback = handle.was_fallback

                if action in (ActionType.CLICK, ActionType.DISMISS):
                    # Dismissing is clicking the control that closes the obstruction; the
                    # distinction is recorded because replay treats it as recovery, not progress.
                    self.surface.click(decision.target)
                elif action is ActionType.TYPE:
                    if decision.value is None:
                        return _Attempt(failure="type requires text in 'value'")
                    self.surface.type(decision.target, decision.value)
                elif action is ActionType.READ:
                    if not decision.extract:
                        return _Attempt(failure="read requires an output field name in 'extract'")
                    extracted_value = self.surface.read(decision.target)
                    log.info("extracted field %s (value withheld from logs)", decision.extract)

        except SurfaceError as exc:
            return _Attempt(failure=str(exc))

        if decision.checkpoint is not None and action is not ActionType.WAIT:
            if not self.surface.wait_for(
                decision.checkpoint, self.checkpoint_timeout_s, self.poll_ms
            ):
                # The action ran and may have had an effect, but it is not recorded: an
                # unverified step would be a step replay cannot trust.
                return _Attempt(
                    failure=(
                        f"the action ran but its checkpoint ({decision.checkpoint.kind}="
                        f"{decision.checkpoint.value!r}) did not hold afterwards"
                    ),
                    locator_by=str(recorded_target.primary.by) if recorded_target else None,
                    locator_value=recorded_target.primary.value if recorded_target else None,
                    used_fallback=used_fallback,
                    checkpoint_passed=False,
                    pruned=pruned,
                )

        return _Attempt(
            step=Step(
                index=next_index,
                action=action,
                target=recorded_target,
                value=_parameterize(decision.value, inputs),
                extract=decision.extract,
                risk=decision.risk,
                checkpoint=decision.checkpoint,
            ),
            locator_by=str(recorded_target.primary.by) if recorded_target else None,
            locator_value=recorded_target.primary.value if recorded_target else None,
            used_fallback=used_fallback,
            checkpoint_passed=None if decision.checkpoint is None else True,
            extracted_value=extracted_value,
            pruned=pruned,
        )

    # -- results ------------------------------------------------------------------------

    def _finish(
        self,
        goal: str,
        target: Target,
        inputs: dict[str, str],
        steps: list[Step],
        success: Checkpoint,
        transcript: list[TranscriptEntry],
    ) -> DiscoveryResult:
        """Assemble the artifact from what was verified, and validate it before returning it."""
        extracted = [step.extract for step in steps if step.extract]
        try:
            artifact = Artifact(
                capability_id=_slug(goal),
                description=goal,
                target=target,
                inputs=[
                    InputParam(
                        name=name,
                        type=ParamType.STRING,
                        description=f"Bound into steps that reference {{{{{name}}}}}.",
                    )
                    for name in inputs
                ],
                outputs=OutputContract(
                    outcome_values=list(DISCOVERED_OUTCOME_VALUES),
                    fields=[
                        OutputField(
                            name=name,
                            type=ParamType.STRING,
                            description=f"Read from the page during discovery as {name!r}.",
                        )
                        for name in dict.fromkeys(extracted)
                    ],
                ),
                steps=steps,
                success=success,
                provenance=Provenance(
                    goal=goal,
                    discovered_by=self.model_id,
                    discovery_run_id=self.run_id,
                    created_at=datetime.now(UTC),
                ),
            )
        except ValidationError as exc:
            # The loop built something the schema rejects. That is a bug here, not a failure of
            # the target -- but it must not masquerade as a successful discovery.
            return self._stop(
                StopReason.DEAD_END,
                transcript,
                steps,
                f"the recorded flow did not satisfy the artifact schema: {exc}",
            )

        log.info("discovery succeeded: %d steps, run %s", len(steps), self.run_id)
        return DiscoveryResult(
            stop_reason=StopReason.SUCCESS,
            artifact=artifact,
            transcript=transcript,
            steps_recorded=len(steps),
        )

    def _stop(
        self,
        reason: StopReason,
        transcript: list[TranscriptEntry],
        steps: list[Step],
        note: str,
    ) -> DiscoveryResult:
        """End the run without an artifact. A partial flow is evidence, not a capability."""
        log.info("discovery stopped (%s): %s", reason, note)
        return DiscoveryResult(
            stop_reason=reason,
            artifact=None,
            transcript=transcript,
            steps_recorded=len(steps),
            note=note,
        )

    def _on_dead_end(
        self, reason: str, observation: Observation, step_index: int, action: str
    ) -> bool:
        """Refer a dead end to a human. Returns True if they resolved it and handed control back.

        Every dead end funnels through this one place, which is why wiring escalation needed no
        new seam: the hook already existed and already had every caller. With no console
        attached it logs and returns False, and the run ends exactly as it did before.
        """
        log.info("dead end (escalation seam): %s at %s", reason, observation.url)
        if self.escalation is None:
            return False

        from src.escalation.request import InterventionRequest, StuckReason

        request = InterventionRequest(
            run_id=self.run_id,
            capability_id=_slug(self.current_goal),
            goal=self.current_goal,
            reason=StuckReason.DEAD_END,
            step_index=step_index,
            action=action,
            url=observation.url,
            page_summary=observation.to_prompt_text()[:400],
            detail=reason,
        )
        response = self.escalation.escalate(
            request, snapshot=lambda: self._observe().to_prompt_text()[:400]
        )
        return bool(response.resumed)

    def _record(
        self,
        transcript: list[TranscriptEntry],
        iteration: int,
        observation: Observation,
        decision: Decision,
        outcome: str,
        recorded: bool = False,
        attempt: _Attempt | None = None,
        elapsed_s: float = 0.0,
        checkpoint_passed: bool | None = None,
    ) -> None:
        """Append one iteration to the evidence transcript and notify the observer.

        Every exit path funnels through here, so a run leaves the same shape of record whether
        it succeeded, failed a checkpoint, or died at a dead end.
        """
        proposed = decision.checkpoint
        entry = TranscriptEntry(
            iteration=iteration,
            checkpoint=describe_checkpoint(proposed),
            pruned=list(attempt.pruned) if attempt else [],
            url=observation.url,
            thought=decision.thought,
            action=str(decision.action),
            outcome=outcome,
            recorded=recorded,
            step_index=attempt.step.index if attempt and attempt.step else None,
            locator_by=attempt.locator_by if attempt else None,
            locator_value=attempt.locator_value if attempt else None,
            used_fallback=bool(attempt and attempt.used_fallback),
            checkpoint_passed=(
                attempt.checkpoint_passed if attempt else checkpoint_passed
            ),
            duration_ms=int(elapsed_s * 1000),
            extracted_value=attempt.extracted_value if attempt else None,
        )
        transcript.append(entry)
        if self.on_iteration is not None:
            self.on_iteration(entry)


def _promote(proposed: Locator, winning_index: int) -> tuple[Locator, list[PrunedCandidate]]:
    """Rebuild the locator with the rule that actually matched as its primary, and normalized.

    The recording should describe what worked, not what was hoped for. If the model's preferred
    label failed and a CSS fallback matched, replay should try the CSS first and keep the label
    as a fallback -- the ordering inverts, the alternatives survive, and the model's rationale is
    carried through unchanged so a reviewer still sees the original claim.

    Normalization of data-shaped candidates happens here too; see `_normalize_candidates`.
    """
    candidates = proposed.candidates()
    winner = candidates[winning_index]
    ordered = [winner, *(rule for index, rule in enumerate(candidates) if index != winning_index)]
    kept, pruned = _normalize_candidates(ordered)
    return (
        Locator(primary=kept[0], fallbacks=kept[1:], rationale=proposed.rationale),
        pruned,
    )


def _normalize_candidates(
    ordered: list[LocatorRule],
) -> tuple[list[LocatorRule], list[PrunedCandidate]]:
    """Drop candidates that target page data rather than structure.

    WHY THIS IS NORMALIZATION, NOT A WEAKENING OF ENFORCEMENT. Three layers, three jobs. The
    prompt *steers* the model. This function lets the recorder *normalize its own output* --
    exactly as `_promote` already reorders candidates to reflect what really resolved. The
    schema validator remains the unconditional *backstop*, and it is not relaxed by one line:
    it still rejects any artifact carrying data-shaped targeting, from this recorder, from a
    hand edit, or from a future loop that never heard of this function. Both layers call the
    same `looks_like_observed_data`, so they cannot disagree about what counts.

    What this buys is proportionality. A model volunteered a third fallback that found the
    balance by its own displayed amount -- a candidate that is worthless on any other run --
    and that one junk suggestion discarded four correctly recorded steps. Dropping it is not
    tolerating the defect; it is declining to let an unused, useless suggestion destroy a
    verified flow.

    Three rules, in order of how much they may assume:

    * Fallbacks are pruned freely. They are optional by construction, and a data-shaped one has
      no value to lose.
    * A data-shaped PRIMARY is never silently dropped. The primary is the rule that actually
      resolved during discovery, so a data-shaped one means the step's *verified* targeting is
      unusable -- a real defect, not noise. The first clean fallback is promoted in its place;
      note that such a fallback was NOT the candidate that resolved, so this is a best-effort
      repair a reviewer should check, and it is logged as a primary-level pruning to make that
      visible.
    * The last remaining candidate is never pruned. A locator with no candidates is not a
      locator, so if nothing clean exists the dirty primary is kept and the artifact goes on to
      fail validation at assembly, exactly as it did before this function existed. Failing
      loudly is the correct outcome there.
    """
    verdicts = [(rule, looks_like_observed_data(rule.value)) for rule in ordered]
    clean = [rule for rule, resembles in verdicts if resembles is None]
    pruned: list[PrunedCandidate] = []

    if not clean:
        # Nothing clean to fall back on: keep the primary and let the validator refuse it.
        primary_resembles = verdicts[0][1]
        for position, (_rule, resembles) in enumerate(verdicts[1:], start=1):
            if resembles:
                pruned.append(PrunedCandidate(role=f"fallback {position}", matched=resembles))
        log.warning(
            "every locator candidate looks like page data (%s); keeping the primary so "
            "assembly fails visibly rather than recording an empty locator",
            primary_resembles,
        )
        return [verdicts[0][0]], pruned

    for position, (_rule, resembles) in enumerate(verdicts):
        if resembles:
            role = "primary" if position == 0 else f"fallback {position}"
            pruned.append(PrunedCandidate(role=role, matched=resembles))
            log.warning("pruned %s locator candidate: it targets %s", role, resembles)

    return clean, pruned


def _parameterize(value: str | None, inputs: dict[str, str]) -> str | None:
    """Replace a literal that exactly matches a declared input with its `{{name}}` placeholder.

    This is the single transformation that turns a transcript of one session into a reusable
    capability. The model types "12345" because that is what it was given; the recording says
    `{{member_id}}`, so the next caller can pass 67890 and replay the same flow.

    Only exact, whole-value matches are substituted. A partial or fuzzy match would risk
    rewriting an unrelated literal -- an amount, a date, a page name -- into a parameter that
    replay would then fill with something wrong, and a silently mis-parameterized step is far
    harder to notice than a literal one. When two inputs share a value, the first declared wins.
    """
    if value is None:
        return None
    for name, supplied in inputs.items():
        if value == supplied:
            return f"{{{{{name}}}}}"
    return value


def _slug(goal: str) -> str:
    """Derive a capability id from the goal, so a recording has a stable name to be invoked by."""
    slug = re.sub(r"[^a-z0-9]+", "_", goal.lower()).strip("_")
    return slug[:48].rstrip("_") or "discovered_capability"
