"""Tests for the discovery loop, driven by a scripted stub against the real mock app.

The stub supplies the decisions; everything else is real -- a real browser, a real legacy page,
real locator resolution, real checkpoint verification. That split is the point of building
stub-first: it makes the parts of discovery that are genuinely hard (executing an action,
verifying it happened, turning a verified action into a valid artifact) testable and free, and
leaves only the model's judgment untested here.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.agent.llm import (
    ControlAction,
    Decision,
    DecisionParseError,
    StubLLMClient,
    parse_decision,
)
from src.agent.loop import DiscoveryAgent, StopReason
from src.artifact.schema import (
    ActionType,
    Artifact,
    Checkpoint,
    CheckpointKind,
    Locator,
    LocatorBy,
    LocatorRule,
    RiskLevel,
    SurfaceType,
    Target,
)
from src.artifact.store import load_artifact, save_artifact

GOAL = "Look up a member by ID and read their savings balance"
MEMBER_ID = "12345"

BALANCE_XPATH = (
    "//td[normalize-space()='Savings Balance']"
    "/following-sibling::td[1]//tr[td[normalize-space()='Available']]/td[2]"
)


def _locator(by: LocatorBy, value: str, rationale: str, *fallbacks: LocatorRule) -> Locator:
    return Locator(
        primary=LocatorRule(by=by, value=value),
        fallbacks=list(fallbacks),
        rationale=rationale,
    )


def _script(base_url: str) -> list[Decision]:
    """The decision sequence a competent model would produce for the mock app flow.

    Mirrors the real path -- navigate, type the member id, search, read the nested-table
    balance, done -- so the assertions below are about a flow that actually works, not a
    contrived one.
    """
    return [
        Decision(
            thought="Start at the member search screen.",
            action=ActionType.NAVIGATE,
            value=base_url,
            checkpoint=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Member Inquiry"),
        ),
        Decision(
            thought="The search field is identified by its visible label.",
            action=ActionType.TYPE,
            target=_locator(
                LocatorBy.LABEL,
                "Member ID",
                "The field has a visible label, which is user-facing contract on this screen.",
                LocatorRule(by=LocatorBy.CSS, value="form input[type=text]"),
            ),
            value=MEMBER_ID,
            checkpoint=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Member Inquiry"),
        ),
        Decision(
            thought="Submit the search.",
            action=ActionType.CLICK,
            target=_locator(
                LocatorBy.TEXT, "Search", "The submit button is identified by its caption."
            ),
            checkpoint=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Member Detail"),
        ),
        Decision(
            thought="The balance sits in a nested table with no label of its own.",
            action=ActionType.READ,
            target=_locator(
                LocatorBy.STRUCTURAL,
                BALANCE_XPATH,
                "Reached through the 'Savings Balance' row heading, then the nested "
                "'Available' row; the value has no label to target directly.",
            ),
            extract="savings_balance",
        ),
        Decision(
            thought="The detail page is showing the member and their balance.",
            action=ControlAction.DONE,
            checkpoint=Checkpoint(
                kind=CheckpointKind.ALL_OF,
                children=[
                    Checkpoint(kind=CheckpointKind.URL_MATCHES, value="/member"),
                    Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Jane Doe"),
                ],
            ),
        ),
    ]


@pytest.fixture
def target(mock_app: str) -> Target:
    return Target(
        app_id="nmcu-back-office",
        entry_url=mock_app,
        surface_type=SurfaceType.LEGACY_WEB,
        tenant_id="branch-014",
        app_version="4.2.1",
    )


# ---------------------------------------------------------------------------------------
# Decision parsing -- no browser required.
# ---------------------------------------------------------------------------------------


def test_parses_a_bare_json_decision():
    decision = parse_decision('{"thought": "go", "action": "navigate", "value": "http://x/"}')

    assert decision.action is ActionType.NAVIGATE
    assert decision.value == "http://x/"
    assert decision.risk is RiskLevel.SAFE, "risk must default to safe, never be assumed"


def test_strips_markdown_fences_and_surrounding_prose():
    """Models package JSON in fences and commentary; that is not worth failing a run over."""
    raw = 'Sure!\n```json\n{"thought": "t", "action": "stuck"}\n```\nHope that helps.'

    assert parse_decision(raw).action is ControlAction.STUCK


def test_rejects_output_that_is_not_json():
    with pytest.raises(DecisionParseError, match="no JSON object"):
        parse_decision("I think you should click the Search button.")


def test_rejects_json_that_violates_the_decision_schema():
    """Schema violations are errors, not something to guess at."""
    with pytest.raises(DecisionParseError, match="did not match the Decision schema"):
        parse_decision('{"thought": "t", "action": "teleport"}')


def test_stub_signals_completion_once_its_script_runs_out():
    stub = StubLLMClient([Decision(thought="one", action=ControlAction.STUCK)])

    assert stub.decide("p").action is ControlAction.STUCK
    assert stub.decide("p").action is ControlAction.DONE


# ---------------------------------------------------------------------------------------
# The full loop against the live app.
# ---------------------------------------------------------------------------------------


@pytest.mark.integration
def test_discovers_and_emits_a_valid_artifact(surface, mock_app: str, target: Target, tmp_path):
    """The end-to-end case: a scripted flow becomes a validated, reusable recording."""
    agent = DiscoveryAgent(
        surface, StubLLMClient(_script(mock_app)), model_id="stub-script", run_id="disc-test-1"
    )

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID})

    assert result.stop_reason is StopReason.SUCCESS, result.note
    artifact = result.artifact
    assert artifact is not None

    # Validates by construction, and again on a round-trip through disk.
    path = save_artifact(artifact, tmp_path / "artifact.json")
    assert load_artifact(path) == artifact

    assert artifact.steps and [step.index for step in artifact.steps] == list(
        range(len(artifact.steps))
    )
    assert [step.action for step in artifact.steps] == [
        ActionType.NAVIGATE,
        ActionType.TYPE,
        ActionType.CLICK,
        ActionType.READ,
    ]


@pytest.mark.integration
def test_typed_input_is_recorded_as_a_parameter_not_a_literal(surface, mock_app, target):
    """The substitution that makes a recording reusable instead of a transcript of one session."""
    agent = DiscoveryAgent(surface, StubLLMClient(_script(mock_app)))

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID})

    type_step = next(s for s in result.artifact.steps if s.action is ActionType.TYPE)
    assert type_step.value == "{{member_id}}"
    assert MEMBER_ID not in (type_step.value or ""), "the literal must not survive in the recording"
    assert [param.name for param in result.artifact.inputs] == ["member_id"]


@pytest.mark.integration
def test_extraction_populates_the_declared_output_contract(surface, mock_app, target):
    """A READ is only useful if the artifact declares somewhere for its value to go."""
    agent = DiscoveryAgent(surface, StubLLMClient(_script(mock_app)))

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID})

    assert [field.name for field in result.artifact.outputs.fields] == ["savings_balance"]
    read_step = next(s for s in result.artifact.steps if s.action is ActionType.READ)
    assert read_step.extract == "savings_balance"
    assert "success" in result.artifact.outputs.outcome_values


@pytest.mark.integration
def test_provenance_is_filled_in(surface, mock_app, target):
    """An artifact executed unsupervised has to carry its own audit trail."""
    agent = DiscoveryAgent(
        surface, StubLLMClient(_script(mock_app)), model_id="stub-script", run_id="disc-test-2"
    )

    provenance = agent.run(GOAL, target, {"member_id": MEMBER_ID}).artifact.provenance

    assert provenance.goal == GOAL
    assert provenance.discovered_by == "stub-script"
    assert provenance.discovery_run_id == "disc-test-2"
    assert provenance.created_at.tzinfo is not None, "timestamps must be unambiguous"


@pytest.mark.integration
def test_the_recording_promotes_the_locator_that_actually_resolved(surface, mock_app, target):
    """When the primary fails, the recording should lead with what worked -- keeping the rest."""
    script = _script(mock_app)
    script[1] = script[1].model_copy(
        update={
            "target": _locator(
                LocatorBy.LABEL,
                "Membership Number",  # never matches this page
                "label preferred; the sole text input is the backstop",
                LocatorRule(by=LocatorBy.CSS, value="form input[type=text]"),
            )
        }
    )
    agent = DiscoveryAgent(surface, StubLLMClient(script))

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID})

    type_step = next(s for s in result.artifact.steps if s.action is ActionType.TYPE)
    assert type_step.target.primary.by is LocatorBy.CSS, "the winner becomes the primary"
    assert any(rule.value == "Membership Number" for rule in type_step.target.fallbacks)
    assert "backstop" in type_step.target.rationale, "the model's rationale is carried through"
    assert surface.fallback_events, "and the drift is still reported"


@pytest.mark.integration
def test_model_reasoning_is_evidence_not_artifact_content(surface, mock_app, target, tmp_path):
    """The recording must not depend on -- or leak -- the words the model used."""
    agent = DiscoveryAgent(surface, StubLLMClient(_script(mock_app)))

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID})

    serialized = save_artifact(result.artifact, tmp_path / "a.json").read_text()
    assert "Start at the member search screen." not in serialized
    assert any("Start at the member search screen." == e.thought for e in result.transcript)
    assert all(entry.url for entry in result.transcript)


@pytest.mark.integration
def test_unverified_actions_are_not_recorded(surface, mock_app, target):
    """A step whose checkpoint fails is fed back to the model, never written into the artifact."""
    script = _script(mock_app)
    # Claim something false will be true after typing; the action runs, the check fails.
    script[1] = script[1].model_copy(
        update={"checkpoint": Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Wire Transfer")}
    )
    agent = DiscoveryAgent(surface, StubLLMClient(script), checkpoint_timeout_s=1.0)

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID}, max_steps=6)

    # The fill did happen -- so the rest of the flow still completes -- but the step the loop
    # could not verify is absent from the recording, and the reason is in the evidence.
    assert result.stop_reason is StopReason.SUCCESS, result.note
    assert [step.action for step in result.artifact.steps] == [
        ActionType.NAVIGATE,
        ActionType.CLICK,
        ActionType.READ,
    ]
    assert any("did not hold" in entry.outcome for entry in result.transcript)


# ---------------------------------------------------------------------------------------
# Stopping conditions.
# ---------------------------------------------------------------------------------------


@pytest.mark.integration
def test_stops_at_max_steps(surface, mock_app, target):
    """A budget the flow cannot fit in ends the run without an artifact."""
    agent = DiscoveryAgent(surface, StubLLMClient(_script(mock_app)))

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID}, max_steps=2)

    assert result.stop_reason is StopReason.MAX_STEPS
    assert result.artifact is None, "a partial flow is not a capability"
    assert result.steps_recorded == 2
    assert "2-step budget" in result.note


@pytest.mark.integration
def test_stops_on_wall_clock_timeout(surface, mock_app, target):
    """Wall clock is a separate budget: a flow can be short in steps but hang on a slow target."""

    class SlowStub(StubLLMClient):
        def decide(self, prompt: str) -> Decision:
            time.sleep(0.2)
            return super().decide(prompt)

    agent = DiscoveryAgent(surface, SlowStub(_script(mock_app)))

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID}, max_steps=15, timeout_s=0.4)

    assert result.stop_reason is StopReason.TIMEOUT
    assert result.artifact is None
    assert "wall-clock" in result.note


@pytest.mark.integration
def test_stops_when_the_model_reports_stuck(surface, mock_app, target):
    """The dead end that escalation will hook into."""
    script = [
        _script(mock_app)[0],
        Decision(thought="I cannot find a way forward.", action=ControlAction.STUCK),
    ]
    agent = DiscoveryAgent(surface, StubLLMClient(script))

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID})

    assert result.stop_reason is StopReason.DEAD_END
    assert result.artifact is None
    assert "stuck" in result.note
    assert result.transcript[-1].thought == "I cannot find a way forward."


@pytest.mark.integration
def test_stops_after_repeated_resolution_failures(surface, mock_app, target):
    """Unresolvable locators are fed back once, then bounded -- not retried forever."""
    unresolvable = Decision(
        thought="try this",
        action=ActionType.CLICK,
        target=_locator(LocatorBy.TEXT, "Wire Transfer Approval", "guessing"),
    )
    agent = DiscoveryAgent(
        surface, StubLLMClient([_script(mock_app)[0], unresolvable, unresolvable])
    )

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID})

    assert result.stop_reason is StopReason.DEAD_END
    assert "consecutive failed attempts" in result.note
    assert result.steps_recorded == 1, "the successful navigate is still recorded"


@pytest.mark.integration
def test_a_single_resolution_failure_is_fed_back_not_fatal(surface, mock_app, target):
    """One bad guess must not end a run; the failure becomes context for the next decision."""
    script = _script(mock_app)
    bad_guess = Decision(
        thought="maybe this",
        action=ActionType.CLICK,
        target=_locator(LocatorBy.TEXT, "Wire Transfer Approval", "guessing"),
    )
    agent = DiscoveryAgent(surface, StubLLMClient([script[0], bad_guess, *script[1:]]))

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID})

    assert result.stop_reason is StopReason.SUCCESS, result.note
    assert any("no element matched" in entry.outcome for entry in result.transcript)
    assert not any(entry.recorded and entry.outcome != "ok" for entry in result.transcript)


@pytest.mark.integration
def test_done_without_a_holding_checkpoint_is_not_success(surface, mock_app, target):
    """Claiming the goal is met does not make it so; the surface has the final say."""
    script = _script(mock_app)
    script[-1] = script[-1].model_copy(
        update={"checkpoint": Checkpoint(kind=CheckpointKind.URL_MATCHES, value="/confirm")}
    )
    agent = DiscoveryAgent(surface, StubLLMClient(script), checkpoint_timeout_s=1.0)

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID}, max_steps=8)

    assert result.stop_reason is not StopReason.SUCCESS
    assert result.artifact is None


@pytest.mark.integration
def test_the_emitted_artifact_is_a_real_reusable_recording(surface, mock_app, target, tmp_path):
    """The whole point, checked end to end: the file on disk is a capability someone can review."""
    agent = DiscoveryAgent(surface, StubLLMClient(_script(mock_app)), model_id="stub-script")

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID})
    path: Path = save_artifact(result.artifact, tmp_path / "capability.json")
    reloaded: Artifact = load_artifact(path)

    assert reloaded.capability_id
    assert reloaded.description == GOAL
    assert reloaded.target.entry_url == mock_app
    assert reloaded.schema_version == "1.0" and reloaded.version == "1.0"
    assert "{{member_id}}" in path.read_text()


# ---------------------------------------------------------------------------------------
# Regressions from the first real discovery run.
# ---------------------------------------------------------------------------------------


def test_prompt_states_where_the_application_is():
    """The first real run died on turn one because the prompt never named the entry URL.

    The model was right to report itself stuck: it starts with nothing open and cannot invent a
    URL it was never told.
    """
    from src.agent.prompt import build_user_prompt
    from src.surface.base import Observation

    prompt = build_user_prompt(
        GOAL, {}, Observation(url="about:blank", title="", elements=[]), [],
        entry_url="http://localhost:5001",
    )

    assert "http://localhost:5001" in prompt


def test_parses_the_first_object_when_the_model_keeps_talking():
    """A second object or a trailing sentence should not cost a turn."""
    two_objects = '{"thought": "a", "action": "stuck"}\n{"thought": "b", "action": "done"}'
    trailing_prose = '{"thought": "a", "action": "stuck"}\nLet me know if that helps!'

    assert parse_decision(two_objects).thought == "a"
    assert parse_decision(trailing_prose).action is ControlAction.STUCK


def test_prompt_shows_what_has_already_been_captured():
    """The fix for the run that read the same balance twelve times and never finished.

    The step lines named the output field, but nothing stated that a value was in hand, so the
    model had no way to see it was done.
    """
    from src.agent.prompt import build_user_prompt
    from src.artifact.schema import Step
    from src.surface.base import Observation

    observation = Observation(url="http://x/", title="t", elements=[])
    recorded = [
        Step(
            index=0, action=ActionType.READ, extract="savings_balance",
            target=_locator(LocatorBy.TEXT, "$1.00", "r"),
        )
    ]

    empty = build_user_prompt(GOAL, {}, observation, [])
    filled = build_user_prompt(GOAL, {}, observation, recorded)

    assert "ALREADY CAPTURED: nothing yet" in empty
    assert "ALREADY CAPTURED: savings_balance" in filled


def test_system_prompt_tells_the_model_when_to_stop():
    """Without this the loop's only exit was the step budget."""
    from src.agent.prompt import build_system_prompt

    system = build_system_prompt()

    assert "ALREADY CAPTURED" in system
    assert '"done"' in system


