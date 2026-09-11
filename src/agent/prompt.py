"""Prompt construction for the discovery agent, kept apart from the loop it feeds.

Prompting is the part of this system most likely to be tuned, and tuning it should never mean
editing control flow. Isolating it here means the prompting strategy can be read, diffed, and
A/B'd on its own, and the loop's correctness does not depend on the wording.

Two principles govern what goes in: the model is shown *state*, not markup -- it receives the
surface's neutral `Observation`, so a desktop surface would produce the same prompt shape -- and
the prompt stays lean, because every discovery turn resends the whole thing.
"""

from __future__ import annotations

from src.artifact.schema import ActionType, Step
from src.surface.base import Observation

SYSTEM_PROMPT = """\
You are a UI automation discovery agent. You are driving a legacy web application one action at \
a time to accomplish a goal, and recording a reusable automation as you go.

Two things matter more than finishing fast:

1. DURABLE TARGETING. The application has no test ids and no data attributes. Prefer a form \
label, then visible text, then an accessibility role. Use CSS or structural paths only when \
nothing else identifies the element. Always give at least one fallback when you can, and state \
in `rationale` why your targeting should still work after the application changes.
2. VERIFIABLE PROGRESS. Whenever an action changes what the screen shows -- navigating, \
submitting a form, dismissing a dialog -- give a `checkpoint` describing what will be observably \
true once it worked, and prefer `text_equals` against text the new page displays. When an action \
changes nothing observable, such as typing into a field, set `checkpoint` to null rather than \
inventing one. An action whose checkpoint fails is discarded, not recorded, so a checkpoint that \
cannot be observed costs you the step.

3. NEVER TARGET OR ASSERT THE DATA ITSELF. Checkpoint values and locator values must describe \
structure -- an element that exists, a page that was reached, a row heading -- and must never \
contain a value read off the page, above all a value you are extracting as an output. That value \
differs on every run, so an artifact containing it works once and fails for every other input.
   WRONG checkpoint: {"kind": "text_equals", "value": "$4,200.00"}
   RIGHT checkpoint: {"kind": "text_equals", "value": "Savings Balance"}
This applies to EVERY locator you write, including fallbacks. A fallback that finds the element \
by the amount it currently displays is worthless: on the next run the amount is different.
   WRONG fallback:   {"by": "text", "value": "$4,200.00"}
   RIGHT fallback:   {"by": "structural", "value": "//td[normalize-space()='Available']/following-sibling::td[1]"}
This is rejected by the artifact schema, not merely discouraged: a recording that targets or \
asserts its own extracted data is thrown away in full, losing every step you got right.

4. FINISH WHEN YOU ARE DONE. ALREADY CAPTURED lists the values you have successfully read. As \
soon as it holds everything the goal asks for and the goal is satisfied, answer with action \
"done" and a `checkpoint` for the overall success condition. Never re-read a value that is \
already listed there -- reading it again makes no progress and burns a step.

5. NEVER REDO COMPLETED WORK. FIELDS FILLED lists every field you have already typed into, with \
the value that went in. Do not type into a field listed there again -- the value is already in \
the form, so re-entering it wastes a step and records a duplicate action in the automation.

Mark an action `risky` when it changes state in the target system -- opening an account, \
submitting a transaction, sending anything. Reads, searches and navigation are safe.

Answer with a single JSON object and nothing else."""

RESPONSE_FORMAT = """\
Respond with ONLY this JSON object:

{
  "thought": "one short sentence",
  "action": "navigate|click|type|read|wait|dismiss|done|stuck",
  "target": {
    "primary":   {"by": "label|text|role|css|structural", "value": "..."},
    "fallbacks": [{"by": "...", "value": "..."}],
    "rationale": "why this targeting is durable"
  },
  "value": "URL for navigate, text for type, else null",
  "extract": "output field name for read, else null",
  "checkpoint": {"kind": "element_exists|url_matches|text_equals|all_of|any_of",
                 "value": "...", "children": null},
  "risk": "safe|risky"
}

Rules: omit or null any field that does not apply. `target` is required for click, type, read \
and dismiss. `value` is required for navigate and type. `extract` is required for read. \
Use "done" when the goal is met, with `checkpoint` set to the overall success condition. Use \
"stuck" only when no action could make progress.

`by` must be exactly one of: label, text, role, css, structural. No other value is accepted.

A locator `value` is also matched mechanically. Note especially that CURRENT STATE shows you \
accessibility roles ("cell", "textbox"), but css and structural values are matched against the \
page's HTML tags, so write td/tr/input -- never //cell or [name=...]:

  label      -- the exact visible label text, e.g. "Member ID"
  text       -- the exact visible text, e.g. "Search". Must match ONE element: a value that
                appears twice on the page (the same amount in two rows) will be refused.
  css        -- a CSS selector over HTML, e.g. "input[value='Search']"
  structural -- an XPath over HTML. To read a value that has no label of its own, address it
                through its row heading, e.g.
                //td[normalize-space()='Member Name']/following-sibling::td[1]
                and for a value inside a nested table, continue into it, e.g.
                //td[normalize-space()='Savings Balance']/following-sibling::td[1]
                //tr[td[normalize-space()='Available']]/td[2]

A checkpoint `value` is matched mechanically, never interpreted, so it must be an expression \
and never a description:

  text_equals    -- visible text that will appear on the page, e.g. "Member Detail"
  url_matches    -- a fragment of the resulting URL, e.g. "/member"
  element_exists -- a CSS selector or an XPath, e.g. "input[value='Search']"
  all_of/any_of  -- use `children` (a list of checkpoints) and leave `value` null

"a textbox containing the member id" is NOT a valid checkpoint. "Member Detail" is."""


