"""Typed, versioned schema for a reusable UI automation capability.

An *artifact* is what discovery produces and replay consumes. It is deliberately a data
contract rather than a script: the LLM explores the target once, writes down what it learned
here, and every subsequent run is executed by the deterministic replay engine with no model in
the loop. That split is only trustworthy if the recording is legible to a human reviewer, so
this file favors explicit, self-documenting types over compact ones -- a reviewer should be able
to read an artifact JSON alongside this module and audit exactly what the automation will do,
what it will touch, what counts as success, and which steps are risky.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)

PARAM_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
"""Matches a ``{{param_name}}`` placeholder in a step value.

Kept module-level and shared so the Artifact validator and the (future) replay binder agree on
exactly one templating syntax; a mismatch there would mean artifacts that validate but cannot
be bound.
"""


_OBSERVED_AMOUNT_RE = re.compile(r"[$£€]?\d[\d,]*\.\d{2}\b")
"""A currency-shaped amount anywhere in a value, e.g. '$4,200.00' or '312.09'.

Searched rather than fully matched, because the defect this catches is a data literal *embedded*
in targeting -- ``input[value='4,200.00']`` is as wrong as a bare ``$4,200.00``.
"""

_OBSERVED_ID_RE = re.compile(r"^\s*\d{4,}\s*$")
"""A value that is nothing but a long digit run, e.g. '12345'.

Anchored, unlike the amount pattern, so that legitimate structural indices survive: an XPath
like ``//tr[3]/td[2]`` contains digits but is not *made of* them.
"""


def looks_like_observed_data(value: str) -> str | None:
    """Name the kind of page data `value` resembles, or None if it looks like targeting.

    Public because the discovery agent normalizes its own recordings against the same rule
    before assembly. Sharing one function is the point: a second copy of these patterns would
    let the recorder and the validator drift apart, and the failure mode of that drift is an
    artifact the recorder considers clean and the loader refuses.

    Deliberately two narrow patterns rather than a general "does this look like data" judgment.
    The cost of a false positive is a valid recording rejected at load time -- which blocks a
    working automation -- while the cost of a false negative is a defect that still shows up in
    review. So this errs heavily toward missing cases.
    """
    if _OBSERVED_AMOUNT_RE.search(value):
        return "a currency amount"
    if _OBSERVED_ID_RE.match(value):
        return "a bare numeric identifier"
    return None


def describe_checkpoint(checkpoint: Checkpoint | None) -> str | None:
    """Render a checkpoint as a compact 'kind=value' string, recursing into composites.

    Lives here rather than in either consumer because discovery's evidence and replay's failure
    reports must describe the same condition the same way -- a reviewer comparing a recording to
    the run that broke it should not have to reconcile two notations.
    """
    if checkpoint is None:
        return None
    if checkpoint.children:
        inner = ", ".join(filter(None, (describe_checkpoint(c) for c in checkpoint.children)))
        return f"{checkpoint.kind}({inner})"
    return f"{checkpoint.kind}={checkpoint.value!r}"


def _walk_checkpoints(checkpoint: Checkpoint | None) -> list[Checkpoint]:
    """Flatten a checkpoint tree, so composite conditions are inspected as thoroughly as leaves."""
    if checkpoint is None:
        return []
    found = [checkpoint]
    for child in checkpoint.children or []:
        found.extend(_walk_checkpoints(child))
    return found


def template_params(text: str | None) -> list[str]:
    """Return the input-parameter names referenced by ``{{...}}`` placeholders in ``text``.

    Used for validation now and by the replay binder later, so "which inputs does this step
    need?" has a single answer.
    """
    if not text:
        return []
    return PARAM_TEMPLATE_RE.findall(text)


# ---------------------------------------------------------------------------------------
# Enums. Closed vocabularies, so a reviewer can see the full space of what an artifact may
# express, and so replay can exhaustively handle every case instead of guessing on strings.
# ---------------------------------------------------------------------------------------


class SurfaceType(StrEnum):
    """Which kind of surface the capability drives.

    Only ``WEB`` is implemented. The other members exist as the extension seam: the artifact
    format should not have to change when a desktop surface is added, so the distinction lives
    in data from day one rather than being retrofitted.
    """

    WEB = "web"
    LEGACY_WEB = "legacy_web"
    """A browser surface with no automation hooks: table layout, no test ids, no ``data-*``."""
    DESKTOP = "desktop"
    """Native application surface. Not implemented; reserved so artifacts stay forward-readable."""


class ActionType(StrEnum):
    """The complete set of primitives replay can execute.

    Kept intentionally small: every action a discovery run records must reduce to one of these,
    which is what makes a recording reviewable and its blast radius knowable.
    """

    NAVIGATE = "navigate"
    """Go to an absolute URL."""
    CLICK = "click"
    TYPE = "type"
    """Fill a field with a literal or templated value."""
    READ = "read"
    """Extract text from the page into a named output field. The only action that produces data."""
    WAIT = "wait"
    """Block until this step's wait policy is satisfied, without touching the page."""
    DISMISS = "dismiss"
    """Close an interstitial or modal that is blocking the flow."""


