"""The surface contract: how the system perceives and acts, independent of any driver.

This module is the project's key architectural boundary. Everything above it -- the discovery
agent, the replay engine, safety, escalation -- talks only to the `Surface` ABC and the neutral
data types defined here, and nothing above this layer imports Playwright. That is what keeps
"how we drive a browser" swappable without touching the recorded flow: an artifact records
`Locator`s and `Checkpoint`s, and any surface that can resolve them can replay it.

The types here are deliberately surface-agnostic. An `Observation` carries roles, names and
text -- not markup -- so a desktop implementation can populate exactly the same structure from
an OS accessibility tree (AXUIElement on macOS, UIAutomation on Windows) and every layer above
continues to work unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.artifact.schema import Checkpoint, Locator, LocatorRule, RiskLevel

MAX_OBSERVED_ELEMENTS = 60
"""Cap on elements in one Observation.

An Observation is fed to an LLM on every discovery turn, so its size is a direct cost and, past
a point, a comprehension problem. A legacy page can contain hundreds of table cells; the most
salient few dozen are what the agent actually reasons about.
"""

MAX_TEXT_CHARS = 160
"""Cap on the text carried per perceived element, for the same reason."""


class SurfaceError(Exception):
    """Base for failures raised by a surface implementation.

    Exists so the replay engine can catch surface problems as a category and map them onto the
    artifact's error taxonomy, without importing or knowing about any specific driver's
    exception types.
    """


class ElementNotFoundError(SurfaceError):
    """No candidate in a `Locator` resolved to exactly one visible element.

    Raised by the acting methods (`click`, `type`, `read`) rather than returning silently,
    because acting on the wrong element on a banking screen is far worse than stopping. `find`
    returns None instead, so callers that want to probe can do so without exception handling.
    """


@dataclass(frozen=True)
class PerceivedElement:
    """One element as perceived by a surface -- roughly an accessibility node.

    Not a DOM node and not markup: role, name and text are the vocabulary shared by a browser's
    accessibility tree and an OS one, which is what lets the same agent reason about both.
    """

    role: str
    """Semantic kind, e.g. 'button', 'textbox', 'cell', 'heading'. Normalized across surfaces."""

    name: str
    """Accessible name -- the label, caption, or heading a human would use to refer to it.
    This is what the agent turns into a LABEL or TEXT locator, so it is the most important
    field for producing durable recordings."""

    text: str
    """Visible text content, truncated to MAX_TEXT_CHARS. Carries the values a legacy screen
    prints without labels (table cells, amounts) that `name` alone would miss."""

    ref: str
    """Opaque handle, unique within one Observation and meaningless outside it.

    Deliberately not a selector: it lets the agent point at an element unambiguously ("read
    ref e17") without the prompt implying that raw selectors are the currency of the system.
    The surface is free to back it with an XPath, an a11y node id, or a native window handle.
    """


@dataclass(frozen=True)
class Observation:
    """A snapshot of everything currently perceivable on the surface.

    This is the agent's entire view of the world -- deliberately not raw HTML. HTML would be
    enormous on a table-based legacy page, would tempt the model into writing brittle selectors
    against incidental markup, and would be meaningless for a desktop surface. Roles, names and
    text are the common denominator, so a desktop implementation populates this same structure
    from an OS accessibility tree and the layers above cannot tell the difference.
    """

    url: str
    """Where the surface currently is. A window or screen identifier on a desktop surface."""

    title: str
    """Human-readable title of the current page or window."""

    elements: list[PerceivedElement] = field(default_factory=list)
    """The perceivable elements, capped at MAX_OBSERVED_ELEMENTS and ordered by document
    position so the agent sees the screen the way a person reads it."""

    truncated: bool = False
    """True when the cap dropped elements. Surfaced rather than hidden: if the agent is
    reasoning about a partial view, that fact belongs in the evidence for the run."""

    def to_prompt_text(self) -> str:
        """Render as compact text for an LLM prompt.

        Lives here, next to the caps, so every decision about how much the model sees is made
        in one place instead of being spread across prompt-building code.
        """
        lines = [f"URL: {self.url}", f"TITLE: {self.title}", "ELEMENTS:"]
        for element in self.elements:
            parts = [f"[{element.ref}]", element.role]
            if element.name:
                parts.append(f'name="{element.name}"')
            if element.text and element.text != element.name:
                parts.append(f'text="{element.text}"')
            lines.append("  " + " ".join(parts))
        if self.truncated:
            lines.append(f"  ... truncated at {len(self.elements)} elements")
        return "\n".join(lines)


@dataclass(frozen=True)
class ElementHandle:
    """A resolved element, plus the record of how it was found.

    The provenance fields are the point of this type. Which candidate matched -- and whether it
    was a fallback -- is the system's earliest signal that the target application has drifted:
    a recording whose primary locator quietly stopped working still succeeds, but it is one
    release away from failing. Carrying that on every resolution means drift can be reported
    from a successful run rather than discovered from a broken one.
    """

    ref: str
    """Opaque handle for the resolved element, in the same namespace as PerceivedElement.ref."""

    rule: LocatorRule
    """The candidate rule that actually matched."""

    candidate_index: int
    """Position in `Locator.candidates()`; 0 is the primary."""

    native: Any = field(default=None, repr=False, compare=False)
    """The driver's own element object. Opaque above this layer -- typed `Any` precisely so
    that no caller is tempted to reach into it and couple itself to Playwright."""

    @property
    def was_fallback(self) -> bool:
        """True when the primary locator failed and a fallback matched instead."""
        return self.candidate_index > 0


@dataclass(frozen=True)
class FallbackEvent:
    """A record that a locator's primary rule failed and a fallback carried the step.

    Collected by the surface over a run so drift can be surfaced in evidence and reviewed,
    rather than being visible only in logs that nobody reads after a green run.
    """

    primary: LocatorRule
    used: LocatorRule
    candidate_index: int
    rationale: str
    """The Locator's recorded justification, copied here so a drift report is readable on its
    own: it states what the recording claimed would be robust, next to what actually worked."""


class Surface(ABC):
    """The perception-and-action contract every surface implementation must satisfy.

    Two responsibilities, kept together because they share the same targeting vocabulary:
    perceiving state (`observe`, `check`, `read`, `current_url`) and changing it (`open`,
    `click`, `type`). Both sides speak the artifact's own `Locator` and `Checkpoint` types, so
    there is no translation layer between what discovery records and what replay executes --
    a recorded locator is handed to the surface verbatim.

    Implementations are expected to be stateful and single-session: construct, `open`, drive,
    `close`.
    """

    @abstractmethod
    def open(self, url: str) -> None:
        """Navigate to `url`, starting the underlying driver if it is not yet running."""

    @abstractmethod
    def observe(self) -> Observation:
        """Return the current perceivable state, sized for an LLM prompt.

        The only method the discovery agent uses to see. Implementations must apply the element
        and text caps rather than returning everything present.
        """

    @abstractmethod
    def find(self, locator: Locator) -> ElementHandle | None:
        """Resolve `locator` by trying `locator.candidates()` in order.

        Returns the first candidate that resolves to exactly one visible element, or None if
        no candidate does. Ambiguity is treated as failure, not as "pick the first" -- on a
        back-office screen, acting on an arbitrary one of several matches is a real hazard.
        """

    @abstractmethod
    def click(self, locator: Locator) -> None:
        """Click the element `locator` resolves to. Raises ElementNotFoundError if it does not."""

    @abstractmethod
    def type(self, locator: Locator, text: str) -> None:
        """Enter `text` into the element `locator` resolves to, replacing any existing value."""

    @abstractmethod
    def read(self, locator: Locator) -> str:
        """Return the visible text of the element `locator` resolves to.

        The only way data leaves the surface, which is what makes the artifact's output
        contract enforceable: every returned field traces back to one READ of one locator.
        """

    @abstractmethod
    def check(self, checkpoint: Checkpoint) -> bool:
        """Evaluate `checkpoint` against the current state, once, without waiting.

        Separate from `wait_for` so the caller decides whether a condition is expected now or
        eventually -- error detection wants the immediate answer, readiness wants the patient
        one.
        """

    @abstractmethod
    def wait_for(self, checkpoint: Checkpoint, timeout_s: float, poll_ms: int) -> bool:
        """Poll `check(checkpoint)` until it passes or `timeout_s` elapses. Returns whether it passed.

        Returning a bool rather than raising is deliberate: a timeout is not automatically a
        failure. It is the observation that distinguishes a slow page (recoverable -- wait and
        retry) from a stuck one (hard failure), and that judgment belongs to the replay engine
        reading the step's error rules, not to the surface.
        """

    @abstractmethod
    def screenshot(self, path: str | Path) -> None:
        """Write a screenshot to `path`, creating parent directories.

        Evidence, not decoration: a replay failure is only reviewable if a human can see what
        the screen looked like at the moment it failed.
        """

    @abstractmethod
    def current_url(self) -> str:
        """Return the current location. Also what the safety allowlist is checked against."""

    @abstractmethod
    def close(self) -> None:
        """Release the driver and all its resources. Must be safe to call more than once."""

    def declare_step_risk(self, risk: RiskLevel | None) -> None:
        """Tell the surface the consequence level of the step about to run. A no-op by default.

        Concrete, unguarded surfaces do not care: a browser clicks what it is told. The hook
        exists so a policy wrapper can learn what the caller believes it is about to do without
        every Surface method growing a risk argument -- which would push a safety concern into
        the perception contract and into every implementation, including the desktop one.

        Callers should declare before each step. A wrapper that enforces policy is entitled to
        treat "never declared" as RISKY, so silence is the safe default rather than a loophole.
        """