def build_system_prompt() -> str:
    """The standing instructions. Stable across turns, which also makes it cacheable."""
    return SYSTEM_PROMPT


def build_user_prompt(
    goal: str,
    inputs: dict[str, str],
    observation: Observation,
    steps_so_far: list[Step],
    last_failure: str | None = None,
    entry_url: str | None = None,
) -> str:
    """Assemble the per-turn prompt: goal, inputs, current state, history, and what to return.

    `last_failure` is how a failed attempt re-enters the conversation. Feeding the failure back
    as ordinary context -- rather than crashing, or silently retrying the same thing -- is what
    lets the model correct its own targeting, and it costs one line in the prompt.
    """
    sections = [
        f"GOAL: {goal}",
        # Without this the agent is genuinely stuck on turn one: it starts with nothing open,
        # and no amount of reasoning can produce a URL it was never told. A real discovery run
        # reported exactly that before this line existed.
        f"TARGET APPLICATION: {entry_url}\n"
        "Navigate here first if nothing is open yet." if entry_url else "",
        _render_inputs(inputs),
        _render_history(steps_so_far),
        "CURRENT STATE:",
        observation.to_prompt_text(),
    ]
    if last_failure:
        sections.append(f"THE PREVIOUS ATTEMPT FAILED: {last_failure}\nTry a different approach.")
    sections.append(RESPONSE_FORMAT)
    return "\n\n".join(section for section in sections if section)


def _render_inputs(inputs: dict[str, str]) -> str:
    """List the declared inputs with their values.

    The values are shown because the model has to type them into the page; the *names* are
    shown because the loop rewrites any typed value that matches one back into a
    `{{name}}` placeholder, which is what makes the recording reusable.
    """
    if not inputs:
        return "INPUTS: none"
    lines = [f"  {name} = {value!r}" for name, value in inputs.items()]
    return "INPUTS (type these values where the page asks for them):\n" + "\n".join(lines)


def _render_history(steps: list[Step]) -> str:
    """One compact line per recorded step, plus what has already been captured.

    Only verified steps appear, and only their essentials. The model does not need its own
    earlier reasoning replayed to it -- that is in the transcript, as evidence -- and resending
    it every turn would grow the prompt without improving the next decision.

    ALREADY CAPTURED and FIELDS FILLED are separated out because burying completed state in the
    step list did not work. Twice over, the model failed to notice work it had already done: in
    one run it read the balance correctly on step 3 and then read it twelve more times until the
    step budget ran out, and in two consecutive runs it typed the member id into the same field
    twice, its own reasoning even noting the value "has already been typed twice".

    The step lines did mention both actions. That turned out not to be enough -- a list of
    things done reads as history, not as state. An explicit answer to "what do I already have?"
    and "what have I already entered?" is what the model can act on, which is why each gets its
    own labelled line.
    """
    if not steps:
        return (
            "STEPS RECORDED SO FAR: none\n"
            "ALREADY CAPTURED: nothing yet\n"
            "FIELDS FILLED: nothing yet"
        )
    lines = []
    for step in steps:
        parts = [f"  {step.index}. {step.action}"]
        if step.target is not None:
            parts.append(f"{step.target.primary.by}={step.target.primary.value!r}")
        if step.value is not None:
            parts.append(f"value={step.value!r}")
        if step.extract:
            parts.append(f"-> {step.extract}")
        lines.append(" ".join(parts))

    captured = list(dict.fromkeys(step.extract for step in steps if step.extract))
    captured_line = (
        "ALREADY CAPTURED: " + ", ".join(captured) if captured else "ALREADY CAPTURED: nothing yet"
    )

    # Keyed by the field's own locator value, so filling the same field twice collapses to one
    # entry -- the point is the field's current state, not a log of attempts.
    filled: dict[str, str] = {}
    for step in steps:
        if step.action is ActionType.TYPE and step.target is not None:
            filled[step.target.primary.value] = step.value or ""
    filled_line = (
        "FIELDS FILLED: " + ", ".join(f"{field} = {value}" for field, value in filled.items())
        if filled
        else "FIELDS FILLED: nothing yet"
    )

    return (
        "STEPS RECORDED SO FAR:\n"
        + "\n".join(lines)
        + "\n"
        + captured_line
        + "\n"
        + filled_line
    )
