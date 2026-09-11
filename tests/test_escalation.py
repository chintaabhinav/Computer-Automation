"""Tests for human-in-the-loop escalation.

The claim being tested is that this is a real control-transfer mechanism rather than a flag: an
automation that holds a stale sense of ownership must be *refused*, not merely discouraged. The
operator UI is mocked throughout -- that is the documented mock -- but the pause, the exclusive
handover, the resume, and the recording of it all are exercised for real.
"""

from __future__ import annotations

import json

import pytest

from src.artifact.schema import ActionType, Locator, LocatorBy, LocatorRule, RiskLevel
from src.escalation.operator import (
    AutoAbortOperatorConsole,
    AutoApproveOperatorConsole,
    OperatorDecision,
    build_console,
)
from src.escalation.request import InterventionRequest, StuckReason
from src.escalation.session import (
    Controller,
    ControlViolation,
    EscalationHandler,
    SessionControl,
)
from src.evidence import RunRecorder
from src.safety.guard import GuardedSurface, PolicyViolation
from src.safety.policy import Policy, RiskyActionMode
from tests.test_safety import EVERY_ACTION, _RecordingSurface

ALLOWED = "re:^http://localhost:5001(/.*)?$"


def _policy(**overrides) -> Policy:
    base = {"allowed_url_patterns": [ALLOWED], "allowed_actions": EVERY_ACTION}
    return Policy(**(base | overrides))


def _locator(value: str = "Open Sub-Account") -> Locator:
    return Locator(primary=LocatorRule(by=LocatorBy.TEXT, value=value), rationale="test")


def _request(**overrides) -> InterventionRequest:
    payload = {
        "run_id": "test-run",
        "capability_id": "lookup_member_balance",
        "goal": "Look up the member given as input",
        "reason": StuckReason.DEAD_END,
        "detail": "nothing matched",
    }
    return InterventionRequest(**(payload | overrides))


# ---------------------------------------------------------------------------------------
# Exclusive control.
# ---------------------------------------------------------------------------------------


def test_control_starts_with_automation():
    assert SessionControl().controller is Controller.AUTOMATION


def test_automation_cannot_act_while_the_human_holds_control():
    """The load-bearing assertion: exclusivity is enforced, not tracked."""
    control = SessionControl()
    control.cede_to_human("risky step needs authorization")

    with pytest.raises(ControlViolation, match="control is exclusive"):
        control.require_automation_control("click")


def test_the_guard_refuses_automation_actions_during_a_handover():
    """Enforced where it matters: at the one chokepoint every action passes through."""
    inner = _RecordingSurface()
    control = SessionControl()
    guard = GuardedSurface(inner, _policy(), control=control)
    guard.declare_step_risk(RiskLevel.SAFE)

    control.cede_to_human("human is fixing the session")

    with pytest.raises(ControlViolation):
        guard.click(_locator())
    with pytest.raises(ControlViolation):
        guard.open("http://localhost:5001/")

    assert inner.calls == [], "nothing reached the driver while the human held control"
    assert [d.allowed for d in guard.decisions] == [False, False]
    assert all("human holds control" in d.rule for d in guard.decisions)


def test_control_returns_to_automation_after_resume_and_is_recorded():
    control = SessionControl()
    control.cede_to_human("session expired")

    transfer = control.resume_automation("logged back in as OPERATOR-07")

    assert control.controller is Controller.AUTOMATION
    control.require_automation_control("click")  # no longer raises
    assert transfer.to is Controller.AUTOMATION
    assert transfer.reason == "logged back in as OPERATOR-07"
    assert transfer.held_s is not None
    assert [t.to for t in control.history] == [Controller.HUMAN, Controller.AUTOMATION]


def test_ceding_twice_is_refused():
    """Two humans holding one session is the same hazard as a human and an automation."""
    control = SessionControl()
    control.cede_to_human("first")

    with pytest.raises(ControlViolation, match="already been ceded"):
        control.cede_to_human("second")


