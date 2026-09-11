# computer-use-automation

## 1. What this is

A system for automating a web application that has no API, on the record-once/replay-many model.
An LLM agent drives the UI to accomplish a goal in plain language, and the run is recorded as a
typed **capability artifact** — steps, locators with fallbacks, checkpoints, declared inputs and
outputs, and per-step risk. Replay then re-runs that artifact deterministically with **no model
in the loop**: a recording costs about $0.013 and five model round-trips, and every replay
afterwards costs $0.00 and takes under a second.

The design reasoning, the runs that shaped it, and the honest limits are in
[REPORT.md](REPORT.md).

## 2. Setup

Requires **Python 3.11+** (the schema uses `StrEnum`); verified on 3.13.5.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

`.env` holds one value, `ANTHROPIC_API_KEY=`. Get a key from
<https://console.anthropic.com/settings/keys>.

**The key is needed only for a real discovery run.** Replay, the entire test suite, and
`discover --stub` need no key and cost nothing — which is most of what a reviewer wants to see.
A discovery artifact is already committed at
[`artifacts/lookup_member_balance.json`](artifacts/lookup_member_balance.json), so you can skip
step 4c entirely and still run every replay below.

## 3. Run the mock app

```bash
python mock_app/app.py        # serves http://127.0.0.1:5001
```

Leave it running in its own terminal. It is a fake credit-union back office serving entirely
fabricated members, and it is **deliberately hostile to automation**: table-based layout, inline
styles, `<font>` tags, no test IDs, no `data-*` attributes, and the savings balance buried inside
a nested table so it cannot be reached by a lucky selector. Elements are identifiable only by
visible text, form labels, or document structure — which is the perception problem a real legacy
application actually poses.

Any URL accepts `?inject=<state>` to force an exceptional condition:
`not_found` (a "No such member" business result, HTTP 200), `slow` (~5s delay),
`popup` (a modal that blocks clicks), `session_expired`, and `server_error` (HTTP 500).

## 4. Demo path

Run these in order. Each line notes what it proves.

**a. Start the target** (separate terminal, as above):

```bash
python mock_app/app.py
```

**b. Zero-cost discovery** — the full loop against a scripted model, no key, nothing billed:

```bash
python cli.py discover \
  --goal "Look up the member given as input and read their current savings balance" \
  --url http://localhost:5001 --input member_id=12345 --stub \
  --out artifacts/dryrun_lookup_member_balance.json
```

```
  ok  0 navigate  ok
  ok  1 type      ok
  ok  2 click     ok
  ok  3 read      ok
  --  4 done      done, verified
stop reason : success
steps       : 4 recorded / 5 iterations
```

**c. Real discovery** — needs the key, costs about $0.013. Optional; the artifact is committed:

```bash
python cli.py discover \
  --goal "Look up the member given as input and read their current savings balance" \
  --url http://localhost:5001 --input member_id=12345 \
  --out artifacts/lookup_member_balance.json
```

**d. Replay it** — the production path, no model, under a second:

```bash
python cli.py replay --artifact artifacts/lookup_member_balance.json --input member_id=12345
```

```json
{
  "status": "success",
  "outcome": "success",
  "outputs": {
    "current_savings_balance": "$4,200.00"
  },
  "failure": null,
  "run_id": "replay-20260911-164225",
  "duration_s": 0.689,
  "steps_executed": 4,
  "recoveries": []
}
```

**e. Replay for members discovery never saw** — the recording holds `{{member_id}}` and no trace
of any particular member, so it generalizes:

```bash
python cli.py replay --artifact artifacts/lookup_member_balance.json --input member_id=67890
python cli.py replay --artifact artifacts/lookup_member_balance.json --input member_id=24680
```

Returns `$18,750.35` and `$312.09` respectively.

**f. A business outcome, which is not a failure** — the application answered, and the answer was
"no such member". Exit code 0:

```bash
python cli.py replay --artifact artifacts/lookup_member_balance.json --input member_id=12345 --inject not_found
python cli.py replay --artifact artifacts/lookup_member_balance.json --input member_id=99999
```

```json
{
  "status": "business_outcome",
  "outcome": "no_such_member",
  "outputs": {},
  "failure": null,
  "steps_executed": 3,
  "recoveries": []
}
```

The second command uses a genuinely unknown member with no injection and returns the same
outcome — the result describes the application's answer, not a trick of the harness.

**g. A hard failure with a populated diagnosis** — expected versus observed, captured while the
page was still failing:

```bash
python cli.py replay --artifact artifacts/lookup_member_balance.json --input member_id=12345 --inject session_expired
```

```json
{
  "status": "hard_failure",
  "outcome": null,
  "failure": {
    "step_index": 1,
    "action": "type",
    "expected": "type to succeed",
    "observed": "url=http://localhost:5001/?inject=session_expired title='NMCU Back Office - Session Expired' ...",
    "message": "a recorded hard-failure condition was detected (text_equals='Your session has expired')"
  }
}
```

