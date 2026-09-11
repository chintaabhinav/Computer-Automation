# Report

Discover a UI workflow once with an LLM, record it as a reviewable artifact, replay it
deterministically with no model in the loop. Every figure comes from a run in
[`evidence/`](evidence/) or a test here; the suite is 161 tests across seven files.

## 1. Architecture

The clearest evidence the layering is load-bearing is the bug that broke it. Replaying against
the injected five-second delay crashed with a raw Playwright `TimeoutError`: the replay engine
catches `SurfaceError`, a driver exception is not one, so it sailed through the layer meant to
absorb exactly that, and a *slow page* — which the taxonomy has a bucket for — became an
unhandled crash. The fix is `_as_surface_error` in [`src/surface/web.py`](src/surface/web.py)
around `open`, `click`, `type`, `read`; a blocked click now arrives as `SurfaceError: click
failed: Locator.click: Timeout 2000ms exceeded`. The same bug showed a click that navigates needs
the navigation budget, not the element budget.

[`mock_app/app.py`](mock_app/app.py) is a deliberately legacy-flavoured Flask target (table
layout, inline styles, no test ids, no `data-*`, balance one table deep). `src/surface/` is the
perceive-act seam: [`base.py`](src/surface/base.py) holds the `Surface` ABC and neutral types,
[`web.py`](src/surface/web.py) is the only file importing Playwright. Above it, `src/agent/`
(discovery), `src/artifact/` (the contract), `src/replay/` (production), `src/safety/`
(chokepoint), `src/escalation/` (handoff).

Two boundaries carry the design: nothing above the surface imports Playwright, nothing in
`src/replay/` imports the agent or an LLM SDK. The second is asserted by
`test_replay_cannot_reach_a_model` in [`tests/test_replay.py`](tests/test_replay.py), parsing each
module with `ast` — its first version grepped source text and failed on the docstring explaining
the rule, which is why grep was wrong: it flags prose while missing an aliased import. A test is
not unbypassable; it buys that the boundary cannot erode *silently*.

## 2. Artifact schema

The schema earned its keep by rejecting the artifact a *successful* run produced.
`discovery-20260911-135047` finished the flow in six iterations and emitted a recording whose
success checkpoint was `text_equals: "$4,200.00"` — the balance it had just read. It validated,
would have replayed green for member 12345, was useless for every other member, and stored a
financial value where the design promises none. It is kept as
[`artifacts/rejected_example_hardcoded_checkpoint.json`](artifacts/rejected_example_hardcoded_checkpoint.json);
loading it raises *"the success checkpoint asserts what looks like a currency amount rather than a
structural fact"*. The message withholds the value, because a refusal echoing the balance would
become the leak the rule prevents.

Four validators run at load time in [`src/artifact/schema.py`](src/artifact/schema.py): contiguous
step indices; every `extract` matching a declared output; every `{{param}}` matching a declared
input; and `_reject_observed_data_in_targeting`, walking every checkpoint an artifact carries
(success, per-step, wait conditions, error detectors, recursing through `all_of`/`any_of`) plus
every locator candidate. Its docstring names the gaps — only `TEXT_EQUALS`/`ELEMENT_EXISTS`, only
artifacts declaring outputs, only two shapes, so a `URL_MATCHES` with an id is *not* caught.
Asymmetric cost justifies that: a false positive blocks a working automation; a false negative
review still catches.

Hence a capability contract, not a step list: typed inputs and outputs, a closed `outcome_values`
set, per-step `risk`, ordered locator candidates each with a written `rationale`, checkpoints, and
`on_error` rules. [`artifacts/lookup_member_balance.json`](artifacts/lookup_member_balance.json)
has 4 steps, 13 error rules, `outcome_values: ["success", "no_such_member"]`. The version fields
differ in lifecycle — `schema_version` gates whether a loader can read the file at all, `version`
versions *this recording* — and the live artifact is `schema_version 1.0 / version 1.1`: format
unchanged, rules authored after discovery.

The three-layer defence was forced by runs, not designed up front. A prompt rule came first, and
`discovery-20260911-140922` obeyed it for checkpoints while still attaching a fallback locator
targeting the balance by its displayed amount; the validator refused the recording, costing four
correct steps over one unused suggestion. So the recorder normalizes its own output:
`_normalize_candidates` in [`src/agent/loop.py`](src/agent/loop.py) prunes data-shaped candidates,
promotes the first clean fallback if the primary is dirty, never prunes the last. Prompt steers,
recorder normalizes, validator enforces — sharing `looks_like_observed_data` so they cannot
drift.

## 3. Determinism & error handling

| Replay run | Member | Result | Duration |
| --- | --- | --- | --- |
| `replay-20260911-143308` | 12345 | success, `$4,200.00` | 0.915s |
| `replay-20260911-143320` | 67890 | success, `$18,750.35` | 0.520s |
| `replay-20260911-143321` | 24680 | success, `$312.09` | 0.329s |