@pytest.mark.integration
def test_a_failed_done_names_the_checkpoint_that_did_not_hold(surface, mock_app, target):
    """A run failed three 'done' verifications and the model could not tell why.

    The feedback said only that "the success checkpoint did not hold", and the evidence recorded
    only that it failed -- not what had been asserted. Both now name the assertion.
    """
    script = _script(mock_app)
    script[-1] = script[-1].model_copy(
        update={"checkpoint": Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Wire Transfer")}
    )
    agent = DiscoveryAgent(surface, StubLLMClient(script), checkpoint_timeout_s=1.0)

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID}, max_steps=6)

    done_entries = [entry for entry in result.transcript if entry.action == "done"]
    assert done_entries, "the run should have reached a done decision"
    assert done_entries[0].checkpoint == "text_equals='Wire Transfer'"
    assert done_entries[0].checkpoint_passed is False


def test_prompt_shows_which_fields_are_already_filled():
    """The duplicate-type fix: the model typed the member id twice in two consecutive runs.

    Same class as the re-reading loop -- completed state was in the step list but not stated as
    state, so the model could not see the field was already populated.
    """
    from src.agent.prompt import build_user_prompt
    from src.artifact.schema import Step
    from src.surface.base import Observation

    observation = Observation(url="http://x/", title="t", elements=[])
    typed = Step(
        index=0,
        action=ActionType.TYPE,
        target=_locator(LocatorBy.LABEL, "Member ID", "visible label"),
        value="{{member_id}}",
    )

    assert "FIELDS FILLED: nothing yet" in build_user_prompt(GOAL, {}, observation, [])
    assert "FIELDS FILLED: Member ID = {{member_id}}" in build_user_prompt(
        GOAL, {}, observation, [typed]
    )


