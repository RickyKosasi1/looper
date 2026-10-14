#!/usr/bin/env python3
"""Stand-in for `claude -p --output-format json` that grinds through turns.

usage: fake_claude.py [claude flags] <argv-log> <turns> <usd-per-turn> <tokens-per-turn> -- <cmd> [args...]

Runs `turns` simulated API turns, each costing `usd-per-turn`. Like the real
CLI, --max-budget-usd is checked after each turn: once spend reaches it the
call stops with an `error_max_budget_usd` result (exit 1) that still reports
the cost. Every invocation's argv is appended to <argv-log> as a JSON line.
The reply text comes from running <cmd> on stdin.
"""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    # Like the real CLI, flags are honored anywhere in argv - the runner
    # appends --max-budget-usd after everything else, past the `--`.
    ceiling = None
    args: list[str] = []
    skip = False
    for index, arg in enumerate(sys.argv[1:], start=1):
        if skip:
            skip = False
        elif arg == "--max-budget-usd":
            ceiling, skip = float(sys.argv[index + 1]), True
        elif arg.startswith("--max-budget-usd="):
            ceiling = float(arg.split("=", 1)[1])
        else:
            args.append(arg)
    sep = args.index("--")
    inner, args = args[sep + 1:], args[:sep]
    rest: list[str] = []
    for index, arg in enumerate(args):
        if skip:
            skip = False
        elif arg == "--output-format":
            skip = True
        elif arg != "-p":
            rest.append(arg)
    log, turns, usd_per_turn, tokens_per_turn = rest[0], int(rest[1]), float(rest[2]), int(rest[3])
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\n")

    prompt = sys.stdin.read()
    spent, done = 0.0, 0
    while done < turns:
        done += 1
        spent += usd_per_turn
        if ceiling is not None and spent >= ceiling and done < turns:
            print(json.dumps({
                "type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                "result": "", "total_cost_usd": round(spent, 6),
                "usage": {"input_tokens": tokens_per_turn * done, "output_tokens": 0},
            }))
            return 1
    reply = subprocess.run(inner, input=prompt, stdout=subprocess.PIPE, text=True, check=False)
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "result": reply.stdout,
        "total_cost_usd": round(spent, 6),
        "usage": {"input_tokens": tokens_per_turn * done, "output_tokens": 0},
    }))
    return reply.returncode


if __name__ == "__main__":
    raise SystemExit(main())
