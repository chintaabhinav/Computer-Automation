"""Tests for the artifact schema: that a full recording round-trips, and that each validator
rejects the malformed case it exists to catch.

The invalid cases matter as much as the happy path. Every rule here is a failure that would
otherwise appear mid-replay, against a live application, as something that looks like the
target's fault rather than the recording's.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

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
    RiskLevel,
    Step,
    SurfaceType,
    Target,
    WaitPolicy,
)
from src.artifact.store import load_artifact, save_artifact

ENTRY_URL = "http://127.0.0.1:5001/"


def _locator(by: LocatorBy, value: str, rationale: str, *fallbacks: LocatorRule) -> Locator:
    return Locator(
        primary=LocatorRule(by=by, value=value),
        fallbacks=list(fallbacks),
        rationale=rationale,
    )


def _full_artifact() -> Artifact:
    """A complete, valid artifact for the mock bank sub-account flow.

    Exercises every construct the schema offers -- templated inputs, fallback locators, a
    nested-table extraction, a RISKY step, all three error buckets, and a composite success
    checkpoint -- so the round-trip test proves the whole format survives serialization, not
    just the simple parts.
    """
    return Artifact(
        capability_id="open_savings_sub_account",
        description="Look up a member by ID and open a savings sub-account for them.",
        target=Target(
            app_id="nmcu-back-office",
            entry_url=ENTRY_URL,
            surface_type=SurfaceType.LEGACY_WEB,
            tenant_id="branch-014",
            app_version="4.2.1",
        ),
        inputs=[
            InputParam(
                name="member_id",
                type=ParamType.STRING,
                description="The member identifier to search for.",
            )
        ],
        outputs=OutputContract(
            outcome_values=["success", "no_such_member"],
            fields=[
                OutputField(
                    name="member_name",
                    type=ParamType.STRING,
                    description="Name on the member record.",
                ),
                OutputField(
                    name="savings_balance",
                    type=ParamType.STRING,
                    description="Available savings balance, as displayed.",
                ),
                OutputField(
                    name="sub_account_number",
                    type=ParamType.STRING,
                    sensitive=True,
                    description="The newly opened sub-account number.",
                ),
            ],
        ),
        steps=[
            Step(
                index=0,
                action=ActionType.NAVIGATE,
                value=ENTRY_URL,
                checkpoint=Checkpoint(
                    kind=CheckpointKind.TEXT_EQUALS, value="Member Inquiry"
                ),
            ),
            Step(
                index=1,
                action=ActionType.TYPE,
                target=_locator(
                    LocatorBy.LABEL,
                    "Member ID",
                    "The field carries a visible label that is user-facing contract; the "
                    "input has no test id to target.",
                    LocatorRule(by=LocatorBy.CSS, value="form input[type=text]"),
                ),
                value="{{member_id}}",
            ),
            Step(
                index=2,
                action=ActionType.CLICK,
                target=_locator(
                    LocatorBy.TEXT,
                    "Search",
                    "Submit button is identified by its caption, which is stable across "
                    "releases of this screen.",
                ),
                wait=WaitPolicy(
                    until=Checkpoint(
                        kind=CheckpointKind.ANY_OF,
                        children=[
                            Checkpoint(
                                kind=CheckpointKind.TEXT_EQUALS, value="Member Detail"
                            ),
                            Checkpoint(
                                kind=CheckpointKind.TEXT_EQUALS, value="No such member"
                            ),
                        ],
                    ),
                    timeout_s=15.0,
                ),
                on_error=[
                    ErrorRule(
                        detect=Checkpoint(
                            kind=CheckpointKind.TEXT_EQUALS, value="No such member"
                        ),
                        bucket=ErrorBucket.BUSINESS_OUTCOME,
                        outcome="no_such_member",
                    ),
                    ErrorRule(
                        detect=Checkpoint(
                            kind=CheckpointKind.TEXT_EQUALS,
                            value="Your session has expired",
                        ),
                        bucket=ErrorBucket.HARD_FAILURE,
                    ),
                ],
            ),
            Step(
                index=3,
                action=ActionType.READ,
                target=_locator(
                    LocatorBy.STRUCTURAL,
                    "//td[normalize-space()='Member Name']/following-sibling::td[1]",
                    "No label element exists; the value is addressed by its row heading "
                    "within the detail table.",
                ),
                extract="member_name",
            ),
            Step(
                index=4,
                action=ActionType.READ,
                target=_locator(
                    LocatorBy.STRUCTURAL,
                    "//td[normalize-space()='Savings Balance']/following-sibling::td[1]"
                    "//tr[td[normalize-space()='Available']]/td[2]",
                    "The balance sits one table deep with no label of its own, so it must be "
                    "reached through the 'Available' row of the nested balance table.",
                ),
                extract="savings_balance",
            ),
            Step(
                index=5,
                action=ActionType.CLICK,
                target=_locator(
                    LocatorBy.TEXT,
                    "Open Sub-Account",
                    "Action button identified by caption; it is the only submit on the form.",
                ),
                risk=RiskLevel.RISKY,
                on_error=[
                    ErrorRule(
                        detect=Checkpoint(
                            kind=CheckpointKind.TEXT_EQUALS, value="System Notice"
                        ),
                        bucket=ErrorBucket.RECOVERABLE,
                        action=RecoveryAction.DISMISS,
                    )
                ],
                checkpoint=Checkpoint(
                    kind=CheckpointKind.URL_MATCHES, value="/confirm"
                ),
            ),
            Step(
                index=6,
                action=ActionType.READ,
                target=_locator(
                    LocatorBy.STRUCTURAL,
                    "//td[normalize-space()='New Sub-Account Number']/following-sibling::td[1]",
                    "Addressed by its row heading; the confirmation page has no hooks.",
                ),
                extract="sub_account_number",
            ),
        ],
        success=Checkpoint(
            kind=CheckpointKind.ALL_OF,
            children=[
                Checkpoint(kind=CheckpointKind.URL_MATCHES, value="/confirm"),
                Checkpoint(
                    kind=CheckpointKind.TEXT_EQUALS,
                    value="Sub-account opened successfully.",
                ),
            ],
        ),
        provenance=Provenance(
            goal="Open a savings sub-account for member 12345.",
            discovered_by="claude-opus-5",
            discovery_run_id="disc-20260910-0001",
            created_at=datetime(2026, 9, 10, 14, 30, tzinfo=UTC),
        ),
    )


# ---------------------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------------------


def test_full_artifact_round_trips_through_save_and_load(tmp_path):
    """A complete artifact survives save/load byte-for-byte in meaning, not just in shape."""
    original = _full_artifact()
    path = save_artifact(original, tmp_path / "runs" / "artifact.json")

    assert path.exists(), "save_artifact should create missing parent directories"
    reloaded = load_artifact(path)

    assert reloaded == original
    assert reloaded.model_dump_json() == original.model_dump_json()


def test_saved_artifact_is_reviewable_json(tmp_path):
    """The on-disk form is indented, declaration-ordered, ISO-dated JSON -- i.e. diffable."""
    path = save_artifact(_full_artifact(), tmp_path / "artifact.json")
    text = path.read_text(encoding="utf-8")

    assert '\n  "capability_id"' in text, "expected indent=2 formatting"
    assert text.index('"capability_id"') < text.index('"steps"'), "keys should not be sorted"
    assert '"created_at": "2026-09-10T14:30:00Z"' in text
    assert text.endswith("\n")


def test_locator_candidates_are_ordered_primary_first():
    """candidates() is the order replay attempts, so primary must come first."""
    fallback = LocatorRule(by=LocatorBy.CSS, value="form input[type=text]")
    locator = _locator(LocatorBy.LABEL, "Member ID", "label is stable", fallback)

    assert locator.candidates() == [locator.primary, fallback]


# ---------------------------------------------------------------------------------------
# Each validator's invalid case.
# ---------------------------------------------------------------------------------------


def test_step_indices_must_be_contiguous_from_zero():
    """A gap in numbering means a step was dropped; replay must not silently run the rest."""
    payload = _full_artifact().model_dump()
    payload["steps"][2]["index"] = 7

    with pytest.raises(ValidationError, match="contiguous starting at 0"):
        Artifact.model_validate(payload)


def test_extract_must_match_a_declared_output_field():
    """An extraction with no declared home would strand the data it reads."""
    artifact = _full_artifact()
    payload = artifact.model_dump()
    payload["steps"][3]["extract"] = "member_nickname"

    with pytest.raises(ValidationError, match="not a declared output field"):
        Artifact.model_validate(payload)


def test_template_param_must_match_a_declared_input():
    """An unbindable {{placeholder}} would be typed into a live form verbatim."""
    artifact = _full_artifact()
    payload = artifact.model_dump()
    payload["steps"][1]["value"] = "{{membership_number}}"

    with pytest.raises(ValidationError, match="not a declared input"):
        Artifact.model_validate(payload)


def test_all_of_checkpoint_requires_children():
    """A composite checkpoint with no children asserts nothing and can never fail."""
    with pytest.raises(ValidationError, match="requires a non-empty 'children' list"):
        Checkpoint(kind=CheckpointKind.ALL_OF)


def test_all_of_checkpoint_rejects_value():
    """Mixing the composite and leaf shapes hides which one the engine will honor."""
    with pytest.raises(ValidationError, match="must not set 'value'"):
        Checkpoint(
            kind=CheckpointKind.ALL_OF,
            value="/confirm",
            children=[Checkpoint(kind=CheckpointKind.URL_MATCHES, value="/confirm")],
        )


def test_leaf_checkpoint_requires_value():
    """A leaf kind without its expected value has nothing to compare against."""
    with pytest.raises(ValidationError, match="requires a 'value'"):
        Checkpoint(kind=CheckpointKind.TEXT_EQUALS)


def test_business_outcome_requires_an_outcome_name():
    """Without a name there is nothing to hand back, so the result would look like a failure."""
    with pytest.raises(ValidationError, match="requires 'outcome'"):
        ErrorRule(
            detect=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="No such member"),
            bucket=ErrorBucket.BUSINESS_OUTCOME,
        )


def test_recoverable_requires_a_recovery_action():
    """Recoverable with no action is indistinguishable from a hard failure at replay time."""
    with pytest.raises(ValidationError, match="requires 'action'"):
        ErrorRule(
            detect=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="System Notice"),
            bucket=ErrorBucket.RECOVERABLE,
        )


def test_hard_failure_rejects_outcome_and_action():
    """A hard failure that carries a recovery or an outcome is a miscategorized rule."""
    with pytest.raises(ValidationError, match="must not set 'outcome' or 'action'"):
        ErrorRule(
            detect=Checkpoint(
                kind=CheckpointKind.TEXT_EQUALS, value="Your session has expired"
            ),
            bucket=ErrorBucket.HARD_FAILURE,
            action=RecoveryAction.RETRY,
        )


def test_click_requires_a_target():
    """A click with no target has no defined effect on the page."""
    with pytest.raises(ValidationError, match="action 'click' requires 'target'"):
        Step(index=0, action=ActionType.CLICK)


def test_navigate_requires_a_url_value():
    """NAVIGATE's value *is* the destination; without it the step is a no-op."""
    with pytest.raises(ValidationError, match="action 'navigate' requires 'value'"):
        Step(index=0, action=ActionType.NAVIGATE)