Two of those members were never involved in recording. Recording took
`discovery-20260911-142524`: 5 model requests, 24.9k input and 4.0k output tokens, $0.0133; each
replay costs $0.00 in under a second. All nine paid discovery runs, including the seven that
failed, total $0.1549 — the project's entire model cost.

Three buckets, three different endings. `BUSINESS_OUTCOME` is **not a failure**: a legitimate
negative answer with a declared name and exit code 0. Collapsing it into failure is what makes
automation untrustworthy — reporting "no such member" as an error trains operators to ignore
errors.

| Scenario | Run | Status | Detail |
| --- | --- | --- | --- |
| clean | `144425` | success | balance extracted, 0.856s |
| `--inject not_found` | `144426` | **business_outcome** | `no_such_member`, 3 steps |
| member 99999, no injection | `144426-2` | **business_outcome** | `no_such_member`, 3 steps |
| `--inject popup` | `144427` | success | 0.317s, 0 recoveries |
| `--inject slow` | `144621` | success | 10.947s, 0 recoveries |
| `--inject session_expired` | `144443` | hard_failure | step 1, `text_equals='Your session has expired'` |
| `--inject server_error` | `144444` | hard_failure | step 1, `text_equals='HTTP 500 - Internal Server Error'` |

The unknown member returning the same outcome as the injected one matters: the result describes
the application's answer, not a trick of the harness.

Two results are not the obvious ones. **Popup succeeded with zero recoveries, correctly** —
verified, not assumed: the modal *is* present, reading the balance through it works, clicking
through it *is* blocked. Occlusion is not invisibility, so the read succeeds, and this capability's
only click (Search) precedes the modal; the `DISMISS` path and its recorded `recovery_target` are
proven by `test_dismiss_recovery_uses_the_recorded_recovery_target`, which clicks a button behind
the modal. **Slow was absorbed, not recovered**: 10.947s, every checkpoint passed, because waits
are conditions with budgets rather than fixed sleeps. The `RECOVERABLE`/`WAIT` rule is the net for
a delay outlasting a step's budget; slow versus stuck is a timeout.

Locators are ordered candidate lists and the recording promotes whichever rule *actually resolved*.
Ambiguity is refused, not resolved by first match: a run proposed `text='$4,200.00'`, matching two
cells because member 12345's Available and Ledger balances are identical. The exception is wrapper
nesting — this markup wraps values in `<font><b>`, so nested matches collapse to the innermost.
Fallback use is a drift signal, logged and surfaced; runs `132729`, `134312`, `134440` promoted 1,
2 and 1. A step passing on its second choice is one release from passing on none.

## 4. Heterogeneity & multi-tenant

The extension point is the `Surface` ABC and neutral `Observation` in
[`src/surface/base.py`](src/surface/base.py): url, title, and capped `PerceivedElement`s of role,
accessible name, visible text and an opaque ref — deliberately not HTML, which would be enormous
on a table-based page and meaningless off-browser. A desktop surface populates the same structure
from an OS accessibility tree (AXUIElement, UIAutomation) and nothing above the seam changes,
because the artifact records `Locator`s and `Checkpoint`s and any surface resolving them can replay
it. `SurfaceType` already carries `WEB`, `LEGACY_WEB`, `DESKTOP`.

Two pieces of evidence the seam holds under pressure: `GuardedSurface` in
[`src/safety/guard.py`](src/safety/guard.py) implements the same ABC and is not a browser; and when
policy needed a step's risk, the answer was `declare_step_risk`, a non-abstract no-op on the ABC,
not a `risk` argument on every method that would have pushed safety into the perception contract
and every implementation including the desktop one.

For multi-tenancy the keys are recorded: `Target.app_id`, `tenant_id`, `app_version`. The intended
model is a base capability per `app_id` plus per-tenant overrides — a differing tenant supplies
replacement locators only for the steps that differ — with `app_version` as the drift trigger and
fallback-promotion events as the detection mechanism, naming which steps drift while runs are
still green. **Designed, not built**: with one tenant, one version and one capability here, an
override engine would be written against imagined divergence.

## 5. Escalation & handoff

The control model is exclusive and *enforced* — the difference between a handoff and a flag
someone remembers to check. `SessionControl` in
[`src/escalation/session.py`](src/escalation/session.py) holds the controller and a transfer
history, and the guard calls `require_automation_control` before every action. With control ceded,
`guard.click(...)` and `guard.open(...)` both raise `ControlViolation`, and the recording surface
confirms nothing reached the driver. The control check precedes the policy checks deliberately: an
action taken during someone's handover is wrong even when policy permits it. Double-cede and
double-resume are refused — two humans on one session is the same hazard.

It attaches to three pre-existing seams: the agent's `_on_dead_end` hook, the guard's confirmation
callback, and a replay `HARD_FAILURE`. On resume the agent continues with a note that a human
changed the page; the engine retries the failed step exactly once, since asking twice would loop a
person; in CONFIRM mode the operator's RESUME *is* the authorization. Under the default
`--operator none` all three stay fail-closed.

