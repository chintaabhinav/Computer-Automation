"""Human handoff: pause a run, package context for a person, and resume from their decision.

Three pieces. `session.py` holds the control-transfer model, which is the load-bearing part:
exactly one party drives the session at a time, and it is *enforced* -- the policy guard refuses
automation actions while a human holds control, so this is not a flag that every caller is
trusted to check. `request.py` is the document a person receives, redacted and written into the
run's evidence. `operator.py` is how they answer.

WHAT IS MOCKED: the operator interface, which is a terminal prompt. A real deployment replaces
`CliOperatorConsole` with a queue, ticket, or chat approval, and nothing above the
`OperatorConsole` protocol changes.

WHAT IS REAL: the pause; the exclusive control transfer; the session itself, which is never torn
down, so with `--headed` the human works in the same Chromium window on the same page with the
same cookies and form state; the resume; and the recording of the handover -- transfer events,
the operator's note, how long they held control, and the page state before and after.

Escalation attaches at three seams that already existed, which is why wiring it required no new
ones: the discovery agent's `_on_dead_end` hook, the policy guard's confirmation callback, and
a replay hard failure. With no console attached, all three keep their previous fail-closed
behaviour exactly.

LIMITS -- deliberate omissions
==============================

* **No input-level recording of the human's actions.** Evidence captures their note plus a
  redacted before/after page summary, which is the thin-but-real version of "record what the
  human did". A full recording -- their clicks, keystrokes and navigations, as a replayable
  trace -- would be the honest audit trail a regulated deployment needs, and would also be the
  raw material for turning a human's fix into a new recorded step. Not built.
* **No leases or timeouts on control.** `SessionControl` assumes one automation thread and one
  human, and a handover blocks indefinitely. Concurrent operators, or an operator who walks
  away, need leases with expiry and a policy for what happens when one lapses.
* **No operator identity.** Nothing records *which* human took control, because this system has
  no notion of users (see the safety limits). An audit trail that cannot name the person who
  authorized a transfer is not an audit trail.
* **No asynchronous handoff.** The run holds a browser and a process while it waits. A real
  queue-backed deployment would need to suspend the session, persist enough state to resume it
  later, and survive the process exiting -- which is a much harder problem than the pause
  implemented here.
"""