def test_filling_the_same_field_twice_collapses_to_one_entry():
    """The line reports the field's state, not a log of attempts."""
    from src.agent.prompt import build_user_prompt
    from src.artifact.schema import Step
    from src.surface.base import Observation

    def typed(index: int) -> Step:
        return Step(
            index=index,
            action=ActionType.TYPE,
            target=_locator(LocatorBy.LABEL, "Member ID", "visible label"),
            value="{{member_id}}",
        )

    prompt = build_user_prompt(
        GOAL, {}, Observation(url="http://x/", title="t", elements=[]), [typed(0), typed(1)]
    )

    assert prompt.count("Member ID = {{member_id}}") == 1


def test_system_prompt_forbids_refilling_a_filled_field():
    from src.agent.prompt import build_system_prompt

    assert "FIELDS FILLED" in build_system_prompt()


def test_system_prompt_forbids_data_in_fallback_locators_by_example():
    """Rule 3 showed a checkpoint example only, and a real run complied for checkpoints while
    still putting the balance in a third fallback locator -- which cost the whole recording."""
    from src.agent.prompt import build_system_prompt

    system = build_system_prompt()

    assert "WRONG fallback" in system and "RIGHT fallback" in system
    assert "including fallbacks" in system


# ---------------------------------------------------------------------------------------
# Assembly-time normalization of data-shaped locator candidates.
# ---------------------------------------------------------------------------------------


