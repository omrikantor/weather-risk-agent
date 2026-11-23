"""The agent loop for one chat turn.

1. The model reads the conversation and either asks for tools or writes its final answer.
2. Our code runs the requested tools (validated arguments, deterministic results) and returns the results.
3. Steps 1 and 2 repeat until the model writes a FinalAnswer (JSON), at most agent_max_steps times.
4. The validator checks the answer: every number must trace to a tool result, ranks must match the tools,
   and a stand-in hub must not be analyzed without the user's confirmation.
5. If checks fail, the model gets one retry with the list of problems. Remaining problems are shown to the
   user as visible warnings rather than hidden.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass, field

from app.agent.answer_schema import FinalAnswer, final_answer_schema
from app.agent.llm_client import LLMClient, Usage, tool_output_item
from app.agent.prompts import SYSTEM_PROMPT, date_message
from app.agent.sessions import SessionStore
from app.agent.textclean import clean_answer
from app.agent.validator import validate
from app.config import get_settings
from app.tools.registry import execute, openai_tools

log = logging.getLogger("agent")


@dataclass
class ToolTrace:
    """One tool call in a turn, for the developer view: name, arguments, time taken and any error."""
    tool: str
    arguments: dict | str
    ms: int
    error: str | None = None


@dataclass
class AgentResult:
    """Everything the API returns for one turn: the answer plus warnings, tool trace, usage and timing."""
    session_id: str
    answer: dict
    validation_warnings: list[str]
    retried: bool
    trace: list[ToolTrace]
    usage: Usage
    latency_ms: int
    steps: int
    stopped_reason: str = "answered"
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["usage"]["cost_usd"] = round(d["usage"]["cost_usd"], 5)
        return d


def _fallback_answer(text: str, question_type: str = "clarification") -> dict:
    """A well-formed answer for cases where the model could not produce one (refusal, step limit and so on),
    so the UI always receives the same answer format."""
    a = FinalAnswer(question_type=question_type, answer_markdown=text, measure_used="none", hubs=[],
                    assumptions=[], uncertainty={"level": "high", "reasons": [text]},
                    awaiting_user_confirmation=False, suggested_followups=[]).model_dump(mode="json")
    return {**a, "figures": [], "data_sources": []}


class Agent:
    """Runs chat turns. One instance serves all users; conversation state lives in the SessionStore."""

    def __init__(self, llm: LLMClient | None = None, store: SessionStore | None = None, max_steps: int | None = None):
        self.llm = llm or LLMClient()
        self.store = store or SessionStore()
        self.max_steps = max_steps or get_settings().agent_max_steps
        self.tools = openai_tools()
        self.schema = final_answer_schema()

    def run_turn(self, message: str, session_id: str | None = None) -> AgentResult:
        """Answer one user message, continuing the conversation identified by session_id (or a new one)."""
        t0 = time.time()
        session = self.store.get_or_create(session_id)
        session.start_turn()
        # Today's date goes in as its own message (not in the system prompt), so the system prompt stays
        # identical across requests and the provider can cache it.
        session.items += [date_message(), {"role": "user", "content": message}]
        usage, trace, turn_tools = Usage(), [], []
        retried, answer, warnings, stopped = False, None, [], "answered"

        step = 0
        for step in range(1, self.max_steps + 1):
            resp = self.llm.create(SYSTEM_PROMPT, session.items, self.tools, self.schema)
            usage.add(resp.usage)
            session.items += resp.output_items

            if resp.tool_calls:
                # The model may ask for several tools at once; run each and hand every result back.
                for call in resp.tool_calls:
                    t1 = time.time()
                    out = execute(call.name, call.arguments)
                    try:
                        args = json.loads(call.arguments)
                    except json.JSONDecodeError:
                        args = call.arguments
                    trace.append(ToolTrace(call.name, args, int((time.time() - t1) * 1000), out.get("error")))
                    turn_tools.append((call.name, out))
                    session.tool_outputs.append((call.name, out))
                    session.items.append(tool_output_item(call.call_id, out))
                continue   # let the model read the results and decide the next step

            # No tool calls: the model is answering (or refusing, or ran out of output room).
            if resp.refusal:
                answer, stopped = _fallback_answer("I can't help with that request. I can answer questions about "
                                                   "weather disruption risk for our hubs.", "out_of_scope"), "refusal"
                break
            if resp.status == "incomplete" and not resp.text:
                answer, stopped = _fallback_answer("I ran out of room to finish this answer; please ask a narrower "
                                                   "question."), f"incomplete:{resp.incomplete_reason}"
                break

            # Numbers may come from any tool output in the conversation (follow-ups reuse earlier results).
            v = validate(resp.text, session.tool_outputs, message, turn_tools)
            if v.ok or retried:
                warnings = v.problems
                if v.answer:
                    # Provenance is built by code, not by the model; text is cleaned deterministically.
                    answer = {**clean_answer(v.answer.model_dump(mode="json")), "figures": v.figures,
                              "data_sources": v.data_sources}
                else:
                    answer = _fallback_answer("I couldn't produce a well-formed answer; please rephrase the question.")
                break
            retried = True
            log.info("validation problems, retrying: %s", v.problems)
            session.items.append({"role": "developer", "content":
                                  "Your answer failed these checks. Fix them and return a corrected FinalAnswer "
                                  "(call tools again if you need numbers):\n- " + "\n- ".join(v.problems)})
        else:
            # The for loop ended without break: the model kept calling tools until the step limit.
            answer, stopped = _fallback_answer("I couldn't finish within the step limit; please ask a narrower "
                                               "question."), "max_steps"

        session.trim()   # shrink old tool outputs so long conversations stay fast and cheap
        return AgentResult(session_id=session.id, answer=answer, validation_warnings=warnings, retried=retried,
                           trace=trace, usage=usage, latency_ms=int((time.time() - t0) * 1000), steps=step,
                           stopped_reason=stopped)
