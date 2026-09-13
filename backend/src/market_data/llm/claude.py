"""The Claude adapter for intelligent insights, through LangChain.

``with_structured_output(schema, method="json_schema")`` drives Claude's own
structured-output feature (``output_config.format``), so a raw JSON Schema
dict -- as ``llm/prompts.py`` builds -- is accepted directly; no Pydantic
model is needed. ``streaming=True`` routes every call through the streaming
API under the hood: an evidence pack is a long input, and thinking can make
the answer slow to start, which a synchronous call can time out waiting on.

Every failure to get a parseable answer is raised as
:class:`InsightProviderError`.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import anthropic
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)

HEARTBEAT_S = 15.0
"""How often a still-outstanding call is logged, so a long wait reads as progress rather than a hang."""


class InsightProviderError(RuntimeError):
    """The LLM produced no usable answer at all: unreachable, timed out, unparseable.

    Distinct from an answer that is merely wrong in places, which verification
    handles item by item. This one is reported to the client as a 503 carrying
    the message, so the message must be safe to show: an endpoint, never a key.
    """


class ClaudeClient:
    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 180.0,
        max_tokens: int = 16_000,
        llm: Any | None = None,
    ) -> None:
        self._model = model
        self._llm = llm or ChatAnthropic(
            model=model, api_key=api_key, timeout=timeout_s, max_tokens=max_tokens, streaming=True
        )

    @property
    def model(self) -> str:
        return self._model

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> object:
        task = schema.get("title", "answer")
        structured = self._llm.with_structured_output(schema, method="json_schema", include_raw=True)
        logger.info(
            "Asking Claude (%s) for %s, prompt %s chars", self._model, task, f"{len(system) + len(user):,}"
        )
        started = time.monotonic()
        try:
            with _heartbeat(task, started):
                result: Any = structured.invoke([SystemMessage(system), HumanMessage(user)])
        except anthropic.NotFoundError as exc:
            raise InsightProviderError(
                f"Claude model {self._model!r} is not available: {exc.message}"
            ) from exc
        except anthropic.RateLimitError as exc:
            raise InsightProviderError("Claude API rate limit reached; try again shortly") from exc
        except anthropic.APIStatusError as exc:
            raise InsightProviderError(f"Claude API returned HTTP {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise InsightProviderError(f"Claude API is unreachable: {exc}") from exc
        usage = getattr(result["raw"], "usage_metadata", None) or {}
        logger.info(
            "Claude answered %s in %.1fs (stop_reason=%s, input_tokens=%s, output_tokens=%s)",
            task,
            time.monotonic() - started,
            result["raw"].response_metadata.get("stop_reason"),
            usage.get("input_tokens", "?"),
            usage.get("output_tokens", "?"),
        )
        return _parsed(result)


@contextmanager
def _heartbeat(task: str, started: float, every_s: float = HEARTBEAT_S) -> Iterator[None]:
    """Logs every ``every_s`` until the wrapped call returns or raises."""
    done = threading.Event()

    def beat() -> None:
        while not done.wait(every_s):
            logger.info("Still waiting on Claude for %s (%.0fs elapsed)", task, time.monotonic() - started)

    thread = threading.Thread(target=beat, name="claude-heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        done.set()
        thread.join()


def _parsed(result: Any) -> object:
    stop_reason = result["raw"].response_metadata.get("stop_reason")
    if stop_reason == "refusal":
        raise InsightProviderError("Claude declined to answer")
    if stop_reason == "max_tokens":
        raise InsightProviderError("Claude's answer was cut off at max_tokens")
    if result["parsing_error"] is not None or result["parsed"] is None:
        raise InsightProviderError("Claude answer was not valid JSON")
    return result["parsed"]