def _rule(by: LocatorBy, value: str) -> LocatorRule:
    return LocatorRule(by=by, value=value)


def test_a_data_shaped_fallback_is_pruned():
    """One junk fallback must not cost a verified flow -- but it must be reported."""
    from src.agent.loop import _normalize_candidates

    clean_primary = _rule(LocatorBy.LABEL, "Member ID")
    kept, pruned = _normalize_candidates(
        [clean_primary, _rule(LocatorBy.TEXT, "$4,200.00"), _rule(LocatorBy.CSS, "input")]
    )

    assert [rule.value for rule in kept] == ["Member ID", "input"]
    assert [(item.role, item.matched) for item in pruned] == [("fallback 1", "a currency amount")]


def test_a_data_shaped_primary_promotes_the_first_clean_fallback():
    """A data-shaped primary is a real defect, so it is reported -- not silently dropped."""
    from src.agent.loop import _normalize_candidates

    kept, pruned = _normalize_candidates(
        [
            _rule(LocatorBy.TEXT, "$4,200.00"),
            _rule(LocatorBy.STRUCTURAL, "//td[normalize-space()='Available']"),
            _rule(LocatorBy.CSS, "td.amount"),
        ]
    )

    assert kept[0].by is LocatorBy.STRUCTURAL, "the first clean candidate becomes primary"
    assert [rule.value for rule in kept[1:]] == ["td.amount"]
    assert [item.role for item in pruned] == ["primary"]


