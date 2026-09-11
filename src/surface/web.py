"""The Playwright-backed web surface -- the only place in the system that imports Playwright.

Everything driver-specific lives behind this file: selector syntax, accessibility snapshots,
visibility rules, timeouts. Layers above receive `Observation`s, `ElementHandle`s and booleans,
so replacing Playwright (or adding a desktop surface alongside it) is a change to this file and
its siblings, not to the agent, the replay engine, or the artifact format.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator as PlaywrightLocator
from playwright.sync_api import Page, sync_playwright

from src.artifact.schema import Checkpoint, CheckpointKind, Locator, LocatorBy, LocatorRule
from src.surface.base import (
    MAX_OBSERVED_ELEMENTS,
    MAX_TEXT_CHARS,
    ElementHandle,
    ElementNotFoundError,
    FallbackEvent,
    Observation,
    PerceivedElement,
    Surface,
    SurfaceError,
)

log = logging.getLogger(__name__)

MAX_MATCH_SCAN = 20
"""Upper bound on matches examined for visibility when resolving a candidate.

A sloppy CSS fallback can match hundreds of legacy table cells. Any candidate matching more
than this is ambiguous by definition, so scanning further only costs round-trips.
"""

_JS_COLLECT_ELEMENTS = """
(limits) => {
  const roleOf = (el) => {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'label') return 'label';
    if (tag === 'th') return 'columnheader';
    if (tag === 'td') return 'cell';
    if (/^h[1-6]$/.test(tag)) return 'heading';
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'submit' || type === 'button' || type === 'reset') return 'button';
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      return 'textbox';
    }
    return tag;
  };

  // Accessible name, best effort, in roughly the order a screen reader would resolve it.
  const nameOf = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label && label.innerText.trim()) return label.innerText.trim();
    }
    const wrapping = el.closest('label');
    if (wrapping && wrapping.innerText.trim()) return wrapping.innerText.trim();
    const tag = el.tagName.toLowerCase();
    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (type === 'submit' || type === 'button' || type === 'reset') return (el.value || '').trim();
      return (el.getAttribute('placeholder') || el.getAttribute('name') || '').trim();
    }
    return (el.innerText || '').trim().split('\\n')[0].trim();
  };

  // Text owned by this element rather than inherited from its children, so a nested table
  // does not report the same amount on every ancestor cell.
  const ownTextOf = (el) => {
    let own = '';
    for (const node of el.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) own += node.textContent;
      else if (node.nodeType === Node.ELEMENT_NODE &&
               ['B', 'I', 'U', 'FONT', 'SPAN', 'EM', 'STRONG', 'SMALL'].includes(node.tagName)) {
        own += node.innerText || node.textContent || '';
      }
    }
    return own.replace(/\\s+/g, ' ').trim();
  };

  const xpathOf = (el) => {
    if (el === document.body) return '/html/body';
    const parent = el.parentElement;
    if (!parent) return '';
    const tag = el.tagName.toLowerCase();
    const siblings = Array.from(parent.children).filter((c) => c.tagName === el.tagName);
    const position = siblings.indexOf(el) + 1;
    const step = siblings.length > 1 ? `${tag}[${position}]` : tag;
    return `${xpathOf(parent)}/${step}`;
  };

  const visible = (el) => {
    if (!el.getClientRects().length) return false;
    const style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  };

  const selector = 'a, button, input, select, textarea, label, [role], ' +
                   'h1, h2, h3, h4, h5, h6, td, th, li, p';
  const out = [];
  let seen = 0;
  for (const el of document.querySelectorAll(selector)) {
    if (!visible(el)) continue;
    const role = roleOf(el);
    const name = nameOf(el).slice(0, limits.maxText);
    const text = ownTextOf(el).slice(0, limits.maxText);
    const interactive = ['link', 'button', 'textbox', 'combobox', 'checkbox', 'radio']
      .includes(role);
    // Keep every control, but only those static elements that actually carry text.
    if (!interactive && !text) continue;
    seen += 1;
    if (out.length < limits.maxElements) {
      out.push({ role, name, text, xpath: xpathOf(el) });
    }
  }
  return { elements: out, total: seen };
}
"""


_JS_INNERMOST_VISIBLE_INDEX = """
(els) => {
  const visible = [];
  els.forEach((el, index) => {
    if (!el.getClientRects().length) return;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') return;
    visible.push([el, index]);
  });
  if (!visible.length) return -1;
  // Drop any match that contains another match: those are wrappers around the real target.
  const innermost = visible.filter(
    ([el]) => !visible.some(([other]) => other !== el && el.contains(other))
  );
  return innermost.length === 1 ? innermost[0][1] : -1;
}
"""
"""Index of the one innermost visible match, or -1 when there is no single answer.