def test_read_requires_an_extract_name():
    """A READ with nowhere to store its result cannot contribute to the output contract."""
    with pytest.raises(ValidationError, match="action 'read' requires 'extract'"):
        Step(
            index=0,
            action=ActionType.READ,
            target=_locator(LocatorBy.TEXT, "Available", "row heading"),
        )


# ---------------------------------------------------------------------------------------
# Observed data must never become targeting.
# ---------------------------------------------------------------------------------------


def _with_success(checkpoint: Checkpoint) -> dict:
    payload = _full_artifact().model_dump()
    payload["success"] = checkpoint.model_dump()
    return payload


def test_rejects_a_success_checkpoint_asserting_an_extracted_amount():
    """The defect a real discovery run produced: assert the balance you just read.

    It validates, replays green for the member it was recorded against, and is useless for
    every other one.
    """
    payload = _with_success(Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="$4,200.00"))

    with pytest.raises(ValidationError, match="a currency amount"):
        Artifact.model_validate(payload)


def test_rejects_an_amount_nested_inside_a_composite_checkpoint():
    """Hiding the literal one level down must not evade the rule."""
    payload = _with_success(
        Checkpoint(
            kind=CheckpointKind.ALL_OF,
            children=[
                Checkpoint(kind=CheckpointKind.URL_MATCHES, value="/member"),
                Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="18,750.35"),
            ],
        )
    )

    with pytest.raises(ValidationError, match="a currency amount"):
        Artifact.model_validate(payload)


