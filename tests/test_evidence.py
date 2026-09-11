"""Tests for the run recorder, with most of the weight on redaction.

Redaction is the one part of the evidence layer where a bug is not merely inconvenient: a
balance or member id written in the clear cannot be un-written, and evidence directories are
exactly the artifacts people copy into tickets and pull requests. These tests assert the
masking positively (the shape survives) and negatively (the original value is gone).
"""

from __future__ import annotations

import json

from src.evidence import RunRecorder, redact, redact_url, start_run


class _FakeEntry:
    """A transcript entry's shape, without importing the agent -- the recorder is generic."""

    def __init__(self, **fields):
        defaults = dict(
            iteration=0,
            step_index=0,
            action="read",
            url="http://127.0.0.1:5001/member?member_id=12345",
            locator_by="structural",
            locator_value="//td",
            used_fallback=False,
            checkpoint="text_equals='Member Detail'",
            pruned=[],
            checkpoint_passed=True,
            duration_ms=12,
            recorded=True,
            outcome="ok",
            thought="reading the balance",
            extracted_value="$4,200.00",
        )
        self.__dict__.update(defaults | fields)


# ---------------------------------------------------------------------------------------
# Redaction.
# ---------------------------------------------------------------------------------------


def test_masks_currency_but_keeps_its_shape():
    """A reviewer should still see 'a thousands-scale amount was read' -- and not the figure."""
    assert redact("$4,200.00") == "$4,***.**"
    assert redact("Balance: $18,750.35 available") == "Balance: $1*,***.** available"
    assert "4,200.00" not in redact("the balance was $4,200.00")


def test_masks_identifiers_keeping_first_and_last_digit():
    """Enough to tell two ids apart in a log; not enough to be one."""
    assert redact("12345") == "1***5"
    assert redact("member 67890 opened") == "member 6***0 opened"


def test_leaves_short_and_structural_numbers_alone():
    """Over-masking makes evidence useless; a row count is not account data."""
    assert redact("row 3 of 12") == "row 3 of 12"
    assert redact("http://localhost:5001/") == "http://localhost:5001/"


def test_redacts_nested_structures():
    """Callers hand over whole event dicts without knowing which fields are sensitive."""
    masked = redact({"inputs": {"member_id": "12345"}, "rows": ["$312.09", 7]})

    assert masked == {"inputs": {"member_id": "1***5"}, "rows": ["$3**.**", 7]}


def test_redact_url_masks_the_query_but_keeps_the_location():
    """Where the run was is operational context; what was passed to it is data."""
    masked = redact_url("http://127.0.0.1:5001/member?member_id=12345&inject=slow")

    assert masked.startswith("http://127.0.0.1:5001/member?")
    assert "12345" not in masked
    assert "inject=slow" in masked


def test_redact_url_passes_through_plain_urls():
    assert redact_url("http://127.0.0.1:5001/") == "http://127.0.0.1:5001/"


def test_redaction_is_lossy_and_one_way():
    """Distinct values of the same shape collapse to one mask: there is nothing to reverse.

    The leading digit survives deliberately -- it is what makes "$4,***.**" more useful to a
    reviewer than "$*,***.**" -- so the collision is among values sharing it.
    """
    assert redact("$4,200.00") == redact("$4,999.99") == "$4,***.**"
    assert redact("12345") == redact("19995") == "1***5"


# ---------------------------------------------------------------------------------------
# Recording.
# ---------------------------------------------------------------------------------------


def test_start_run_creates_a_timestamped_directory(tmp_path):
    run_id = start_run("discovery", root=tmp_path)

    assert run_id.startswith("discovery-")
    assert (tmp_path / run_id / "screenshots").is_dir()


def test_events_are_appended_one_json_object_per_line(tmp_path):
    """Line-per-event and flushed immediately, so a killed run still leaves its history."""
    recorder = RunRecorder.start("discovery", root=tmp_path)

    recorder.event("run_started", goal="look up 12345")
    recorder.event("step", action="read")

    lines = recorder.events_path.read_text().strip().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["seq"] == 1 and second["seq"] == 2
    assert first["goal"] == "look up 1***5", "event fields must be redacted on the way to disk"


def test_step_event_redacts_the_extracted_value_and_url(tmp_path):
    """The headline guarantee: a financial value read from the page never lands raw on disk."""
    recorder = RunRecorder.start("discovery", root=tmp_path)

    recorder.step_event(_FakeEntry())

    written = recorder.events_path.read_text()
    assert "$4,200.00" not in written
    assert "12345" not in written
    assert "$4,***.**" in written
    assert "member_id=1***5" in written


def test_summary_records_the_outcome_and_is_written_last(tmp_path):
    recorder = RunRecorder.start("discovery", root=tmp_path)
    recorder.event("step", action="click")

    path = recorder.finish(kind="discovery", stop_reason="success", steps=4)
    summary = json.loads(path.read_text())

    assert summary["run_id"] == recorder.run_id
    assert summary["stop_reason"] == "success"
    assert summary["events"] == 1
    assert summary["duration_s"] >= 0


def test_a_failed_screenshot_does_not_break_the_run(tmp_path):
    """Evidence collection must never cause the incident it exists to document."""

    class BrokenSurface:
        def screenshot(self, path):
            raise RuntimeError("browser already closed")

    recorder = RunRecorder.start("discovery", root=tmp_path)

    assert recorder.screenshot(BrokenSurface(), "failure") is None
    assert "screenshot_failed" in recorder.events_path.read_text()


def test_two_runs_in_the_same_second_get_separate_directories(tmp_path):
    """Replays finish in milliseconds, and a shared directory interleaves two runs' records.

    Found in practice: two scenario replays landed in the same second and appended their events
    to one run.jsonl, mixing a success and a business outcome in the file a reviewer would use
    to tell them apart.
    """
    first = start_run("replay", root=tmp_path)
    second = start_run("replay", root=tmp_path)
    third = start_run("replay", root=tmp_path)

    assert len({first, second, third}) == 3
    assert (tmp_path / second).is_dir() and (tmp_path / third).is_dir()


def test_two_recorders_started_together_do_not_share_an_events_file(tmp_path):
    one = RunRecorder.start("replay", root=tmp_path)
    two = RunRecorder.start("replay", root=tmp_path)

    one.event("step", action="click")
    two.event("step", action="read")

    assert one.events_path != two.events_path
    assert len(one.events_path.read_text().strip().splitlines()) == 1