class RiskLevel(StrEnum):
    """Whether a step needs policy review before it is allowed to run.

    Separated from the action type because risk is about consequence, not mechanics: two
    identical clicks differ entirely depending on whether the button submits money.
    """

    SAFE = "safe"
    """Read-only or trivially reversible; may run unattended."""
    RISKY = "risky"
    """Has a side effect on the target system, so the safety policy gates or escalates it."""


class ParamType(StrEnum):
    """Scalar types for capability inputs and outputs.

    Only scalars: the caller-facing contract stays trivially serializable and easy to validate,
    and anything richer would push structure into strings where it could not be reviewed.
    """

    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class LocatorBy(StrEnum):
    """The strategy used to find an element on the page.

    Ordered loosely from most durable to most brittle. On a legacy surface there are no test
    ids, so recording *how* an element was found -- not just a raw selector -- is what lets a
    reviewer judge whether the targeting will survive the next release.
    """

    LABEL = "label"
    """By its form label. Most durable: labels are user-visible contract."""
    TEXT = "text"
    """By visible text, e.g. a button caption."""
    CSS = "css"
    """Raw CSS selector. Brittle against markup churn; a last resort."""
    ROLE = "role"
    """By accessibility role and accessible name."""
    STRUCTURAL = "structural"
    """By position within the document, e.g. a cell in a nested table. Brittle but sometimes
    the only option when a value has no label of its own."""


class CheckpointKind(StrEnum):
    """The kinds of observable conditions an artifact can assert about page state.

    Checkpoints are the artifact's only way to talk about "what should be true", and they are
    reused for three different jobs -- step verification, success criteria, and error detection
    -- so that a reviewer learns one vocabulary rather than three.
    """

    ELEMENT_EXISTS = "element_exists"
    """Value is a locator expression; passes when a matching element is present."""
    URL_MATCHES = "url_matches"
    """Value is matched against the current URL."""
    TEXT_EQUALS = "text_equals"
    """Value must appear as text on the page."""
    ALL_OF = "all_of"
    """Composite: every child must pass. Uses ``children``, never ``value``."""
    ANY_OF = "any_of"
    """Composite: at least one child must pass. Uses ``children``, never ``value``."""


class ErrorBucket(StrEnum):
    """The error taxonomy: how replay must interpret a detected exceptional state.

    This classification is the difference between a useful automation and a noisy one. A legacy
    app answers "no such member" with a normal-looking page, and treating that as a crash would
    be wrong: it is a real answer the caller asked for. Bucketing forces every recorded error
    condition to declare which of the three fundamentally different responses it wants.
    """

    BUSINESS_OUTCOME = "business_outcome"
    """A legitimate negative answer from the app. Replay finishes successfully and returns the
    declared outcome; it is reported to the caller, not raised as a failure."""
    RECOVERABLE = "recoverable"
    """A transient obstacle (interstitial, slow load). Replay applies the recovery action and
    continues the same step."""
    HARD_FAILURE = "hard_failure"
    """The run cannot legitimately continue (session expired, HTTP 500). Replay stops and
    escalates rather than guessing."""


