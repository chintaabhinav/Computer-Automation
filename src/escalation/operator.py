"""The operator surface: how a human is asked, and how they answer.

WHAT IS MOCKED AND WHAT IS REAL. The *user interface* is mocked -- it is a terminal prompt, and
a real deployment would replace it with a queue, a ticket, a chat approval, or a web console.
Everything the interface is attached to is real:

* the pause is real -- the automation stops and does not proceed while the human decides;
* the control transfer is real and exclusive -- `SessionControl` refuses automation actions
  while the human holds control, so this is not a flag that everyone is trusted to respect;
* the session is the same one -- with `--headed`, the human works in the very Chromium window
  the automation was driving, on the same page, with the same cookies and form state, because
  the surface is not torn down and rebuilt;
* the resume is real -- the automation picks the flow back up from where it stopped;
* the handover is recorded -- transfer events, the operator's note, how long they held control,
  and the page state before and after all land in the run's evidence.

Swapping `CliOperatorConsole` for a queue-backed one is a change to this file only, because
everything above talks to the `OperatorConsole` protocol.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from src.escalation.request import InterventionRequest

log = logging.getLogger(__name__)


class OperatorDecision(StrEnum):
    """What the human decided."""

    RESUME = "resume"
    """The human dealt with it; hand control back and carry on."""
    ABORT = "abort"
    """Stop the run. The right answer when the request should not have been made at all."""


class OperatorResponse(BaseModel):
    """The human's answer, plus what they say they did."""

    decision: OperatorDecision
    note: str = Field(
        default="",
        description="Free text describing what the human did while holding control. Recorded "
        "verbatim in evidence: it is the only account of actions taken outside the automation, "
        "and a thin account is far better than none.",
    )

    @property
    def resumed(self) -> bool:
        return self.decision is OperatorDecision.RESUME


@runtime_checkable
class OperatorConsole(Protocol):
    """Anything that can put an intervention request in front of a decision-maker.

    A protocol rather than a base class so a real deployment's queue client satisfies it without
    inheriting from anything in this project.
    """

    def handle(self, request: InterventionRequest) -> OperatorResponse:
        """Present the request and block until a decision is available."""
        ...


class CliOperatorConsole:
    """Prints the request to a terminal and blocks on stdin until the operator answers.

    Blocking is the point, not a limitation: while this call is outstanding the automation is
    stopped and the human holds the session. With `--headed`, the browser window in front of
    them *is* the automation's session -- they can click, type, log back in, dismiss whatever
    was in the way -- and when they type `resume` the automation continues on the page they
    leave behind.
    """

    def __init__(self, headed: bool = False) -> None:
        self.headed = headed

    def handle(self, request: InterventionRequest) -> OperatorResponse:
        print("\n" + "=" * 78)
        print("AUTOMATION PAUSED -- HUMAN INTERVENTION REQUESTED")
        print("=" * 78)
        print(request.to_console_text())
        print("-" * 78)
        if self.headed:
            print("The browser window is yours. Do what is needed on that page.")
        else:
            print(
                "NOTE: this run is headless, so there is no window to work in. Re-run with\n"
                "      --headed to take over the session directly."
            )
        print("Then type:  resume   (hand control back and continue)")
        print("            abort    (stop the run)")
        print("-" * 78)

        while True:
            try:
                answer = input("decision [resume/abort]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                # No answer is not consent. An unattended terminal must not approve anything.
                print("\nno decision received; aborting (fail closed)")
                return OperatorResponse(decision=OperatorDecision.ABORT, note="no response")
            if answer in ("resume", "r"):
                note = input("what did you do? (optional): ").strip()
                return OperatorResponse(decision=OperatorDecision.RESUME, note=note)
            if answer in ("abort", "a"):
                note = input("why abort? (optional): ").strip()
                return OperatorResponse(decision=OperatorDecision.ABORT, note=note)
            print("please type 'resume' or 'abort'")


class AutoApproveOperatorConsole:
    """Approves every request without asking. For tests and deliberately unattended runs.

    Never a default. An automation that approves its own risky actions has no human in the loop,
    only the paperwork of one, so choosing this has to be an explicit act -- `--operator
    auto-approve` -- that shows up in the command line and in the evidence.
    """

    def __init__(self, note: str = "auto-approved (no human involved)") -> None:
        self.note = note
        self.handled: list[InterventionRequest] = []

    def handle(self, request: InterventionRequest) -> OperatorResponse:
        self.handled.append(request)
        log.warning(
            "AUTO-APPROVING intervention (%s) with no human involved: %s",
            request.reason, request.detail,
        )
        return OperatorResponse(decision=OperatorDecision.RESUME, note=self.note)


class AutoAbortOperatorConsole:
    """Refuses every request. The fail-closed console: useful in CI, where nobody is watching."""

    def __init__(self, note: str = "auto-aborted (no operator available)") -> None:
        self.note = note
        self.handled: list[InterventionRequest] = []

    def handle(self, request: InterventionRequest) -> OperatorResponse:
        self.handled.append(request)
        log.warning("auto-aborting intervention (%s): %s", request.reason, request.detail)
        return OperatorResponse(decision=OperatorDecision.ABORT, note=self.note)


def build_console(kind: str, *, headed: bool = False) -> OperatorConsole | None:
    """Map a CLI flag to a console. `none` returns None, which keeps the fail-closed default."""
    match kind:
        case "cli":
            return CliOperatorConsole(headed=headed)
        case "auto-approve":
            return AutoApproveOperatorConsole()
        case "auto-abort":
            return AutoAbortOperatorConsole()
        case "none":
            return None
    raise ValueError(f"unknown operator console: {kind!r}")
