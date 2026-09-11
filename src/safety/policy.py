"""The policy a run is held to: where it may go, what it may do, and what needs a human.

Loaded from a config file rather than hardcoded, because the answer to "may this automation
click that" is an operational decision that changes per deployment and must be reviewable by
someone who does not read Python. The file is small and declarative on purpose -- see
`src/safety/__init__.py` for why a policy DSL is deliberately not built.

FAIL CLOSED, EVERYWHERE. Every default in this module denies. That is not defensiveness for its
own sake: this system drives a back office, and the cost of wrongly refusing an action is a
stopped run that a person looks at, while the cost of wrongly permitting one is a transaction
nobody authorized. Those are not comparable, so every ambiguity resolves toward refusal:

* A URL matching no pattern is denied. An allowlist that defaults to "allow" is not an
  allowlist.
* An action type absent from `allowed_actions` is denied, so adding a new primitive to the
  system does not silently grant it everywhere.
* Risk that was never declared is treated as RISKY, so a caller that forgets to declare gets
  the strict path rather than the permissive one.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from fnmatch import fnmatch
from pathlib import Path

from pydantic import BaseModel, Field

from src.artifact.schema import ActionType, RiskLevel

DEFAULT_POLICY_PATH = Path("config/policy.yaml")

REGEX_PREFIX = "re:"
"""Marks a URL pattern as a regular expression. Anything else is a glob.

Two forms because they fail differently. A glob is easy to read and easy to get subtly wrong --
`http://localhost:5001*` also matches `http://localhost:5001.evil.example` -- so the shipped
policy uses an anchored regex, and the glob form stays available for patterns where the risk of
a loose match is not there.
"""


class RiskyActionMode(StrEnum):
    """What to do when a step declares itself RISKY."""

    BLOCK = "block"
    """Refuse it. The right default for anything unattended."""
    CONFIRM = "confirm"
    """Ask a human, via an injected callback. Denies when no callback is supplied."""
    FLAG = "flag"
    """Permit it, but log prominently. For observation runs where nothing is at stake."""


class Policy(BaseModel):
    """The rules a guarded surface enforces."""

    allowed_url_patterns: list[str] = Field(
        default_factory=list,
        description="Globs, or regexes prefixed with 're:', matched against the FULL URL. "
        "Navigation targets and the page an action is performed on must match at least one. "
        "An empty list denies everything, which is the correct reading of an empty allowlist.",
    )
    allowed_actions: list[ActionType] = Field(
        default_factory=list,
        description="The action types permitted at all. Absent means denied, so a primitive "
        "added to the system later is not implicitly authorized in existing deployments.",
    )
    risky_action_mode: RiskyActionMode = Field(
        default=RiskyActionMode.BLOCK,
        description="How to treat a step declared RISKY. Defaults to BLOCK: an unattended run "
        "that can move money without being told it may is the failure this layer exists to "
        "prevent.",
    )
    require_confirmation_for: list[ActionType] = Field(
        default_factory=list,
        description="Action types that always need confirmation, whatever their declared risk. "
        "A per-action override for cases where the action itself is the hazard regardless of "
        "what the recording claims about it.",
    )

    def match_url(self, url: str) -> str | None:
        """Return the pattern that permits `url`, or None if none does.

        Returns the matching pattern rather than a bool so the decision log can name the rule
        that allowed something. In a regulated context, "it was permitted" is not an answer;
        "it was permitted by this line of this policy file" is.
        """
        for pattern in self.allowed_url_patterns:
            if pattern.startswith(REGEX_PREFIX):
                if re.match(pattern[len(REGEX_PREFIX) :], url):
                    return pattern
            elif fnmatch(url, pattern):
                return pattern
        return None

    def allows_action(self, action: ActionType) -> bool:
        return action in self.allowed_actions

    def needs_confirmation(self, action: ActionType, risk: RiskLevel | None) -> bool:
        """Whether this action must be confirmed before it runs.

        True when the action is on the always-confirm list, or when it is risky under CONFIRM
        mode. Undeclared risk counts as risky -- see `effective_risk`.
        """
        if action in self.require_confirmation_for:
            return True
        return (
            self.risky_action_mode is RiskyActionMode.CONFIRM
            and effective_risk(risk) is RiskLevel.RISKY
        )


def effective_risk(risk: RiskLevel | None) -> RiskLevel:
    """Resolve a declared risk, treating anything unset or unrecognized as RISKY.

    The single place that decision is made, so it cannot be made differently elsewhere. A step
    with no declared risk is not a safe step; it is a step nobody has classified, and in a
    back office those are the ones to stop.
    """
    if isinstance(risk, RiskLevel):
        return risk
    if risk is None:
        return RiskLevel.RISKY
    try:
        return RiskLevel(risk)
    except ValueError:
        return RiskLevel.RISKY


def load_policy(path: str | Path = DEFAULT_POLICY_PATH) -> Policy:
    """Load and validate a policy from YAML or JSON.

    Raises rather than falling back to a permissive default when the file is missing or
    malformed: "the policy could not be read" must never resolve to "so we allowed everything".
    """
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(
            f"no policy file at {source}. Refusing to run without one -- an unreadable policy "
            f"is not an empty policy. Pass --policy, or copy the default from "
            f"{DEFAULT_POLICY_PATH}."
        )

    text = source.read_text(encoding="utf-8")
    if source.suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                "pyyaml is required to read a .yaml policy; install it or use a .json policy"
            ) from exc
        payload = yaml.safe_load(text)
    else:
        payload = json.loads(text)

    if not isinstance(payload, dict):
        raise ValueError(f"{source} does not contain a policy object")
    return Policy.model_validate(payload)
