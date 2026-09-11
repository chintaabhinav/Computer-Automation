"""Integration tests for the web surface, driven against the real mock app in a real browser.

These are deliberately not mocked. The surface exists to absorb the messiness of a legacy page
-- nested tables, no test ids, elements that match several times -- and a fake Playwright would
assert only that the code calls the methods we already believe it calls. The mock app is served
as a subprocess so every assertion here is about markup a browser actually rendered.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.artifact.schema import Checkpoint, CheckpointKind, Locator, LocatorBy, LocatorRule
from src.surface.base import ElementNotFoundError, Observation
from src.surface.web import WebSurface

pytestmark = pytest.mark.integration

pytest.importorskip("playwright", reason="playwright is required for surface integration tests")

# The balance is one table deep with no label of its own: reach it through the outer row
# heading, then the 'Available' row of the nested balance table.
BALANCE_XPATH = (
    "//td[normalize-space()='Savings Balance']"
    "/following-sibling::td[1]//tr[td[normalize-space()='Available']]/td[2]"
)


@pytest.fixture
def member_page(surface: WebSurface, mock_app: str) -> WebSurface:
    """A surface sitting on the detail page for member 12345 (Jane Doe, $4,200.00)."""
    surface.open(f"{mock_app}/member?member_id=12345")
    return surface


def _locator(by: LocatorBy, value: str, *fallbacks: LocatorRule) -> Locator:
    return Locator(
        primary=LocatorRule(by=by, value=value),
        fallbacks=list(fallbacks),
        rationale="test locator",
    )


# ---------------------------------------------------------------------------------------
# Targeting.
# ---------------------------------------------------------------------------------------


def test_finds_member_id_field_by_label(surface: WebSurface, mock_app: str):
    """The search field has no test id; its visible label is the durable way in."""
    surface.open(mock_app)

    handle = surface.find(_locator(LocatorBy.LABEL, "Member ID"))

    assert handle is not None
    assert handle.rule.by is LocatorBy.LABEL
    assert handle.was_fallback is False
    assert surface.fallback_events == []


def test_typing_by_label_reaches_the_real_input(surface: WebSurface, mock_app: str):
    """Resolution is only useful if the element it returns is the one that accepts input."""
    surface.open(mock_app)

    surface.type(_locator(LocatorBy.LABEL, "Member ID"), "12345")

    assert surface.page.input_value("#mid") == "12345"


def test_falls_back_to_css_and_records_the_drift(surface: WebSurface, mock_app: str):
    """A dead primary must not fail the step -- but it must be recorded as a drift signal."""
    surface.open(mock_app)
    locator = Locator(
        primary=LocatorRule(by=LocatorBy.LABEL, value="Membership Number"),  # never matches
        fallbacks=[LocatorRule(by=LocatorBy.CSS, value="form input[type=text]")],
        rationale="label is preferred; the form has exactly one text input as a backstop",
    )

    handle = surface.find(locator)

    assert handle is not None
    assert handle.was_fallback is True
    assert handle.candidate_index == 1
    assert handle.rule.by is LocatorBy.CSS

    assert len(surface.fallback_events) == 1
    event = surface.fallback_events[0]
    assert event.primary.value == "Membership Number"
    assert event.used.by is LocatorBy.CSS
    assert "backstop" in event.rationale


def test_find_returns_none_when_no_candidate_resolves(surface: WebSurface, mock_app: str):
    """Probing for an absent element is a question, not an error."""
    surface.open(mock_app)

    assert surface.find(_locator(LocatorBy.TEXT, "Wire Transfer Approval")) is None


def test_acting_on_an_unresolvable_locator_raises(surface: WebSurface, mock_app: str):
    """Acting is different: silently doing nothing would be worse than stopping."""
    surface.open(mock_app)

    with pytest.raises(ElementNotFoundError, match="cannot click"):
        surface.click(_locator(LocatorBy.TEXT, "Wire Transfer Approval"))


def test_ambiguous_locator_is_rejected_rather_than_guessed(member_page: WebSurface):
    """Several visible matches means the recording is underspecified; picking one is a hazard."""
    # The detail page has many table cells; a bare 'td' matches all of them.
    assert member_page.find(_locator(LocatorBy.CSS, "td")) is None


def test_reads_nested_table_balance_via_structural_locator(member_page: WebSurface):
    """The headline case: a value buried a table deep, with no label of its own."""
    balance = member_page.read(_locator(LocatorBy.STRUCTURAL, BALANCE_XPATH))

    assert balance == "$4,200.00"


def test_structural_locator_distinguishes_available_from_ledger(
    member_page: WebSurface, mock_app: str
):
    """Proof the nested-table path is precise, not just landing on the first amount it sees."""
    member_page.open(f"{mock_app}/member?member_id=67890")
    available = member_page.read(_locator(LocatorBy.STRUCTURAL, BALANCE_XPATH))

    assert available == "$18,750.35"  # ledger is $18,900.35 in the row below


def test_reads_member_name_by_structural_row_heading(member_page: WebSurface):
    """The same row-heading pattern generalizes to the flat part of the detail table."""
    name = member_page.read(
        _locator(
            LocatorBy.STRUCTURAL,
            "//td[normalize-space()='Member Name']/following-sibling::td[1]",
        )
    )

    assert name == "Jane Doe"


# ---------------------------------------------------------------------------------------
# Perception.
# ---------------------------------------------------------------------------------------


def test_observe_returns_a_neutral_capped_observation(member_page: WebSurface):
    """The agent's whole view of the world: semantic, small, and free of markup."""
    observation = member_page.observe()

    assert isinstance(observation, Observation)
    assert observation.url.endswith("/member?member_id=12345")
    assert "Member Detail" in observation.title
    assert observation.elements

    rendered = observation.to_prompt_text()
    assert "<table" not in rendered and "bgcolor" not in rendered, "must not leak raw markup"
    assert "Jane Doe" in rendered, "the page's actual content should be perceivable"
    assert all(len(element.text) <= member_page.max_text_chars for element in observation.elements)


