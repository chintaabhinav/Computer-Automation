"""The enforcement chokepoint: a Surface that checks policy before it acts.

SINGLE CHOKEPOINT BY CONSTRUCTION. `GuardedSurface` implements the same `Surface` ABC as the
real one and wraps it, so the discovery agent and the replay engine drive policy without
knowing policy exists -- they were handed a surface, and every navigation, click, keystroke and
read they perform goes through this class on its way to the browser. There is no second path to
the driver: the only object that holds the concrete surface is this wrapper.

HONEST FRAMING. That is a *structural* guarantee, not an unbypassable one -- the same framing as
the replay layer's import boundary. Anyone can construct a raw `WebSurface` and drive it
directly, and the `--unsafe` CLI flag does exactly that on purpose. What the design buys is that
bypassing policy requires a deliberate, visible act rather than an oversight: no ordinary code
path, and no amount of new agent or engine code, reaches the driver without passing here. A real
deployment would harden this further by making the driver unreachable outside this module (a
private process boundary, or a browser that only accepts commands signed by the guard).

Every decision is logged, allowed ones included. In a regulated environment an enforcement with
no record is not an enforcement: "the automation was not permitted to do that" is only credible
if the permitted actions are equally accounted for.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field

from src.artifact.schema import ActionType, Checkpoint, Locator, RiskLevel
from src.evidence import redact, redact_url
from src.escalation.session import ControlViolation, SessionControl
from src.safety.policy import Policy, RiskyActionMode, effective_risk
from src.surface.base import ElementHandle, Observation, Surface, SurfaceError

log = logging.getLogger(__name__)

ConfirmationCallback = Callable[[ActionType, str, RiskLevel], bool]
"""Asked to approve a risky action. Receives the action, a redacted target, and the risk.

