#!/usr/bin/env python3
"""Generated Looper runner.

This file executes a resolved loop spec. It intentionally reads only
loop.resolved.json and uses only Python stdlib.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any


PASS = "pass"
REVISE = "revise"

DEFAULT_REDACTIONS = [".env", ".env.*", "secrets/**", "**/*.key"]

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}


class RunnerError(RuntimeError):
    pass


class ModelError(RunnerError):
    """A failed model call; carries whatever usage the CLI reported anyway."""

    def __init__(self, message: str, usage: dict[str, Any] | None = None, kind: str = "") -> None:
        super().__init__(message)
        self.usage = usage
        self.kind = kind


def ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError as exc:
        raise RunnerError(
            "Interactive input required but stdin is closed; run in an interactive terminal"
        ) from exc


def spec_get(data: dict[str, Any], *keys: str) -> Any:
    node: Any = data
    trail: list[str] = []
    for key in keys:
        trail.append(key)
        if not isinstance(node, dict) or key not in node:
            raise RunnerError(f"loop.resolved.json is missing required field: {'.'.join(trail)}")
        node = node[key]
    return node


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise RunnerError(f"Could not read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RunnerError(f"Could not parse JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RunnerError(f"{path} must contain a JSON object")
    return data


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def ensure_argv(value: Any, field: str) -> list[str]:
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return value
    raise RunnerError(f"{field} must be a non-empty argv array")


def relative_to_base(path_text: str, base_dir: Path) -> Path:
    path = Path(path_text)
    resolved = path if path.is_absolute() else base_dir / path
    try:
        resolved.resolve().relative_to(base_dir.resolve())
    except ValueError:
        raise RunnerError(
            f"Path {path_text!r} escapes the loop directory {base_dir}; "
            "workspace and context paths must stay inside the loop directory"
        ) from None
    return resolved


def is_redacted(path: Path, base_dir: Path, globs: list[str]) -> bool:
    try:
        rel = path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        rel = path.name
    # Match each pattern against the relative path and every path suffix so
    # bare patterns like ".env" also cover nested files like "config/.env".
    parts = rel.split("/")
    suffixes = {"/".join(parts[i:]) for i in range(len(parts))}
    for pattern in globs:
        normalized = pattern[3:] if pattern.startswith("**/") else pattern
        if any(fnmatch.fnmatch(candidate, normalized) for candidate in suffixes):
            return True
    return False


def _windows_kill_on_close_job(proc: subprocess.Popen) -> Any:
    """Put a process in a Job Object that kills every member when closed.

    Children inherit the job, so terminating it takes down the whole tree -
    the Windows stand-in for a POSIX process group. None if unavailable.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        extended_limit_information = 9
        if not kernel32.SetInformationJobObject(job, extended_limit_information, ctypes.byref(limits), ctypes.sizeof(limits)) \
                or not kernel32.AssignProcessToJobObject(job, int(proc._handle)):
            kernel32.CloseHandle(job)
            return None
        return (kernel32, job)
    except Exception:
        return None


_SUBREAPER: bool | None = None


def _become_subreaper() -> bool:
    """Linux: adopt orphaned descendants instead of letting init take them.

    A tool process whose parent CLI already exited is otherwise untraceable
    by parentage; as a subreaper the runner stays its ancestor.
    """
    global _SUBREAPER
    if _SUBREAPER is None:
        _SUBREAPER = False
        if sys.platform.startswith("linux"):
            try:
                import ctypes

                libc = ctypes.CDLL(None, use_errno=True)
                _SUBREAPER = libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER
            except Exception:
                pass
    return _SUBREAPER


def _children_by_parent() -> dict[int, list[int]]:
    table: dict[int, list[int]] = {}
    proc_dir = Path("/proc")
    if proc_dir.is_dir():
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            # comm may contain spaces and parens; ppid follows the last ')'.
            fields = stat[stat.rfind(")") + 2:].split()
            if len(fields) > 1 and fields[1].isdigit():
                table.setdefault(int(fields[1]), []).append(int(entry.name))
        return table
    try:
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return table
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            table.setdefault(int(parts[1]), []).append(int(parts[0]))
    return table


def _descendants(roots: list[int]) -> set[int]:
    table = _children_by_parent()
    found: set[int] = set()
    stack = list(roots)
    while stack:
        for child in table.get(stack.pop(), []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


class ProcessTree:
    """A subprocess plus everything it spawns, killable as one unit.

    Model CLIs run tools in child processes - Claude Code starts each Bash
    tool in its own session, out of reach of a process-group kill - and
    killing only the CLI orphans them to keep working outside any budget.
    POSIX follows parentage (on Linux the runner is a subreaper, so tools
    orphaned by an exited CLI stay traceable); Windows uses a Job Object.
    """

    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.job = _windows_kill_on_close_job(proc) if os.name == "nt" else None

    @staticmethod
    def spawn_kwargs() -> dict[str, Any]:
        if os.name == "nt":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}  # own process group, pgid == pid

    def kill(self) -> None:
        if os.name != "nt":
            self._kill_posix()
        elif self.job is not None:
            kernel32, job = self.job
            self.job = None
            kernel32.CloseHandle(job)  # kill-on-close takes the whole job down
        elif self.proc.poll() is None:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            )
        else:
            return
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass

    def _kill_posix(self) -> None:
        # Calls run one at a time, so every descendant of a subreaper runner
        # belongs to this call. Repeat until nothing is left, in case a
        # process forked between the listing and the kill.
        roots = [self.proc.pid] + ([os.getpid()] if _SUBREAPER else [])
        for _ in range(5):
            victims = _descendants(roots)
            if self.proc.poll() is None:
                victims.add(self.proc.pid)
            victims.discard(os.getpid())
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            live = False
            for pid in victims:
                try:
                    os.kill(pid, signal.SIGKILL)
                    live = True
                except (ProcessLookupError, PermissionError):
                    pass
            self._reap(victims - {self.proc.pid})
            if not live:
                return

    @staticmethod
    def _reap(pids: set[int]) -> None:
        """Collect killed processes the runner adopted, so none stay zombies."""
        deadline = time.monotonic() + 2
        pending = set(pids)
        while pending and time.monotonic() < deadline:
            for pid in list(pending):
                try:
                    done, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:  # not ours: init reaps it
                    done = pid
                if done:
                    pending.discard(pid)
            if pending:
                time.sleep(0.02)