def test_resuming_when_automation_already_has_control_is_refused():
    with pytest.raises(ControlViolation, match="already holds control"):
        SessionControl().resume_automation("nothing happened")


# ---------------------------------------------------------------------------------------
# The intervention request.
# ---------------------------------------------------------------------------------------


def test_the_request_carries_every_piece_of_context_a_human_needs(tmp_path):
    request = _request(
        reason=StuckReason.UNRECOVERABLE_ERROR,
        step_index=3,
        action="read",
        url="http://localhost:5001/member?member_id=12345",
        page_summary="Member Detail showing $4,200.00",
        screenshot="evidence/test-run/screenshots/failure.png",
        detail="the checkpoint did not hold",
    )

    path = request.save(root=tmp_path)
    written = json.loads(path.read_text())[0]

    assert path.name == "intervention.json"
    for field in (
        "run_id", "capability_id", "goal", "reason", "step_index",
        "action", "url", "page_summary", "screenshot", "detail", "requested_at",
    ):
        assert field in written, f"{field} missing from the request a human has to act on"
    assert written["run_id"] == "test-run", "must link back to the evidence directory"


def test_the_request_carries_no_raw_sensitive_data(tmp_path):
    """A request is a document that leaves the system, so it is masked at construction."""
    request = _request(
        goal="Look up member 12345",
        url="http://localhost:5001/member?member_id=12345",
        page_summary="Available $4,200.00 / Ledger $4,200.00",
        detail="could not read the balance of 12345",
    )

    raw = request.save(root=tmp_path).read_text()

    assert "12345" not in raw
    assert "4,200.00" not in raw
    assert "1***5" in raw and "$4,***.**" in raw


def test_several_interventions_in_one_run_are_all_kept(tmp_path):
    """Overwriting would erase the earlier reason, usually the more interesting one."""
    _request(detail="first").save(root=tmp_path)
    path = _request(detail="second").save(root=tmp_path)

    assert [entry["detail"] for entry in json.loads(path.read_text())] == ["first", "second"]


# ---------------------------------------------------------------------------------------
# The handover, end to end.
# ---------------------------------------------------------------------------------------


def test_a_handover_cedes_asks_resumes_and_records_everything(tmp_path):
    recorder = RunRecorder.start("replay", root=tmp_path)
    console = AutoApproveOperatorConsole(note="dismissed the notice by hand")
    handler = EscalationHandler(console=console, recorder=recorder)
    pages = iter(["before: modal showing", "after: modal gone"])

    response = handler.escalate(_request(run_id=recorder.run_id), snapshot=lambda: next(pages))

    assert response.decision is OperatorDecision.RESUME
    assert handler.control.controller is Controller.AUTOMATION
    assert [t.to for t in handler.control.history] == [Controller.HUMAN, Controller.AUTOMATION]

    events = [json.loads(line) for line in recorder.events_path.read_text().splitlines()]
    kinds = [event["event"] for event in events]
    assert kinds == ["control_ceded", "control_returned"]

    returned = events[-1]
    assert returned["operator_note"] == "dismissed the notice by hand"
    assert returned["human_held_s"] is not None, "how long a human held the session is recorded"
    assert returned["page_before"] == "before: modal showing"
    assert returned["page_after"] == "after: modal gone"
    assert returned["page_changed"] is True, "the before/after diff is the record of their work"


def test_an_aborting_console_returns_abort_and_still_restores_control(tmp_path):
    """Even a refusal ends with control back where it belongs, or the run could never clean up."""
    handler = EscalationHandler(
        console=AutoAbortOperatorConsole(), recorder=RunRecorder.start("replay", root=tmp_path)
    )

    response = handler.escalate(_request(run_id=handler.recorder.run_id))

    assert response.decision is OperatorDecision.ABORT
    assert handler.control.controller is Controller.AUTOMATION


