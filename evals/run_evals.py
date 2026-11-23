"""Run the eval set against the real model and print a scorecard.

    python -m evals.run_evals                      # all cases once
    python -m evals.run_evals --repeat 2           # each case twice (consistency)
    python -m evals.run_evals --case dallas_why    # one case
    python -m evals.run_evals --category names     # one category
    python -m evals.run_evals --model gpt-6.1-terra

Exit code 1 if the case pass rate is below --threshold (default 0.85), for CI.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import yaml

from app.agent.agent import Agent
from app.agent.llm_client import LLMClient
from app.config import get_settings
from evals.checks import check_turn

CASES_FILE = Path(__file__).with_name("cases.yaml")
RESULTS_DIR = Path(__file__).with_name("results")


def run_case(agent: Agent, case: dict) -> dict:
    session, turns, cost, latency = None, [], 0.0, 0
    for turn in case["turns"]:
        r = agent.run_turn(turn["message"], session).to_dict()
        session = r["session_id"]
        checks = check_turn(turn, r)
        cost += r["usage"]["cost_usd"]
        latency += r["latency_ms"]
        turns.append({"message": turn["message"], "checks": checks, "passed": all(c[1] for c in checks),
                      "answer": r["answer"]["answer_markdown"], "tools": [t["tool"] for t in r["trace"]],
                      "latency_ms": r["latency_ms"], "cost_usd": r["usage"]["cost_usd"],
                      "tokens": {k: r["usage"][k] for k in ("input_tokens", "cached_tokens", "output_tokens")}})
    return {"id": case["id"], "category": case["category"], "passed": all(t["passed"] for t in turns),
            "turns": turns, "cost_usd": cost, "latency_ms": latency}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", nargs="*")
    ap.add_argument("--category", nargs="*")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--model")
    ap.add_argument("--threshold", type=float, default=0.85)
    args = ap.parse_args()

    cases = yaml.safe_load(CASES_FILE.read_text(encoding="utf-8"))["cases"]
    if args.case:
        cases = [c for c in cases if c["id"] in args.case]
    if args.category:
        cases = [c for c in cases if c["category"] in args.category]
    model = args.model or get_settings().llm_model
    agent = Agent(llm=LLMClient(model=model))

    runs = []
    for rep in range(args.repeat):
        for case in cases:
            res = run_case(agent, case)
            res["repeat"] = rep + 1
            runs.append(res)
            failed = [f"{c[0]} ({c[2]})" if c[2] else c[0] for t in res["turns"] for c in t["checks"] if not c[1]]
            mark = "PASS" if res["passed"] else "FAIL"
            print(f"{mark}  {res['id']:<30} {res['latency_ms'] / 1000:>5.1f}s  ${res['cost_usd']:.3f}"
                  + (f"   failed: {'; '.join(failed)[:240]}" if failed else ""))

    by_cat, by_check = defaultdict(list), defaultdict(list)
    for r in runs:
        by_cat[r["category"]].append(r["passed"])
        for t in r["turns"]:
            for name, ok, _ in t["checks"]:
                by_check[name.split(":")[0]].append(ok)
    n = len(runs)
    pass_rate = sum(r["passed"] for r in runs) / n if n else 0
    turns = [t for r in runs for t in r["turns"]]
    summary = {
        "run_at": datetime.now().isoformat(timespec="seconds"), "model": model, "repeat": args.repeat,
        "cases": len(cases), "runs": n, "case_pass_rate": round(pass_rate, 3),
        "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_cat.items())},
        "by_check_type": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_check.items())},
        "avg_latency_s_per_turn": round(sum(t["latency_ms"] for t in turns) / len(turns) / 1000, 1) if turns else 0,
        "total_cost_usd": round(sum(r["cost_usd"] for r in runs), 4),
        "avg_cost_usd_per_turn": round(sum(t["cost_usd"] for t in turns) / len(turns), 4) if turns else 0,
    }
    print("\n" + json.dumps(summary, indent=1))
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report = {"summary": summary, "runs": runs}
    (RESULTS_DIR / f"eval-{stamp}.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    if not args.case and not args.category:
        (RESULTS_DIR / "eval-latest.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    sys.exit(0 if pass_rate >= args.threshold else 1)


if __name__ == "__main__":
    main()
