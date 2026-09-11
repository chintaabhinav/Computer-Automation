# Evidence index

Every run this system has performed -- discovery and replay -- kept in full. The failures are retained
deliberately: they are the only place the stopping conditions, the error feedback loop, and
fallback promotion can be seen working against live model output rather than a scripted stub,
and each one names a real defect that the next run fixed.

An example capability artifact is included here as
[`example_artifact.json`](example_artifact.json) — a copy of the recording produced by
`discovery-20260911-142524`, so this directory is self-contained for review. The canonical
artifacts, including the preserved rejected one, live in [`/artifacts/`](../artifacts/).

Each run directory holds `run.jsonl` (one JSON line per event, appended as the run happens),
`summary.json` (outcome, duration, token usage and cost), and `screenshots/` (one per
iteration, plus a failure capture when a run ends badly).

**Redaction.** Every value written to the text files passes through `redact()` in
[`src/evidence.py`](../src/evidence.py): balances become `$4,***.**`, identifiers become
`1***5`, and URL query strings are masked while the location stays readable. The masking is
lossy and one-way. This is why these directories are committed rather than ignored.

**Screenshots are pixels, not text, and are not redacted.** They show the rendered page,
including the balance and member id. That is acceptable here only because the mock app serves
entirely fabricated data ("Test system -- fictitious data"). A deployment against a real
back office would need image masking or screenshot suppression before evidence could be
committed anywhere.

| Run | Model | Stop reason | Steps | Iter | Shots | Fallbacks | Cost | What it demonstrates |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `discovery-20260910-200444` | stub | success | 4 | 5 | 5 | 0 | $0 | The zero-cost dry run: CLI, evidence writing, redaction and artifact emission end to end with no API call. |
| `discovery-20260910-200601` | claude-haiku-4-5 | dead_end | 0 | 1 | 0 | 0 | $0.0032 | **DEAD_END on turn one, and the agent was right.** The prompt never told it the entry URL: *"no URL was provided in the inputs or current state."* Also shows the recorder surviving a screenshot failure on a browser that was never opened. |
| `discovery-20260911-121035` | claude-haiku-4-5 | dead_end | 1 | 3 | 4 | 0 | $0.0071 | **DEAD_END from the consecutive-failure bound.** The model wrote checkpoints as prose (`element_exists: "textbox with value <the member id>"`) because the response format never specified the syntax. Two unverified actions in a row ended the run, and neither was recorded — the artifact-only-from-verified-actions rule holding under live output. |
| `discovery-20260911-132729` | claude-haiku-4-5 | dead_end | 5 | 7 | 8 | 1 | $0.0155 | **First fallback promotion on live output:** the model's `label='Savings Balance'` failed and its own `text` fallback carried the step, logged as drift. Then it proposed the balance's own visible text as a locator, which genuinely matches two cells on this page -- Available and Ledger hold the identical amount for the member under test -- and the surface correctly refused rather than guessing. |
| `discovery-20260911-134312` | claude-haiku-4-5 | dead_end | 5 | 8 | 9 | 2 | $0.0198 | **Vocabulary mismatch found.** The model wrote `//cell[@name='Available']` — XPath over the accessibility roles the `Observation` shows it, not over HTML — because the prompt documented `structural` without saying which tree it addresses. |
| `discovery-20260911-134440` | claude-haiku-4-5 | max_steps | 15 | 15 | 16 | 1 | $0.0452 | **MAX_STEPS.** With the locator syntax documented, the model found the balance correctly on step 3 — then read it twelve more times until the budget ran out. The step history named the output field but never said a value was captured, so "am I finished?" had no answer it could see. |
| `discovery-20260911-135047` | claude-haiku-4-5 | success | 5 | 6 | 6 | 0 | $0.0136 | **First run to finish the flow**, in six iterations. Its artifact hardcoded the balance in the success checkpoint, which is the recording preserved as `rejected_example_hardcoded_checkpoint.json` and the reason the schema now enforces against it. |
| `discovery-20260911-140114` | claude-haiku-4-5 | dead_end | 6 | 9 | 10 | 0 | $0.0247 | **Three failed `done` verifications, cause unknowable.** The transcript recorded only *that* the checkpoint failed, never what was asserted, and the feedback to the model was equally vague -- so it re-proposed three times without converging. This run is why proposed checkpoints are now recorded and named back to the model. |
| `discovery-20260911-140922` | claude-haiku-4-5 | dead_end | 4 | 5 | 6 | 0 | $0.0125 | **Rejected at artifact assembly by the schema validator.** Flow, checkpoints and the duplicate-type fix all worked -- `text_equals='Savings Balance'` verified true -- but the model attached a third fallback locator targeting the balance by its displayed amount, and the recording was refused in full. The two-layer design catching what the prompt did not, at the cost of four good steps. This run motivated assembly-time pruning. |
| `discovery-20260911-142524` | claude-haiku-4-5 | **success** | 4 | 5 | 5 | 0 | $0.0133 | **The clean run, and the current artifact.** Five iterations, every locator resolving on its primary, no duplicate type, a structural success checkpoint, nothing pruned because nothing needed pruning, and an artifact that loads. Emitted [`artifacts/lookup_member_balance.json`](../artifacts/lookup_member_balance.json). |