Real: the pause; the exclusive transfer; the session, never torn down, so with `--headed` the
human works in the same Chromium window with the same cookies and form state; the resume; and the
recording — `control_ceded`/`control_returned` events with the operator's note, `human_held_s`,
and redacted before/after page state. Mocked: the terminal UI. Demonstrated end to end: a `RISKY`
"Open Sub-Account" click under CONFIRM, ceded, approved, executed, landing on `/confirm`, with
`intervention.json` in the run's evidence and the goal masked to `member 1***5`. The "after"
snapshot is taken *after* resuming, because it goes through the same guarded surface and would
otherwise trip the exclusivity being enforced.

## 6. Safety

The allowlist uses an anchored regex because the readable glob has a hole:
`http://localhost:5001*` also matches `http://localhost:5001.evil.example/`, since a glob star does
not stop at a host boundary. Verified both ways in
`test_a_glob_star_does_not_stop_at_a_host_boundary`, with [`config/policy.yaml`](config/policy.yaml)
shipping `re:^http://(localhost|127\.0\.0\.1):5001([/?][^\s]*)?$`. An allowlist with a hole is
worse than none, because it invites trust. The `?` alternative was itself a fix: requiring a path
segment silently refused every `--inject` scenario, whose URLs carry only a query string.

Enforcement is a single chokepoint: `GuardedSurface` implements the `Surface` ABC and wraps the
real one, and `cli.py:187` is the only place in `src/`, `cli.py` or `scripts/` constructing a
`WebSurface`. *Structural*, not unbypassable — `--unsafe` bypasses it deliberately with a loud
warning — so bypassing takes a visible act rather than an oversight. Every decision is logged,
allows included: "the automation was not permitted to do that" is only credible if the permitted
actions are equally accounted for.

Defaults fail closed on cost asymmetry — wrongly refusing costs a stopped run someone looks at,
wrongly permitting costs a transaction nobody authorized. Unmatched URL denied; unlisted action
denied; undeclared risk resolving to `RISKY` in one place (`effective_risk`); a missing policy file
raising rather than defaulting open. Risky actions take one of three modes: `BLOCK` (default)
refuses, `CONFIRM` routes to the operator and denies when none is attached, `FLAG` proceeds at
WARNING.

Redaction is reused, not reimplemented: `guard.py` imports `redact` from
[`src/evidence.py`](src/evidence.py) and defines no patterns of its own. The persistence boundary is
the point — evidence records `$4,***.**` while `ReplayResult.outputs` returns `$4,200.00` to the
caller. A scan over every text file under `evidence/` finds no raw balance, member id, or key.

Wiring policy in exposed a category error: under `BLOCK` the agent could not take its first
observation, because no risk had been declared and fail-closed read the silence as risky. Risk
describes what a recorded *step* does; looking at a screen is not a step, so `observe` and
`screenshot` are location-gated but not risk-gated.

Limits are enumerated in [`src/safety/__init__.py`](src/safety/__init__.py): no RBAC, no rate
limiting or circuit breakers, no secrets management beyond `.env`, no policy DSL, no encryption at
rest. Three are easy to assume are covered and are not. **No host attestation** — an allowlist
trusts DNS. **No defence against a compromised artifact author** — anyone who can edit one can point the
automation anywhere policy permits, so artifact review is the control, which is why the format is
built to be read. **No image redaction** — text masking cannot touch pixels, so
screenshots show balances and identifiers in full, acceptable only because this target serves
fabricated data.

## 7. Cuts

Not built, deliberately. A **checkpoint expression language** — five kinds plus two combinators
covered every condition needed, and a DSL nobody fully understands is worse for review than a
verbose tree. **Conditional or looping steps** — branching is where an artifact stops being
reviewable at a glance, and a linear flow plus an error taxonomy handled every scenario here.
The **multi-tenant override engine** and a **desktop surface**, whose seams exist but which would be
designed against imagined requirements. A **real operator console**, since the protocol is the
interesting part. **Input-level recording of human actions**, **per-step retry tuning** (one bound
of 2 per step, 5 per run), and **confidence scoring** on locators, where the recorded `rationale`
plus observed fallback promotion proved better signal than a number the model makes up.

Next, strongest first. **Capture the human's fix during handoff well enough to turn it into a
recorded step.** Escalation records a note and a before/after summary; an input-level trace would
let the system propose the missing step, review it, and append it — closing the loop between
escalation and the record-once thesis. Today every handoff is a permanent tax: the human fixes the
same gap every run, and nothing learns.

Second, **fix the non-idempotent recovery assumption.** Recovery retries the whole step, assuming
the action is repeatable — true for a read and a click behind a modal, false for a click that
already navigated, where the retry would click a button no longer on the page. Resolve it before
this drives anything that moves money, by recording idempotency per action type or having recovery
resume mid-step. Third, the override engine once a second tenant exists to shape it. Fourth,
screenshot redaction, the only remaining place raw member data reaches disk.
