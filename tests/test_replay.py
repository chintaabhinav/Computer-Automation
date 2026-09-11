"""Tests for the deterministic replay engine, against the live mock app.

The headline assertion is the boundary test below: replay must not be able to reach a model.
Everything else here is about the result contract, because that is what callers program
against -- a legitimate business answer and a broken application must never arrive in the same
shape.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime

import pytest

from src.artifact.schema import (
    ActionType,
    Artifact,
    Checkpoint,
    CheckpointKind,
    ErrorBucket,
    ErrorRule,
    InputParam,
    Locator,
    LocatorBy,
    LocatorRule,
    OutputContract,
    OutputField,
    ParamType,
    Provenance,
    RecoveryAction,
    Step,
    SurfaceType,
    Target,
)
from src.evidence import RunRecorder
from src.surface.base import ElementHandle, Observation, Surface
from src.replay.engine import MissingInputError, ReplayEngine
from src.replay.result import ReplayStatus

REPLAY_PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "src" / "replay"

BALANCE_XPATH = (
    "//td[normalize-space()='Savings Balance']"
    "/following-sibling::td[1]//tr[td[normalize-space()='Available']]/td[2]"
)


# ---------------------------------------------------------------------------------------
# The boundary. Not a convention -- a test.
# ---------------------------------------------------------------------------------------


def test_replay_cannot_reach_a_model():
    """No module under src/replay/ may import the agent or any LLM SDK.

    This is the guarantee an operator actually cares about: a replay cannot improvise, cannot
    call out to a model, and cannot cost money. A guarantee that depends on nobody adding an
    import is not a guarantee, so it is asserted here.
    """
    modules = sorted(REPLAY_PACKAGE.glob("*.py"))
    assert modules, "expected to find replay modules to check"

    forbidden = ("src.agent", "anthropic", "openai")
    for module in modules:
        # Parsed, not grepped: the module docstrings discuss this very boundary, and a text
        # search would flag the prose that documents the rule while missing an aliased import.
        tree = ast.parse(module.read_text())
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")

        for name in imported:
            root = name.split(".")[0]
            assert not any(
                name == bad or name.startswith(f"{bad}.") or root == bad for bad in forbidden
            ), f"{module.name} imports {name!r}, which replay may never depend on"


# ---------------------------------------------------------------------------------------
# Fixtures: a hand-built artifact, so these tests do not depend on a discovery run.
# ---------------------------------------------------------------------------------------


def _locator(by: LocatorBy, value: str, *fallbacks: LocatorRule) -> Locator:
    return Locator(
        primary=LocatorRule(by=by, value=value),
        fallbacks=list(fallbacks),
        rationale="test locator",
    )


def _artifact(entry_url: str, success: Checkpoint | None = None) -> Artifact:
    """The member-balance capability, written by hand.

    Built here rather than loaded from `artifacts/` so a failing discovery run can never break
    the replay suite: these tests are about the engine, not about what a model produced.
    """
    return Artifact(
        capability_id="lookup_member_balance",
        description="Look up a member and read their savings balance.",
        target=Target(
            app_id="nmcu-back-office", entry_url=entry_url, surface_type=SurfaceType.LEGACY_WEB
        ),
        inputs=[
            InputParam(
                name="member_id",
                type=ParamType.STRING,
                description="The member identifier to look up.",
            )
        ],
        outputs=OutputContract(
            outcome_values=["success", "no_such_member"],
            fields=[
                OutputField(
                    name="savings_balance",
                    type=ParamType.STRING,
                    description="Available savings balance as displayed.",
                )
            ],
        ),
        steps=[
            Step(
                index=0,
                action=ActionType.NAVIGATE,
                value=entry_url,
                checkpoint=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Member Inquiry"),
            ),
            Step(
                index=1,
                action=ActionType.TYPE,
                target=_locator(
                    LocatorBy.LABEL,
                    "Member ID",
                    LocatorRule(by=LocatorBy.CSS, value="form input[type=text]"),
                ),
                value="{{member_id}}",
            ),
            Step(
                index=2,
                action=ActionType.CLICK,
                target=_locator(LocatorBy.TEXT, "Search"),
                checkpoint=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Member Detail"),
            ),
            Step(
                index=3,
                action=ActionType.READ,
                target=_locator(LocatorBy.STRUCTURAL, BALANCE_XPATH),
                extract="savings_balance",
            ),
        ],
        success=success
        or Checkpoint(
            kind=CheckpointKind.ALL_OF,
            children=[
                Checkpoint(kind=CheckpointKind.URL_MATCHES, value="/member"),
                Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Savings Balance"),
            ],
        ),
        provenance=Provenance(
            goal="Look up the member given as input and read their savings balance.",
            discovered_by="test",
            discovery_run_id="test-run",
            created_at=datetime(2026, 9, 11, tzinfo=UTC),
        ),
    )


@pytest.fixture
def engine(tmp_path) -> ReplayEngine:
    """An engine whose evidence goes to a temporary directory, not the committed one."""
    return ReplayEngine(recorder=RunRecorder.start("replay", root=tmp_path))


# ---------------------------------------------------------------------------------------
# The clean path.
# ---------------------------------------------------------------------------------------


@pytest.mark.integration
def test_a_clean_replay_succeeds_and_extracts_the_balance(engine, surface, mock_app):
    """The production path: four steps, no model, the declared output captured."""
    result = engine.replay(_artifact(mock_app), {"member_id": "12345"}, surface)

    assert result.status is ReplayStatus.SUCCESS
    assert result.outcome == "success"
    assert result.outputs == {"savings_balance": "$4,200.00"}
    assert result.steps_executed == 4
    assert result.failure is None
    assert result.recoveries == []
    assert result.is_failure is False


@pytest.mark.integration
def test_the_same_artifact_replays_for_a_different_member(engine, surface, mock_app):
    """The whole point of parameterization: one recording, many inputs.

    Member 67890 was never involved in recording this flow, and the artifact contains no trace
    of any particular member -- only `{{member_id}}`.
    """
    result = engine.replay(_artifact(mock_app), {"member_id": "67890"}, surface)

    assert result.status is ReplayStatus.SUCCESS
    assert result.outputs == {"savings_balance": "$18,750.35"}


@pytest.mark.integration
def test_replay_writes_evidence_without_raw_values(engine, surface, mock_app):
    """Replay evidence is held to the same redaction rule as discovery evidence."""
    engine.replay(_artifact(mock_app), {"member_id": "12345"}, surface)

    written = engine.recorder.events_path.read_text()
    assert "$4,200.00" not in written, "the extracted balance must not reach disk raw"
    assert "12345" not in written
    assert '"event": "step"' in written
    summary = engine.recorder.summary_path
    assert not summary.exists() or "success" in summary.read_text()


# ---------------------------------------------------------------------------------------
# Rejected requests.
# ---------------------------------------------------------------------------------------


def test_a_missing_required_input_fails_fast_before_any_browser_opens(engine):
    """A malformed request is not a replay outcome, so it raises instead of returning a result.

    Note there is no surface fixture here: the check happens before anything is opened, which
    is the point -- discovering this halfway through would mean having already clicked things
    in a real back office.
    """
    with pytest.raises(MissingInputError, match="missing required input"):
        engine.replay(_artifact("http://127.0.0.1:5001"), {}, surface=None)


def test_the_error_names_the_inputs_the_capability_declares(engine):
    """The message has to be actionable without reading the artifact."""
    with pytest.raises(MissingInputError, match=r"declares \['member_id'\]"):
        engine.replay(_artifact("http://127.0.0.1:5001"), {"wrong_name": "x"}, surface=None)


# ---------------------------------------------------------------------------------------
# Hard failure.
# ---------------------------------------------------------------------------------------


@pytest.mark.integration
def test_a_success_checkpoint_that_cannot_hold_is_a_hard_failure(engine, surface, mock_app):
    """Every step can pass while the capability still fails to achieve its goal."""
    artifact = _artifact(
        mock_app,
        success=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Wire Transfer Approved"),
    )

    result = engine.replay(artifact, {"member_id": "12345"}, surface)

    assert result.status is ReplayStatus.HARD_FAILURE
    assert result.is_failure is True
    assert result.outcome is None, "a hard failure has no answer to report"

    failure = result.failure
    assert failure is not None
    assert failure.action == "success_checkpoint"
    assert failure.expected == "text_equals='Wire Transfer Approved'"
    assert "Member Detail" in failure.observed or "member" in failure.observed
    assert "did not hold" in failure.message

    # The outputs read before the failure are still returned; they were legitimately read.
    assert result.outputs == {"savings_balance": "$4,200.00"}


@pytest.mark.integration
def test_a_failure_detail_names_the_step_and_its_locator(engine, surface, mock_app):
    """A failure mid-flow must locate itself precisely enough to fix without re-running."""
    artifact = _artifact(mock_app)
    broken = artifact.steps[2].model_copy(
        update={"target": _locator(LocatorBy.TEXT, "Wire Transfer Approval")}
    )
    artifact = artifact.model_copy(
        update={"steps": [*artifact.steps[:2], broken, *artifact.steps[3:]]}
    )

    result = engine.replay(artifact, {"member_id": "12345"}, surface)

    assert result.status is ReplayStatus.HARD_FAILURE
    assert result.failure.step_index == 2
    assert result.failure.action == "click"
    assert "no element matched" in result.failure.message
    assert result.steps_executed == 2, "the two steps that did work are still counted"


@pytest.mark.integration
def test_the_failure_observation_is_redacted(engine, surface, mock_app):
    """A failure report is the artifact most likely to be pasted into a ticket."""
    artifact = _artifact(
        mock_app, success=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Nothing Here")
    )

    result = engine.replay(artifact, {"member_id": "12345"}, surface)

    assert "4,200.00" not in result.failure.observed
    assert "12345" not in result.failure.observed


# ---------------------------------------------------------------------------------------
# Drift.
# ---------------------------------------------------------------------------------------


@pytest.mark.integration
def test_a_fallback_locator_carries_the_step_and_is_reported(engine, surface, mock_app):
    """Replay must not hide a recording that is limping: the step passes, the drift is logged."""
    artifact = _artifact(mock_app)
    limping = artifact.steps[1].model_copy(
        update={
            "target": Locator(
                primary=LocatorRule(by=LocatorBy.LABEL, value="Membership Number"),
                fallbacks=[LocatorRule(by=LocatorBy.CSS, value="form input[type=text]")],
                rationale="the label no longer exists; the sole text input is the backstop",
            )
        }
    )
    artifact = artifact.model_copy(
        update={"steps": [artifact.steps[0], limping, *artifact.steps[2:]]}
    )

    result = engine.replay(artifact, {"member_id": "12345"}, surface)

    assert result.status is ReplayStatus.SUCCESS, "a fallback should still complete the flow"
    events = engine.recorder.events_path.read_text()
    assert '"fallback_used": true' in events, "the drift must appear in evidence"


# ---------------------------------------------------------------------------------------
# Recovery.
# ---------------------------------------------------------------------------------------


@pytest.mark.integration
def test_a_blocked_click_surfaces_as_a_surface_error_not_a_driver_exception(mock_app):
    """The boundary has to hold when things go wrong, or the taxonomy cannot classify them.

    A driver timeout once escaped WebSurface untouched, sailed past the engine's
    `except SurfaceError`, and crashed the CLI with a Playwright traceback -- turning a blocked
    click, which the error taxonomy has a bucket for, into an unhandled failure.
    """
    from src.surface.base import SurfaceError
    from src.surface.web import WebSurface

    web_surface = WebSurface(headless=True, navigation_timeout_ms=2_000)
    try:
        web_surface.open(f"{mock_app}/member?member_id=12345&inject=popup")
        blocked = _locator(LocatorBy.TEXT, "Open Sub-Account")

        assert web_surface.find(blocked) is not None, "the button is present, just covered"
        with pytest.raises(SurfaceError, match="click failed"):
            web_surface.click(blocked)
    finally:
        web_surface.close()


@pytest.mark.integration
def test_dismiss_recovery_uses_the_recorded_recovery_target(mock_app, tmp_path):
    """The modal blocks a click; the recorded recovery_target clears it and the step retries.

    This is the case the caption heuristic existed to guess at. With a `recovery_target` the
    engine knows exactly which control closes the obstruction.
    """
    from src.surface.web import WebSurface

    artifact = Artifact(
        capability_id="open_sub_account",
        description="Open a savings sub-account for a member.",
        target=Target(app_id="nmcu-back-office", entry_url=mock_app),
        outputs=OutputContract(outcome_values=["success"]),
        steps=[
            Step(
                index=0,
                action=ActionType.NAVIGATE,
                value=f"{mock_app}/member?member_id=12345&inject=popup",
                checkpoint=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Member Detail"),
            ),
            Step(
                index=1,
                action=ActionType.CLICK,
                target=_locator(LocatorBy.TEXT, "Open Sub-Account"),
                checkpoint=Checkpoint(kind=CheckpointKind.URL_MATCHES, value="/confirm"),
                on_error=[
                    ErrorRule(
                        detect=Checkpoint(
                            kind=CheckpointKind.TEXT_EQUALS, value="System Notice"
                        ),
                        bucket=ErrorBucket.RECOVERABLE,
                        action=RecoveryAction.DISMISS,
                        recovery_target=Locator(
                            primary=LocatorRule(by=LocatorBy.TEXT, value="Continue"),
                            rationale="the interstitial's only button",
                        ),
                    )
                ],
            ),
        ],
        success=Checkpoint(
            kind=CheckpointKind.TEXT_EQUALS, value="Sub-account opened successfully."
        ),
        provenance=Provenance(
            goal="Open a sub-account.",
            discovered_by="test",
            discovery_run_id="test-run",
            created_at=datetime(2026, 9, 11, tzinfo=UTC),
        ),
    )

    engine = ReplayEngine(recorder=RunRecorder.start("replay", root=tmp_path))
    web_surface = WebSurface(headless=True, navigation_timeout_ms=3_000)
    try:
        result = engine.replay(artifact, {}, web_surface)
    finally:
        web_surface.close()

    assert result.status is ReplayStatus.SUCCESS, result.failure
    assert len(result.recoveries) == 1
    recovery = result.recoveries[0]
    assert recovery.step_index == 1
    assert recovery.action is RecoveryAction.DISMISS
    assert recovery.detected == "text_equals='System Notice'"
    assert recovery.attempt == 1


def test_wait_recovery_retries_the_step_and_records_the_event(tmp_path):
    """A condition that is not true yet, then is: recover, retry, succeed.

    Driven by a scripted surface rather than the browser. The browser path is covered by the
    integration tests above; what needs testing here is the engine's own bookkeeping -- that a
    recovery is recorded, that the step is retried rather than skipped, and that the bounds are
    arithmetic rather than aspiration.
    """
    surface = _ScriptedSurface(checkpoint_results=[False, True])
    artifact = _one_read_artifact(recoverable=True)
    engine = ReplayEngine(recorder=RunRecorder.start("replay", root=tmp_path))

    result = engine.replay(artifact, {}, surface)

    assert result.status is ReplayStatus.SUCCESS
    assert [r.action for r in result.recoveries] == [RecoveryAction.WAIT]
    assert surface.reads == 2, "the step is retried, not skipped"
    assert result.outputs == {"balance": "read-value"}


def test_recovery_is_bounded_per_step(tmp_path):
    """A condition that never clears is not transient, and must stop rather than loop."""
    surface = _ScriptedSurface(checkpoint_results=[False] * 10)
    artifact = _one_read_artifact(recoverable=True)
    engine = ReplayEngine(recorder=RunRecorder.start("replay", root=tmp_path))

    result = engine.replay(artifact, {}, surface)

    assert result.status is ReplayStatus.HARD_FAILURE
    assert len(result.recoveries) == 2, "bounded at two attempts for one step"
    assert "recovery limit reached" in result.failure.message


def test_an_unmatched_failure_is_a_hard_failure_with_detail(tmp_path):
    """No rule matched, so the engine has nothing recorded to justify continuing."""
    surface = _ScriptedSurface(checkpoint_results=[False] * 5)
    artifact = _one_read_artifact(recoverable=False)
    engine = ReplayEngine(recorder=RunRecorder.start("replay", root=tmp_path))

    result = engine.replay(artifact, {}, surface)

    assert result.status is ReplayStatus.HARD_FAILURE
    assert result.recoveries == []
    assert result.failure.step_index == 0
    assert "did not hold" in result.failure.message


# ---------------------------------------------------------------------------------------
# A scripted surface, for testing the engine's bookkeeping rather than the browser.
# ---------------------------------------------------------------------------------------


class _ScriptedSurface(Surface):
    """A Surface whose checkpoints answer from a script.

    Deliberately not a mock of Playwright -- it implements the same ABC the real surface does,
    which is what the boundary is for: the engine cannot tell the difference, so its recovery
    arithmetic can be tested in milliseconds and without a browser.
    """

    def __init__(self, checkpoint_results: list[bool]) -> None:
        self.checkpoint_results = list(checkpoint_results)
        self.reads = 0
        self.clicks = 0

    def _next(self) -> bool:
        return self.checkpoint_results.pop(0) if self.checkpoint_results else True

    def open(self, url: str) -> None: ...

    def observe(self):
        return Observation(url="scripted://page", title="scripted", elements=[])

    def find(self, locator):
        return ElementHandle(ref="e1", rule=locator.primary, candidate_index=0, native=None)

    def click(self, locator) -> None:
        self.clicks += 1

    def type(self, locator, text: str) -> None: ...

    def read(self, locator) -> str:
        self.reads += 1
        return "read-value"

    def check(self, checkpoint) -> bool:
        # The recovery detector must match, or nothing would be recoverable; the step's own
        # checkpoint answers from the script.
        if checkpoint.value == "still-loading":
            return True
        return self._next()

    def wait_for(self, checkpoint, timeout_s: float, poll_ms: int) -> bool:
        return self.check(checkpoint)

    def screenshot(self, path) -> None: ...

    def current_url(self) -> str:
        return "scripted://page"

    def close(self) -> None: ...


def _one_read_artifact(*, recoverable: bool) -> Artifact:
    """A single READ step whose checkpoint may need a retry. READ is idempotent, so retrying it
    is safe -- which is what makes it the honest shape for exercising recovery."""
    rules = (
        [
            ErrorRule(
                detect=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="still-loading"),
                bucket=ErrorBucket.RECOVERABLE,
                action=RecoveryAction.WAIT,
            )
        ]
        if recoverable
        else []
    )
    return Artifact(
        capability_id="read_one_value",
        description="Read a single value.",
        target=Target(app_id="scripted", entry_url="scripted://page"),
        outputs=OutputContract(
            outcome_values=["success"],
            fields=[
                OutputField(name="balance", type=ParamType.STRING, description="A value.")
            ],
        ),
        steps=[
            Step(
                index=0,
                action=ActionType.READ,
                target=_locator(LocatorBy.TEXT, "Available"),
                extract="balance",
                checkpoint=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Loaded"),
                on_error=rules,
            )
        ],
        success=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Loaded"),
        provenance=Provenance(
            goal="Read one value.",
            discovered_by="test",
            discovery_run_id="test-run",
            created_at=datetime(2026, 9, 11, tzinfo=UTC),
        ),
    )
