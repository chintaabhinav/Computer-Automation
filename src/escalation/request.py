"""The intervention request: what a human is told when the automation stops.

A request is a document that leaves the system -- it is printed to a console, and in a real
deployment it would be posted to a queue, a ticket, or a chat channel. So it carries everything
needed to act without access to the machine, and it carries no raw member data: every text field
is masked through `src.evidence.redact` at construction, not at the point of display, because a
value that is already masked cannot be leaked by the next thing that reads it.

The request is also evidence. It is written to `evidence/<run_id>/intervention.json` alongside
the run's events and screenshots, so the reason a human was involved lives in the same directory
as the run they were involved in.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, model_validator

from src.evidence import EVIDENCE_ROOT, redact, redact_url

INTERVENTION_FILENAME = "intervention.json"


class StuckReason(StrEnum):
    """Why the automation stopped and asked for a person.

    Four reasons because a human needs different things from each: a dead end needs someone to
    finish the task, an unrecoverable error needs someone to judge whether it is safe to retry,
    a risky action needs an authorization decision, and a policy refusal needs someone who can
    change the policy -- or decide it was right.
    """

    DEAD_END = "dead_end"
    """The discovery agent could see no way forward."""
    UNRECOVERABLE_ERROR = "unrecoverable_error"
    """A replay hit a hard failure its recorded error rules could not handle."""
    RISKY_ACTION_CONFIRMATION = "risky_action_confirmation"
    """A step with a side effect on the target system needs authorization before it runs."""
    POLICY_REFUSAL = "policy_refusal"
    """Policy refused the action. A human decides whether the policy or the request is wrong."""


class InterventionRequest(BaseModel):
    """Everything a person needs to take over, in one serializable object."""

    run_id: str = Field(description="Links to evidence/<run_id>/ -- events, screenshots, this.")
    capability_id: str = Field(description="Which capability was running.")
    goal: str = Field(description="What it was trying to accomplish, in the original words.")
    reason: StuckReason
    step_index: int | None = Field(
        default=None, description="Where in the flow it stopped, or None outside a step."
    )
    action: str | None = Field(default=None, description="The action being attempted.")
    url: str = Field(default="", description="Where the session is, with query values masked.")
    page_summary: str = Field(
        default="",
        description="Redacted summary of what the page shows -- enough to recognize the screen "
        "without reproducing its data.",
    )
    screenshot: str | None = Field(
        default=None, description="Path to the screenshot taken when the run stopped."
    )
    detail: str = Field(default="", description="The specific failure or request, in a sentence.")
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _mask_free_text(self) -> InterventionRequest:
        """Redact on the way in, so no later consumer has to remember to.

        Masking at construction rather than at display is deliberate: this object is printed,
        serialized, and in a real deployment forwarded to systems this code does not control.
        A field that was never populated with a raw balance cannot leak one.
        """
        object.__setattr__(self, "goal", redact(self.goal))
        object.__setattr__(self, "url", redact_url(self.url) if self.url else "")
        object.__setattr__(self, "page_summary", redact(self.page_summary))
        object.__setattr__(self, "detail", redact(self.detail))
        return self

    def save(self, root: Path | str = EVIDENCE_ROOT) -> Path:
        """Write the request into this run's evidence directory and return the path."""
        destination = Path(root) / self.run_id / INTERVENTION_FILENAME
        destination.parent.mkdir(parents=True, exist_ok=True)
        existing = json.loads(destination.read_text()) if destination.exists() else []
        if not isinstance(existing, list):
            existing = [existing]
        existing.append(json.loads(self.model_dump_json()))
        # A list, because one run can need a person more than once, and overwriting would erase
        # the earlier reason -- usually the more interesting one.
        destination.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        return destination

    def to_console_text(self) -> str:
        """Render for a human reading a terminal."""
        lines = [
            f"reason      : {self.reason}",
            f"capability  : {self.capability_id}",
            f"goal        : {self.goal}",
        ]
        if self.step_index is not None:
            lines.append(f"step        : {self.step_index} ({self.action})")
        lines += [
            f"url         : {self.url}",
            f"detail      : {self.detail}",
        ]
        if self.page_summary:
            lines.append(f"page        : {self.page_summary}")
        if self.screenshot:
            lines.append(f"screenshot  : {self.screenshot}")
        lines.append(f"evidence    : evidence/{self.run_id}/")
        return "\n".join(lines)
