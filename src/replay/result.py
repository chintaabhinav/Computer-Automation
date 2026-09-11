"""What a replay returns: the contract every caller of this system programs against.

Designed around one distinction that most automation frameworks get wrong. There are three
possible endings to a run, not two:

* ``SUCCESS`` -- the flow completed and the declared outputs were captured.
* ``BUSINESS_OUTCOME`` -- **not a failure.** The application gave a legitimate negative answer
  to the question that was asked: no such member, account ineligible, transfer declined. The
  automation worked perfectly; the answer is simply "no". Callers must handle these as normal
  results, and they arrive with a name from the artifact's declared ``outcome_values`` so a
  caller can branch exhaustively without parsing prose.
* ``HARD_FAILURE`` -- the run could not legitimately continue: the session expired, the server
  returned 500, a locator no longer resolves, a checkpoint did not hold.

Collapsing the middle case into failure is what makes automation untrustworthy in practice. A
system that reports "no such member" as an error trains its operators to ignore errors, and by
the time a real failure arrives nobody is looking. Keeping it separate is why an artifact
declares a closed set of outcomes in the first place.

Nothing here imports the discovery agent or any LLM SDK -- replay is the production path and has
no model in it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from src.artifact.schema import RecoveryAction


class ReplayStatus(StrEnum):
    """How a replay ended. Three cases, because three genuinely different responses are needed."""

    SUCCESS = "success"
    """The flow completed and the success checkpoint held."""

    BUSINESS_OUTCOME = "business_outcome"
    """The application answered, and the answer was a declared negative outcome. NOT a failure:
    the caller reads `outcome` and handles it as a normal result."""

    HARD_FAILURE = "hard_failure"
    """The run could not continue. `failure` is populated and is where a human should look."""


class FailureDetail(BaseModel):
    """Everything needed to diagnose a hard failure without re-running it.

    Populated at the moment of failure, while the page is still in the failing state. The
    expected/observed pair is the heart of it: a replay failure is almost always "the
    application changed", and the fastest way to see that is to read what the recording
    asserted next to what the screen actually showed.
    """

    step_index: int = Field(description="Which step of the artifact failed.")
    action: str = Field(description="The action that step was performing.")
    expected: str = Field(
        description="What the recording asserted, rendered as 'kind=value'. The claim that "
        "turned out to be false."
    )
    observed: str = Field(
        description="A short, redacted summary of what the page actually showed -- URL, title "
        "and the most salient visible text. Redacted because a failure report is the artifact "
        "most likely to be pasted into a ticket."
    )
    locator_used: str | None = Field(
        default=None,
        description="The locator rule that resolved, as 'by=value', or None when the failure "
        "was not about finding an element.",
    )
    fallback_used: bool = Field(
        default=False,
        description="Whether a fallback locator carried this step. A failure on a step that was "
        "already limping is a strong hint that the application has drifted.",
    )
    message: str = Field(description="One human sentence naming what went wrong.")


class RecoveryEvent(BaseModel):
    """One recovery the engine performed, recorded even when the run went on to succeed.

    Surfaced rather than swallowed: a flow that needs a dismissal on every run is telling you
    the application has grown an interstitial the recording does not account for, and that is
    worth seeing in a green result.
    """

    step_index: int
    detected: str = Field(description="The error rule's detector that matched, as 'kind=value'.")
    action: RecoveryAction = Field(description="The recovery applied before retrying the step.")
    attempt: int = Field(description="Which recovery attempt this was for that step, from 1.")


class ReplayResult(BaseModel):
    """The complete outcome of one replay.

    A single object so that callers, evidence, and the CLI all read the same thing -- there is no
    second, richer story available only to whoever was in the room.
    """

    status: ReplayStatus
    outcome: str | None = Field(
        default=None,
        description="The business outcome name, always one of the artifact's declared "
        "outcome_values. 'success' on a clean run; the matched rule's outcome on a "
        "BUSINESS_OUTCOME; None on a hard failure, where there is no answer to report.",
    )
    outputs: dict[str, str] = Field(
        default_factory=dict,
        description="Declared output fields that were actually extracted, by name. Partial on a "
        "BUSINESS_OUTCOME -- whatever was captured before the answer arrived is still returned, "
        "since it was legitimately read.",
    )
    failure: FailureDetail | None = Field(
        default=None, description="Populated if and only if status is HARD_FAILURE."
    )
    run_id: str = Field(description="Evidence directory for this run, under evidence/.")
    duration_s: float = 0.0
    steps_executed: int = Field(
        default=0, description="How many steps completed, which locates a partial run in the flow."
    )
    recoveries: list[RecoveryEvent] = Field(
        default_factory=list,
        description="Every recovery performed, in order. Empty on a clean run.",
    )

    @property
    def is_failure(self) -> bool:
        """True only for HARD_FAILURE.

        Provided so callers stop writing `status != SUCCESS`, which quietly treats a legitimate
        business answer as a problem -- the exact mistake this contract exists to prevent.
        """
        return self.status is ReplayStatus.HARD_FAILURE

    @property
    def used_fallback_locator(self) -> bool:
        """Whether any recovery or failure record shows the recording limping."""
        return bool(self.failure and self.failure.fallback_used)