def test_observe_respects_the_element_cap(member_page: WebSurface):
    """The cap is a prompt-cost guarantee, so it must hold on a cell-heavy legacy page."""
    member_page.max_elements = 5
    observation = member_page.observe()

    assert len(observation.elements) <= 5
    assert observation.truncated is True


def test_observed_refs_are_unique(member_page: WebSurface):
    """Refs are how the agent points at one element unambiguously; collisions would mislead it."""
    observation = member_page.observe()
    refs = [element.ref for element in observation.elements]

    assert len(refs) == len(set(refs))


# ---------------------------------------------------------------------------------------
# Checkpoints.
# ---------------------------------------------------------------------------------------


def test_check_element_exists(member_page: WebSurface):
    assert member_page.check(
        Checkpoint(kind=CheckpointKind.ELEMENT_EXISTS, value="input[value='Open Sub-Account']")
    )
    assert not member_page.check(
        Checkpoint(kind=CheckpointKind.ELEMENT_EXISTS, value="input[value='Close Account']")
    )


def test_check_url_matches(member_page: WebSurface):
    assert member_page.check(Checkpoint(kind=CheckpointKind.URL_MATCHES, value="/member"))
    assert not member_page.check(Checkpoint(kind=CheckpointKind.URL_MATCHES, value="/confirm"))