def _drain(stream: Any, sink: list[bytes]) -> None:
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                return
            sink.append(chunk)
    except (OSError, ValueError):
        return


def _feed(stream: Any, data: bytes) -> None:
    try:
        stream.write(data)
        stream.close()
    except (OSError, ValueError):
        return


def run_argv(
    argv: list[str],
    *,
    cwd: Path,
    timeout_sec: float,
    stdin: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run argv, then kill everything it spawned.

    The tree is killed as soon as the main process exits or times out: a
    leftover tool process is not the loop's to keep, and one still holding
    stdout would otherwise stall the read until the timeout - turning a
    finished call into a timed-out one. Exit code 124 means timed out.
    """
    if os.name != "nt":
        _become_subreaper()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            **ProcessTree.spawn_kwargs(),
        )
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))
    tree = ProcessTree(proc)
    out: list[bytes] = []
    err: list[bytes] = []
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True),
    ]
    for thread in readers + [threading.Thread(target=_feed, args=(proc.stdin, stdin.encode("utf-8")), daemon=True)]:
        thread.start()
    try:
        returncode = proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        returncode = 124
    finally:
        # Also on Ctrl+C: the child runs in its own session, so the terminal's
        # SIGINT no longer reaches it - without this it would keep spending.
        tree.kill()
    for thread in readers:
        # A descendant that escaped the tree (setsid, job breakaway) can hold
        # a pipe open forever; keep what arrived rather than hang.
        thread.join(timeout=2)
    return subprocess.CompletedProcess(
        argv,
        returncode,
        b"".join(out).decode("utf-8", errors="replace"),
        b"".join(err).decode("utf-8", errors="replace"),
    )


def _json_events(text: str) -> list[dict[str, Any]]:
    """Parse stdout as one JSON object or as JSONL; non-JSON lines are skipped."""
    stripped = text.strip()
    if not stripped.startswith("{"):
        return []
    try:
        whole = json.loads(stripped)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, dict):
        return [whole]
    events: list[dict[str, Any]] = []
    for line in stripped.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_model_output(stdout: str, stderr: str) -> tuple[str, dict[str, Any] | None]:
    """Split CLI output into response text and the usage it reports.

    Recognized formats: Claude `-p --output-format json` / `stream-json`
    (a `result` event carrying `usage` and `total_cost_usd`), Codex
    `exec --json` (`turn.completed` events carrying token `usage`; no cost),
    and plain `codex exec` (a `tokens used` total on stderr). Anything else
    is returned verbatim with usage None - the call is unmetered.
    """
    events = _json_events(stdout)

    results = [e for e in events if e.get("type") == "result" and ("usage" in e or "total_cost_usd" in e)]
    if results:
        final = results[-1]
        text = final.get("result")
        usage = final.get("usage") if isinstance(final.get("usage"), dict) else {}
        # Anthropic reports cache reads/writes outside input_tokens.
        cache = _int(usage.get("cache_creation_input_tokens")) + _int(usage.get("cache_read_input_tokens"))
        input_tokens = _int(usage.get("input_tokens"))
        output_tokens = _int(usage.get("output_tokens"))
        # modelUsage totals every model the session used, and stays filled in
        # on error results, where `usage` comes back zeroed.
        per_model = [u for u in (final.get("modelUsage") or {}).values() if isinstance(u, dict)]
        if per_model:
            cache = sum(_int(u.get("cacheCreationInputTokens")) + _int(u.get("cacheReadInputTokens")) for u in per_model)
            input_tokens = sum(_int(u.get("inputTokens")) for u in per_model)
            output_tokens = sum(_int(u.get("outputTokens")) for u in per_model)
        cost = final.get("total_cost_usd")
        reported = {
            "tokens": input_tokens + cache + output_tokens,
            "input_tokens": input_tokens,
            "cache_tokens": cache,
            "output_tokens": output_tokens,
            "usd": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
        }
        # An error result (e.g. error_max_budget_usd) still spent money.
        if final.get("is_error") or not isinstance(text, str):
            kind = str(final.get("subtype") or "error")
            raise ModelError(f"Model reported an error: {text or kind}", reported, kind)
        return text.strip(), reported

    turns = [e for e in events if e.get("type") in {"turn.completed", "turn.failed"}]
    if turns:
        failed = [e for e in turns if e.get("type") == "turn.failed"]
        messages = [
            e["item"].get("text", "")
            for e in events
            if e.get("type") == "item.completed"
            and isinstance(e.get("item"), dict)
            and e["item"].get("type") == "agent_message"
        ]
        # OpenAI counts cached input inside input_tokens and reasoning
        # inside output_tokens, so the two fields are the whole total.
        input_tokens = sum(_int((e.get("usage") or {}).get("input_tokens")) for e in turns)
        output_tokens = sum(_int((e.get("usage") or {}).get("output_tokens")) for e in turns)
        # A failed turn reports no usage of its own: only completed turns
        # are known spend.
        reported = None
        if any(e.get("type") == "turn.completed" for e in turns):
            reported = {
                "tokens": input_tokens + output_tokens,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "usd": None,
            }
        if failed:
            error = failed[-1].get("error") or {}
            raise ModelError(f"Model reported an error: {error.get('message', 'turn failed')}", reported, "turn_failed")
        return (messages[-1] if messages else "").strip(), reported

    totals = re.findall(r"tokens used\s*:?\s*([\d,]+)", stderr, re.IGNORECASE)
    if totals:
        return stdout.strip(), {"tokens": int(totals[-1].replace(",", "")), "usd": None}
    return stdout.strip(), None


def invoke_meters(member: dict[str, Any], key: str) -> bool | None:
    """Whether a member's invoke reports `key` ("tokens" or "usd").

    True/False for CLIs whose output parse_model_output knows; None when the
    argv alone cannot tell (a wrapper script may well emit Claude JSON).
    Local members cost nothing, so USD is always counted for them.
    """
    if key == "usd" and member.get("local"):
        return True
    argv = member.get("invoke", []) or []
    exe = Path(argv[0]).stem.lower() if argv else ""
    if exe == "claude":
        fmt = ""
        for index, arg in enumerate(argv):
            if arg == "--output-format" and index + 1 < len(argv):
                fmt = argv[index + 1]
            elif arg.startswith("--output-format="):
                fmt = arg.split("=", 1)[1]
        return fmt in {"json", "stream-json"}
    if exe == "codex":
        return key == "tokens" and "exec" in argv
    if exe in {"gemini", "llm", "ollama"}:
        return False
    return None


def with_usd_ceiling(argv: list[str], remaining_usd: float) -> list[str]:
    """Hand the remaining USD budget to Claude as --max-budget-usd.

    Claude checks it between API turns, so a long agentic call stops itself
    near the cap instead of finishing. An existing flag keeps the lower value.
    """
    # Round down: never grant a call more than is actually left.
    ceiling = math.floor(max(remaining_usd, 0.0) * 1_000_000) / 1_000_000
    out = list(argv)
    for index, arg in enumerate(out):
        if arg == "--max-budget-usd" and index + 1 < len(out):
            try:
                ceiling = min(ceiling, float(out[index + 1]))
            except ValueError:
                pass
            out[index + 1] = f"{ceiling:.6f}"
            return out
        if arg.startswith("--max-budget-usd="):
            try:
                ceiling = min(ceiling, float(arg.split("=", 1)[1]))
            except ValueError:
                pass
            out[index] = f"--max-budget-usd={ceiling:.6f}"
            return out
    return out + ["--max-budget-usd", f"{ceiling:.6f}"]


def call_model(
    member: dict[str, Any],
    prompt: str,
    base_dir: Path,
    *,
    argv: list[str] | None = None,
    timeout_sec: float | None = None,
) -> tuple[str, dict[str, Any] | None]:
    argv = argv or ensure_argv(member.get("invoke"), f"{member.get('id', member.get('cli', 'model'))}.invoke")
    if timeout_sec is None:
        timeout_sec = int(member.get("timeout_sec", 600))
    result = run_argv(argv, cwd=base_dir, timeout_sec=timeout_sec, stdin=prompt)
    # Parse even a failed call: its reported usage is real spend.
    text, usage = parse_model_output(result.stdout, result.stderr)
    if result.returncode != 0:
        raise ModelError(
            f"Model invocation failed ({' '.join(argv)}): exit {result.returncode}\n{result.stderr}",
            usage,
            "timeout" if result.returncode == 124 else "exit",
        )
    return text, usage


def _judge_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1).strip())
    stripped = text.strip()
    candidates.append(stripped)
    brace = stripped.find("{")
    if brace > 0:
        candidates.append(stripped[brace:])
    return [candidate for candidate in candidates if candidate]


def parse_judge_output(text: str) -> dict[str, Any]:
    parsed: Any = None
    for candidate in _judge_candidates(text):
        try:
            parsed = json.loads(candidate)
            break
        except json.JSONDecodeError:
            try:
                # Tolerate prose after the JSON object (e.g. a trailing summary).
                parsed, _ = json.JSONDecoder().raw_decode(candidate)
                break
            except json.JSONDecodeError:
                continue
    if parsed is None:
        return {
            "verdict": REVISE,
            "blocking_issues": ["Judge output was not parseable JSON."],
            "confidence": 0.0,
            "notes": text.strip(),
            "warning": "unparseable_judge_output",
        }
    if not isinstance(parsed, dict):
        return {
            "verdict": REVISE,
            "blocking_issues": ["Judge output was not a JSON object."],
            "confidence": 0.0,
            "notes": text.strip(),
            "warning": "invalid_judge_output",
        }
    verdict = parsed.get("verdict")
    if verdict not in {PASS, REVISE}:
        parsed["verdict"] = REVISE
        parsed.setdefault("blocking_issues", []).append("Judge verdict was not pass or revise.")
    parsed.setdefault("blocking_issues", [])
    parsed.setdefault("confidence", 0.0)
    parsed.setdefault("notes", "")
    return parsed


class Runner:
    def __init__(self, spec_path: Path) -> None:
        self.spec_path = spec_path.resolve()
        self.base_dir = self.spec_path.parent
        self.spec = load_json(self.spec_path)
        self.workspace = relative_to_base(str(spec_get(self.spec, "workspace", "dir")), self.base_dir)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.observability = self.spec.get("observability", {})
        self.run_log_path = self.workspace / self.observability.get("run_log", "run-log.md")
        self.state_path = self.workspace / self.observability.get("state_file", "state.json")
        self.state = self.load_state()
        # Wall-clock budget accrues across resumed runs.
        self.prior_elapsed = float(self.state.get("wall_clock_used_sec", 0) or 0)
        self.started = time.monotonic()
        self.stop_reason: str | None = None
        # Per-run caches for the scrub layer: flagged-file contents are read
        # once per glob set, surfaced events are logged once per destination.
        self._flagged_cache: dict[tuple[str, ...], list[tuple[str, str]]] = {}
        self._unscrubbable_reported: set[str] = set()
        self._surfaced_events: set[tuple[str, tuple[str, ...]]] = set()

    def load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            return load_json(self.state_path)
        return {
            "status": "initialized",
            "started_at": utc_now(),
            "iteration": 0,
            "warnings": [],
            "consent": {},
        }

    def elapsed_sec(self) -> float:
        return self.prior_elapsed + (time.monotonic() - self.started)

    def save_state(self, **updates: Any) -> None:
        self.state.update(updates)
        self.state["updated_at"] = utc_now()
        self.state["wall_clock_used_sec"] = round(self.elapsed_sec(), 1)
        write_json(self.state_path, self.state)

    def append_log(self, event: str, **fields: Any) -> None:
        self.run_log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = f" {json.dumps(fields, sort_keys=True)}" if fields else ""
        with self.run_log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"- {utc_now()} `{event}`{payload}\n")

    def budget(self) -> dict[str, Any]:
        return self.spec.get("loop_control", {}).get("budget", {}) or {}

    def remaining_wall_clock_sec(self) -> float | None:
        wall_clock_min = self.budget().get("wall_clock_min")
        if wall_clock_min is None:
            return None
        return float(wall_clock_min) * 60 - self.elapsed_sec()

    def remaining_usd(self) -> float | None:
        cap = self.budget().get("usd")
        if cap is None:
            return None
        return float(cap) - float((self.state.get("usage", {}) or {}).get("usd", 0) or 0)

    def budget_stop(self, reason: str, message: str, **fields: Any) -> None:
        self.save_state(status="failed", failure=reason)
        self.append_log("stop", reason=reason, **fields)
        raise RunnerError(message)

    def enforce_budget(self) -> None:
        # Usage accrues across resumes. Token/USD caps stop once reached:
        # with nothing left there is no budget to hand the next call.
        budget = self.budget()
        used = self.state.get("usage", {}) or {}
        remaining_sec = self.remaining_wall_clock_sec()
        if remaining_sec is not None and remaining_sec < 0:
            self.budget_stop(
                "wall_clock_budget_exceeded",
                "Wall-clock budget exceeded",
                used=round(self.elapsed_sec(), 1),
                cap=float(budget["wall_clock_min"]) * 60,
            )
        tokens = int(used.get("tokens", 0) or 0)
        if budget.get("tokens") is not None and tokens >= budget["tokens"]:
            self.budget_stop(
                "token_budget_exceeded",
                f"Token budget exceeded: {tokens} of {budget['tokens']} tokens used",
                used=tokens,
                cap=budget["tokens"],
            )
        usd = round(float(used.get("usd", 0) or 0), 6)
        if budget.get("usd") is not None and usd >= float(budget["usd"]):
            self.budget_stop(
                "usd_budget_exceeded",
                f"USD budget exceeded: ${usd:.4f} of ${budget['usd']} spent",
                used=usd,
                cap=float(budget["usd"]),
            )

    def record_usage(self, who: str, member: dict[str, Any], usage: dict[str, Any] | None) -> None:
        totals = dict(self.state.get("usage", {}) or {})
        by_member = dict(totals.get("by_member", {}) or {})
        mine = dict(by_member.get(who, {}) or {})
        totals["calls"] = int(totals.get("calls", 0) or 0) + 1
        mine["calls"] = int(mine.get("calls", 0) or 0) + 1
        tokens = (usage or {}).get("tokens")
        usd = (usage or {}).get("usd")
        if usd is None and member.get("local"):
            usd = 0.0
        unmetered = []
        for key, value in (("tokens", tokens), ("usd", usd)):
            if value is None:
                untracked = dict(totals.get("untracked_calls", {}) or {})
                untracked[key] = int(untracked.get(key, 0) or 0) + 1
                totals["untracked_calls"] = untracked
                unmetered.append(key)
                continue
            if key == "usd":
                totals[key] = round(float(totals.get(key, 0) or 0) + value, 6)
                mine[key] = round(float(mine.get(key, 0) or 0) + value, 6)
            else:
                totals[key] = int(totals.get(key, 0) or 0) + value
                mine[key] = int(mine.get(key, 0) or 0) + value
        by_member[who] = mine
        totals["by_member"] = by_member
        self.save_state(usage=totals)
        if usage is not None:
            self.append_log("model_usage", member=who, tokens=tokens, usd=usd)
        # Fail closed: a cap that cannot count this call is not enforceable,
        # and an unenforceable cap is worse than none.
        for key in unmetered:
            if self.budget().get(key) is not None:
                self.refuse_unenforceable_budget(who, member, key, status="failed")

    def refuse_unenforceable_budget(self, who: str, member: dict[str, Any], key: str, *, status: str) -> None:
        argv = " ".join(member.get("invoke", []) or [])
        fix = (
            "invoke Claude as `claude -p --output-format json` or Codex as `codex exec --json`"
            if key == "tokens"
            else "only `claude -p --output-format json` reports cost - switch the member or drop budget.usd"
        )
        reason = f"budget_unenforceable:{key}:{who}"
        self.save_state(status=status, failure=reason)
        self.append_log("stop", reason=reason, invoke=argv)
        raise RunnerError(f"budget.{key} cannot be enforced: {who} ({argv}) reports no {key}; {fix}")

    def invoked_members(self) -> list[tuple[str, dict[str, Any]]]:
        """Every model this run can call: the host, reviewers, verdict sources."""
        members: list[tuple[str, dict[str, Any]]] = [("host", spec_get(self.spec, "host"))]
        seen: set[str] = set()
        for gate in (self.spec.get("gates") or {}).values():
            ids = [mid for mid in gate.get("members", []) or [] if self.member(mid).get("role") == "reviewer"]
            source = gate.get("verdict_source")
            if gate.get("verdict_policy") == "revise_until_clean" and source and source != "human":
                ids.append(source)
            for mid in ids:
                if mid not in seen:
                    seen.add(mid)
                    members.append((mid, self.member(mid)))
        return members

    def preflight_budget(self) -> None:
        # Refuse before spending anything when a set cap provably cannot count
        # some member; argv the runner cannot judge is caught on its first call.
        for key in ("tokens", "usd"):
            if self.budget().get(key) is None:
                continue
            for who, member in self.invoked_members():
                if invoke_meters(member, key) is False:
                    self.refuse_unenforceable_budget(who, member, key, status="blocked")

    def invoke_member(self, who: str, member: dict[str, Any], prompt: str) -> str:
        # Hand what is left of the budget down to the call itself where the
        # CLI can enforce it: the remaining wall clock caps the subprocess
        # timeout, and Claude gets the remaining USD as --max-budget-usd. Token
        # caps have no per-call lever, so a call can still cross one; the
        # check before the next call then stops the run.
        self.enforce_budget()
        argv = ensure_argv(member.get("invoke"), f"{who}.invoke")
        timeout_sec: float = int(member.get("timeout_sec", 600))
        remaining_sec = self.remaining_wall_clock_sec()
        clamped = remaining_sec is not None and remaining_sec < timeout_sec
        if clamped:
            timeout_sec = max(remaining_sec, 0.0)
        remaining_usd = self.remaining_usd()
        if remaining_usd is not None and not member.get("local") and Path(argv[0]).stem.lower() == "claude":
            argv = with_usd_ceiling(argv, remaining_usd)
        try:
            text, usage = call_model(member, prompt, self.base_dir, argv=argv, timeout_sec=timeout_sec)
        except ModelError as exc:
            if exc.kind == "timeout" and clamped:
                self.budget_stop(
                    "wall_clock_budget_exceeded",
                    f"Wall-clock budget exceeded: {who} was stopped when the budget ran out",
                    member=who,
                )
            if exc.usage is not None:
                self.record_usage(who, member, exc.usage)
            if exc.kind == "error_max_budget_usd":
                spent = round(float((self.state.get("usage", {}) or {}).get("usd", 0) or 0), 6)
                self.budget_stop(
                    "usd_budget_exceeded",
                    f"USD budget exceeded: {who} stopped at its --max-budget-usd ceiling "
                    f"(${spent:.4f} of ${self.budget().get('usd')} spent)",
                    member=who,
                    used=spent,
                    cap=self.budget().get("usd"),
                )
            raise
        self.record_usage(who, member, usage)
        return text

    def no_progress_reached(self, gate_name: str, failures: list[str]) -> bool:
        if not failures:
            self.save_state(no_progress={"count": 0, "signature": "", "gate": gate_name})
            return False
        config = self.spec.get("loop_control", {}).get("no_progress", {})
        threshold = int(config.get("max_stalled_iterations", 2))
        signature = "\n".join(sorted(failures))
        previous = self.state.get("no_progress", {})
        same_gate = previous.get("gate") == gate_name
        same_signature = previous.get("signature") == signature
        count = int(previous.get("count", 0)) + 1 if same_gate and same_signature else 1
        progress = {
            "gate": gate_name,
            "signature": signature,
            "count": count,
            "threshold": threshold,
            "updated_at": utc_now(),
        }
        self.save_state(no_progress=progress)
        if count < threshold:
            return False
        self.append_log("no_progress_detected", gate=gate_name, count=count, failures=failures)
        if config.get("action", "stop") == "human_checkpoint":
            answer = ask("No-progress detected. Type 'continue' to allow one more revision: ").strip().lower()
            if answer == "continue":
                progress["count"] = 0
                self.save_state(no_progress=progress)
                self.append_log("no_progress_override", gate=gate_name)
                return False
        self.stop_reason = "no_progress_detected"
        self.save_state(status="failed", failure="no_progress_detected", blocking_issues=failures)
        return True

    def criteria(self, ids: list[str]) -> list[dict[str, Any]]:
        by_id = self.spec.get("criteria_by_id", {})
        missing = [item for item in ids if item not in by_id]
        if missing:
            raise RunnerError(f"Gate references unknown criteria: {', '.join(missing)}")
        return [by_id[item] for item in ids]

    def member(self, member_id: str) -> dict[str, Any]:
        by_id = spec_get(self.spec, "council_by_id")
        if member_id not in by_id:
            raise RunnerError(f"Unknown council member: {member_id}")
        return by_id[member_id]

    def redactions_for(self, member_id: str) -> list[str]:
        # Defaults always apply; configured egress redactions extend them.
        redactions = list(DEFAULT_REDACTIONS)
        for entry in self.spec.get("privacy", {}).get("egress", []):
            if entry.get("to") == member_id:
                for pattern in entry.get("redact", []):
                    if pattern not in redactions:
                        redactions.append(pattern)
        return redactions

    def all_redaction_globs(self) -> list[str]:
        globs = list(DEFAULT_REDACTIONS)
        for entry in self.spec.get("privacy", {}).get("egress", []):
            for pattern in entry.get("redact", []):
                if pattern not in globs:
                    globs.append(pattern)
        return globs

    def iter_redaction_files(self, globs: list[str]):
        for root, dirs, files in os.walk(self.base_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for name in files:
                path = Path(root) / name
                if is_redacted(path, self.base_dir, globs):
                    yield path

    def flagged_file_contents(self, globs: list[str]) -> list[tuple[str, str]]:
        """Read flagged files once per glob set; surface the unreadable ones.

        A flagged file the scrub cannot read (too large, not UTF-8) cannot be
        detected if its content leaks, so that blind spot is reported instead
        of silently skipped.
        """
        key = tuple(sorted(globs))
        if key in self._flagged_cache:
            return self._flagged_cache[key]
        contents: list[tuple[str, str]] = []
        for path in self.iter_redaction_files(globs):
            rel = path.relative_to(self.base_dir).as_posix()
            reason = ""
            try:
                if path.stat().st_size > 1_000_000:
                    reason = "larger than 1MB"
                else:
                    secret_text = path.read_text(encoding="utf-8")
                    if secret_text.strip():
                        contents.append((rel, secret_text))
                    continue
            except UnicodeDecodeError:
                reason = "not valid UTF-8"
            except OSError as exc:
                reason = f"unreadable ({exc})"
            if reason and rel not in self._unscrubbable_reported:
                self._unscrubbable_reported.add(rel)
                self.append_log("redaction_unscrubbable", source=rel, reason=reason)
                self.add_state_warning(
                    f"flagged file {rel} is {reason}; its content cannot be "
                    "detected by the scrub layer if it leaks into artifacts"
                )
        self._flagged_cache[key] = contents
        return contents

    def scrub_flagged_content(self, text: str, globs: list[str]) -> tuple[str, list[str]]:
        """Remove content originating from redaction-glob files.

        The flagged files themselves are never read into prompts (path-based
        non-send in gather_context); this second layer catches their content
        when it re-surfaces elsewhere - a cmd context source that printed it,
        or an artifact a model copied it into. Returns the scrubbed text and
        the relative paths whose content was found. Best effort by design,
        erring toward over-redaction: reformatted content or lines shorter
        than 8 characters can survive, and a flagged-file line that
        legitimately appears elsewhere is masked too.
        """
        original = text
        hits: list[str] = []
        for rel, secret_text in self.flagged_file_contents(globs):
            marker = f"[redacted:{rel}]"
            # Detect against the original text so a secret shared by two
            # flagged files attributes both, not just the first replaced.
            lines = [line.strip() for line in secret_text.splitlines() if len(line.strip()) >= 8]
            if secret_text in original or any(line in original for line in lines):
                hits.append(rel)
            text = text.replace(secret_text, marker)
            for line in lines:
                text = text.replace(line, marker)
        return text, hits

    def add_state_warning(self, note: str) -> None:
        warnings = list(self.state.get("warnings", []) or [])
        if note not in warnings:
            warnings.append(note)
            self.save_state(warnings=warnings)

    def surface_redaction(self, where: str, hits: list[str]) -> None:
        if not hits:
            return
        event_key = (where, tuple(hits))
        if event_key not in self._surfaced_events:
            self._surfaced_events.add(event_key)
            self.append_log("redaction_applied", where=where, sources=hits)
        self.add_state_warning(
            f"flagged content from {', '.join(hits)} appeared in {where}; "
            "scrubbed before use"
        )

    def redact_prompt_for_member(self, member_id: str, prompt: str) -> str:
        scrubbed, hits = self.scrub_flagged_content(prompt, self.redactions_for(member_id))
        self.surface_redaction(f"prompt for {member_id}", hits)
        return scrubbed

    def ensure_consent(self, member_id: str) -> None:
        member = self.member(member_id)
        if member.get("local"):
            return
        if self.state.get("consent", {}).get(member_id):
            return
        matching = [
            entry
            for entry in self.spec.get("privacy", {}).get("egress", [])
            if entry.get("to") == member_id
        ]
        # Consent fails closed: a non-local member always needs consent unless
        # every egress entry for it explicitly pre-grants with consent: granted.
        if matching and all(entry.get("consent") == "granted" for entry in matching):
            return
        sends = sorted({item for entry in matching for item in entry.get("sends", [])})
        redactions = self.redactions_for(member_id)
        print()
        print(f"Looper is about to send {', '.join(sends) or 'loop artifacts'} to {member_id}.")
        print(f"CLI: {member.get('cli')} / model: {member.get('model', 'default')}")
        print(f"Redactions: {', '.join(redactions)}")
        for note in self.state.get("warnings", []) or []:
            if f"prompt for {member_id}" in note:
                print(f"Warning: {note}")
        if not matching:
            print("No privacy.egress entry covers this member; consent is required by default.")
        answer = ask("Type 'yes' to consent to this first send: ").strip().lower()
        if answer != "yes":
            self.save_state(status="blocked", failure=f"consent_refused:{member_id}")
            raise RunnerError(f"Consent refused for {member_id}")
        consent = dict(self.state.get("consent", {}))
        consent[member_id] = {"granted_at": utc_now(), "sends": sends, "redact": redactions}
        self.save_state(consent=consent)

    def gather_context(self) -> str:
        goal = spec_get(self.spec, "goal")
        redaction_globs = self.all_redaction_globs()
        chunks: list[str] = []
        for index, source in enumerate(goal.get("context_sources", []) or [], start=1):
            self.enforce_budget()
            if "file" in source:
                try:
                    path = relative_to_base(source["file"], self.base_dir)
                except RunnerError:
                    chunks.append(f"## Context source {index}: {source['file']}\n[blocked: outside loop directory]\n")
                    self.append_log("context", source=source["file"], status="blocked_outside_base")
                    continue
                if is_redacted(path, self.base_dir, redaction_globs):
                    chunks.append(f"## Context source {index}: {source['file']}\n[redacted]\n")
                    self.append_log("context", source=source["file"], status="redacted")
                elif path.exists():
                    chunks.append(f"## Context source {index}: {source['file']}\n{path.read_text(encoding='utf-8')}\n")
                    self.append_log("context", source=source["file"], status="read")
                else:
                    chunks.append(f"## Context source {index}: {source['file']}\n[missing]\n")
                    self.append_log("context", source=source["file"], status="missing")
            elif "cmd" in source:
                argv = ensure_argv(source["cmd"], f"context_sources[{index}].cmd")
                result = run_argv(argv, cwd=self.base_dir, timeout_sec=int(source.get("timeout_sec", 60)))
                # Command output can reproduce flagged-file content (cat, git
                # log, env dumps); scrub it before it enters any prompt.
                block = (
                    f"exit={result.returncode}\nstdout:\n{result.stdout}\n"
                    f"stderr:\n{result.stderr}\n"
                )
                block, hits = self.scrub_flagged_content(block, redaction_globs)
                self.surface_redaction(f"context command output ({' '.join(argv)})", hits)
                chunks.append(f"## Context source {index}: {' '.join(argv)}\n{block}")
                self.append_log("context_cmd", argv=argv, returncode=result.returncode)
        context = "\n".join(chunks).strip()
        write_text(self.workspace / "context.md", context or "No context sources configured.")
        return context

    def host_prompt(self, phase: str, artifact: str = "", review: str = "") -> str:
        statement = spec_get(self.spec, "goal", "statement")
        done = spec_get(self.spec, "goal").get("definition_of_done", "")
        if phase == "plan":
            return (
                "Draft plan.md for this loop.\n\n"
                f"Goal:\n{statement}\n\n"
                f"Definition of done:\n{done}\n\n"
                f"Context:\n{(self.workspace / 'context.md').read_text(encoding='utf-8')}\n"
            )
        if phase == "delivery":
            return (
                "Write the next delivery artifact for this loop.\n\n"
                f"Goal:\n{statement}\n\n"
                f"Definition of done:\n{done}\n\n"
                f"Plan:\n{(self.workspace / 'plan.md').read_text(encoding='utf-8')}\n"
            )
        if phase == "revise":
            return (
                "Revise the artifact to address the review. Return only the revised artifact.\n\n"
                f"Artifact:\n{artifact}\n\nReview:\n{review}\n"
            )
        raise RunnerError(f"Unknown host phase: {phase}")

    def call_host(self, prompt: str) -> str:
        # The host is a send like any other: flagged content is scrubbed for
        # every recipient, host included. Files a loop genuinely needs must
        # not match redaction globs.
        scrubbed, hits = self.scrub_flagged_content(prompt, self.all_redaction_globs())
        self.surface_redaction("prompt for host", hits)
        return self.invoke_member("host", spec_get(self.spec, "host"), scrubbed)

    def run_host(self, phase: str, target: Path, artifact: str = "", review: str = "") -> None:
        self.enforce_budget()
        self.append_log("host_start", phase=phase, target=target.name)
        output = self.call_host(self.host_prompt(phase, artifact, review))
        write_text(target, output)
        self.append_log("host_done", phase=phase, target=target.name)

    def run_programmatic(self, criterion: dict[str, Any]) -> dict[str, Any]:
        argv = ensure_argv(criterion["check"], f"{criterion['id']}.check")
        result = run_argv(argv, cwd=self.base_dir, timeout_sec=int(criterion.get("timeout_sec", 300)))
        expect = criterion.get("expect")
        passed = False
        if expect == "exit_zero":
            passed = result.returncode == 0
        elif expect == "exit_nonzero":
            passed = result.returncode != 0
        elif expect == "stdout_contains":
            passed = criterion.get("contains", "") in result.stdout
        self.append_log(
            "programmatic_check",
            criterion=criterion["id"],
            passed=passed,
            returncode=result.returncode,
        )
        return {
            "id": criterion["id"],
            "type": "programmatic",
            "passed": passed,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def judge_prompt(
        self,
        gate_name: str,
        artifact_label: str,
        artifact_text: str,
        criteria: list[dict[str, Any]],
    ) -> str:
        rubric_lines = []
        for criterion in criteria:
            if criterion["type"] == "judge":
                rubric_lines.append(f"- {criterion['id']}: {criterion['rubric']}")
            elif criterion["type"] == "programmatic":
                rubric_lines.append(f"- {criterion['id']}: programmatic check result is included below.")
            elif criterion["type"] == "human":
                rubric_lines.append(f"- {criterion['id']}: human signoff is required separately.")
        return (
            "You are the Looper judge. Return only a fenced JSON object with keys "
            "verdict, blocking_issues, confidence, and notes. verdict must be pass or revise.\n\n"
            f"Gate: {gate_name}\n"
            f"Artifact: {artifact_label}\n\n"
            "Criteria:\n" + "\n".join(rubric_lines) + "\n\n"
            f"Artifact content:\n{artifact_text}\n"
        )

    def run_judge(
        self,
        member_id: str,
        gate_name: str,
        artifact_label: str,
        artifact_text: str,
        criteria: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # Scrub (and surface any leak) before asking for consent, so the
        # consent decision is made with the leak warning already visible.
        prompt = self.redact_prompt_for_member(
            member_id,
            self.judge_prompt(gate_name, artifact_label, artifact_text, criteria),
        )
        self.ensure_consent(member_id)
        output = self.invoke_member(member_id, self.member(member_id), prompt)
        verdict = parse_judge_output(output)
        verdict["member"] = member_id
        self.append_log("judge_verdict", gate=gate_name, member=member_id, verdict=verdict.get("verdict"))
        return verdict

    def run_reviewers(
        self,
        gate_name: str,
        artifact_label: str,
        artifact_text: str,
        member_ids: list[str],
    ) -> list[str]:
        notes = []
        for member_id in member_ids:
            member = self.member(member_id)
            if member.get("role") != "reviewer":
                continue
            prompt = (
                "You are a Looper reviewer. Return concise blocking and non-blocking notes. "
                "Do not return a verdict.\n\n"
                f"Gate: {gate_name}\nArtifact: {artifact_label}\n\n{artifact_text}\n"
            )
            prompt = self.redact_prompt_for_member(member_id, prompt)
            self.ensure_consent(member_id)
            notes.append(f"## {member_id}\n\n{self.invoke_member(member_id, member, prompt)}")
            self.append_log("reviewer_notes", gate=gate_name, member=member_id)
        return notes

    def human_check(self, criterion: dict[str, Any]) -> dict[str, Any]:
        print()
        print(criterion["prompt"])
        answer = ask("Type 'pass' to approve, anything else to request revision: ").strip().lower()
        return {
            "id": criterion["id"],
            "type": "human",
            "passed": answer == PASS,
            "notes": "approved" if answer == PASS else "human requested revision",
        }

    def run_gate(self, gate_name: str, artifact_path: Path, artifact_label: str) -> bool:
        gate = spec_get(self.spec, "gates", gate_name)
        criteria = self.criteria(gate.get("criteria", []) or [])
        max_revisions = int(gate.get("max_revisions", 0))
        revision = 0
        self.append_log("gate_start", gate=gate_name, artifact=artifact_label)

        while True:
            self.enforce_budget()
            artifact_text = artifact_path.read_text(encoding="utf-8")
            review_parts: list[str] = []
            failures: list[str] = []

            for criterion in criteria:
                if criterion["type"] == "programmatic":
                    result = self.run_programmatic(criterion)
                    review_parts.append(f"## Programmatic {criterion['id']}\n\n```json\n{json.dumps(result, indent=2)}\n```")
                    if not result["passed"]:
                        failures.append(f"Programmatic check failed: {criterion['id']}")
                elif criterion["type"] == "human":
                    result = self.human_check(criterion)
                    review_parts.append(f"## Human {criterion['id']}\n\n{result['notes']}")
                    if not result["passed"]:
                        failures.append(f"Human check failed: {criterion['id']}")

            reviewer_notes = self.run_reviewers(
                gate_name,
                artifact_label,
                artifact_text,
                list(gate.get("members", [])),
            )
            review_parts.extend(reviewer_notes)

            policy = gate.get("verdict_policy")
            verdict: dict[str, Any] | None = None
            if policy == "revise_until_clean" and not failures:
                source = gate.get("verdict_source")
                if source == "human":
                    answer = ask(f"Type 'pass' if {artifact_label} is clean: ").strip().lower()
                    verdict = {
                        "verdict": PASS if answer == PASS else REVISE,
                        "blocking_issues": [] if answer == PASS else ["human requested revision"],
                        "confidence": 1.0,
                        "notes": "human verdict",
                    }
                else:
                    verdict = self.run_judge(source, gate_name, artifact_label, artifact_text, criteria)
                review_parts.append(f"## Verdict\n\n```json\n{json.dumps(verdict, indent=2)}\n```")
                if verdict.get("verdict") == REVISE:
                    failures.extend(verdict.get("blocking_issues") or ["Judge requested revision"])

            synthetic_pass = False
            if policy == "fixed_passes":
                if failures:
                    pass
                elif revision >= max_revisions:
                    return True
                else:
                    # Not a real failure: it only forces another reviewer pass,
                    # so it must not feed the no-progress stall detector.
                    synthetic_pass = True
                    failures.append("fixed_passes reviewer pass")

            if not failures:
                self.save_state(status=f"{gate_name}_passed", **{gate_name: {"passed_at": utc_now()}})
                self.append_log("gate_passed", gate=gate_name, artifact=artifact_label)
                return True

            review_text = "\n\n".join(review_parts + ["## Blocking Issues", "\n".join(f"- {item}" for item in failures)])
            review_path = self.workspace / f"review-{gate_name}-{revision + 1}.md"
            write_text(review_path, review_text)
            self.append_log("gate_blocked", gate=gate_name, review=review_path.name, failures=failures)

            if not synthetic_pass and self.no_progress_reached(gate_name, failures):
                return False

            if revision >= max_revisions:
                self.save_state(
                    status="failed",
                    failure=f"{gate_name}_max_revisions_reached",
                    last_review=str(review_path),
                )
                self.append_log("stop", reason=f"{gate_name}_max_revisions_reached")
                return False

            revised = self.call_host(self.host_prompt("revise", artifact_text, review_text))
            write_text(artifact_path, revised)
            revision += 1
            self.save_state(status=f"{gate_name}_revision_{revision}", last_review=str(review_path))
            self.append_log("revision", gate=gate_name, revision=revision, artifact=artifact_label)

    def human_checkpoint(self, name: str) -> None:
        checkpoints = self.spec.get("loop_control", {}).get("human_checkpoints", []) or []
        if name not in checkpoints:
            return
        answer = ask(f"Human checkpoint {name!r}: type 'continue' to proceed: ").strip().lower()
        if answer != "continue":
            self.save_state(status="blocked", failure=f"human_checkpoint:{name}")
            self.append_log("stop", reason=f"human_checkpoint:{name}")
            raise RunnerError(f"Stopped at human checkpoint {name}")
        self.append_log("human_checkpoint_passed", checkpoint=name)

    def run(self) -> int:
        try:
            return self._run()
        except BaseException as exc:
            # Any crash or interruption (Ctrl+C, SIGTERM) - not just
            # RunnerError - must leave a terminal state: a state file claiming
            # "running" after the process died is a phantom in-flight run for
            # resume logic and audits.
            error = str(exc) or type(exc).__name__
            if self.state.get("status") not in {"failed", "blocked"}:
                self.save_state(status="failed", failure=error)
            self.append_log("run_error", error=error)
            raise

    def _run(self) -> int:
        if self.state.get("status") == "passed":
            print("Looper run already passed. Delete the workspace state to re-run.")
            return 0
        # Resuming: stale stall counts from an interrupted run must not
        # insta-trip the detector on the first new gate round.
        self.save_state(status="running", no_progress={"count": 0, "signature": "", "gate": ""})
        self.append_log("run_start", spec=str(self.spec_path))
        self.preflight_budget()
        # A resumed run that already spent its budget stops before any work.
        self.enforce_budget()
        self.gather_context()

        plan_path = self.workspace / "plan.md"
        if not plan_path.exists():
            self.run_host("plan", plan_path)
        if not self.run_gate("plan_gate", plan_path, "plan.md"):
            return 1
        self.human_checkpoint("after_plan")

        max_iterations = int(spec_get(self.spec, "loop_control", "max_iterations"))
        start_iteration = max(1, int(self.state.get("iteration", 0) or 0))
        for iteration in range(start_iteration, max_iterations + 1):
            self.enforce_budget()
            self.save_state(status="delivery", iteration=iteration)
            delivery_path = self.workspace / f"delivery-{iteration}.md"
            if not delivery_path.exists():
                self.run_host("delivery", delivery_path)
            if self.run_gate("delivery_gate", delivery_path, delivery_path.name):
                self.save_state(status="passed", final_delivery=str(delivery_path), completed_at=utc_now())
                self.append_log("run_passed", final_delivery=str(delivery_path))
                print(f"Looper run passed. Final delivery: {delivery_path}")
                return 0
            if self.stop_reason:
                # A stop-class failure (e.g. no-progress with action: stop)
                # ends the whole run, not just this delivery attempt.
                return 1

        self.save_state(status="failed", failure="max_iterations_reached")
        self.append_log("stop", reason="max_iterations_reached")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a compiled Looper loop.")
    parser.add_argument(
        "spec_path",
        nargs="?",
        type=Path,
        default=Path(__file__).with_name("loop.resolved.json"),
        help="Path to loop.resolved.json (defaults to the file next to run-loop.py).",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if os.name != "nt":
        # Turn SIGTERM into SystemExit so cleanup runs: run_argv kills the
        # model's process tree on the way out instead of orphaning it.
        signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    try:
        return Runner(args.spec_path).run()
    except RunnerError as exc:
        print(f"run-loop: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
