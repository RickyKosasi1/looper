#!/usr/bin/env python3
"""Wrap another fake model's stdout in a real CLI's usage-reporting format.

usage: metered.py <claude|codex|codex-plain> <tokens> <usd> <cmd> [args...]

claude      -> `claude -p --output-format json` result object
codex       -> `codex exec --json` JSONL event stream (tokens only)
codex-plain -> plain `codex exec`: text on stdout, `tokens used` on stderr
"""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    if len(sys.argv) < 5:
        print(__doc__, file=sys.stderr)
        return 2
    fmt, tokens, usd = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
    inner = subprocess.run(
        sys.argv[4:], input=sys.stdin.read(), stdout=subprocess.PIPE, text=True, check=False
    )
    if inner.returncode != 0:
        return inner.returncode
    text = inner.stdout
    if fmt == "claude":
        print(json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "total_cost_usd": usd,
            "usage": {
                "input_tokens": tokens // 4,
                "cache_creation_input_tokens": tokens // 4,
                "cache_read_input_tokens": tokens // 4,
                "output_tokens": tokens - 3 * (tokens // 4),
            },
        }))
    elif fmt == "codex":
        for event in (
            {"type": "thread.started", "thread_id": "fixture"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "thinking"}},
            {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": text}},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": tokens - tokens // 3,
                    "cached_input_tokens": tokens // 5,
                    "output_tokens": tokens // 3,
                },
            },
        ):
            print(json.dumps(event))
    elif fmt == "codex-plain":
        sys.stdout.write(text)
        print(f"tokens used\n{tokens:,}", file=sys.stderr)
    else:
        print(f"unknown format {fmt!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