Resolved inside the page in a single call: the alternative is one round-trip per match to test
visibility and containment, which on a page of hundreds of table cells is slow enough to matter
on every step of every replay.
"""


class WebSurface(Surface):
    """A browser surface driven by the Playwright sync API.

    Headless by default because that is how replay runs in CI and unattended. The constructor
    argument exists for the human-handoff path: when a run escalates, a person has to be able
    to see and take over the same session, which requires a headed browser.

    The instance owns its Playwright lifecycle and is not thread-safe; the sync API requires
    that all calls come from the thread that started it.
    """

    def __init__(
        self,
        headless: bool = True,
        *,
        max_elements: int = MAX_OBSERVED_ELEMENTS,
        max_text_chars: int = MAX_TEXT_CHARS,
        default_timeout_ms: int = 5_000,
        navigation_timeout_ms: int = 30_000,
    ) -> None:
        self.headless = headless
        self.max_elements = max_elements
        self.max_text_chars = max_text_chars
        self.default_timeout_ms = default_timeout_ms
        """Budget for element operations. Short on purpose: a missing element is usually
        genuinely missing, and failing fast lets the step's error rules decide what it means."""
        self.navigation_timeout_ms = navigation_timeout_ms
        """Budget for page loads, deliberately much longer. A back-office server taking seconds
        to answer is normal behavior, not a missing element, and cutting navigation short would
        misreport a slow target as a broken locator. How long a *flow* is willing to wait stays
        the artifact's decision, expressed as a WaitPolicy."""

        self.fallback_events: list[FallbackEvent] = []
        """Every resolution that needed a fallback, in order. Read by the evidence writer:
        a run that passed only via fallbacks is a drift warning worth reporting."""

        self._playwright: Any = None
        self._browser: Any = None
        self._page: Page | None = None
        self._ref_counter = 0

    # -- lifecycle ----------------------------------------------------------------------

    def __enter__(self) -> WebSurface:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def page(self) -> Page:
        """The live page, or an error naming the actual problem rather than an AttributeError."""
        if self._page is None:
            raise SurfaceError("surface is not open; call open(url) first")
        return self._page

    def _ensure_started(self) -> None:
        """Start Playwright lazily, so constructing a surface costs nothing."""
        if self._page is not None:
            return
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._page = self._browser.new_page()
        self._page.set_default_timeout(self.default_timeout_ms)
        self._page.set_default_navigation_timeout(self.navigation_timeout_ms)
        log.info("web surface started (headless=%s)", self.headless)

    def open(self, url: str) -> None:
        self._ensure_started()
        # Wrapped like the acting methods: a target that never answers is a surface failure the
        # replay engine must be able to classify, not a driver exception nobody above can name.
        # domcontentloaded, not load: legacy pages often trail slow assets that the flow does
        # not depend on. Anything the flow *does* depend on is expressed as a wait condition.
        with _as_surface_error("open"):
            self.page.goto(url, wait_until="domcontentloaded")
        log.info("opened %s", url)

    def close(self) -> None:
        for name, resource in (("browser", self._browser), ("playwright", self._playwright)):
            if resource is None:
                continue
            try:
                resource.stop() if name == "playwright" else resource.close()
            except PlaywrightError as exc:  # already gone; closing twice must stay safe
                log.debug("error closing %s: %s", name, exc)
        self._page = None
        self._browser = None
        self._playwright = None

    def current_url(self) -> str:
        return self.page.url

    def screenshot(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(destination), full_page=True)

    # -- targeting ----------------------------------------------------------------------

    def _resolve_rule(self, rule: LocatorRule) -> PlaywrightLocator:
        """Translate one `LocatorRule` into a Playwright locator.

        The whole mapping from the artifact's strategy vocabulary to a driver's selector syntax
        lives here, in one readable table, because it is the thing a reviewer must trust when
        deciding whether a recorded locator means what they think it means.
        """
        page = self.page
        match rule.by:
            case LocatorBy.LABEL:
                return page.get_by_label(rule.value)
            case LocatorBy.TEXT:
                return page.get_by_text(rule.value)
            case LocatorBy.ROLE:
                # "button" or "button:Open Sub-Account" (role, then accessible name).
                role, _, name = rule.value.partition(":")
                role = role.strip()
                name = name.strip()
                return page.get_by_role(role, name=name) if name else page.get_by_role(role)
            case LocatorBy.CSS:
                return page.locator(rule.value)
            case LocatorBy.STRUCTURAL:
                # An XPath, or a Playwright chain such as "table >> nth=1 >> tr >> nth=2".
                value = rule.value
                if value.startswith("xpath="):
                    return page.locator(value)
                if value.startswith(("/", "(", "./")):
                    return page.locator(f"xpath={value}")
                return page.locator(value)
        raise SurfaceError(f"unsupported locator strategy: {rule.by}")

    def _unique_visible(self, candidate: PlaywrightLocator) -> PlaywrightLocator | None:
        """Return the single visible match, or None if there are zero or several.

        Ambiguity is a failure rather than a coin flip: on a back-office screen, "some element
        matching 'Search'" is not good enough to click.

        But an element and its own wrappers are not several candidates. This markup nests inline
        tags around every value -- `<td><font><b>$4,200.00</b></font></td>` -- so a text match
        legitimately hits three elements that are all the same target. Nested matches collapse
        to the innermost; only genuinely distinct elements count as ambiguous.
        """
        try:
            index = candidate.evaluate_all(_JS_INNERMOST_VISIBLE_INDEX)
        except PlaywrightError as exc:
            log.debug("innermost-match resolution unavailable, scanning: %s", exc)
        else:
            return candidate.nth(index) if index >= 0 else None

        # Fallback path: one visible match wins, more than one is ambiguous.
        try:
            count = candidate.count()
        except PlaywrightError as exc:
            log.debug("candidate could not be counted: %s", exc)
            return None

        found: PlaywrightLocator | None = None
        for index in range(min(count, MAX_MATCH_SCAN)):
            nth = candidate.nth(index)
            try:
                if not nth.is_visible():
                    continue
            except PlaywrightError:
                continue
            if found is not None:
                return None  # ambiguous
            found = nth
        return found

    def find(self, locator: Locator) -> ElementHandle | None:
        candidates = locator.candidates()
        for index, rule in enumerate(candidates):
            try:
                resolved = self._unique_visible(self._resolve_rule(rule))
            except (PlaywrightError, SurfaceError) as exc:
                log.debug("candidate %d (%s) failed to resolve: %s", index, rule.by, exc)
                continue
            if resolved is None:
                log.debug(
                    "candidate %d (%s=%r) matched zero or multiple visible elements",
                    index, rule.by, rule.value,
                )
                continue

            if index == 0:
                log.info("located via primary %s=%r", rule.by, rule.value)
            else:
                # Drift signal: the recording's preferred targeting no longer works, even
                # though the step will still succeed. Recorded loudly and durably.
                log.warning(
                    "located via FALLBACK %d (%s=%r); primary %s=%r failed. rationale: %s",
                    index, rule.by, rule.value,
                    candidates[0].by, candidates[0].value, locator.rationale,
                )
                self.fallback_events.append(
                    FallbackEvent(
                        primary=candidates[0],
                        used=rule,
                        candidate_index=index,
                        rationale=locator.rationale,
                    )
                )

            self._ref_counter += 1
            return ElementHandle(
                ref=f"e{self._ref_counter}",
                rule=rule,
                candidate_index=index,
                native=resolved,
            )

        log.warning(
            "no candidate resolved for locator (primary %s=%r, %d fallbacks)",
            candidates[0].by, candidates[0].value, len(candidates) - 1,
        )
        return None

    def _require(self, locator: Locator, action: str) -> ElementHandle:
        handle = self.find(locator)
        if handle is None:
            raise ElementNotFoundError(
                f"cannot {action}: no candidate resolved to exactly one visible element "
                f"(primary {locator.primary.by}={locator.primary.value!r}); "
                f"recorded rationale: {locator.rationale}"
            )
        return handle

    # -- acting -------------------------------------------------------------------------

    def click(self, locator: Locator) -> None:
        """Click the element, allowing for a click that navigates.

        Uses the navigation budget rather than the element budget: Playwright waits for any
        navigation the click schedules, and how long the *target* takes to answer is not an
        element-location concern. With the short element timeout, clicking a search button on a
        slow back office raised a driver timeout even though the element was found instantly.
        """
        element = self._require(locator, "click").native
        with _as_surface_error("click"):
            element.click(timeout=self.navigation_timeout_ms)

    def type(self, locator: Locator, text: str) -> None:
        # fill() clears first, so replaying a step is idempotent rather than appending to
        # whatever a previous attempt left in the field.
        element = self._require(locator, "type").native
        with _as_surface_error("type"):
            element.fill(text)

    def read(self, locator: Locator) -> str:
        element = self._require(locator, "read").native
        with _as_surface_error("read"):
            text = (element.inner_text() or "").strip()
        if not text:
            # Form controls carry their value rather than inner text.
            try:
                text = (element.input_value() or "").strip()
            except PlaywrightError:
                text = ""
        return text

    # -- perceiving ---------------------------------------------------------------------

    def observe(self) -> Observation:
        """Build a capped, surface-neutral Observation of the current page.

        Prefers Playwright's accessibility snapshot, since that is the browser's own semantic
        view and the closest analogue to what a desktop surface would read from the OS. Legacy
        markup often yields a thin tree (a table-layout page has few real roles), so a
        condensed DOM walk is the fallback -- it recovers labels, captions and cell values that
        the a11y tree omits.
        """
        page = self.page
        elements, truncated = self._observe_via_accessibility()
        if not elements:
            elements, truncated = self._observe_via_dom()
        return Observation(
            url=page.url,
            title=page.title(),
            elements=elements,
            truncated=truncated,
        )

    def _observe_via_accessibility(self) -> tuple[list[PerceivedElement], bool]:
        """Flatten the browser accessibility tree into perceived elements."""
        try:
            snapshot = self.page.accessibility.snapshot()
        except (PlaywrightError, AttributeError) as exc:
            log.debug("accessibility snapshot unavailable: %s", exc)
            return [], False
        if not snapshot:
            return [], False

        collected: list[PerceivedElement] = []
        total = 0

        def walk(node: dict[str, Any]) -> None:
            nonlocal total
            role = str(node.get("role") or "")
            name = str(node.get("name") or "").strip()
            value = str(node.get("value") or "").strip()
            if role and role not in ("WebArea", "RootWebArea", "generic") and (name or value):
                total += 1
                if len(collected) < self.max_elements:
                    self._ref_counter += 1
                    collected.append(
                        PerceivedElement(
                            role=role,
                            name=name[: self.max_text_chars],
                            text=(value or name)[: self.max_text_chars],
                            ref=f"a{self._ref_counter}",
                        )
                    )
            for child in node.get("children") or []:
                walk(child)

        walk(snapshot)
        return collected, total > len(collected)

    def _observe_via_dom(self) -> tuple[list[PerceivedElement], bool]:
        """Condensed DOM walk: every control, plus static elements that own visible text.

        Runs as a single `evaluate` so a page with hundreds of cells costs one round-trip
        instead of hundreds, and applies the caps inside the browser so the oversized result is
        never transferred in the first place.
        """
        try:
            result = self.page.evaluate(
                _JS_COLLECT_ELEMENTS,
                {"maxElements": self.max_elements, "maxText": self.max_text_chars},
            )
        except PlaywrightError as exc:
            log.warning("DOM observation failed: %s", exc)
            return [], False

        elements: list[PerceivedElement] = []
        for item in result.get("elements", []):
            self._ref_counter += 1
            elements.append(
                PerceivedElement(
                    role=str(item.get("role") or ""),
                    name=str(item.get("name") or ""),
                    text=str(item.get("text") or ""),
                    ref=f"e{self._ref_counter}",
                )
            )
        return elements, int(result.get("total", 0)) > len(elements)

    # -- conditions ---------------------------------------------------------------------

    def check(self, checkpoint: Checkpoint) -> bool:
        """Evaluate a checkpoint once against the current page.

        The composite kinds recurse, so an arbitrarily nested condition tree from the artifact
        is evaluated here without the caller needing to know its shape.
        """
        match checkpoint.kind:
            case CheckpointKind.ALL_OF:
                return all(self.check(child) for child in checkpoint.children or [])
            case CheckpointKind.ANY_OF:
                return any(self.check(child) for child in checkpoint.children or [])
            case CheckpointKind.URL_MATCHES:
                # Treated as a regular expression, so a plain substring works unchanged.
                return re.search(checkpoint.value or "", self.page.url) is not None
            case CheckpointKind.TEXT_EQUALS:
                return self._page_contains_text(checkpoint.value or "")
            case CheckpointKind.ELEMENT_EXISTS:
                return self._element_exists(checkpoint.value or "")
        raise SurfaceError(f"unsupported checkpoint kind: {checkpoint.kind}")

    def _page_contains_text(self, needle: str) -> bool:
        """Whitespace-normalized search of the rendered text.

        Uses rendered text rather than markup so a hidden element cannot satisfy a checkpoint,
        and normalizes whitespace because legacy pages break lines and pad cells arbitrarily.
        """
        try:
            body = self.page.inner_text("body")
        except PlaywrightError as exc:
            log.debug("could not read body text: %s", exc)
            return False
        return _normalize(needle) in _normalize(body)

    def _element_exists(self, selector: str) -> bool:
        """Whether at least one visible element matches the checkpoint's selector expression.

        Existence, unlike targeting, tolerates several matches: the question is whether the
        page shows this thing at all, not which one to act on.
        """
        expression = f"xpath={selector}" if selector.startswith(("/", "(", "./")) else selector
        try:
            candidate = self.page.locator(expression)
            count = candidate.count()
        except PlaywrightError as exc:
            log.debug("element_exists selector %r failed: %s", selector, exc)
            return False
        return any(
            candidate.nth(index).is_visible() for index in range(min(count, MAX_MATCH_SCAN))
        )

    def wait_for(self, checkpoint: Checkpoint, timeout_s: float, poll_ms: int) -> bool:
        """Poll until the checkpoint passes or the budget runs out.

        The configured poll interval is the only sleep in this class. Nothing waits a fixed
        duration hoping a page has caught up: the artifact says what to wait *for*, and the
        answer is either "it happened" or "it did not happen within the budget" -- which is
        exactly the distinction the replay engine needs to tell a slow page from a stuck one.
        """
        deadline = time.monotonic() + timeout_s
        interval = max(poll_ms, 1) / 1000
        while True:
            if self.check(checkpoint):
                return True
            if time.monotonic() >= deadline:
                log.info(
                    "wait_for(%s) timed out after %.1fs", checkpoint.kind, timeout_s
                )
                return False
            time.sleep(min(interval, max(deadline - time.monotonic(), 0)))


@contextmanager
def _as_surface_error(what: str) -> Iterator[None]:
    """Translate any driver exception into a `SurfaceError`.

    The architectural boundary is only real if it holds when things go wrong. Without this, a
    Playwright timeout escaped `WebSurface` untouched, sailed past a replay engine that catches
    `SurfaceError`, and crashed the CLI with a driver traceback -- turning a slow page, which the
    error taxonomy has a bucket for, into an unhandled failure nothing could classify.
    """
    try:
        yield
    except PlaywrightError as exc:
        # Only the first line: Playwright appends a multi-line call log that is useful in a
        # debug log and unusable in a failure report.
        summary = str(exc).splitlines()[0]
        raise SurfaceError(f"{what} failed: {summary}") from exc


def _normalize(text: str) -> str:
    """Collapse whitespace so page formatting does not defeat a text comparison."""
    return re.sub(r"\s+", " ", text).strip()