Returning False denies. This is also the seam the escalation layer will attach to: a human
handoff is, structurally, a confirmation callback that takes a long time to answer.
"""


class PolicyViolation(Exception):
    """An action was refused by policy.

    Carries the rule, the action and a redacted target so a caller can report the refusal
    without re-deriving it -- and never the raw value, consistent with the schema validator's
    error messages. A refusal notice that leaks the account number it was protecting would be a
    poor kind of enforcement.
    """

    def __init__(self, rule: str, action: str, target: str, detail: str = "") -> None:
        self.rule = rule
        self.action = action
        self.target = target
        self.detail = detail
        suffix = f" {detail}" if detail else ""
        super().__init__(
            f"policy refused {action} on {target}: {rule}.{suffix}"
        )


class PolicyDecision(BaseModel):
    """One allow-or-deny decision, recorded whichever way it went."""

    action: str
    target: str = Field(description="Redacted: a URL with its query masked, or a locator.")
    allowed: bool
    rule: str = Field(description="The policy rule that decided it, by name or pattern.")
    risk: str | None = None
    detail: str = ""


class GuardedSurface(Surface):
    """Wraps a concrete surface and enforces a `Policy` before delegating to it.

    Transparent when policy permits: the wrapped surface's behaviour is unchanged, so the engine
    and agent need no special handling and the same tests pass through the wrapper. Opaque when
    it does not: a refused action raises `PolicyViolation` and never reaches the driver.
    """

    def __init__(
        self,
        inner: Surface,
        policy: Policy,
        *,
        confirm: ConfirmationCallback | None = None,
        control: SessionControl | None = None,
    ) -> None:
        self.inner = inner
        self.policy = policy
        self.control = control
        """Whose turn it is. When set, every action is refused while a human holds the session --
        the enforcement half of the escalation model, checked here because this is the one place
        every action passes through."""
        self.confirm = confirm
        """Consulted under CONFIRM mode. None denies every risky action -- fail closed: a run
        that cannot ask anybody has not been authorized by anybody."""
        self.decisions: list[PolicyDecision] = []
        """Every decision in order, for evidence and for after-the-fact review."""
        self._risk: RiskLevel | None = None

    # -- risk declaration ---------------------------------------------------------------

    def declare_step_risk(self, risk: RiskLevel | None) -> None:
        """Record the risk of the step about to run, as declared by the caller.

        Persists until the next declaration, because one step performs several surface calls
        (resolve, then act). A caller that never declares leaves this None, which
        `effective_risk` reads as RISKY.
        """
        self._risk = risk
        self.inner.declare_step_risk(risk)

    # -- enforcement --------------------------------------------------------------------

    def _record(self, decision: PolicyDecision) -> None:
        """Log and retain one decision."""
        self.decisions.append(decision)
        if decision.allowed:
            log.info(
                "POLICY allow %s on %s (rule: %s%s)",
                decision.action, decision.target, decision.rule,
                f", risk={decision.risk}" if decision.risk else "",
            )
        else:
            log.error(
                "POLICY DENY %s on %s (rule: %s) %s",
                decision.action, decision.target, decision.rule, decision.detail,
            )

    def _deny(self, action: str, target: str, rule: str, detail: str = "") -> PolicyViolation:
        self._record(
            PolicyDecision(
                action=action,
                target=target,
                allowed=False,
                rule=rule,
                risk=str(effective_risk(self._risk)),
                detail=detail,
            )
        )
        return PolicyViolation(rule=rule, action=action, target=target, detail=detail)

    def _authorize(
        self, action: ActionType, target: str, *, acting: bool, risk_checked: bool = True
    ) -> None:
        """The one gate. Checks action type, then location, then risk -- in that order.

        Ordered cheapest-and-most-absolute first: an action type that is simply not permitted
        needs no location or risk analysis, and reporting the most fundamental reason makes the
        denial easier to act on.

        `risk_checked=False` is for the harness's own perception (`observe`, `screenshot`).
        Risk describes what a *recorded step* does to the target system; looking at the screen is
        not a step and has no side effect, so applying a step's risk to it is a category error.
        It was also a real one: under the default BLOCK mode the discovery agent could not take
        its first observation, because nothing had been declared yet and fail-closed correctly
        read that as risky. Perception is still location-gated -- reading a page that is not on
        the allowlist is exactly the exfiltration an allowlist is for.
        """
        # Whose turn it is, before what is allowed: an action taken during a human's handover
        # is wrong even when policy would permit it, and interleaving two parties' input into
        # one form is worse than either acting alone.
        if self.control is not None:
            try:
                self.control.require_automation_control(str(action))
            except ControlViolation as exc:
                self._record(
                    PolicyDecision(
                        action=str(action),
                        target=target,
                        allowed=False,
                        rule="a human holds control of this session",
                        detail=str(exc),
                    )
                )
                raise

        if not self.policy.allows_action(action):
            raise self._deny(
                str(action),
                target,
                "action type is not in allowed_actions",
                detail=f"permitted: {[str(a) for a in self.policy.allowed_actions]}",
            )

        location = target if action is ActionType.NAVIGATE else self._current_location()
        if location is None:
            if acting:
                raise self._deny(
                    str(action),
                    target,
                    "the current location could not be determined",
                    detail="refusing to act somewhere we cannot verify",
                )
            # Perception with nothing open has no location to violate and nothing to read.
        else:
            matched = self.policy.match_url(location)
            if matched is None:
                raise self._deny(
                    str(action),
                    redact_url(location),
                    "url is not covered by allowed_url_patterns",
                )

        risk = effective_risk(self._risk)
        rule = f"allowed_url_patterns + allowed_actions (risk={risk})"

        if not risk_checked:
            self._record(
                PolicyDecision(
                    action=str(action),
                    target=target,
                    allowed=True,
                    rule="allowed_url_patterns (perception is not risk-gated)",
                )
            )
            return

        if risk is RiskLevel.RISKY or self.policy.needs_confirmation(action, self._risk):
            self._authorize_risky(action, target, risk)
            return

        self._record(
            PolicyDecision(
                action=str(action), target=target, allowed=True, rule=rule, risk=str(risk)
            )
        )

    def _authorize_risky(self, action: ActionType, target: str, risk: RiskLevel) -> None:
        """Apply the configured treatment for a risky action."""
        if self.policy.needs_confirmation(action, self._risk):
            if self.confirm is None:
                raise self._deny(
                    str(action),
                    target,
                    "risky action requires confirmation and no confirmation callback was supplied",
                    detail="fail closed: nobody could authorize this",
                )
            if not self.confirm(action, target, risk):
                raise self._deny(
                    str(action), target, "risky action was not confirmed", detail="denied by callback"
                )
            self._record(
                PolicyDecision(
                    action=str(action),
                    target=target,
                    allowed=True,
                    rule="risky action confirmed by callback",
                    risk=str(risk),
                )
            )
            return

        if self.policy.risky_action_mode is RiskyActionMode.BLOCK:
            raise self._deny(
                str(action),
                target,
                "risky_action_mode is BLOCK",
                detail="the step declares a side effect on the target system",
            )

        # FLAG: permitted, but never quietly.
        log.warning(
            "POLICY FLAG risky %s on %s permitted by risky_action_mode=flag", action, target
        )
        self._record(
            PolicyDecision(
                action=str(action),
                target=target,
                allowed=True,
                rule="risky_action_mode is FLAG",
                risk=str(risk),
            )
        )

    def _current_location(self) -> str | None:
        try:
            return self.inner.current_url()
        except SurfaceError:
            return None

    @staticmethod
    def _describe(locator: Locator) -> str:
        """A locator, redacted, for logs and refusal messages."""
        return redact(f"{locator.primary.by}={locator.primary.value}")

    # -- Surface: gated ------------------------------------------------------------------

    def open(self, url: str) -> None:
        self._authorize(ActionType.NAVIGATE, redact_url(url), acting=True)
        self.inner.open(url)

    def click(self, locator: Locator) -> None:
        self._authorize(ActionType.CLICK, self._describe(locator), acting=True)
        self.inner.click(locator)

    def type(self, locator: Locator, text: str) -> None:
        self._authorize(ActionType.TYPE, self._describe(locator), acting=True)
        self.inner.type(locator, text)

    def read(self, locator: Locator) -> str:
        self._authorize(ActionType.READ, self._describe(locator), acting=True)
        return self.inner.read(locator)

    def observe(self) -> Observation:
        # Perception, not action: location-gated, tolerant of nothing being open, not
        # risk-gated. See _authorize.
        self._authorize(ActionType.READ, "observe()", acting=False, risk_checked=False)
        return self.inner.observe()

    def screenshot(self, path: str | Path) -> None:
        self._authorize(ActionType.READ, "screenshot()", acting=False, risk_checked=False)
        self.inner.screenshot(path)

    # -- Surface: pass-through -----------------------------------------------------------
    #
    # Resolution and condition evaluation touch nothing and change nothing, and the engine needs
    # them precisely when a step has failed -- including on a page policy would refuse to act
    # on, in order to classify what went wrong. Gating them would blind the error taxonomy
    # without preventing any harm.

    def find(self, locator: Locator) -> ElementHandle | None:
        return self.inner.find(locator)

    def check(self, checkpoint: Checkpoint) -> bool:
        return self.inner.check(checkpoint)

    def wait_for(self, checkpoint: Checkpoint, timeout_s: float, poll_ms: int) -> bool:
        return self.inner.wait_for(checkpoint, timeout_s, poll_ms)

    def current_url(self) -> str:
        return self.inner.current_url()

    def close(self) -> None:
        self.inner.close()
