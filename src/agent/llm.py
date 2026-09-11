"""The model boundary: one method, one typed answer.

Everything the LLM is allowed to influence passes through `Decision`, and everything the loop
needs from a model is `decide(prompt) -> Decision`. That narrowness is what makes the discovery
loop testable without spending money: `StubLLMClient` satisfies the same contract as the real
client, so the entire loop -- prompting, execution, verification, artifact assembly -- runs
end-to-end against a scripted sequence before any API call is made.

The model proposes; it does not act. A `Decision` is data that the loop validates, executes, and
verifies against the real surface, and only a verified action becomes part of an artifact.
"""

from __future__ import annotations

import json
import logging
import os
import re
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from src.artifact.schema import ActionType, Checkpoint, Locator, RiskLevel

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"
"""Model used for discovery.

Haiku is the cheapest capable model in the family, and discovery is the expensive phase: it is
the only part of the system that calls an LLM at all, and it may take a dozen turns of
observation before a flow is recorded. Since every replay afterwards is model-free, the quality
bar here is "can it read a page and propose a locator", which Haiku clears. A production
deployment recording high-value or unfamiliar workflows would use a stronger model -- the cost
difference is paid once per recording, not once per run, so it is usually worth it there.
"""

MAX_TOKENS = 1024
"""Output cap per decision.

A Decision is a small JSON object; 1024 tokens is generous for one. Capping it low bounds the
cost of a runaway generation and makes a truncated response fail fast and visibly rather than
producing half a decision that happens to parse.
"""


PRICE_PER_MTOK = {"claude-haiku-4-5": (1.00, 5.00)}
"""Published USD per million tokens (input, output), for reporting only.

Hardcoded rather than fetched because this is a run summary, not a billing system: the number
exists so a reviewer can see what a discovery run cost without leaving the evidence directory.
An unknown model reports tokens without a dollar figure rather than guessing.
"""


class TokenUsage(BaseModel):
    """What a run spent, accumulated across every request a client made.

    Tracked on the client because that is the only place that sees the API's own accounting.
    Discovery is the sole paid phase of this system, so this is the whole cost story for a
    recording -- every replay afterwards is free.
    """

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def estimated_cost_usd(self) -> float | None:
        """Cost at published list prices, or None when the model's rates are unknown."""
        rates = PRICE_PER_MTOK.get(self.model)
        if rates is None:
            return None
        input_rate, output_rate = rates
        return (self.input_tokens * input_rate + self.output_tokens * output_rate) / 1_000_000


class ControlAction(StrEnum):
    """Loop-control answers that are not surface actions.

    Kept in their own enum rather than added to `ActionType` because they are not things an
    artifact can record: no replay ever executes "done". Only the agent understands them.
    """

    DONE = "done"
    """The goal appears to be met; the loop verifies the success checkpoint before believing it."""
    STUCK = "stuck"
    """The model sees no way forward. Ends the run at a dead end, which is the seam where
    escalation to a human will attach."""


class Decision(BaseModel):
    """One proposed action from the model, as strict JSON.

    Typed rather than free text so a malformed or fantastical answer is rejected at the boundary
    instead of reaching the browser. The fields mirror the artifact's own vocabulary -- the model
    proposes a `Locator` with fallbacks and a rationale, and a `Checkpoint` for what should be
    true afterwards -- so a verified decision converts into a recorded `Step` with no
    translation.
    """

    thought: str = Field(
        default="",
        description="Brief reasoning. Recorded in the run transcript as evidence and "
        "deliberately NOT copied into the artifact: an artifact is a reviewable contract, not a "
        "model monologue, and a replay must never depend on the words the model used.",
    )
    action: ActionType | ControlAction = Field(
        description="A surface primitive to execute, or 'done'/'stuck' to end the loop."
    )
    target: Locator | None = Field(
        default=None,
        description="The element to act on, proposed with fallbacks and a written rationale. "
        "The model is asked for durable targeting here because this is the moment it can see "
        "the page; nothing downstream gets a second chance to judge robustness.",
    )
    value: str | None = Field(
        default=None,
        description="URL for navigate, text for type. The loop parameterizes it against the "
        "declared inputs before recording.",
    )
    extract: str | None = Field(
        default=None, description="For read: the output field name to store the text under."
    )
    checkpoint: Checkpoint | None = Field(
        default=None,
        description="What should be true after this action. Verified against the real surface; "
        "an unverified action is never recorded.",
    )
    risk: RiskLevel = Field(
        default=RiskLevel.SAFE,
        description="The model's assessment of consequence. Defaults to SAFE, and the safety "
        "layer treats it as a claim to be checked rather than trusted.",
    )

    @property
    def is_control(self) -> bool:
        """Whether this ends the loop rather than driving the surface."""
        return isinstance(self.action, ControlAction)


class DecisionParseError(ValueError):
    """The model's output could not be read as a valid Decision.

    Distinct from a validation error so the client can tell "the model wrote prose" from "the
    loop asked for something impossible", and retry only the former.
    """