class RecoveryAction(StrEnum):
    """What replay does about a ``RECOVERABLE`` error.

    A closed set on purpose: recovery must never be open-ended improvisation, because anything
    creative here would break the guarantee that replay is deterministic.
    """

    DISMISS = "dismiss"
    """Close the blocking element, then retry the step."""
    RETRY = "retry"
    """Re-attempt the step as recorded."""
    WAIT = "wait"
    """Wait out the condition, then retry the step."""


# ---------------------------------------------------------------------------------------
# Targeting.
# ---------------------------------------------------------------------------------------


class LocatorRule(BaseModel):
    """One concrete way to find an element: a strategy plus its argument."""

    by: LocatorBy = Field(
        description="Which strategy to use. Recorded explicitly so a reviewer can see at a "
        "glance whether this step leans on durable labels or brittle structure."
    )
    value: str = Field(
        description="The argument for the strategy: the label text, the visible text, the CSS "
        "selector, the accessible name, or the structural path."
    )


class Locator(BaseModel):
    """How to find one element, with ordered fallbacks and a written justification.

    Legacy surfaces offer no stable hooks, so a single selector is a liability: the same field
    may be findable by label today and only by table position after the next release. Recording
    a ranked list lets replay degrade gracefully instead of failing outright, and the required
    rationale keeps the discovery agent honest -- it must state why this targeting should hold,
    which is precisely the claim a human reviewer needs to check.
    """

    primary: LocatorRule = Field(
        description="The preferred strategy. Tried first on every replay."
    )
    fallbacks: list[LocatorRule] = Field(
        default_factory=list,
        description="Alternates tried in order when the primary finds nothing. Ordered "
        "most-durable-first so a degraded match is still the best available one.",
    )
    rationale: str = Field(
        description="Why this targeting is expected to be robust on this surface. Required, "
        "not optional: it is the reviewable claim behind the selector and the first thing to "
        "re-read when a replay starts failing."
    )

    def candidates(self) -> list[LocatorRule]:
        """Return every rule to try, primary first, in the order replay should attempt them."""
        return [self.primary, *self.fallbacks]


# ---------------------------------------------------------------------------------------
# Observable conditions.
# ---------------------------------------------------------------------------------------


class Checkpoint(BaseModel):
    """An assertion about observable page state, composable via ``ALL_OF``/``ANY_OF``.

    Self-referencing so that real-world conditions can be expressed without inventing a
    separate expression language: "we are on the confirmation page *and* it shows a sub-account
    number" is one checkpoint tree, and it stays readable in JSON.

    Leaf kinds carry a ``value``; composite kinds carry ``children``. The two are mutually
    exclusive and that is enforced, so a malformed checkpoint fails at load time rather than
    silently passing during a replay -- a checkpoint that cannot fail is worse than none.
    """

    kind: CheckpointKind = Field(
        description="Which condition this asserts, or which combinator it applies."
    )
    value: str | None = Field(
        default=None,
        description="The expected value for leaf kinds: a locator expression, a URL fragment, "
        "or the text to find. Must be absent for ALL_OF/ANY_OF.",
    )
    children: list[Checkpoint] | None = Field(
        default=None,
        description="Sub-conditions for ALL_OF/ANY_OF. Must be absent for leaf kinds.",
    )

    @model_validator(mode="after")
    def _validate_shape(self) -> Checkpoint:
        """Reject checkpoints that mix leaf and composite shapes, or that assert nothing."""
        composite = self.kind in (CheckpointKind.ALL_OF, CheckpointKind.ANY_OF)
        if composite:
            if not self.children:
                raise ValueError(
                    f"checkpoint kind {self.kind} requires a non-empty 'children' list"
                )
            if self.value is not None:
                raise ValueError(f"checkpoint kind {self.kind} must not set 'value'")
        else:
            if self.value is None:
                raise ValueError(f"checkpoint kind {self.kind} requires a 'value'")
            if self.children:
                raise ValueError(f"checkpoint kind {self.kind} must not set 'children'")
        return self