def test_check_text_equals(member_page: WebSurface):
    assert member_page.check(Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Jane Doe"))
    assert not member_page.check(
        Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Robert Chen")
    )


def test_check_text_equals_ignores_page_whitespace(member_page: WebSurface):
    """Legacy pages pad and wrap text arbitrarily; a checkpoint must survive that."""
    assert member_page.check(
        Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Savings   Balance")
    )


def test_check_all_of_and_any_of(member_page: WebSurface):
    on_member = Checkpoint(kind=CheckpointKind.URL_MATCHES, value="/member")
    shows_name = Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Jane Doe")
    absent = Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Wire Transfer Approval")

    assert member_page.check(
        Checkpoint(kind=CheckpointKind.ALL_OF, children=[on_member, shows_name])
    )
    assert not member_page.check(
        Checkpoint(kind=CheckpointKind.ALL_OF, children=[on_member, absent])
    )
    assert member_page.check(Checkpoint(kind=CheckpointKind.ANY_OF, children=[absent, shows_name]))
    assert not member_page.check(Checkpoint(kind=CheckpointKind.ANY_OF, children=[absent, absent]))


def test_check_nested_composites_recurse(member_page: WebSurface):
    """Artifacts may nest conditions arbitrarily; evaluation must follow the whole tree."""
    nested = Checkpoint(
        kind=CheckpointKind.ALL_OF,
        children=[
            Checkpoint(kind=CheckpointKind.URL_MATCHES, value="/member"),
            Checkpoint(
                kind=CheckpointKind.ANY_OF,
                children=[
                    Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="No such member"),
                    Checkpoint(
                        kind=CheckpointKind.ALL_OF,
                        children=[
                            Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Jane Doe"),
                            Checkpoint(
                                kind=CheckpointKind.ELEMENT_EXISTS,
                                value="input[value='Open Sub-Account']",
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )

    assert member_page.check(nested)


# ---------------------------------------------------------------------------------------
# Waiting.
# ---------------------------------------------------------------------------------------


def test_wait_for_returns_true_immediately_when_already_satisfied(member_page: WebSurface):
    """A satisfied condition must not cost a poll interval."""
    started = time.monotonic()

    assert member_page.wait_for(
        Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Jane Doe"),
        timeout_s=5.0,
        poll_ms=250,
    )
    assert time.monotonic() - started < 2.0


def test_wait_for_returns_false_on_timeout(member_page: WebSurface):
    """A timeout is an observation the replay engine classifies, not an exception."""
    started = time.monotonic()

    passed = member_page.wait_for(
        Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Wire Transfer Approval"),
        timeout_s=1.0,
        poll_ms=100,
    )
    elapsed = time.monotonic() - started

    assert passed is False
    assert 1.0 <= elapsed < 4.0, "should honor the budget without hanging past it"


def test_wait_for_outlasts_the_injected_slow_page(surface: WebSurface, mock_app: str):
    """The slow/stuck distinction in practice: a 5s response is waited out, not failed.

    Also pins the reason navigation has its own timeout budget -- under the short element
    timeout this navigation would abort before the page ever arrived, reporting a slow target
    as a broken one.
    """
    started = time.monotonic()
    surface.open(f"{mock_app}/member?member_id=12345&inject=slow")

    assert surface.wait_for(
        Checkpoint(kind=CheckpointKind.TEXT_EQUALS, value="Member Detail"),
        timeout_s=15.0,
        poll_ms=250,
    )
    assert time.monotonic() - started >= 5.0, "the injected delay should actually have applied"


# ---------------------------------------------------------------------------------------
# Lifecycle.
# ---------------------------------------------------------------------------------------


def test_screenshot_writes_evidence(surface: WebSurface, mock_app: str, tmp_path: Path):
    """Evidence capture must create its own directories; a failed run cannot prepare them."""
    surface.open(mock_app)
    destination = tmp_path / "run-1" / "step-0.png"

    surface.screenshot(destination)

    assert destination.exists() and destination.stat().st_size > 0


def test_close_is_idempotent(mock_app: str):
    """Cleanup runs on failure paths too, sometimes more than once."""
    web_surface = WebSurface(headless=True)
    web_surface.open(mock_app)

    web_surface.close()
    web_surface.close()


def test_using_a_closed_surface_reports_the_real_problem(mock_app: str):
    """The error should name the cause, not surface as an AttributeError on None."""
    web_surface = WebSurface(headless=True)

    with pytest.raises(Exception, match="not open"):
        web_surface.current_url()


def test_text_locator_collapses_inline_wrappers_to_the_innermost_match(
    member_page: WebSurface, mock_app: str
):
    """`<font><b>$18,750.35</b></font>` is one target, not two.

    This markup wraps every value in inline tags, so a text match hits the value element and
    its wrappers. Treating that as ambiguous would make visible text -- the most durable
    targeting this app offers -- unusable for reading values.
    """
    member_page.open(f"{mock_app}/member?member_id=67890")

    assert member_page.read(_locator(LocatorBy.TEXT, "$18,750.35")) == "$18,750.35"


def test_a_value_appearing_twice_on_the_page_is_still_ambiguous(member_page: WebSurface):
    """Collapsing wrappers must not weaken the guarantee for genuinely distinct elements.

    Member 12345's Available and Ledger balances are both $4,200.00 -- two different cells in
    two different rows. A real discovery run proposed exactly this locator, and refusing it is
    correct: which of the two amounts the caller wanted is unknowable from the text alone.
    """
    assert member_page.find(_locator(LocatorBy.TEXT, "$4,200.00")) is None


def test_distinct_sibling_elements_are_still_ambiguous(member_page: WebSurface):
    """Table cells nest, but the leaf cells are siblings -- several real targets, so a refusal."""
    assert member_page.find(_locator(LocatorBy.CSS, "td")) is None
