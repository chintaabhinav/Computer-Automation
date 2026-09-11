"""Run recorder: what happened, in a form a human can audit and a machine can diff.

Both discovery and replay write here, in the same shape, because the question asked of them
afterwards is the same one -- what did the automation do, on which elements, and did it work?
A run leaves three things behind: `run.jsonl` (one line per event, appended as the run
happens, so a crashed run still leaves everything up to the moment it died), `summary.json`
(the outcome at a glance), and `screenshots/` (what the page actually looked like).

REDACTION. Every value routed to disk passes through `redact()`. This is the persistence
boundary for sensitive data, and it is here now -- before the safety layer exists -- because
evidence is written on the very first run, and a balance or member id written in the clear
cannot be un-written. Two rules hold across the whole system:

* The artifact never stores values at all. It records the *shape* of a flow -- locators,
  checkpoints, field names -- so there is nothing sensitive in it to leak. Typed inputs are
  recorded as `{{parameter}}` placeholders, not literals.
* Logs and evidence store only masked values. A reviewer can confirm that a balance was read
  and that it looked like a currency amount, without the evidence directory becoming a place
  where account data lives.

Masking is deliberately lossy and one-way: there is no un-redact, because a reversible scheme
would just be encryption with the key in the repository.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

EVIDENCE_ROOT = Path("evidence")
RunKind = Literal["discovery", "replay"]

_CURRENCY_RE = re.compile(r"[$£€]?\d[\d,]*\.\d{2}")
"""Currency-shaped amounts. Matched before bare digit runs so '4,200.00' is masked as one
amount rather than mangled piecewise."""

_DIGIT_RUN_RE = re.compile(r"(?<!:)\b\d{4,}")
"""Runs of four or more digits: member ids, account numbers, reference numbers.

