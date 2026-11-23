"""Deterministic answer checks ("critic as code").

Problems (trigger one retry, then are returned as visible warnings):
- answer doesn't parse into FinalAnswer
- a `figures` value isn't found in any tool result (allowing display rounding and share->percent)
- a number in answer_markdown isn't traceable to tool results / the user's question / known thresholds
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


@dataclass
class Validation:
    ok: bool
    problems: list[str] = field(default_factory=list)
    answer: FinalAnswer | None = None


def _numbers_in_text(text: str) -> list[tuple[float, int]]:
    """(value, decimals) for each number in a string."""
    out = []
    for m in NUM_RE.finditer(text):
        s = m.group().replace(",", "").lstrip("+")
        decimals = len(s.split(".")[1]) if "." in s else 0
        out.append((float(s), decimals))
    return out


def collect_numbers(obj, pool: set | None = None) -> set[float]:
    """All numbers in a JSON-like object: numeric values, numbers inside strings and inside keys."""
    pool = set() if pool is None else pool
    if isinstance(obj, bool) or obj is None:
        return pool
    if isinstance(obj, (int, float)):
        pool.add(float(obj))
    elif isinstance(obj, str):
        pool.update(v for v, _ in _numbers_in_text(obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            collect_numbers(k, pool)
            collect_numbers(v, pool)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            collect_numbers(v, pool)
    return pool


def traceable(value: float, decimals: int, pool: set[float]) -> bool:
    """value (as displayed with `decimals` decimals) matches a pool number after rounding, or a share shown as %."""
    tol = 0.5 * 10 ** (-decimals) + 1e-9
    for p in pool:
        if abs(value - p) <= tol or abs(value - p * 100) <= tol:
            return True
    return False


def _decimals(x: float) -> int:
    s = repr(float(x))
    return 0 if s.endswith(".0") else len(s.split(".")[1])


def validate(raw_text: str | None, tool_results: list[dict], user_text: str,
             turn_tools: list[tuple[str, dict]]) -> Validation:
    """tool_results: all tool outputs in this session (for traceability);
    turn_tools: (tool_name, output) pairs from the current turn (for rank / stand-in checks)."""
    try:
        answer = FinalAnswer.model_validate(json.loads(raw_text or ""))
    except (json.JSONDecodeError, ValidationError) as e:
        return Validation(False, [f"answer is not valid FinalAnswer JSON: {str(e)[:300]}"])

    pool = collect_numbers(tool_results)
    pool |= {v for v, _ in _numbers_in_text(user_text)}
    problems = []

    for f in answer.figures:
        if not traceable(f.value, _decimals(f.value), pool):
            problems.append(f"figure '{f.label}' = {f.value} does not appear in any tool result; use the tool's "
                            f"number or remove it")

    figure_values = [(f.value, _decimals(f.value)) for f in answer.figures]
    for value, dec in _numbers_in_text(answer.answer_markdown):
        if dec == 0 and (abs(value) <= SMALL_INT_MAX or 1900 <= value <= 2100):
            continue                                            # small counts / ordinals, years
        if traceable(value, dec, pool) or traceable(value, dec, {v for v, _ in figure_values if
                                                                 traceable(v, _decimals(v), pool)}):
            continue
        problems.append(f"the number {value:g} in the answer text is not in any tool result (don't compute or "
                        f"estimate numbers; quote tool values)")

    ranks: dict[str, set[int]] = {}
    for name, out in turn_tools:
        if name == "rank_hubs":
            for r in out.get("ranking", []):
                ranks.setdefault(r["hub_id"], set()).add(r["rank"])
        elif name in ("get_hub_risk_breakdown",):
            ranks.setdefault(out.get("hub_id"), set()).add(out.get("rank"))
        elif name == "compare_hubs":
            for h in out.get("hubs", []):
                ranks.setdefault(h["hub_id"], set()).add(h["overall_rank"])
    for h in answer.hubs:
        if h.rank is not None and h.hub_id.value in ranks and h.rank not in ranks[h.hub_id.value]:
            problems.append(f"{h.hub_id.value} is given rank {h.rank} but tools reported {sorted(ranks[h.hub_id.value])}")

    names = [n for n, _ in turn_tools]
    stand_in = any(n == "find_nearest_hubs" and o.get("status") == "matched" for n, o in turn_tools)
    if stand_in and not answer.awaiting_user_confirmation and not (set(names) & ANALYSIS_TOOLS):
        problems.append("a stand-in hub was recommended: ask the user to confirm before analyzing it "
                        "(set awaiting_user_confirmation = true)")

    return Validation(not problems, problems, answer)
