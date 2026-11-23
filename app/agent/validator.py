"""Deterministic answer checks ("critic as code") and code-built provenance.

The model returns only the answer text and a few structured fields. Code then:
- traces every number in answer_markdown to the tool-result field it came from (allowing
  display rounding and a share shown as a percent) -> `figures`;
- derives `data_sources` from the tools actually called.

Problems (trigger one retry, then are returned as visible warnings):
- answer doesn't parse into FinalAnswer
- a number in the answer isn't traceable to tool results / the user's question
- a hub's stated rank differs from the rank tools reported
- a stand-in was recommended but the answer doesn't ask for confirmation (and no stand-in analysis was run)
"""

import json
import re
from dataclasses import dataclass, field

from pydantic import ValidationError

from app.agent.answer_schema import FinalAnswer

# A number not preceded by a letter, digit or dot (underscores count as separators: "within_150km").
NUM_RE = re.compile(r"(?<![A-Za-z0-9.])[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![A-Za-z0-9.])[-+]?\d+(?:\.\d+)?")
SMALL_INT_MAX = 10          # small integers in prose ("top 3", "2 hubs") aren't checked
ANALYSIS_TOOLS = {"get_hub_risk_breakdown", "compare_hubs", "rank_hubs", "get_weather_stat"}
TOOL_SOURCES = {             # sources for tools whose output has no explicit "source" field
    "list_hubs": "Hub network and US Census regions (prototype assumption)",
    "find_nearest_hubs": "Census Gazetteer places + population estimates; FEMA National Risk Index county profiles",
    "get_methodology": "Method parameters (params.yaml)",
}


@dataclass
class Validation:
    ok: bool
    problems: list[str] = field(default_factory=list)
    answer: FinalAnswer | None = None
    figures: list[dict] = field(default_factory=list)
    data_sources: list[str] = field(default_factory=list)


def numbers_in_text(text: str) -> list[tuple[str, float, int, int]]:
    """(as written, value, decimals, position) for each number in a string."""
    out = []
    for m in NUM_RE.finditer(text):
        raw = m.group()
        s = raw.replace(",", "").lstrip("+")
        out.append((raw, float(s), len(s.split(".")[1]) if "." in s else 0, m.start()))
    return out


def flatten_numbers(obj, path: str = "") -> list[tuple[str, float]]:
    """(json path, number) for every number in a JSON-like object: values, numbers in strings and in keys."""
    out = []
    if isinstance(obj, bool) or obj is None:
        return out
    if isinstance(obj, (int, float)):
        out.append((path, float(obj)))
    elif isinstance(obj, str):
        out += [(path, v) for _, v, _, _ in numbers_in_text(obj)]
    elif isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            out += [(p + " (key)", n) for _, n, _, _ in numbers_in_text(str(k))]
            out += flatten_numbers(v, p)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out += flatten_numbers(v, f"{path}[{i}]")
    return out


def collect_numbers(obj) -> set[float]:
    return {v for _, v in flatten_numbers(obj)}


def _matches(value: float, decimals: int, number: float) -> str | None:
    tol = 0.5 * 10 ** (-decimals) + 1e-9
    if abs(value - number) <= tol:
        return "exact or rounded"
    if abs(value - number * 100) <= tol:
        return "share shown as percent"
    return None


def traceable(value: float, decimals: int, pool: set[float]) -> bool:
    return any(_matches(value, decimals, p) for p in pool)


def trace_figures(text: str, tool_outputs: list[tuple[str, dict]], user_text: str
                  ) -> tuple[list[dict], list[tuple[str, int]]]:
    """Trace every checked number in `text` to a tool-result field. Returns (figures, untraced[(raw, pos)]).

    tool_outputs: (tool_name, output) for the whole session, most recent last (recent matches win).
    """
    index = [(name, path, v) for name, out in reversed(tool_outputs) for path, v in flatten_numbers(out)]
    user_numbers = {v for _, v, _, _ in numbers_in_text(user_text)}
    figures, untraced = [], []
    for raw, value, dec, pos in numbers_in_text(text):
        if dec == 0 and (abs(value) <= SMALL_INT_MAX or 1900 <= value <= 2100):
            continue                                            # small counts / ordinals, years
        context = text[max(0, pos - 60): pos + len(raw) + 20].replace("\n", " ").replace("**", "")
        # Prefer a direct (exact/rounded) match anywhere over a share-shown-as-percent match,
        # e.g. "3.6 heat warning days" must not be attributed to a 0.036 share.
        matches = [(n, p, v, how) for n, p, v in index if (how := _matches(value, dec, v))]
        hit = next((m for m in matches if m[3] == "exact or rounded"), matches[0] if matches else None)
        if hit:
            n, p, v, how = hit
            figures.append({"value_in_answer": raw, "context": context.strip(), "source_tool": n,
                            "field": p, "tool_value": v, "match": how})
        elif any(_matches(value, dec, u) for u in user_numbers):
            figures.append({"value_in_answer": raw, "context": context.strip(), "source_tool": "user question",
                            "field": None, "tool_value": value, "match": "from the question"})
        else:
            untraced.append((raw, pos))
    return figures, untraced


def data_sources(turn_tools: list[tuple[str, dict]]) -> list[str]:
    seen: list[str] = []
    for name, out in turn_tools:
        found = [out[k] for k in ("source", "evidence_sources") if isinstance(out.get(k), str)]
        for s in (found or [TOOL_SOURCES.get(name)]):
            if s and s not in seen:
                seen.append(s)
    return seen


def validate(raw_text: str | None, session_tools: list[tuple[str, dict]], user_text: str,
             turn_tools: list[tuple[str, dict]]) -> Validation:
    """session_tools: all (tool, output) pairs this session (for tracing numbers);
    turn_tools: (tool, output) pairs from the current turn (for rank / stand-in checks and sources)."""
    try:
        answer = FinalAnswer.model_validate(json.loads(raw_text or ""))
    except (json.JSONDecodeError, ValidationError) as e:
        return Validation(False, [f"answer is not valid FinalAnswer JSON: {str(e)[:300]}"])

    problems = []
    figures, untraced = trace_figures(answer.answer_markdown, session_tools, user_text)
    for raw, _ in untraced:
        problems.append(f"the number {raw} in the answer is not in any tool result (don't compute or estimate "
                        f"numbers; quote tool values or describe in words)")

    ranks: dict[str, set[int]] = {}
    for name, out in turn_tools:
        if name == "rank_hubs":
            for r in out.get("ranking", []):
                ranks.setdefault(r["hub_id"], set()).add(r["rank"])
        elif name == "get_hub_risk_breakdown":
            ranks.setdefault(out.get("hub_id"), set()).add(out.get("rank"))
        elif name == "compare_hubs":
            for h in out.get("hubs", []):
                ranks.setdefault(h["hub_id"], set()).add(h["overall_rank"])
    for h in answer.hubs:
        if h.rank is not None and h.hub_id.value in ranks and h.rank not in ranks[h.hub_id.value]:
            problems.append(f"{h.hub_id.value} is given rank {h.rank} but tools reported {sorted(ranks[h.hub_id.value])}")

    names = {n for n, _ in turn_tools}
    stand_in = any(n == "find_nearest_hubs" and o.get("status") == "matched" for n, o in turn_tools)
    if stand_in and not answer.awaiting_user_confirmation and not (names & ANALYSIS_TOOLS):
        problems.append("a stand-in hub was recommended: ask the user to confirm before analyzing it "
                        "(set awaiting_user_confirmation = true)")

    return Validation(not problems, problems, answer, figures, data_sources(turn_tools))