def test_no_console_means_no_escalation():
    """The documented fail-closed default: `--operator none` builds nothing."""
    assert build_console("none") is None


def test_an_unknown_console_kind_is_rejected():
    with pytest.raises(ValueError, match="unknown operator console"):
        build_console("telepathy")


# ---------------------------------------------------------------------------------------
# Seam 2: the guard's confirmation callback becomes a human decision.
# ---------------------------------------------------------------------------------------


def _confirming_guard(console, *, control: SessionControl, recorder: RunRecorder):
    handler = EscalationHandler(console=console, control=control, recorder=recorder)

    def confirm(action: ActionType, target: str, risk: RiskLevel) -> bool:
        request = _request(
            run_id=recorder.run_id,
            reason=StuckReason.RISKY_ACTION_CONFIRMATION,
            action=str(action),
            detail=f"a {risk} {action} on {target}",
        )
        return handler.escalate(request).resumed

    inner = _RecordingSurface()
    guard = GuardedSurface(
        inner,
        _policy(risky_action_mode=RiskyActionMode.CONFIRM),
        control=control,
        confirm=confirm,
    )
    return guard, inner, handler


def test_a_risky_step_proceeds_when_the_operator_resumes(tmp_path):
    """The operator's RESUME *is* the authorization."""
    control = SessionControl()
    recorder = RunRecorder.start("replay", root=tmp_path)
    console = AutoApproveOperatorConsole(note="checked the account; approved")
    guard, inner, handler = _confirming_guard(console, control=control, recorder=recorder)
    guard.declare_step_risk(RiskLevel.RISKY)

    guard.click(_locator())

    assert inner.calls == ["click"], "the action ran, but only after a human said so"
    assert len(console.handled) == 1
    assert console.handled[0].reason is StuckReason.RISKY_ACTION_CONFIRMATION
    assert [t.to for t in control.history] == [Controller.HUMAN, Controller.AUTOMATION]
    assert "control_returned" in recorder.events_path.read_text()


def test_a_risky_step_is_denied_when_the_operator_aborts(tmp_path):
    control = SessionControl()
    guard, inner, _ = _confirming_guard(
        AutoAbortOperatorConsole(),
        control=control,
        recorder=RunRecorder.start("replay", root=tmp_path),
    )
    guard.declare_step_risk(RiskLevel.RISKY)

    with pytest.raises(PolicyViolation, match="not confirmed"):
        guard.click(_locator())

    assert inner.calls == []
    assert control.controller is Controller.AUTOMATION, "control comes back even on a refusal"


def test_confirm_mode_still_fails_closed_with_no_console():
    """Attaching an operator adds a path; it does not remove the safe default."""
    inner = _RecordingSurface()
    guard = GuardedSurface(inner, _policy(risky_action_mode=RiskyActionMode.CONFIRM))
    guard.declare_step_risk(RiskLevel.RISKY)

    with pytest.raises(PolicyViolation, match="no confirmation callback"):
        guard.click(_locator())

    assert inner.calls == []


# ---------------------------------------------------------------------------------------
# Seam 3: a replay hard failure is referred to a human, and retried once on resume.
# ---------------------------------------------------------------------------------------