def test_the_last_remaining_candidate_is_never_pruned():
    """A locator with no candidates is not a locator; failing at validation is the right end."""
    from src.agent.loop import _normalize_candidates

    kept, pruned = _normalize_candidates(
        [_rule(LocatorBy.TEXT, "$4,200.00"), _rule(LocatorBy.TEXT, "18,750.35")]
    )

    from src.artifact.schema import looks_like_observed_data

    assert len(kept) == 1 and kept[0].value == "$4,200.00"
    assert [item.role for item in pruned] == ["fallback 1"]
    assert looks_like_observed_data(kept[0].value), "kept dirty, so the validator will refuse it"


def test_pruning_leaves_clean_candidates_untouched():
    from src.agent.loop import _normalize_candidates

    ordered = [_rule(LocatorBy.LABEL, "Member ID"), _rule(LocatorBy.CSS, "input[type=text]")]
    kept, pruned = _normalize_candidates(ordered)

    assert kept == ordered and pruned == []


@pytest.mark.integration
def test_a_pruned_fallback_still_yields_a_valid_artifact(surface, mock_app, target, tmp_path):
    """End to end: the exact defect that cost a real run now costs one fallback."""
    script = _script(mock_app)
    script[3] = script[3].model_copy(
        update={
            "target": Locator(
                primary=LocatorRule(by=LocatorBy.STRUCTURAL, value=BALANCE_XPATH),
                fallbacks=[LocatorRule(by=LocatorBy.TEXT, value="$4,200.00")],
                rationale="row heading first; the amount's own text as a last resort",
            )
        }
    )
    agent = DiscoveryAgent(surface, StubLLMClient(script))

    result = agent.run(GOAL, target, {"member_id": MEMBER_ID})

    assert result.stop_reason is StopReason.SUCCESS, result.note
    read_step = next(s for s in result.artifact.steps if s.action is ActionType.READ)
    assert read_step.target.fallbacks == [], "the data-shaped fallback is gone"
    assert load_artifact(save_artifact(result.artifact, tmp_path / "a.json")) == result.artifact

    dropped = [item for entry in result.transcript for item in entry.pruned]
    assert [(item.role, item.matched) for item in dropped] == [
        ("fallback 1", "a currency amount")
    ]


@pytest.mark.integration
def test_a_data_shaped_primary_with_no_clean_fallback_still_fails_assembly(
    surface, mock_app, target
):
    """Enforcement is not weakened: with nothing clean to keep, the artifact is refused."""
    script = _script(mock_app)
    script[3] = script[3].model_copy(
        update={
            "target": Locator(
                primary=LocatorRule(by=LocatorBy.TEXT, value="$0.00"),
                rationale="targets the amount by its own text; nothing else proposed",
            )
        }
    )
    agent = DiscoveryAgent(surface, StubLLMClient(script))

    # The Holds/Pending amount is unique on this page, so the locator really does resolve --
    # which is the point: it works today and is worthless the moment the amount changes.
    result = agent.run(GOAL, target, {"member_id": MEMBER_ID}, max_steps=8)

    assert result.artifact is None, "a data-shaped primary must not reach a loadable artifact"
    assert result.stop_reason is StopReason.DEAD_END
    assert "did not satisfy the artifact schema" in result.note