Try `--inject popup`, `--inject slow` and `--inject server_error` too; `evidence/README.md`
explains why popup and slow both succeed.

**h. Human-in-the-loop escalation** — a step with a real side effect, approved by a person in the
same browser window:

```bash
python cli.py replay --artifact artifacts/open_sub_account.json --input member_id=12345 \
  --policy config/policy-confirm.yaml --operator cli --headed
```

The automation opens Chromium, navigates to the member, and stops before clicking "Open
Sub-Account" because that step is recorded as `RISKY`. Control is ceded to you exclusively — the
policy guard refuses automation actions until you hand it back — and the window in front of you
is the automation's own session. Type `resume` to approve and the run continues to `/confirm`; the
transfer, your note, and how long you held control are written to the run's evidence. Without
`--operator`, the same command exits 3: `CONFIRM` with nobody to ask is a denial.

## 5. Running without live services

No API key is required for any of this.

```bash
pytest                          # 161 tests, ~50s (drives a real browser against the mock app)
pytest -m "not integration"     # 104 tests, under a second, no browser needed
```

The integration tests start the mock app themselves as a subprocess, so `pytest` works whether or
not you have it running. For the discovery loop without a key, use `discover --stub` as in 4b.

One flake to know about: `test_wait_for_outlasts_the_injected_slow_page` asserts a timing lower
bound against the 5-second injected delay, and it failed once under heavy contention while
several slow-inject replays were running alongside the suite. Three consecutive clean full runs
followed. If you see it, re-run it alone.

## 6. Repo layout

| Path | Responsibility |
| --- | --- |
| [`src/surface/`](src/surface/) | The perceive-act seam: the `Surface` ABC and neutral `Observation`, plus the Playwright implementation — the only file that imports Playwright |
| [`src/agent/`](src/agent/) | Discovery: the model boundary, the prompt, and the loop that records verified steps |
| [`src/artifact/`](src/artifact/) | The capability contract — typed schema, cross-field validators, JSON store |
| [`src/replay/`](src/replay/) | Deterministic execution and the result contract; imports neither the agent nor an LLM SDK |
| [`src/safety/`](src/safety/) | Policy: URL allowlist, action allowlist, risky-action modes, enforced by a surface wrapper |
| [`src/escalation/`](src/escalation/) | Human handoff: exclusive control transfer, intervention requests, operator consoles |
| [`src/evidence.py`](src/evidence.py) | The run recorder and the one redaction function every layer reuses |
| [`mock_app/`](mock_app/) | The deliberately legacy-flavoured target application |
| [`artifacts/`](artifacts/) | Recorded capabilities, including a preserved rejected one |
| [`evidence/`](evidence/) | Every run's events, summary, and screenshots — see its README |
| [`config/`](config/) | `policy.yaml` (default, risky actions blocked) and `policy-confirm.yaml` |
| [`scripts/`](scripts/) | Post-discovery authoring: error rules, and the hand-authored risky capability |
| [`tests/`](tests/) | 161 tests; `-m "not integration"` selects the browser-free 104 |

## 7. Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success, **or** a business outcome — the automation worked and the answer may be negative |
| 1 | Hard failure — the run could not legitimately continue |
| 2 | Bad request — missing input, unloadable artifact, target not running |
| 3 | Policy refusal or control violation — the action was not permitted, so it never ran |

A business outcome exits 0 deliberately. Treating "no such member" as an error trains operators
to ignore errors.

## 8. Configuration

[`config/policy.yaml`](config/policy.yaml) is enforced on every navigation, click, keystroke and
read. It controls which URLs may be visited (`allowed_url_patterns`, globs or `re:`-prefixed
regexes matched against the full URL), which action types are permitted at all
(`allowed_actions`), and what happens to a step declared `RISKY` (`risky_action_mode`:
`block`, `confirm`, or `flag`). Defaults fail closed: an unmatched URL is denied, an unlisted
action is denied, risk that was never declared is treated as risky, and a missing policy file
raises rather than defaulting open.

Both subcommands take `--policy <path>` (default `config/policy.yaml`) and `--operator
<cli|auto-approve|auto-abort|none>` (default `none`).

`--unsafe` **bypasses the guard entirely** — no allowlist, no risky-action policy, no decision
log — and sends every action straight to the browser. It prints a warning and exists for
debugging only. Do not use it against anything you care about.

## 9. Evidence

[`evidence/README.md`](evidence/README.md) indexes every run this system has performed: ten
discovery runs **including the seven that failed**, each of which named a real defect that the
next run fixed, and ten replay runs covering the clean path and all five injected error states.
It also explains the preserved
[`artifacts/rejected_example_hardcoded_checkpoint.json`](artifacts/rejected_example_hardcoded_checkpoint.json)
— a recording a successful discovery run produced that the schema validator now refuses, kept as
the worked example of that enforcement.

Values are redacted on the way to disk: balances become `$4,***.**`, identifiers `1***5`, and URL
query strings are masked while the location stays readable. Screenshots are **not** redacted —
text masking cannot touch pixels — which is acceptable only because this target serves fabricated
data.
