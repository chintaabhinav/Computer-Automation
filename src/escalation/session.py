"""The control-transfer model: exactly one party drives the session at a time.

This is the load-bearing piece of the escalation design. When a run escalates, the automation
does not merely *stop asking* -- it loses the right to act, and the guard refuses its actions
until control comes back. Exclusivity is enforced rather than tracked, because a flag that
everyone is expected to check is not a control model: the first caller who forgets turns a
handover into two parties typing into the same form.

The session itself is never torn down. Control moves; the browser, the page, the cookies and the
half-filled form all stay exactly as they were, which is what makes "a human operates the same
live session" true rather than aspirational.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from src.escalation.operator import OperatorConsole, OperatorResponse
from src.escalation.request import INTERVENTION_FILENAME, InterventionRequest
from src.evidence import EVIDENCE_ROOT, RunRecorder, redact

log = logging.getLogger(__name__)


class Controller(StrEnum):
    """Who currently holds the session."""

    AUTOMATION = "automation"
    HUMAN = "human"


class ControlViolation(Exception):
    """The automation tried to act while a human held control.

    A distinct exception from `PolicyViolation`: policy is about what the automation is allowed
    to do in principle, and this is about whose turn it is. Conflating them would make an
    ordinary handover look like a policy breach in the logs.
    """


class ControlTransfer(BaseModel):
    """One handover, recorded so the whole custody chain of a run can be read back."""

    at: datetime
    to: Controller
    reason: str = Field(description="Why control moved. On a resume, the operator's own note.")
    held_s: float | None = Field(
        default=None,
        description="How long the previous holder had control. On the resume transfer this is "
        "how long the human was working, which is the number an operations team asks for.",
    )


class SessionControl:
    """Tracks and enforces who may act on the session.

    Deliberately tiny and synchronous. Exclusivity here means "one thread of automation and one
    human"; a deployment with several concurrent operators needs real leases with expiry, which
    is out of scope and noted in the safety limits.
    """

    def __init__(self) -> None:
        self.controller: Controller = Controller.AUTOMATION
        self.history: list[ControlTransfer] = []
        self._held_since = time.monotonic()

    @property
    def held_by_human(self) -> bool:
        return self.controller is Controller.HUMAN

    def cede_to_human(self, reason: str) -> ControlTransfer:
        """Hand the session to a person. Automation actions are refused from this moment."""
        if self.controller is Controller.HUMAN:
            raise ControlViolation("control has already been ceded to a human")
        transfer = self._transfer(Controller.HUMAN, reason)
        log.warning("CONTROL ceded to human: %s", reason)
        return transfer

    def resume_automation(self, summary: str) -> ControlTransfer:
        """Take the session back, recording what the human reports having done."""
        if self.controller is Controller.AUTOMATION:
            raise ControlViolation("automation already holds control; nothing to resume")
        transfer = self._transfer(Controller.AUTOMATION, summary or "(no note given)")
        log.warning("CONTROL returned to automation after %.1fs: %s", transfer.held_s, summary)
        return transfer

    def require_automation_control(self, action: str) -> None:
        """Raise unless the automation may act right now.

        Called by the guard before every action. This is the enforcement the whole model rests
        on: while a human holds the session, an automated action is refused rather than
        interleaved with theirs.
        """
        if self.controller is not Controller.AUTOMATION:
            raise ControlViolation(
                f"automation attempted {action} while a human holds the session; "
                "control is exclusive and must be returned with resume_automation() first"
            )

    def _transfer(self, to: Controller, reason: str) -> ControlTransfer:
        now = time.monotonic()
        transfer = ControlTransfer(
            at=datetime.now(UTC),
            to=to,
            reason=reason,
            held_s=round(now - self._held_since, 2),
        )
        self.controller = to
        self._held_since = now
        self.history.append(transfer)
        return transfer


@dataclass
class EscalationHandler:
    """Runs one handover end to end: pause, ask, record, resume.

    The single place the sequence lives, so the three seams that escalate -- the agent's dead
    end, the guard's confirmation callback, and a replay hard failure -- cannot drift into three
    slightly different handovers.
    """

    console: OperatorConsole
    control: SessionControl = field(default_factory=SessionControl)
    recorder: RunRecorder | None = None
    """Evidence sink. The handover is written here, in the same run.jsonl as everything else."""

    def escalate(
        self,
        request: InterventionRequest,
        snapshot: Callable[[], str] | None = None,
    ) -> OperatorResponse:
        """Pause, hand control to the human, and return their decision.

        `snapshot` is called before and after the human works, and its redacted result is
        recorded as the before/after page state. That plus the operator's note is the honest,
        thin version of "record what the human did" -- a full input-level recording of their
        keystrokes and clicks is out of scope, and is noted as such in the evidence README.
        """
        before = redact(snapshot()) if snapshot else ""
        # Into the recorder's own root, not the default one. The handover and the request it
        # explains have to land in the same directory, and taking the root from the recorder is
        # what makes that true for a caller using a temporary or per-batch evidence tree --
        # otherwise a test with a tmp recorder silently writes into the committed evidence/.
        request.save(root=self.recorder.dir.parent if self.recorder else EVIDENCE_ROOT)

        self.control.cede_to_human(f"{request.reason}: {request.detail}")
        self._record(
            "control_ceded",
            reason=str(request.reason),
            step_index=request.step_index,
            detail=request.detail,
            page_before=before,
            # The filename only. A full path would be routed through redact() like every other
            # event field, and the digit masking would mangle the timestamp in the run id into
            # something unnavigable -- the directory is already implied by the run.
            intervention=INTERVENTION_FILENAME,
        )

        response = self.console.handle(request)

        # Resume before snapshotting. The snapshot goes through the same guarded surface the
        # automation uses, and while the human holds control that surface refuses to act -- so
        # capturing "after" first would trip the very exclusivity this handler enforces.
        transfer = self.control.resume_automation(response.note)
        after = redact(snapshot()) if snapshot else ""
        self._record(
            "control_returned",
            decision=str(response.decision),
            operator_note=response.note,
            human_held_s=transfer.held_s,
            page_before=before,
            page_after=after,
            page_changed=before != after,
        )
        return response

    def _record(self, event: str, **fields: object) -> None:
        if self.recorder is not None:
            self.recorder.event(event, **fields)