Total discovery spend across all runs: **$0.1546**. Replay costs nothing: there is no model in
that path, which is the whole point of the split.

## Replay runs

The production path, executing `artifacts/lookup_member_balance.json` with no LLM in the
decision loop. Each replays the same recording against a different member -- the recording
contains `{{member_id}}` and no trace of any particular one -- and each cost $0.00.

| Run | Status | Steps | Duration | Extracted |
| --- | --- | --- | --- | --- |
| `replay-20260911-143308` | success | 4 | 0.92s | `current_savings_balance` |
| `replay-20260911-143320` | success | 4 | 0.52s | `current_savings_balance` |
| `replay-20260911-143321` | success | 4 | 0.33s | `current_savings_balance` |

Half a second against nine model round-trips and $0.013 for the discovery run that produced the
recording. Extracted values are masked in the evidence (`$3**.**`) and returned in the clear to
the caller through `ReplayResult.outputs`, which is the point of the persistence-boundary split.

## Error handling: every runtime condition the target can produce

The capability's `on_error` rules were **authored after discovery**, by
[`scripts/author_error_rules.py`](../scripts/author_error_rules.py), and the artifact's version
moved to 1.1 to say so. This is an honest boundary worth being explicit about: the discovery run
typed a valid member id into a healthy application and walked only the success path. It never
saw a no-such-member result, a maintenance modal, an expired session or an HTTP 500, so it could
not record how those should be handled -- and having a model invent rules for states it never
observed is the kind of plausible fabrication this whole format exists to prevent. Discovery
establishes *how to drive the app*; review establishes *what its answers mean*.

Each run below replays the same artifact with one of the mock app's injectable states.

| Run | Scenario | Status | Outcome / failure | Recoveries |
| --- | --- | --- | --- | --- |
| `replay-20260911-144425` | clean, member 1***5 | success | `success`, balance extracted | 0 |
| `replay-20260911-144426` | `--inject not_found` | **business_outcome** | `no_such_member` | 0 |
| `replay-20260911-144426-2` | member 9***9, no injection | **business_outcome** | `no_such_member` | 0 |
| `replay-20260911-144427` | `--inject popup` | success | `success`, balance extracted | 0 |
| `replay-20260911-144621` | `--inject slow` | success | `success` after 10.9s | 0 |
| `replay-20260911-144443` | `--inject session_expired` | hard_failure | detected `text_equals='Your session has expired'` at step 1 | 0 |
| `replay-20260911-144444` | `--inject server_error` | hard_failure | detected `text_equals='HTTP 500 - Internal Server Error'` at step 1 | 0 |