@pytest.mark.integration
def test_a_replay_hard_failure_retries_the_step_when_the_operator_resumes(
    surface, mock_app, tmp_path
):
    """The human fixes the page; the engine retries the failed step exactly once and continues.

    The step here fails because the element is not on the page yet -- the console's 'human'
    navigates to the member detail page while holding control, which is precisely the real
    behaviour being modelled: the automation could not proceed, a person acted on the same
    live session, and the flow resumed.
    """
    from src.replay.engine import ReplayEngine
    from src.replay.result import ReplayStatus
    from tests.test_replay import _artifact

    class NavigatingConsole:
        """A 'human' who acts on the same session, then hands control back."""

        def __init__(self) -> None:
            self.handled: list[InterventionRequest] = []

        def handle(self, request):
            self.handled.append(request)
            # Acts through the raw surface, exactly as a person at the keyboard would -- the
            # guard is refusing the automation right now, not them.
            surface.open(f"{mock_app}/member?member_id=12345")
            from src.escalation.operator import OperatorResponse

            return OperatorResponse(
                decision=OperatorDecision.RESUME, note="navigated to the member page by hand"
            )

    artifact = _artifact(mock_app)
    # Drop the two steps that would have got us there, so step 0 fails on the wrong page.
    broken = artifact.model_copy(
        update={
            "steps": [
                artifact.steps[3].model_copy(update={"index": 0}),
            ]
        }
    )
    console = NavigatingConsole()
    recorder = RunRecorder.start("replay", root=tmp_path)
    engine = ReplayEngine(
        recorder=recorder,
        escalation=EscalationHandler(console=console, recorder=recorder),
    )

    surface.open(f"{mock_app}/")  # start somewhere the balance is not present
    result = engine.replay(broken, {"member_id": "12345"}, surface)

    assert len(console.handled) == 1, "the failure was referred to a human exactly once"
    assert console.handled[0].reason is StuckReason.UNRECOVERABLE_ERROR
    assert result.status is ReplayStatus.SUCCESS, result.failure
    assert result.outputs == {"savings_balance": "$4,200.00"}
    assert "control_returned" in recorder.events_path.read_text()


@pytest.mark.integration
def test_a_replay_hard_failure_is_returned_when_the_operator_aborts(surface, mock_app, tmp_path):
    """Abort restores the previous behaviour exactly: the hard failure is reported."""
    from src.replay.engine import ReplayEngine
    from src.replay.result import ReplayStatus
    from tests.test_replay import _artifact

    artifact = _artifact(mock_app)
    broken = artifact.model_copy(
        update={"steps": [artifact.steps[3].model_copy(update={"index": 0})]}
    )
    console = AutoAbortOperatorConsole()
    recorder = RunRecorder.start("replay", root=tmp_path)
    engine = ReplayEngine(
        recorder=recorder,
        escalation=EscalationHandler(console=console, recorder=recorder),
    )

    surface.open(f"{mock_app}/")
    result = engine.replay(broken, {"member_id": "12345"}, surface)

    assert result.status is ReplayStatus.HARD_FAILURE
    assert result.failure is not None
    assert len(console.handled) == 1


@pytest.mark.integration
def test_a_replay_without_a_console_does_not_escalate(surface, mock_app, tmp_path):
    """No operator attached means the engine behaves exactly as it did before escalation."""
    from src.replay.engine import ReplayEngine
    from src.replay.result import ReplayStatus
    from tests.test_replay import _artifact

    artifact = _artifact(mock_app)
    broken = artifact.model_copy(
        update={"steps": [artifact.steps[3].model_copy(update={"index": 0})]}
    )
    engine = ReplayEngine(recorder=RunRecorder.start("replay", root=tmp_path))

    surface.open(f"{mock_app}/")
    result = engine.replay(broken, {"member_id": "12345"}, surface)

    assert result.status is ReplayStatus.HARD_FAILURE


def test_the_request_is_written_into_the_recorders_own_evidence_directory(tmp_path):
    """The handover and the request explaining it must land in the same directory.

    Found the hard way: the handler saved to the default `evidence/` root regardless of the
    recorder, so test runs with a temporary recorder quietly deposited orphan directories in the
    committed evidence tree.
    """
    recorder = RunRecorder.start("replay", root=tmp_path)
    handler = EscalationHandler(console=AutoApproveOperatorConsole(), recorder=recorder)

    handler.escalate(_request(run_id=recorder.run_id))

    assert (tmp_path / recorder.run_id / "intervention.json").exists()
    assert not (tmp_path / recorder.run_id / "intervention.json").is_dir()
