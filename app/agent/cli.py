"""Command-line chat with the agent.

    python -m app.agent.cli "Which Midwest hubs are most exposed to winter disruption?"
    python -m app.agent.cli --interactive --trace
"""

import argparse
import json
import sys

from app.agent.agent import Agent


def show(result, trace: bool) -> None:
    """Print an answer; with trace, also the tool calls, tokens, cost and where each number came from."""
    a = result.answer
    print("\n" + a["answer_markdown"])
    if result.validation_warnings:
        print("\n[validation warnings]\n- " + "\n- ".join(result.validation_warnings))
    if a.get("awaiting_user_confirmation"):
        print("\n(awaiting your confirmation)")
    if trace:
        print("\n--- trace ---")
        for t in result.trace:
            print(f"  {t.tool}({json.dumps(t.arguments)}) {t.ms} ms" + (f"  ERROR {t.error}" if t.error else ""))
        u = result.usage
        print(f"  steps={result.steps} retried={result.retried} llm_calls={u.calls} in={u.input_tokens} "
              f"cached={u.cached_tokens} out={u.output_tokens} (reasoning {u.reasoning_tokens}) "
              f"cost=${u.cost_usd:.4f} latency={result.latency_ms / 1000:.1f}s stop={result.stopped_reason}")
        print(f"  question_type={a['question_type']} measure={a['measure_used']} "
              f"uncertainty={a['uncertainty']['level']} traced_figures={len(a['figures'])}")
        for f in a["figures"]:
            print(f"    {f['value_in_answer']:>8} <- {f['source_tool']}.{f['field']} ({f['match']})")
        print(f"  sources: {a['data_sources']}")


def main() -> None:
    """Ask one question, or chat interactively until an empty line, "exit" or "quit"."""
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="?")
    ap.add_argument("--interactive", "-i", action="store_true")
    ap.add_argument("--trace", "-t", action="store_true")
    args = ap.parse_args()
    agent, session = Agent(), None
    if args.question:
        r = agent.run_turn(args.question, session)
        session = r.session_id
        show(r, args.trace)
    if args.interactive:
        while True:
            try:
                q = input("\nyou> ").strip()
            except EOFError:
                break
            if q in ("", "exit", "quit"):
                break
            r = agent.run_turn(q, session)
            session = r.session_id
            show(r, args.trace)


if __name__ == "__main__":
    main()