Checkpoint.model_rebuild()


class WaitPolicy(BaseModel):
    """How long a step may take to settle, expressed as a condition instead of a sleep.

    Recording the condition rather than a duration is what makes replay both deterministic and
    fast: the same artifact works on a 200ms response and on the injected 5s one, and a timeout
    becomes a specific, classifiable failure instead of a flaky race.
    """

    until: Checkpoint | None = Field(
        default=None,
        description="Condition the page must reach AFTER the step's action, before the step "
        "counts as settled. This is a post-action condition, not a pre-action gate: the useful "
        "thing to wait for is the consequence of the action -- the page a click navigated to -- "
        "and replay checks it in that position (see ReplayEngine._run_step). None means the "
        "surface's own readiness (document loaded) is sufficient.",
    )
    timeout_s: float = Field(
        default=10.0,
        description="How long to keep polling before declaring the wait failed. Bounded so a "
        "hung target surfaces as a reported error rather than a stuck run.",
    )
    poll_ms: int = Field(
        default=250,
        description="Interval between condition checks. Explicit so replay timing is a "
        "reviewable property of the artifact, not a hidden engine constant.",
    )


class ErrorRule(BaseModel):
    """A recorded exceptional state, plus how replay must classify and answer it.

    Discovery is where these are cheapest to learn -- the agent sees the "session expired" page
    once and writes down what it means -- and replay then handles it without a model. The
    required-field rules below make the bucket's promise concrete: a business outcome has a name
    to return, a recoverable error has a recovery to apply, and a hard failure has neither
    because there is nothing to do but stop.
    """

    detect: Checkpoint = Field(
        description="How replay recognizes this state on the page. Checked before a step's "
        "success checkpoint, since a legacy app often returns HTTP 200 for its error pages."
    )
    bucket: ErrorBucket = Field(
        description="How to interpret the state: a real answer, a transient obstacle, or a stop."
    )
    outcome: str | None = Field(
        default=None,
        description="For BUSINESS_OUTCOME only: the outcome name returned to the caller. Must "
        "be one of the capability's declared outcome_values.",
    )
    action: RecoveryAction | None = Field(
        default=None,
        description="For RECOVERABLE only: the recovery to apply before retrying the step.",
    )
    recovery_target: Locator | None = Field(
        default=None,
        description="The control to act on when recovering: the modal's dismiss button, the "
        "retry link. Recorded separately from the step's own target because they are different "
        "elements -- the step acts on the field it wanted, the recovery acts on whatever is in "
        "the way. Without this, an engine has to guess which element closes an obstruction, "
        "and guessing is exactly what a deterministic replay must not do.",
    )

    @model_validator(mode="after")
    def _validate_bucket_fields(self) -> ErrorRule:
        """Require exactly the fields the chosen bucket needs, and forbid the rest."""
        if self.bucket is ErrorBucket.BUSINESS_OUTCOME:
            if not self.outcome:
                raise ValueError("bucket 'business_outcome' requires 'outcome'")
            if self.action is not None:
                raise ValueError("bucket 'business_outcome' must not set 'action'")
        elif self.bucket is ErrorBucket.RECOVERABLE:
            if self.action is None:
                raise ValueError("bucket 'recoverable' requires 'action'")
            if self.outcome is not None:
                raise ValueError("bucket 'recoverable' must not set 'outcome'")
        else:
            if self.outcome is not None or self.action is not None:
                raise ValueError(
                    "bucket 'hard_failure' must not set 'outcome' or 'action'; there is "
                    "nothing to return and nothing to recover"
                )

        if self.action is RecoveryAction.DISMISS and self.recovery_target is None:
            # A warning, not an error, and deliberately so. Requiring it would reject an
            # otherwise-valid recording for a missing *hint*: the engine still has a documented
            # caption heuristic, and if that fails the recovery simply does not clear the
            # obstruction, the retry fails, and the bounded recovery limit turns it into a
            # reported hard failure. That degradation is visible and safe, so refusing to load
            # the artifact would cost more than it protects -- while staying silent would leave
            # a reviewer unaware that this rule is running on a guess.
            log.warning(
                "error rule detects %s and recovers by DISMISS but records no recovery_target; "
                "replay will fall back to guessing at conventional dismissal captions",
                self.detect.kind,
            )
        return self


