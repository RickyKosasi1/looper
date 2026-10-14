#!/usr/bin/env python3
"""A host that, like Claude Code, runs a tool in a detached child process.

usage: spawning_host.py <hang|finish> <beat-dir>

The tool starts in its own session (detached on Windows) - out of reach of a
process-group kill - inherits stdout, and appends to <beat-dir>/<pid> every
0.1s for as long as it lives. `hang` then never returns; `finish` replies
like `claude -p --output-format json` (text from fake_host.py) and exits,
leaving the tool running.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

TOOL = """
import os, sys, time
beat = os.path.join(sys.argv[1], str(os.getpid()))
while True:
    with open(beat, "a") as fh:
        fh.write(".")
    time.sleep(0.1)
"""


def main() -> int:
    mode, beat_dir = sys.argv[1], Path(sys.argv[2])
    beat_dir.mkdir(parents=True, exist_ok=True)
    prompt = sys.stdin.read()
    detach = (
        {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    tool = subprocess.Popen([sys.executable, "-c", TOOL, str(beat_dir)], **detach)
    deadline = time.monotonic() + 10
    while not (beat_dir / str(tool.pid)).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    if mode == "hang":
        time.sleep(120)
        return 0
    reply = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("fake_host.py"))],
        input=prompt, stdout=subprocess.PIPE, text=True, check=False,
    ).stdout
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "result": reply,
        "total_cost_usd": 0.01, "usage": {"input_tokens": 5, "output_tokens": 5},
    }))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
