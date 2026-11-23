"""The only module that talks to the LLM provider (OpenAI Responses API).

The rest of the app sees provider-neutral types (LLMResponse, ToolCall, Usage), so switching
providers means rewriting this file only.
"""

import json
import logging
import time
from dataclasses import dataclass, field

from openai import APIConnectionError, APITimeoutError, InternalServerError, OpenAI, RateLimitError

from app.config import get_settings

log = logging.getLogger("llm")


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass
class Usage:
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        for f in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "cost_usd", "calls"):
            setattr(self, f, getattr(self, f) + getattr(other, f))


@dataclass
class LLMResponse:
    tool_calls: list[ToolCall]
    text: str | None                  # final message text (JSON when a response schema is used)
    refusal: str | None
    status: str                       # completed | incomplete | failed
    incomplete_reason: str | None
    output_items: list[dict]          # to append to the conversation history as-is
    usage: Usage = field(default_factory=Usage)


def cost_usd(input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    s = get_settings()
    uncached = max(input_tokens - cached_tokens, 0)
    return (uncached * s.price_input_per_m + cached_tokens * s.price_cached_input_per_m
            + output_tokens * s.price_output_per_m) / 1e6


class LLMClient:
    RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)

    def __init__(self, model: str | None = None, effort: str | None = None):
        s = get_settings()
        if not s.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set (see .env.example)")
        self.client = OpenAI(api_key=s.openai_api_key, max_retries=0, timeout=120)
        self.model = model or s.llm_model
        self.effort = effort or s.llm_reasoning_effort
        self.max_output_tokens = s.llm_max_output_tokens

    def create(self, instructions: str, input_items: list[dict], tools: list[dict],
               response_schema: dict | None = None, attempts: int = 4) -> LLMResponse:
        kwargs = dict(model=self.model, instructions=instructions, input=input_items, tools=tools,
                      tool_choice="auto", parallel_tool_calls=True, reasoning={"effort": self.effort},
                      max_output_tokens=self.max_output_tokens)
        if response_schema:
            kwargs["text"] = {"format": {"type": "json_schema", "name": "final_answer", "strict": True,
                                         "schema": response_schema}}
        for i in range(attempts):
            try:
                r = self.client.responses.create(**kwargs)
                break
            except self.RETRYABLE as e:
                if i == attempts - 1:
                    raise
                wait = 2 ** i * 2
                log.warning("LLM call failed (%s); retrying in %ss", type(e).__name__, wait)
                time.sleep(wait)
        return self._convert(r)

    def _convert(self, r) -> LLMResponse:
        calls, text, refusal = [], None, None
        for item in r.output:
            if item.type == "function_call":
                calls.append(ToolCall(item.call_id, item.name, item.arguments))
            elif item.type == "message":
                for c in item.content:
                    if c.type == "output_text":
                        text = (text or "") + c.text
                    elif c.type == "refusal":
                        refusal = c.refusal
        u = r.usage
        usage = Usage(calls=1)
        if u:
            usage = Usage(input_tokens=u.input_tokens, cached_tokens=u.input_tokens_details.cached_tokens,
                          output_tokens=u.output_tokens, reasoning_tokens=u.output_tokens_details.reasoning_tokens,
                          cost_usd=cost_usd(u.input_tokens, u.input_tokens_details.cached_tokens, u.output_tokens),
                          calls=1)
        log.info("llm %s status=%s in=%s cached=%s out=%s cost=$%.4f tools=%s", self.model, r.status,
                 usage.input_tokens, usage.cached_tokens, usage.output_tokens, usage.cost_usd,
                 [c.name for c in calls])
        reason = r.incomplete_details.reason if getattr(r, "incomplete_details", None) else None
        return LLMResponse(tool_calls=calls, text=text, refusal=refusal, status=r.status or "completed",
                           incomplete_reason=reason,
                           output_items=[o.model_dump(exclude_none=True) for o in r.output], usage=usage)


def tool_output_item(call_id: str, result: dict) -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": json.dumps(result, default=str)}