# ---------------------------------------------------------------------------------------
# Steps.
# ---------------------------------------------------------------------------------------


class Step(BaseModel):
    """One replayable operation: what to do, where, how to know it worked, and what can go wrong.

    A step is self-contained by design. Everything replay needs -- targeting with fallbacks, a
    wait condition, a success checkpoint, and the error rules that apply at this point in the
    flow -- lives on the step itself, so a reviewer reads it in one place and a failure points
    at one record.

    ``value`` supports ``{{param_name}}`` templating, bound to the capability's declared inputs
    at replay time. That is what makes a recording *reusable* rather than a transcript of one
    session: the member ID typed during discovery becomes a parameter, and the Artifact
    validator checks that every referenced name is actually declared.
    """

    index: int = Field(
        description="Position in the flow, contiguous from 0. Explicit rather than implied by "
        "list order so logs, evidence files, and error reports can name a step unambiguously."
    )
    action: ActionType = Field(description="Which primitive to execute.")
    target: Locator | None = Field(
        default=None,
        description="The element to act on. Required for CLICK/TYPE/READ/DISMISS; unused by "
        "NAVIGATE and WAIT.",
    )
    value: str | None = Field(
        default=None,
        description="The literal or templated payload: the URL for NAVIGATE, the text to enter "
        "for TYPE. Supports {{param_name}} placeholders bound to declared inputs.",
    )
    extract: str | None = Field(
        default=None,
        description="For READ: the output field name to store the extracted text under. Must "
        "match a field declared in the capability's output contract.",
    )
    risk: RiskLevel = Field(
        default=RiskLevel.SAFE,
        description="Consequence of this step. Defaults to SAFE so risk must be asserted "
        "deliberately; RISKY steps are what the safety policy gates and escalation pauses on.",
    )
    wait: WaitPolicy = Field(
        default_factory=WaitPolicy,
        description="Readiness condition for this step. Always present so replay never falls "
        "back to an implicit sleep.",
    )
    checkpoint: Checkpoint | None = Field(
        default=None,
        description="What must be true after the step for it to count as done. Without this, "
        "replay could sail past a silently failed click.",
    )
    on_error: list[ErrorRule] = Field(
        default_factory=list,
        description="Exceptional states known to occur at this step, checked before the "
        "checkpoint. Scoped per step because the same page text means different things at "
        "different points in a flow.",
    )

    @model_validator(mode="after")
    def _validate_action_requirements(self) -> Step:
        """Enforce the fields each action cannot execute without."""
        if self.action in (
            ActionType.CLICK,
            ActionType.TYPE,
            ActionType.READ,
            ActionType.DISMISS,
        ) and self.target is None:
            raise ValueError(f"action '{self.action}' requires 'target'")
        if self.action is ActionType.NAVIGATE and not self.value:
            raise ValueError("action 'navigate' requires 'value' (the URL)")
        if self.action is ActionType.TYPE and self.value is None:
            raise ValueError("action 'type' requires 'value'")
        if self.action is ActionType.READ and not self.extract:
            raise ValueError("action 'read' requires 'extract' (the output field name)")
        return self


# ---------------------------------------------------------------------------------------
# Caller-facing contract.
# ---------------------------------------------------------------------------------------


