"""Smoke test on the real model: a few conversations, printing answers, traces and cost.

    python -m evals.smoke [--out evals/results/smoke.json]
"""

import argparse
import json
import sys
from datetime import datetime

from app.agent.agent import Agent
from app.agent.cli import show

CONVERSATIONS = [
    ["Which hubs in the Midwest are most exposed to winter disruption?", "What about Chicago?"],
    ["Compare Miami and Houston in terms of hurricane and flood exposure."],
    ["What percentage of days in Denver last year had snowfall?"],
    ["Why is the Dallas hub's weather disruption risk high?"],
    ["What's the weather risk for a hub in Tampa?", "Yes, please analyze it."],
    ["Why is Seattle the riskiest hub in our network?"],
    ["Will it rain in Denver tomorrow?"],
]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="evals/results/smoke.json")
    ap.add_argument("--only", type=int, nargs="*", help="conversation indexes to run")
    args = ap.parse_args()
    agent, results, total = Agent(), [], 0.0
    for i, conv in enumerate(CONVERSATIONS):
        if args.only and i not in args.only:
            continue
        session = None
        for q in conv:
            print("\n" + "=" * 100 + f"\nQ: {q}")
            r = agent.run_turn(q, session)
            session = r.session_id
            show(r, trace=True)
            total += r.usage.cost_usd
            results.append({"question": q, **r.to_dict()})
    print(f"\nTOTAL COST ${total:.4f} for {len(results)} turns")
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"run_at": datetime.now().isoformat(timespec="seconds"), "total_cost_usd": total,
                   "results": results}, f, indent=1, default=str)


if __name__ == "__main__":
    main()