Three of these deserve a note, because the honest result is not the obvious one.

**`not_found` is not a failure, and the run proves the distinction is real.** The application
answers with an ordinary HTTP 200 page, and the recorded step checkpoint (`url_matches='/member'`)
holds on it -- so the flow only notices at the read, where the balance is missing. The
BUSINESS_OUTCOME rule turns that into `no_such_member` and a normal return with exit code 0.
The same result arrives for a genuinely unknown member with no injection at all, which is the
point: the outcome describes the application's answer, not a trick of the harness.

**`popup` returned success with no recovery, and that is correct here.** Verified directly
rather than assumed: on that page the modal *is* present, reading the balance through it works
(occlusion is not invisibility), and clicking through it is blocked. This capability's only
click -- Search -- happens on the previous page, before the modal exists, so nothing is ever
obstructed and no rule is consulted. The DISMISS recovery and its `recovery_target` are
exercised where the modal genuinely blocks a click, in
`test_dismiss_recovery_uses_the_recorded_recovery_target`.

**`slow` returned success with no recovery, absorbed rather than recovered.** Every response is
delayed five seconds; the run took 10.9s and passed every checkpoint, because waits are
conditions with budgets rather than fixed sleeps. The RECOVERABLE/WAIT rule is the safety net
for a delay that outlasts a step's own budget, which this one does not. That is the designed
behaviour: the distinction between *slow* and *stuck* is a timeout, and nothing here was stuck.

## The defect that shaped the schema: observed data in targeting

Run `135047` finished the flow and produced a valid-looking artifact with two defects:

1. **The success checkpoint hardcodes an observed value** — a `text_equals` whose value is the
   balance literal read from the page. That is one member's balance, so replaying the capability
   for a different member would fail its own success condition. Parameterization only rewrites
   values matching a declared *input*, and a balance is an output, not an input.
2. **A recorded fallback locator embeds the same value** — a `text` locator on the read step
   carrying that same balance literal. This also means a financial value is stored in the
   artifact, the one place the design intends never to hold values.

Both came from the same gap: nothing told the model that observed data must not appear in a
checkpoint or a locator.

**This is now fixed at two layers.** A prompt rule
([`src/agent/prompt.py`](../src/agent/prompt.py), rule 3) asks the model not to do it, and an
Artifact validator ([`src/artifact/schema.py`](../src/artifact/schema.py),
`_reject_observed_data_in_targeting`) refuses to load a recording that does -- because a prompt
rule is advisory and this invariant has to hold whatever a model proposes.

The offending recording is kept as
[`artifacts/rejected_example_hardcoded_checkpoint.json`](../artifacts/rejected_example_hardcoded_checkpoint.json).
It is evidence, not a stale file: it is what a real model actually produced, and it is the
worked example of the validator doing its job. `load_artifact` on it raises, naming the success
checkpoint as asserting a currency amount rather than a structural fact:

```
ValidationError: the success checkpoint asserts what looks like a currency amount rather
than a structural fact. A checkpoint must assert that an element exists or that the page
reached a state -- never the data being extracted ... (value withheld on purpose)
```

Keep it for the regression value: any future change that makes this file load again has broken
the rule.

Getting to a compliant recording took three more runs, and each failure was a layer that was
missing rather than a model that could not cope: the prompt rule steered checkpoints but showed
no locator example (`140922`), diagnosis was impossible without recording the proposed
checkpoint (`140114`), and all-or-nothing rejection threw away four correct steps over one
unused fallback. The recorder now normalizes its own output -- pruning data-shaped candidates
at assembly, `_normalize_candidates` in [`src/agent/loop.py`](../src/agent/loop.py) -- while
the validator stays the unconditional backstop. Run `142524` then came out clean on the first
attempt with nothing to prune.

Step 1 and step 2 are also a duplicated `type` of the same value into the same field. Harmless
on replay, since typing clears the field first, but redundant.