class InputParam(BaseModel):
    """One parameter the caller supplies, bound into templated step values at replay time."""

    name: str = Field(
        description="Parameter name, as referenced by {{name}} in step values."
    )
    type: ParamType = Field(
        description="Scalar type, so a caller's arguments can be validated before a browser "
        "is ever launched."
    )
    required: bool = Field(
        default=True,
        description="Whether the caller must supply it. Defaults to True because a missing "
        "value should fail fast rather than type an empty string into a live form.",
    )
    description: str = Field(
        description="What this parameter means in the target application's own terms, so a "
        "caller does not have to read the steps to use the capability."
    )


class OutputField(BaseModel):
    """One value the capability returns, read off the page by a READ step.

    ``sensitive=True`` means the value is redacted everywhere it would otherwise be written
    down -- logs, evidence files, escalation packets -- and is never persisted raw. It is
    declared here, in the contract, rather than decided by the logger, so that a reviewer can
    audit the handling of a field from the artifact alone.
    """

    name: str = Field(
        description="Field name, matching the 'extract' value of the READ step that fills it."
    )
    type: ParamType = Field(description="Scalar type of the returned value.")
    sensitive: bool = Field(
        default=False,
        description="When True, the value is redacted in logs and evidence and never persisted "
        "raw; only its presence and shape are recorded. Defaults to False so redaction is an "
        "explicit, reviewable decision.",
    )
    description: str = Field(description="What the value represents to the caller.")


class OutputContract(BaseModel):
    """Everything the capability promises to return: a closed set of outcomes plus typed fields.

    ``outcome_values`` is how expected business results reach the caller *without* being treated
    as failures. A legacy app reports "no such member" as an ordinary page, and that is a real
    answer to the question the caller asked -- so replay returns it as a named outcome and exits
    successfully. Enumerating the set closes the loop: replay can reject an outcome nobody
    declared, and a caller can exhaustively handle the possibilities without reading the steps.
    """

    outcome_values: list[str] = Field(
        default_factory=list,
        description="The complete set of business outcomes this capability can return, e.g. "
        "['success', 'no_such_member']. Every ErrorRule outcome must be a member, and none of "
        "them count as errors.",
    )
    fields: list[OutputField] = Field(
        default_factory=list,
        description="The data fields returned on success, each filled by exactly one READ step.",
    )


# ---------------------------------------------------------------------------------------
# Where and when the capability was recorded.
# ---------------------------------------------------------------------------------------


class Target(BaseModel):
    """The application this capability drives, identified well enough to detect drift.

    ``tenant_id`` and ``app_version`` are the keys for cross-tenant reuse and drift management.
    The intent (designed, not built) is that one recording generalizes across deployments of the
    same app: a capability discovered on tenant A is a candidate for tenant B when the app
    version matches, and a version change is the signal to re-verify rather than trust a
    recording made against different markup. Carrying them in the artifact now means that
    reasoning can be added later without reformatting every stored artifact.
    """

    app_id: str = Field(
        description="Stable identifier for the target application, independent of URL."
    )
    entry_url: str = Field(
        description="Where replay starts. Also the URL the safety allowlist is checked against."
    )
    surface_type: SurfaceType = Field(
        default=SurfaceType.WEB,
        description="Which surface implementation replay should use.",
    )
    tenant_id: str | None = Field(
        default=None,
        description="Which deployment this was recorded against. None means tenant-agnostic. "
        "Reuse key: see the model docstring.",
    )
    app_version: str | None = Field(
        default=None,
        description="The target's version at discovery time. Drift key: a mismatch at replay "
        "is grounds to re-verify the recording rather than trust it.",
    )


class Provenance(BaseModel):
    """Who recorded this capability, from what instruction, and when.

    An artifact is generated by a model and then executed unsupervised, so it has to carry its
    own audit trail: the goal in the user's words, the model that interpreted it, and the run
    whose evidence backs it up. Without this, a reviewer looking at a suspicious step has no way
    back to the context that produced it.
    """

    goal: str = Field(
        description="The original natural-language goal given to the discovery agent. The "
        "reviewable statement of intent that the steps are supposed to implement."
    )
    discovered_by: str = Field(
        description="Model id that performed discovery, e.g. 'claude-opus-5'. Lets a whole "
        "generation of artifacts be re-examined if a model is found to record badly."
    )
    discovery_run_id: str = Field(
        description="Identifier of the discovery run, keying this artifact to its evidence "
        "directory (screenshots, transcript) for after-the-fact review."
    )
    created_at: datetime = Field(
        description="When the recording was made. Serialized ISO-8601; the basis for judging "
        "whether an artifact is stale relative to the target."
    )


