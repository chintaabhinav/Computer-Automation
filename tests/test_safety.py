"""Tests for the policy layer.

Two things are being checked. That every denial actually denies -- in a regulated context a
guardrail that can be talked past is worse than none, because it is trusted. And that the
wrapper is *transparent* when policy permits: if guarding a run changed its behaviour, nobody
would leave it on.
"""

from __future__ import annotations

import logging

import pytest

from src.artifact.schema import ActionType, Locator, LocatorBy, LocatorRule, RiskLevel
from src.safety.guard import GuardedSurface, PolicyViolation
from src.safety.policy import Policy, RiskyActionMode, effective_risk, load_policy
from src.surface.base import ElementHandle, Observation, Surface

ALLOWED = "re:^http://localhost:5001(/.*)?$"
EVERY_ACTION = [
    ActionType.NAVIGATE,
    ActionType.CLICK,
    ActionType.TYPE,
    ActionType.READ,
    ActionType.WAIT,
    ActionType.DISMISS,
]


class _RecordingSurface(Surface):
    """A surface that records what reached it, so a denial can be proven to have stopped short.

    The assertion that matters is not just "PolicyViolation was raised" but "the driver was
    never asked" -- an enforcement that raises after acting has not enforced anything.
    """

    def __init__(self, url: str = "http://localhost:5001/") -> None:
        self.url = url
        self.calls: list[str] = []

    def open(self, url: str) -> None:
        self.calls.append(f"open:{url}")
        self.url = url

    def observe(self) -> Observation:
        self.calls.append("observe")
        return Observation(url=self.url, title="t", elements=[])

    def find(self, locator):
        return ElementHandle(ref="e1", rule=locator.primary, candidate_index=0, native=None)

    def click(self, locator) -> None:
        self.calls.append("click")

    def type(self, locator, text: str) -> None:
        self.calls.append("type")

    def read(self, locator) -> str:
        self.calls.append("read")
        return "value"

    def check(self, checkpoint) -> bool:
        return True

    def wait_for(self, checkpoint, timeout_s, poll_ms) -> bool:
        return True

    def screenshot(self, path) -> None:
        self.calls.append("screenshot")

    def current_url(self) -> str:
        return self.url

    def close(self) -> None:
        self.calls.append("close")


def _locator(value: str = "Search") -> Locator:
    return Locator(primary=LocatorRule(by=LocatorBy.TEXT, value=value), rationale="test")


def _policy(**overrides) -> Policy:
    base = {"allowed_url_patterns": [ALLOWED], "allowed_actions": EVERY_ACTION}
    return Policy(**(base | overrides))


def _guard(policy: Policy | None = None, **kwargs) -> tuple[GuardedSurface, _RecordingSurface]:
    inner = _RecordingSurface()
    guard = GuardedSurface(inner, policy or _policy(), **kwargs)
    guard.declare_step_risk(RiskLevel.SAFE)
    return guard, inner


# ---------------------------------------------------------------------------------------
# Allowlist.
# ---------------------------------------------------------------------------------------


def test_navigation_off_the_allowlist_is_denied():
    guard, inner = _guard()

    with pytest.raises(PolicyViolation, match="allowed_url_patterns"):
        guard.open("https://real-bank.example/transfer")

    assert inner.calls == [], "the driver must never have been asked"


def test_acting_on_a_page_off_the_allowlist_is_denied():
    """The allowlist covers where an action happens, not just where navigation was aimed."""
    guard, inner = _guard()
    inner.url = "https://real-bank.example/transfer"

    with pytest.raises(PolicyViolation, match="allowed_url_patterns"):
        guard.click(_locator())

    assert inner.calls == []


def test_an_empty_allowlist_denies_everything():
    """An allowlist that defaults to allow is not an allowlist."""
    guard, _ = _guard(_policy(allowed_url_patterns=[]))

    with pytest.raises(PolicyViolation):
        guard.open("http://localhost:5001/")


def test_a_glob_star_does_not_stop_at_a_host_boundary():
    """Why the shipped policy uses an anchored regex: this glob has a hole in it."""
    loose = Policy(allowed_url_patterns=["http://localhost:5001*"], allowed_actions=EVERY_ACTION)
    anchored = _policy()

    assert loose.match_url("http://localhost:5001.evil.example/") is not None
    assert anchored.match_url("http://localhost:5001.evil.example/") is None


def test_the_allowed_url_report_names_the_rule_that_permitted_it():
    """'It was permitted' is not an answer; 'permitted by this line' is."""
    assert _policy().match_url("http://localhost:5001/member") == ALLOWED