def test_rejects_a_bare_identifier_as_a_checkpoint():
    payload = _with_success(Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="12345"))

    with pytest.raises(ValidationError, match="a bare numeric identifier"):
        Artifact.model_validate(payload)


def test_rejects_an_element_exists_selector_containing_a_data_literal():
    payload = _with_success(
        Checkpoint(kind=CheckpointKind.ELEMENT_EXISTS, value="td:has-text('$4,200.00')")
    )

    with pytest.raises(ValidationError, match="a currency amount"):
        Artifact.model_validate(payload)


def test_rejects_a_locator_that_targets_an_amount():
    """The same run recorded `text='$4,200.00'` as a fallback locator."""
    payload = _full_artifact().model_dump()
    payload["steps"][4]["target"]["fallbacks"].append({"by": "text", "value": "$4,200.00"})

    with pytest.raises(ValidationError, match="fallback .* locator targets what looks like"):
        Artifact.model_validate(payload)


def test_rejects_a_primary_locator_that_targets_an_amount():
    payload = _full_artifact().model_dump()
    payload["steps"][4]["target"]["primary"] = {"by": "text", "value": "$18,750.35"}

    with pytest.raises(ValidationError, match="primary locator targets what looks like"):
        Artifact.model_validate(payload)


def test_rejects_a_data_literal_in_a_step_checkpoint_or_error_detector():
    """Every checkpoint an artifact carries is covered, not just the headline one."""
    step_payload = _full_artifact().model_dump()
    step_payload["steps"][0]["checkpoint"] = {
        "kind": "text_equals", "value": "$4,200.00", "children": None,
    }
    with pytest.raises(ValidationError, match="step 0's checkpoint"):
        Artifact.model_validate(step_payload)

    detector_payload = _full_artifact().model_dump()
    detector_payload["steps"][2]["on_error"][0]["detect"] = {
        "kind": "text_equals", "value": "4,200.00", "children": None,
    }
    with pytest.raises(ValidationError, match="on_error\\[0\\] detector"):
        Artifact.model_validate(detector_payload)