# ---------------------------------------------------------------------------------------
# Root.
# ---------------------------------------------------------------------------------------


class Artifact(BaseModel):
    """A complete, reviewable recording of one reusable UI automation capability.

    This is the contract between the two halves of the system: discovery writes it, replay
    executes it, and a human reviews it in between. It is stored as JSON precisely so that
    review can happen in a pull request, where a changed selector or a newly RISKY step shows
    up as a readable diff.

    The two version fields have deliberately different lifecycles and must not be conflated:

    * ``schema_version`` versions the *format* -- the models in this file. It changes when
      fields are added or their meaning shifts, and it tells a loader whether it can safely
      interpret the file at all. Every artifact in the fleet moves to a new schema version
      together, via migration.
    * ``version`` versions *this particular capability recording*. It is bumped when the
      capability is re-recorded, typically because the target application drifted and the old
      steps no longer hold. Two artifacts can share ``schema_version`` and differ in ``version``,
      and the same ``version`` under a new ``schema_version`` still describes the same
      automation.

    Confusing them would break both jobs at once: a format migration would look like behavior
    change, and a re-recording would look like it needed a loader upgrade.
    """

    schema_version: str = Field(
        default="1.0",
        description="Version of the artifact FORMAT (the models in this module). Read by the "
        "loader to decide whether it can interpret this file. See the class docstring.",
    )
    version: str = Field(
        default="1.0",
        description="Version of THIS capability recording, bumped on re-record when the target "
        "drifts. Independent of schema_version. See the class docstring.",
    )
    capability_id: str = Field(
        description="Stable name callers invoke, e.g. 'open_savings_sub_account'. Stays "
        "constant across re-recordings; 'version' is what changes."
    )
    description: str = Field(
        description="What the capability accomplishes, in one human sentence. The summary a "
        "reviewer reads before the steps."
    )
    target: Target = Field(description="The application this recording was made against.")
    inputs: list[InputParam] = Field(
        default_factory=list,
        description="Parameters the caller supplies, bound into templated step values.",
    )
    outputs: OutputContract = Field(
        description="The closed set of outcomes and typed fields this capability returns."
    )
    steps: list[Step] = Field(
        default_factory=list,
        description="The flow to replay, in order. The executable heart of the artifact and the "
        "part a reviewer reads most closely.",
    )
    success: Checkpoint = Field(
        description="What must be true for the whole run to count as successful. Distinct from "
        "per-step checkpoints: every step can pass while the overall goal is unmet, so the "
        "capability states its own end condition."
    )
    provenance: Provenance = Field(
        description="Audit trail linking this recording back to its goal, model, and evidence."
    )

    @model_validator(mode="after")
    def _validate_internal_references(self) -> Artifact:
        """Check that the artifact is internally consistent before anything tries to run it.

        These three cross-field rules are the ones that would otherwise surface as a confusing
        mid-replay crash: an off-by-one in step numbering, an extraction with nowhere to go, or
        a template placeholder that can never be bound. Catching them at load time keeps replay
        failures attributable to the target application rather than to the recording.
        """
        indices = [step.index for step in self.steps]
        if indices != list(range(len(self.steps))):
            raise ValueError(
                f"step indices must be contiguous starting at 0, got {indices}"
            )

        declared_outputs = {field.name for field in self.outputs.fields}
        for step in self.steps:
            if step.extract and step.extract not in declared_outputs:
                raise ValueError(
                    f"step {step.index} extracts '{step.extract}', which is not a declared "
                    f"output field (declared: {sorted(declared_outputs)})"
                )

        declared_inputs = {param.name for param in self.inputs}
        for step in self.steps:
            for name in template_params(step.value):
                if name not in declared_inputs:
                    raise ValueError(
                        f"step {step.index} references {{{{{name}}}}}, which is not a declared "
                        f"input (declared: {sorted(declared_inputs)})"
                    )

        return self

    @model_validator(mode="after")
    def _reject_observed_data_in_targeting(self) -> Artifact:
        """Reject artifacts that bake observed page data into checkpoints or locators.

        A capability that extracts a member's balance must not also *assert* that balance. A
        discovery run produced exactly that: a success checkpoint of
        ``text_equals: "<the balance it had just read>"``, plus a fallback locator carrying the
        same literal. The artifact validated, replayed green for the member it was recorded
        against, and was silently useless for every other member -- while storing a financial
        value in the one place the design promises never holds values.

        There is a prompt rule against this too, but a prompt rule is advisory: it asks a model
        to behave. This is enforcement. The invariant has to hold whatever any model proposes,
        whatever a human hand-edits into a JSON file, and whatever a future discovery loop
        assembles -- so it lives at the boundary every artifact must cross.

        HOW IT WORKS, AND WHAT IT MISSES. The schema never sees the extracted values themselves
        -- an artifact records field *names*, not data -- so this cannot compare a checkpoint
        against what a run actually read. It is a structural heuristic instead: a checkpoint or
        locator value that looks like a currency amount or a bare numeric id is data, not
        targeting. Three deliberate narrowings, all erring toward false negatives:

        * Only TEXT_EQUALS and ELEMENT_EXISTS checkpoints are examined. A URL_MATCHES value
          containing an id (``/member?member_id=12345``) is *not* caught, because ids in URLs are
          often genuinely structural.
        * Only artifacts that declare at least one output are examined. With nothing being
          extracted there is no extracted value to leak, and a numeric literal is more likely to
          be a real page string.
        * Only two shapes are recognized. An account number (``000-12345-01``), a date, or a
          name will pass. Catching those would need real data flowing into the schema.

        So this closes the case that actually occurred and keeps valid recordings loadable. It is
        a backstop against the common defect, not a proof of absence -- review still matters.

        Offending values are named by category and location, never echoed, so a validation error
        cannot become the leak it exists to prevent.
        """
        if not self.outputs.fields:
            return self

        checked_kinds = (CheckpointKind.TEXT_EQUALS, CheckpointKind.ELEMENT_EXISTS)
        sources: list[tuple[str, Checkpoint | None]] = [("the success checkpoint", self.success)]
        for step in self.steps:
            sources.append((f"step {step.index}'s checkpoint", step.checkpoint))
            sources.append((f"step {step.index}'s wait condition", step.wait.until))
            for position, rule in enumerate(step.on_error):
                sources.append((f"step {step.index}'s on_error[{position}] detector", rule.detect))

        for location, root in sources:
            for checkpoint in _walk_checkpoints(root):
                if checkpoint.kind not in checked_kinds or checkpoint.value is None:
                    continue
                resembles = looks_like_observed_data(checkpoint.value)
                if resembles:
                    raise ValueError(
                        f"{location} asserts what looks like {resembles} rather than a "
                        f"structural fact. A checkpoint must assert that an element exists or "
                        f"that the page reached a state -- never the data being extracted, "
                        f"which differs on every run and makes the capability single-use. "
                        f"(value withheld from this message on purpose)"
                    )

        for step in self.steps:
            if step.target is None:
                continue
            for position, rule in enumerate(step.target.candidates()):
                resembles = looks_like_observed_data(rule.value)
                if resembles:
                    which = "primary" if position == 0 else f"fallback {position}"
                    raise ValueError(
                        f"step {step.index}'s {which} locator targets what looks like "
                        f"{resembles} rather than a stable element. Target the element by its "
                        f"label, its heading, or its position -- not by the value it happens to "
                        f"display. (value withheld from this message on purpose)"
                    )

        return self