Four is the threshold because shorter runs are usually structural (a year, a row count) and
masking them would make the evidence unreadable without protecting anything. A run directly
after a colon is skipped for the same reason -- that is a port, and turning every URL into
`localhost:5**1` costs legibility while hiding nothing: the data in a URL lives in the query
string, which `redact_url` masks in full.
"""


def redact(value: Any) -> Any:
    """Mask anything that looks like money or an identifier, preserving its shape.

    Shape is preserved on purpose: "$4,***.**" still tells a reviewer that a currency amount in
    the thousands was read from the page, which is what makes the evidence useful for debugging
    a locator. The actual figure is gone and cannot be recovered.

    Non-strings pass through unchanged, and structures are masked recursively, so callers can
    hand this a whole event dict without first knowing which fields are sensitive.
    """
    if isinstance(value, str):
        masked = _CURRENCY_RE.sub(_mask_currency, value)
        return _DIGIT_RUN_RE.sub(_mask_digits, masked)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _mask_currency(match: re.Match[str]) -> str:
    """'$4,200.00' -> '$4,***.**': keep the symbol and leading digit, mask the rest."""
    text = match.group(0)
    out, seen_digit = [], False
    for char in text:
        if char.isdigit():
            out.append(char if not seen_digit else "*")
            seen_digit = True
        else:
            out.append(char)
    return "".join(out)


def _mask_digits(match: re.Match[str]) -> str:
    """'12345' -> '1***5': keep the first and last digit so a reviewer can still tell ids apart."""
    digits = match.group(0)
    return digits[0] + "*" * (len(digits) - 2) + digits[-1]


def redact_url(url: str) -> str:
    """Mask query-string values but leave the location readable.

    Split deliberately: the host and path are operational facts a reviewer needs ("the run was
    on the member detail page"), while query parameters are where the data hides -- on this
    target, `?member_id=12345` is the member id in the clear.
    """
    if "?" not in url:
        return url
    location, _, query = url.partition("?")
    return f"{location}?{redact(query)}"


def start_run(kind: RunKind, root: Path | str = EVIDENCE_ROOT) -> str:
    """Create a fresh evidence directory for a new run and return its id.

    The id leads with a timestamp so runs sort chronologically and read well, and it is the same
    string used as the artifact's `discovery_run_id` -- which is what links a recording back to
    the evidence that justifies it.

    A suffix is appended when that name is already taken. Not hypothetical: replays finish in
    well under a second, and exercising a handful of scenarios in a loop produced two runs in the
    same second that then appended their events to one `run.jsonl`. Two runs sharing an evidence
    directory is worse than a clumsy name -- it silently interleaves the record of a success and
    a failure, which is exactly the record someone reaches for when trying to tell them apart.
    """
    stamp = f"{kind}-{datetime.now():%Y%m%d-%H%M%S}"
    run_id = stamp
    attempt = 2
    while (Path(root) / run_id).exists():
        run_id = f"{stamp}-{attempt}"
        attempt += 1
    (Path(root) / run_id / "screenshots").mkdir(parents=True, exist_ok=True)
    return run_id


class RunRecorder:
    """Writes one run's evidence: events as they happen, then a summary at the end."""

    def __init__(self, run_id: str, root: Path | str = EVIDENCE_ROOT) -> None:
        self.run_id = run_id
        self.dir = Path(root) / run_id
        self.screenshots = self.dir / "screenshots"
        self.screenshots.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "run.jsonl"
        self.summary_path = self.dir / "summary.json"
        self.started = datetime.now(UTC)
        self._event_count = 0

    @classmethod
    def start(cls, kind: RunKind, root: Path | str = EVIDENCE_ROOT) -> RunRecorder:
        """Begin a new run and return a recorder bound to it."""
        return cls(start_run(kind, root), root)

    def event(self, event_type: str, **fields: Any) -> None:
        """Append one redacted JSON line.

        Flushed per line rather than buffered: evidence is most valuable for the runs that end
        badly, and a buffer that dies with the process would lose exactly those.
        """
        self._event_count += 1
        record = {
            "seq": self._event_count,
            "ts": datetime.now(UTC).isoformat(),
            "event": event_type,
            **redact(fields),
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def step_event(self, entry: Any) -> None:
        """Record one discovery iteration from its transcript entry.

        Takes the entry structurally rather than importing the agent's type, so the recorder
        stays usable by replay -- which will hand it a differently-shaped record for the same
        fields.
        """
        self.event(
            "step",
            iteration=entry.iteration,
            step_index=entry.step_index,
            action=entry.action,
            url=redact_url(entry.url),
            locator_by=entry.locator_by,
            locator_value=entry.locator_value,
            used_fallback=entry.used_fallback,
            checkpoint=entry.checkpoint,
            pruned=[
                {"role": item.role, "matched": item.matched} for item in entry.pruned
            ],
            checkpoint_passed=entry.checkpoint_passed,
            duration_ms=entry.duration_ms,
            recorded=entry.recorded,
            outcome=entry.outcome,
            thought=entry.thought,
            extracted=entry.extracted_value,
        )

    def screenshot(self, surface: Any, name: str) -> Path | None:
        """Capture the page, never letting a capture failure take down the run.

        Evidence collection must not be able to cause the incident it is meant to document, so a
        browser that has already crashed simply yields no screenshot and a logged warning.
        """
        destination = self.screenshots / f"{name}.png"
        try:
            surface.screenshot(destination)
            return destination
        except Exception as exc:  # noqa: BLE001 - deliberately broad; see docstring
            log.warning("could not capture screenshot %s: %s", name, exc)
            self.event("screenshot_failed", name=name, error=str(exc))
            return None

    def finish(self, **summary: Any) -> Path:
        """Write summary.json and return its path.

        The summary is written last and in one shot, so its presence is itself the signal that a
        run completed rather than being killed midway.
        """
        finished = datetime.now(UTC)
        payload = {
            "run_id": self.run_id,
            "started_at": self.started.isoformat(),
            "finished_at": finished.isoformat(),
            "duration_s": round((finished - self.started).total_seconds(), 2),
            "events": self._event_count,
            **redact(summary),
        }
        self.summary_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return self.summary_path