# ---------------------------------------------------------------------------------------
# Action types.
# ---------------------------------------------------------------------------------------


def test_a_disallowed_action_type_is_denied():
    guard, inner = _guard(_policy(allowed_actions=[ActionType.NAVIGATE, ActionType.READ]))

    with pytest.raises(PolicyViolation, match="allowed_actions"):
        guard.type(_locator("Member ID"), "12345")

    assert inner.calls == []


def test_the_denial_does_not_echo_the_typed_value():
    """A refusal notice that leaks what it was protecting is a poor kind of enforcement."""
    guard, _ = _guard(_policy(allowed_actions=[ActionType.NAVIGATE]))

    with pytest.raises(PolicyViolation) as caught:
        guard.type(_locator("Member ID"), "12345")

    assert "12345" not in str(caught.value)


# ---------------------------------------------------------------------------------------
# Risky actions.
# ---------------------------------------------------------------------------------------


def test_a_risky_step_is_blocked_under_block_mode():
    guard, inner = _guard(_policy(risky_action_mode=RiskyActionMode.BLOCK))
    guard.declare_step_risk(RiskLevel.RISKY)

    with pytest.raises(PolicyViolation, match="BLOCK"):
        guard.click(_locator("Open Sub-Account"))

    assert inner.calls == []


def test_confirm_mode_with_no_callback_denies():
    """Fail closed: a run that cannot ask anybody has not been authorized by anybody."""
    guard, inner = _guard(_policy(risky_action_mode=RiskyActionMode.CONFIRM), confirm=None)
    guard.declare_step_risk(RiskLevel.RISKY)

    with pytest.raises(PolicyViolation, match="no confirmation callback"):
        guard.click(_locator("Open Sub-Account"))

    assert inner.calls == []


def test_confirm_mode_with_an_approving_callback_proceeds():
    asked: list[tuple[str, str, str]] = []

    def approve(action, target, risk) -> bool:
        asked.append((str(action), target, str(risk)))
        return True

    guard, inner = _guard(
        _policy(risky_action_mode=RiskyActionMode.CONFIRM), confirm=approve
    )
    guard.declare_step_risk(RiskLevel.RISKY)

    guard.click(_locator("Open Sub-Account"))

    assert inner.calls == ["click"]
    assert asked == [("click", "text=Open Sub-Account", "risky")]


def test_confirm_mode_with_a_refusing_callback_denies():
    guard, inner = _guard(
        _policy(risky_action_mode=RiskyActionMode.CONFIRM), confirm=lambda *_: False
    )
    guard.declare_step_risk(RiskLevel.RISKY)

    with pytest.raises(PolicyViolation, match="not confirmed"):
        guard.click(_locator("Open Sub-Account"))

    assert inner.calls == []


def test_flag_mode_proceeds_but_warns(caplog):
    guard, inner = _guard(_policy(risky_action_mode=RiskyActionMode.FLAG))
    guard.declare_step_risk(RiskLevel.RISKY)

    with caplog.at_level(logging.WARNING):
        guard.click(_locator("Open Sub-Account"))

    assert inner.calls == ["click"]
    assert any("FLAG risky" in record.getMessage() for record in caplog.records)


def test_undeclared_risk_is_treated_as_risky():
    """A step nobody classified is not a safe step, so silence takes the strict path."""
    inner = _RecordingSurface()
    guard = GuardedSurface(inner, _policy(risky_action_mode=RiskyActionMode.BLOCK))
    # Never declared: no declare_step_risk call at all.

    with pytest.raises(PolicyViolation, match="BLOCK"):
        guard.click(_locator())

    assert inner.calls == []


def test_effective_risk_resolves_unknown_values_to_risky():
    assert effective_risk(None) is RiskLevel.RISKY
    assert effective_risk("nonsense") is RiskLevel.RISKY
    assert effective_risk(RiskLevel.SAFE) is RiskLevel.SAFE


def test_require_confirmation_for_overrides_a_safe_declaration():
    """Some actions are the hazard regardless of what a recording claims about them."""
    guard, inner = _guard(
        _policy(require_confirmation_for=[ActionType.CLICK]), confirm=lambda *_: False
    )
    guard.declare_step_risk(RiskLevel.SAFE)

    with pytest.raises(PolicyViolation, match="not confirmed"):
        guard.click(_locator())

    assert inner.calls == []


# ---------------------------------------------------------------------------------------
# The decision log.
# ---------------------------------------------------------------------------------------


