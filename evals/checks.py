"""Deterministic eval checks.

Expected values come from an oracle that calls the same tool functions directly, so nothing is hard-coded and
the evals stay valid after a data rebuild. A case in cases.yaml lists, per turn, what must happen: which tools
are called with which arguments, which figures appear, in which order hubs are named, required and forbidden
phrases, the question type and whether the answer waits for confirmation.
"""

import json
import re
from functools import lru_cache

from app.agent.validator import _matches, numbers_in_text
from app.tools.registry import execute

# Path syntax used in cases.yaml: field names, list indexes [0] and list filters [hub_id=chicago].
PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]|\[(\w+)=([^\]]+)\]")


def get_path(obj, path: str):
    """Read a value by path, for example 'hubs[0].hazards.flood.x' or 'ranking[hub_id=chicago].value'.
    Raises KeyError, IndexError or StopIteration when the path does not exist."""
    for name, idx, key, val in PATH_TOKEN.findall(path):
        if name:
            obj = obj[name]
        elif idx:
            obj = obj[int(idx)]
        else:
            obj = next(x for x in obj if str(x.get(key)) == val)
    return obj


@lru_cache(maxsize=256)
def _oracle_cached(tool: str, args_json: str) -> dict:
    """Run a tool once per distinct argument set (results are deterministic, so caching is safe)."""
    out = execute(tool, json.loads(args_json))
    if "error" in out:
        raise ValueError(f"oracle {tool} failed: {out}")
    return out


def oracle(spec: dict) -> dict:
    """The expected tool output for a spec {tool, args} from cases.yaml."""
    return _oracle_cached(spec["tool"], json.dumps(spec.get("args", {}), sort_keys=True))


def value_in_text(value: float, text: str) -> bool:
    """True if some number in the text is `value` as displayed (rounding / share-as-percent allowed)."""
    return any(_matches(n, dec, float(value)) for _, n, dec, _ in numbers_in_text(text))


def args_match(expected: dict, actual: dict) -> bool:
    """Every expected argument must match; a value starting with "~" only has to appear inside the actual
    text (for example "~tampa" matches "Tampa, FL"). Other text compares without regard to case."""
    for k, v in (expected or {}).items():
        a = actual.get(k)
        if isinstance(v, str) and v.startswith("~"):
            if not isinstance(a, str) or v[1:].lower() not in a.lower():
                return False
        elif isinstance(v, str) and isinstance(a, str):
            if v.lower() != a.lower():
                return False
        elif a != v:
            return False
    return True


def hub_name_variants(name: str) -> list[str]:
    """Ways a hub name may be written in an answer ("Newark (NYC metro)" may appear as "Newark")."""
    base = re.sub(r"\s*\(.*\)", "", name)
    return [base, base.replace("St. ", "Saint "), base.replace("Saint ", "St. ")]


def first_position(text: str, name: str) -> int | None:
    """Where a hub is first mentioned in the text (any variant), or None if it is not mentioned."""
    low = text.lower()
    pos = [low.find(v.lower()) for v in hub_name_variants(name)]
    pos = [p for p in pos if p >= 0]
    return min(pos) if pos else None


def check_turn(turn: dict, result: dict) -> list[tuple[str, bool, str]]:
    """Run all checks for one turn. result: AgentResult.to_dict(). Returns (check, passed, detail)."""
    a = result["answer"]
    text = a["answer_markdown"]
    calls = [(t["tool"], t["arguments"] if isinstance(t["arguments"], dict) else {}) for t in result["trace"]]
    out = []

    # Global quality gates
    out.append(("answered", result["stopped_reason"] == "answered", result["stopped_reason"]))
    out.append(("validation_passed", not result["validation_warnings"], "; ".join(result["validation_warnings"])[:200]))
    dashes = re.findall("[\u2014\u2013\u2026]", text)   # em dash, en dash, ellipsis character
    out.append(("no_dashes", not dashes, "".join(dashes)))

    for exp in turn.get("tools", []):
        ok = any(name == exp["name"] and args_match(exp.get("args"), args) for name, args in calls)
        out.append((f"tool:{exp['name']}", ok, f"called {[(n, a) for n, a in calls]}"[:300]))
    for exp in turn.get("tool_arg_paths", []):
        ok = False
        for name, args in calls:
            if name == exp["name"]:
                try:
                    ok = ok or get_path(args, exp["path"]) == exp["value"]
                except (KeyError, IndexError, TypeError, StopIteration):
                    pass
        out.append((f"tool_arg:{exp['path']}", ok, ""))
    if turn.get("no_tools"):
        out.append(("no_tools", not calls, f"called {[n for n, _ in calls]}"))

    for f in turn.get("figures", []):
        specs = f["any_of"] if "any_of" in f else [f]
        expected = []
        for spec in specs:
            try:
                expected.append(get_path(oracle(spec), spec["path"]))
            except (KeyError, IndexError, StopIteration) as e:
                expected.append(f"oracle path error {e}")
        nums = [v for v in expected if isinstance(v, (int, float))]
        ok = any(value_in_text(v, text) for v in nums)
        out.append((f"figure:{specs[0]['path']}", ok, f"expected one of {expected}"))

    # Ranking order: the expected top hubs must all be named, in the same order as the oracle ranking.
    if "order" in turn:
        o = turn["order"]
        names = [h["name"] for h in get_path(oracle(o), o["list"])[: o["top"]]]
        pos = [first_position(text, n) for n in names]
        ok = all(p is not None for p in pos) and pos == sorted(pos)
        out.append(("order", ok, f"expected {names}, positions {pos}"))

    for pat in turn.get("mention", []):
        out.append((f"mention:{pat}", re.search(pat, text, re.I) is not None, ""))
    for pat in turn.get("not_mention", []):
        out.append((f"not_mention:{pat}", re.search(pat, text, re.I) is None, ""))
    if "question_type" in turn:
        out.append(("question_type", a["question_type"] in turn["question_type"], a["question_type"]))
    if "confirm" in turn:
        out.append(("confirm", a["awaiting_user_confirmation"] == turn["confirm"],
                    str(a["awaiting_user_confirmation"])))
    return out