def test_the_error_message_does_not_echo_the_offending_value():
    """A validation error must not become the leak the rule exists to prevent."""
    payload = _with_success(Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="$4,200.00"))

    with pytest.raises(ValidationError) as caught:
        Artifact.model_validate(payload)

    assert "4,200.00" not in str(caught.value)
    assert "withheld" in str(caught.value)


def test_accepts_structural_targeting_that_merely_contains_digits():
    """Row and column indices are structure, not data -- the rule must not block them.

    This is the whole reason the identifier pattern is anchored: an XPath full of positional
    indices is exactly what durable targeting looks like on this surface.
    """
    payload = _full_artifact().model_dump()
    payload["steps"][4]["target"]["primary"] = {
        "by": "structural",
        "value": "//table[2]//tr[3]/td[2]",
    }
    payload["success"] = {
        "kind": "element_exists", "value": "table:nth-child(2) tr:nth-child(3)", "children": None,
    }

    assert Artifact.model_validate(payload).steps[4].target.primary.value.endswith("td[2]")


def test_accepts_ordinary_structural_checkpoints():
    """The rule must leave the normal, correct recording completely alone."""
    for value in ("Member Detail", "Savings Balance", "Sub-account opened successfully."):
        payload = _with_success(Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value=value))
        assert Artifact.model_validate(payload).success.value == value


def test_the_rule_is_gated_on_the_artifact_declaring_outputs():
    """Narrowing, stated in the docstring: with nothing extracted there is no value to leak.

    A capability that only clicks through a flow may legitimately assert a numeric string.
    """
    click_only = Artifact(
        capability_id="acknowledge_notice",
        description="Click through the maintenance notice.",
        target=Target(app_id="nmcu-back-office", entry_url=ENTRY_URL),
        outputs=OutputContract(outcome_values=["success"]),
        steps=[
            Step(
                index=0,
                action=ActionType.CLICK,
                target=_locator(LocatorBy.TEXT, "Continue", "the button caption is stable"),
            )
        ],
        success=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="12345"),
        provenance=Provenance(
            goal="Acknowledge the notice.",
            discovered_by="test",
            discovery_run_id="run-1",
            created_at=datetime(2026, 9, 11, tzinfo=UTC),
        ),
    )

    assert click_only.success.value == "12345"


def test_url_matches_is_a_documented_false_negative():
    """Pinning a known gap so it stays a decision rather than becoming a surprise."""
    payload = _with_success(
        Checkpoint(kind=CheckpointKind.URL_MATCHES, value="/member?member_id=12345")
    )

    assert Artifact.model_validate(payload).success.value.endswith("12345")
