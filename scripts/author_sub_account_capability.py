#!/usr/bin/env python3
"""Author the sub-account capability, whose one step has a real side effect.

HAND-AUTHORED, NOT DISCOVERED. This exists to exercise the risky-action and escalation paths,
which need a step that changes state in the target system -- the discovered member-balance
capability only reads. Writing it here rather than recording it keeps that distinction honest:
`provenance.discovered_by` says `hand-authored`, so nobody reads this as something a model
produced.

Run:  python scripts/author_sub_account_capability.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.artifact.schema import (  # noqa: E402
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
)
from src.artifact.store import load_artifact, save_artifact  # noqa: E402

OUT = "artifacts/open_sub_account.json"
ENTRY = "http://localhost:5001"


def build() -> Artifact:
    dismiss_notice = ErrorRule(
        detect=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="System Notice"),
        bucket=ErrorBucket.RECOVERABLE,
        action=RecoveryAction.DISMISS,
        recovery_target=Locator(
            primary=LocatorRule(by=LocatorBy.TEXT, value="Continue"),
            fallbacks=[LocatorRule(by=LocatorBy.CSS, value="input[value='Continue']")],
            rationale="The interstitial's only button, by its visible caption.",
        ),
    )
    session_expired = ErrorRule(
        detect=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Your session has expired"),
        bucket=ErrorBucket.HARD_FAILURE,
    )

    return Artifact(
        version="1.0",
        capability_id="open_sub_account",
        description=(
            "Open a savings sub-account for the member given as input. Hand-authored (see "
            "scripts/author_sub_account_capability.py) to exercise the risky-action and "
            "escalation paths, which need a step with a real side effect."
        ),
        target=Target(
            app_id="nmcu-back-office", entry_url=ENTRY, surface_type=SurfaceType.LEGACY_WEB
        ),
        inputs=[
            InputParam(
                name="member_id",
                type=ParamType.STRING,
                description="The member whose sub-account is being opened.",
            )
        ],
        outputs=OutputContract(
            outcome_values=["success", "no_such_member"],
            fields=[
                OutputField(
                    name="sub_account_number",
                    type=ParamType.STRING,
                    sensitive=True,
                    description="The newly opened sub-account number.",
                )
            ],
        ),
        steps=[
            Step(
                index=0,
                action=ActionType.NAVIGATE,
                value=f"{ENTRY}/member?member_id={{{{member_id}}}}",
                checkpoint=Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Member Detail"),
                on_error=[
                    ErrorRule(
                        detect=Checkpoint(
                            kind=CheckpointKind.TEXT_EQUALS, value="No such member"
                        ),
                        bucket=ErrorBucket.BUSINESS_OUTCOME,
                        outcome="no_such_member",
                    ),
                    session_expired,
                ],
            ),
            Step(
                index=1,
                action=ActionType.CLICK,
                target=Locator(
                    primary=LocatorRule(by=LocatorBy.TEXT, value="Open Sub-Account"),
                    rationale="The action button, by its caption; the only submit on the form.",
                ),
                # The whole point of this capability: a step that changes the target system, so
                # the safety policy has something real to gate and a human something to approve.
                risk=RiskLevel.RISKY,
                checkpoint=Checkpoint(kind=CheckpointKind.URL_MATCHES, value="/confirm"),
                on_error=[session_expired, dismiss_notice],
            ),
            Step(
                index=2,
                action=ActionType.READ,
                target=Locator(
                    primary=LocatorRule(
                        by=LocatorBy.STRUCTURAL,
                        value="//td[normalize-space()='New Sub-Account Number']"
                        "/following-sibling::td[1]",
                    ),
                    rationale="Addressed through its row heading; the value has no label.",
                ),
                extract="sub_account_number",
            ),
        ],
        success=Checkpoint(
            kind=CheckpointKind.TEXT_EQUALS, value="Sub-account opened successfully."
        ),
        provenance=Provenance(
            goal="Open a savings sub-account for the member given as input.",
            discovered_by="hand-authored",
            discovery_run_id="n/a-hand-authored",
            created_at=datetime.now(UTC),
        ),
    )


def main() -> int:
    path = save_artifact(build(), OUT)
    artifact = load_artifact(path)  # prove it loads before claiming success
    risky = [s.index for s in artifact.steps if s.risk is RiskLevel.RISKY]
    print(f"wrote {path} ({len(artifact.steps)} steps, risky: {risky}) and confirmed it loads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