class LLMClient(ABC):
    """The one thing the discovery loop needs from a model.

    A single method keeps the fake and the real implementation genuinely interchangeable -- there
    is no second code path for tests to miss.
    """

    usage: TokenUsage
    """Running total of what this client has spent. Present on every implementation -- zero for
    the stub -- so the evidence writer never has to ask which kind of client it was given."""

    @abstractmethod
    def decide(self, prompt: str) -> Decision:
        """Return the model's next proposed action for the given prompt."""


class StubLLMClient(LLMClient):
    """Replays a scripted list of decisions, then reports completion. Zero cost, deterministic.

    This is what the tests and the first end-to-end runs use. Building the loop against it first
    is a deliberate sequencing choice: the hard parts of discovery -- executing actions,
    verifying checkpoints, parameterizing values, assembling a valid artifact -- are all
    independent of where decisions come from, and debugging them against a nondeterministic,
    billable oracle would be slower and worse.
    """

    def __init__(self, scripted: list[Decision]) -> None:
        self.scripted = list(scripted)
        self.usage = TokenUsage(model="stub")
        self.calls = 0
        """How many decisions have been served. Lets a test assert the loop stopped early."""

    def decide(self, prompt: str) -> Decision:
        """Serve the next scripted decision, or 'done' once the script runs out.

        Ending with `done` rather than raising means a script that is shorter than the run simply
        terminates the loop cleanly, instead of turning an over-running loop into a crash that
        hides the real problem.
        """
        if self.calls < len(self.scripted):
            decision = self.scripted[self.calls]
            self.calls += 1
            return decision
        self.calls += 1
        return Decision(thought="script exhausted", action=ControlAction.DONE)


class AnthropicLLMClient(LLMClient):
    """Real discovery client backed by the Anthropic Messages API.

    Nothing in the default path constructs this: discovery runs on the stub until a human
    deliberately opts into spending money. It is written now so the boundary is proven to fit a
    real API, not so it runs by default.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = MAX_TOKENS,
        system_prompt: str | None = None,
        api_key: str | None = None,
    ) -> None:
        # Imported here rather than at module scope so the whole agent package -- and the test
        # suite -- works without the anthropic SDK installed. Only paying users need it.
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                "the 'anthropic' package is required for AnthropicLLMClient; "
                "install it or use StubLLMClient"
            ) from exc

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in, "
                "then load it in your entry point (python-dotenv) before constructing this "
                "client."
            )

        self.model = model
        self.max_tokens = max_tokens
        self.system_prompt = system_prompt
        self.usage = TokenUsage(model=model)
        self._client = anthropic.Anthropic(api_key=key)

    def decide(self, prompt: str) -> Decision:
        """Ask the model once, and re-prompt exactly once if the answer will not parse.

        One retry, not a loop: a model that fails twice on an explicit repair instruction is not
        going to succeed on the third try, and an unbounded retry against a paid API is a way to
        spend money on nothing.
        """
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        raw = self._complete(messages)
        try:
            return parse_decision(raw)
        except DecisionParseError as first_failure:
            log.warning("unparseable decision, re-prompting once: %s", first_failure)
            messages += [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        f"That response could not be parsed ({first_failure}). Reply with "
                        "ONLY the JSON object described in the response format -- no prose, no "
                        "markdown fences."
                    ),
                },
            ]
            return parse_decision(self._complete(messages))

    def _complete(self, messages: list[dict[str, Any]]) -> str:
        """Send one request and return the concatenated text of the response."""
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        if self.system_prompt:
            request["system"] = self.system_prompt

        response = self._client.messages.create(**request)
        # Counted before anything can go wrong with the body: a response that fails to parse was
        # still billed, and a cost report that quietly omits the retries is a misleading one.
        self.usage.requests += 1
        self.usage.input_tokens += response.usage.input_tokens
        self.usage.output_tokens += response.usage.output_tokens

        if response.stop_reason == "refusal":
            raise DecisionParseError("the model declined to answer this prompt")
        return "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_decision(raw: str) -> Decision:
    """Turn raw model output into a Decision, tolerating the usual packaging.

    Defensive on purpose. Models wrap JSON in markdown fences, prefix it with a sentence, or
    append an explanation, and none of that is worth a failed run -- but the *content* is held
    to the schema exactly. Everything outside the outermost JSON object is discarded; anything
    inside that does not validate is an error, not something to guess at.
    """
    if not raw or not raw.strip():
        raise DecisionParseError("empty response")

    text = _FENCE_RE.sub("", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise DecisionParseError(f"no JSON object found in response: {raw[:120]!r}")

    try:
        # raw_decode, not loads: models sometimes emit a second object or a trailing sentence
        # after a perfectly good one, and discarding that is better than failing the turn.
        payload, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError as exc:
        raise DecisionParseError(f"invalid JSON: {exc}") from exc

    try:
        return Decision.model_validate(payload)
    except ValidationError as exc:
        raise DecisionParseError(f"JSON did not match the Decision schema: {exc}") from exc