def test_both_allow_and_deny_decisions_are_recorded(caplog):
    """An enforcement with no record is not an enforcement -- and that includes the allows."""
    guard, _ = _guard()

    with caplog.at_level(logging.INFO):
        guard.open("http://localhost:5001/")
        guard.read(_locator("Available"))
        with pytest.raises(PolicyViolation):
            guard.open("https://real-bank.example/")

    assert [(d.action, d.allowed) for d in guard.decisions] == [
        ("navigate", True),
        ("read", True),
        ("navigate", False),
    ]
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "POLICY allow navigate" in logged
    assert "POLICY DENY navigate" in logged


def test_decision_targets_are_redacted():
    guard, _ = _guard()

    guard.open("http://localhost:5001/member?member_id=12345")

    target = guard.decisions[-1].target
    assert "12345" not in target and "1***5" in target


# ---------------------------------------------------------------------------------------
# Transparency, and the shipped policy.
# ---------------------------------------------------------------------------------------


def test_the_shipped_policy_permits_the_mock_app_and_nothing_else():
    policy = load_policy("config/policy.yaml")

    assert policy.match_url("http://localhost:5001/member?member_id=1")
    assert policy.match_url("http://127.0.0.1:5001/")
    assert policy.match_url("https://real-bank.example/") is None
    assert policy.risky_action_mode is RiskyActionMode.BLOCK


def test_a_missing_policy_file_refuses_rather_than_defaulting_open():
    """'The policy could not be read' must never resolve to 'so we allowed everything'."""
    with pytest.raises(FileNotFoundError, match="Refusing to run without one"):
        load_policy("config/does-not-exist.yaml")


@pytest.mark.integration
def test_a_guarded_replay_still_succeeds_for_the_clean_case(surface, mock_app, tmp_path):
    """The wrapper must be invisible when policy permits, or nobody would leave it on."""
    from src.evidence import RunRecorder
    from src.replay.engine import ReplayEngine
    from src.replay.result import ReplayStatus
    from tests.test_replay import _artifact

    guarded = GuardedSurface(surface, load_policy("config/policy.yaml"))
    engine = ReplayEngine(recorder=RunRecorder.start("replay", root=tmp_path))

    result = engine.replay(_artifact(mock_app), {"member_id": "12345"}, guarded)

    assert result.status is ReplayStatus.SUCCESS
    assert result.outputs == {"savings_balance": "$4,200.00"}
    assert guarded.decisions, "the run should have been policed, not merely permitted"
    assert all(decision.allowed for decision in guarded.decisions)


def test_perception_is_not_risk_gated():
    """Observing is not a step and has no side effect, so a step's risk must not gate it.

    This was a real failure: under the default BLOCK mode the discovery agent could not take
    its first observation, because no risk had been declared yet and fail-closed correctly read
    the silence as risky. Perception stays location-gated -- reading a page off the allowlist is
    exactly the exfiltration an allowlist exists to stop.
    """
    inner = _RecordingSurface()
    guard = GuardedSurface(inner, _policy(risky_action_mode=RiskyActionMode.BLOCK))
    # No declare_step_risk at all, as on the agent's first turn.

    guard.observe()
    guard.screenshot("/tmp/shot.png")

    assert inner.calls == ["observe", "screenshot"]
    assert all(decision.allowed for decision in guard.decisions)


def test_perception_off_the_allowlist_is_still_denied():
    guard, inner = _guard()
    inner.url = "https://real-bank.example/statements"

    with pytest.raises(PolicyViolation, match="allowed_url_patterns"):
        guard.observe()

    assert inner.calls == []


def test_a_url_with_only_a_query_string_is_allowed():
    """`--inject` produces http://localhost:5001?inject=slow -- no path, just a query.

    The shipped pattern once required a path segment, which silently refused every injected
    error scenario through the guarded CLI. The allowlist is about the host; "?" after the port
    is as legitimate as "/".
    """
    policy = load_policy("config/policy.yaml")

    assert policy.match_url("http://localhost:5001?inject=slow")
    assert policy.match_url("http://localhost:5001/?inject=slow")
    assert policy.match_url("http://localhost:5001")


def test_widening_for_query_strings_did_not_open_the_host_boundary():
    """The anchoring that matters must survive the fix."""
    policy = load_policy("config/policy.yaml")

    assert policy.match_url("http://localhost:5001.evil.example/") is None
    assert policy.match_url("http://localhost:50011/") is None
    assert policy.match_url("http://evil.example/?x=http://localhost:5001") is None
