from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
LOOPER = ROOT / "scripts" / "looper.py"
RUNNER_TEMPLATE = ROOT / "templates" / "run-loop.py"
FIXTURES = ROOT / "tests" / "fixtures"


def run_cmd(argv: list[str], cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        input=stdin,
        check=False,
    )


def write_loop_yaml(
    path: Path,
    *,
    judge_role: str = "judge",
    judge_script: Path | None = None,
    judge_local: bool = True,
) -> None:
    judge_script = judge_script or (FIXTURES / "fake_judge.py")
    judge_state = path.parent / "judge-state.json"
    python = Path(sys.executable).as_posix()
    path.write_text(
        textwrap.dedent(
            f"""
            version: 1
            meta:
              name: fixture-loop
              description: Fixture loop
              author: tests
              created: 2026-06-18

            goal:
              statement: Produce a checked LOOP.md.
              context_sources:
                - file: ./inputs/process-notes.md
              definition_of_done: LOOP.md includes an owner and no TBD.
              verification:
                - id: has-owner
                  type: programmatic
                  check: ["{python}", "{(FIXTURES / 'check_contains.py').as_posix()}", "loop-workspace/delivery-1.md", "Owner:"]
                  expect: exit_zero
                - id: covers-goal
                  type: judge
                  rubric: The artifact includes an owner and no unresolved TBD.

            host:
              cli: fake-host
              model: fixture
              invoke: ["{python}", "{(FIXTURES / 'fake_host.py').as_posix()}"]
              timeout_sec: 30

            council:
              - id: reviewer-1
                role: {judge_role}
                cli: fake-judge
                model: fixture
                invoke: ["{python}", "{judge_script.as_posix()}", "{judge_state.as_posix()}", "1"]
                timeout_sec: 30
                scope: [plan, delivery]
                local: {str(judge_local).lower()}

            gates:
              plan_gate:
                when: after_plan
                members: [reviewer-1]
                verdict_policy: revise_until_clean
                verdict_source: reviewer-1
                criteria: [covers-goal]
                max_revisions: 2
              delivery_gate:
                when: after_each_delivery
                members: [reviewer-1]
                verdict_policy: revise_until_clean
                verdict_source: reviewer-1
                criteria: [has-owner, covers-goal]
                max_revisions: 2

            loop_control:
              max_iterations: 2
              budget:
                wall_clock_min: 5
              human_checkpoints: []
              stop_conditions:
                - all deliveries pass their gate clean
                - max_iterations reached

            privacy:
              egress: []

            workspace:
              dir: ./loop-workspace
              layout: [plan.md, "delivery-{{n}}.md", "review-{{n}}.md", state.json]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


class LooperTests(unittest.TestCase):
    def test_compile_and_runner_revise_then_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml")
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            compiled = run_cmd(
                [
                    sys.executable,
                    str(LOOPER),
                    "compile",
                    "loop.yaml",
                    "--out",
                    "loop.resolved.json",
                    "--render",
                    "LOOP.md",
                    "--session-prompt",
                    "RUN_IN_SESSION.md",
                ],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            self.assertTrue((work / "loop.resolved.json").exists())
            self.assertTrue((work / "LOOP.md").exists())
            loop_doc = (work / "LOOP.md").read_text(encoding="utf-8")
            self.assertIn("## Flow Preview", loop_doc)
            self.assertIn("+--------------------------------+", loop_doc)
            self.assertNotIn("flowchart TD", loop_doc)
            session_prompt = (work / "RUN_IN_SESSION.md").read_text(encoding="utf-8")
            self.assertIn("Run `fixture-loop` In This Session", session_prompt)
            self.assertIn("Do not use `run-loop.py`", session_prompt)
            self.assertIn("Max iterations: `2`", session_prompt)
            self.assertIn("run-log.md", session_prompt)
            self.assertIn("No-progress", session_prompt)

            result = run_cmd([sys.executable, "run-loop.py"], work)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue((work / "loop-workspace" / "plan.md").exists())
            self.assertTrue((work / "loop-workspace" / "delivery-1.md").exists())
            self.assertTrue((work / "loop-workspace" / "review-plan_gate-1.md").exists())
            self.assertTrue((work / "loop-workspace" / "run-log.md").exists())
            state = json.loads((work / "loop-workspace" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "passed")

    def test_compile_rejects_reviewer_as_verdict_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml", judge_role="reviewer")
            result = run_cmd([sys.executable, str(LOOPER), "compile", "loop.yaml"], work)
            self.assertEqual(result.returncode, 2)
            self.assertIn("must name a judge", result.stderr)

    def test_unparseable_judge_degrades_to_revision_and_stops_at_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml", judge_script=FIXTURES / "bad_judge.py")
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = run_cmd([sys.executable, "run-loop.py"], work)
            self.assertNotEqual(result.returncode, 0)
            review = (work / "loop-workspace" / "review-plan_gate-1.md").read_text(encoding="utf-8")
            self.assertIn("unparseable_judge_output", review)

    def test_fixed_passes_does_not_bypass_failed_programmatic_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml")
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            loop_yaml = (work / "loop.yaml").read_text(encoding="utf-8")
            loop_yaml = loop_yaml.replace("verdict_policy: revise_until_clean", "verdict_policy: fixed_passes")
            loop_yaml = loop_yaml.replace("verdict_source: reviewer-1\n", "")
            loop_yaml = loop_yaml.replace("        criteria:", "    criteria:")
            fail_check = work / "fail_check.py"
            fail_check.write_text("import sys\nsys.exit(9)\n", encoding="utf-8")
            lines = []
            for line in loop_yaml.splitlines():
                if "check_contains.py" in line:
                    lines.append(f'      check: ["{Path(sys.executable).as_posix()}", "{fail_check.as_posix()}"]')
                else:
                    lines.append(line)
            loop_yaml = "\n".join(lines) + "\n"
            (work / "loop.yaml").write_text(loop_yaml, encoding="utf-8")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = run_cmd([sys.executable, "run-loop.py"], work)
            self.assertNotEqual(result.returncode, 0)
            state = json.loads((work / "loop-workspace" / "state.json").read_text(encoding="utf-8"))
            self.assertNotEqual(state["status"], "passed")
            self.assertIn(state["failure"], {"delivery_gate_max_revisions_reached", "no_progress_detected"})

    def test_runner_redacts_configured_files_before_judge_send(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            secret_file = work / "inputs" / "secret.txt"
            secret_file.write_text("SUPERSECRET-LOOPER-VALUE\n", encoding="utf-8")
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            capture_file = work / "judge-stdin.txt"
            judge_script = work / "spy_judge.py"
            judge_script.write_text(
                "import json\n"
                "import pathlib\n"
                "import sys\n"
                "\n"
                "prompt = sys.stdin.read()\n"
                f"capture = pathlib.Path({str(capture_file)!r})\n"
                "previous = capture.read_text(encoding='utf-8') if capture.exists() else ''\n"
                "capture.write_text(previous + '\\n---CALL---\\n' + prompt, encoding='utf-8')\n"
                "if 'SUPERSECRET-LOOPER-VALUE' in prompt:\n"
                "    sys.exit(7)\n"
                "print('```json')\n"
                "print(json.dumps({'verdict': 'pass', 'blocking_issues': [], 'confidence': 1.0, 'notes': 'ok'}))\n"
                "print('```')\n",
                encoding="utf-8",
            )
            write_loop_yaml(work / "loop.yaml", judge_script=judge_script)
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            loop_yaml = (work / "loop.yaml").read_text(encoding="utf-8")
            loop_yaml = loop_yaml.replace(
                "privacy:\n  egress: []",
                "privacy:\n  egress:\n    - to: reviewer-1\n      sends: [plan, deliveries]\n      redact: [\"inputs/**\"]\n      consent: required",
            )
            (work / "loop.yaml").write_text(loop_yaml, encoding="utf-8")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            workspace = work / "loop-workspace"
            workspace.mkdir()
            (workspace / "plan.md").write_text("Plan leaked SUPERSECRET-LOOPER-VALUE before redaction.\n", encoding="utf-8")
            result = run_cmd([sys.executable, "run-loop.py"], work)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            judge_prompt = capture_file.read_text(encoding="utf-8")
            self.assertNotIn("SUPERSECRET-LOOPER-VALUE", judge_prompt)
            self.assertIn("[redacted:inputs/secret.txt]", judge_prompt)

    def test_cmd_context_source_output_is_scrubbed_and_surfaced(self) -> None:
        # Regression: cmd context-source output used to be inlined verbatim,
        # so a command that printed a flagged file leaked it into context.md
        # and every downstream prompt.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            (work / "inputs" / "secret.txt").write_text("SUPERSECRET-LOOPER-VALUE\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml")
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            python = Path(sys.executable).as_posix()
            loop_yaml = (work / "loop.yaml").read_text(encoding="utf-8")
            loop_yaml = loop_yaml.replace(
                "- file: ./inputs/process-notes.md",
                "- file: ./inputs/process-notes.md\n"
                f'    - cmd: ["{python}", "-c", "import pathlib; print(pathlib.Path(\'inputs/secret.txt\').read_text())"]',
            )
            loop_yaml = loop_yaml.replace(
                "privacy:\n  egress: []",
                "privacy:\n  egress:\n    - to: reviewer-1\n      sends: [plan, deliveries]\n"
                "      redact: [\"inputs/secret.txt\"]\n      consent: required",
            )
            (work / "loop.yaml").write_text(loop_yaml, encoding="utf-8")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = run_cmd([sys.executable, "run-loop.py"], work)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            context = (work / "loop-workspace" / "context.md").read_text(encoding="utf-8")
            self.assertNotIn("SUPERSECRET-LOOPER-VALUE", context)
            self.assertIn("[redacted:inputs/secret.txt]", context)
            run_log = (work / "loop-workspace" / "run-log.md").read_text(encoding="utf-8")
            self.assertIn("redaction_applied", run_log)
            state = json.loads((work / "loop-workspace" / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(
                any("context command output" in item for item in state.get("warnings", [])),
                state.get("warnings"),
            )

    def test_artifact_leak_scrub_is_surfaced_in_state_and_log(self) -> None:
        # Content-scrubbing an artifact send is no longer silent: the runner
        # records which flagged files leaked into which prompt.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            (work / "inputs" / "secret.txt").write_text("SUPERSECRET-LOOPER-VALUE\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml")
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            loop_yaml = (work / "loop.yaml").read_text(encoding="utf-8")
            loop_yaml = loop_yaml.replace(
                "privacy:\n  egress: []",
                "privacy:\n  egress:\n    - to: reviewer-1\n      sends: [plan, deliveries]\n"
                "      redact: [\"inputs/**\"]\n      consent: required",
            )
            (work / "loop.yaml").write_text(loop_yaml, encoding="utf-8")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            workspace = work / "loop-workspace"
            workspace.mkdir()
            (workspace / "plan.md").write_text(
                "Plan leaked SUPERSECRET-LOOPER-VALUE before redaction.\n", encoding="utf-8"
            )
            result = run_cmd([sys.executable, "run-loop.py"], work)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            run_log = (workspace / "run-log.md").read_text(encoding="utf-8")
            self.assertIn("redaction_applied", run_log)
            state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(
                any("prompt for reviewer-1" in item for item in state.get("warnings", [])),
                state.get("warnings"),
            )

    def test_host_prompts_are_scrubbed_of_flagged_content(self) -> None:
        # Regression: the host was the one recipient whose prompts were never
        # scrubbed - an artifact leak went verbatim to the host CLI.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            (work / "inputs" / "secret.txt").write_text("SUPERSECRET-LOOPER-VALUE\n", encoding="utf-8")
            capture_file = work / "host-stdin.txt"
            spy_host = work / "spy_host.py"
            spy_host.write_text(
                "import pathlib\n"
                "import sys\n"
                "\n"
                "prompt = sys.stdin.read()\n"
                f"capture = pathlib.Path({str(capture_file)!r})\n"
                "previous = capture.read_text(encoding='utf-8') if capture.exists() else ''\n"
                "capture.write_text(previous + '\\n---CALL---\\n' + prompt, encoding='utf-8')\n"
                "print('# Artifact')\n"
                "print()\n"
                "print('Owner: host')\n"
                "print('No TBD')\n",
                encoding="utf-8",
            )
            write_loop_yaml(work / "loop.yaml")
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            python = Path(sys.executable).as_posix()
            loop_yaml = (work / "loop.yaml").read_text(encoding="utf-8")
            loop_yaml = loop_yaml.replace(
                f'invoke: ["{python}", "{(FIXTURES / "fake_host.py").as_posix()}"]',
                f'invoke: ["{python}", "{spy_host.as_posix()}"]',
            )
            loop_yaml = loop_yaml.replace(
                "privacy:\n  egress: []",
                "privacy:\n  egress:\n    - to: reviewer-1\n      sends: [plan, deliveries]\n"
                "      redact: [\"inputs/**\"]\n      consent: required",
            )
            (work / "loop.yaml").write_text(loop_yaml, encoding="utf-8")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            workspace = work / "loop-workspace"
            workspace.mkdir()
            (workspace / "plan.md").write_text(
                "Plan leaked SUPERSECRET-LOOPER-VALUE before redaction.\nOwner: host\nNo TBD\n",
                encoding="utf-8",
            )
            result = run_cmd([sys.executable, "run-loop.py"], work)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            host_prompts = capture_file.read_text(encoding="utf-8")
            self.assertNotIn("SUPERSECRET-LOOPER-VALUE", host_prompts)
            self.assertIn("[redacted:inputs/secret.txt]", host_prompts)
            state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(
                any("prompt for host" in item for item in state.get("warnings", [])),
                state.get("warnings"),
            )

    def test_leak_attribution_names_all_matching_files_and_dedupes_log(self) -> None:
        # Two flagged files sharing the same content must both be named in
        # the warning, and repeated identical scrubs log one event.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            (work / "inputs" / "secret-a.txt").write_text("SUPERSECRET-LOOPER-VALUE\n", encoding="utf-8")
            (work / "inputs" / "secret-b.txt").write_text("SUPERSECRET-LOOPER-VALUE\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml")
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            loop_yaml = (work / "loop.yaml").read_text(encoding="utf-8")
            loop_yaml = loop_yaml.replace(
                "privacy:\n  egress: []",
                "privacy:\n  egress:\n    - to: reviewer-1\n      sends: [plan, deliveries]\n"
                "      redact: [\"inputs/secret-a.txt\", \"inputs/secret-b.txt\"]\n      consent: required",
            )
            (work / "loop.yaml").write_text(loop_yaml, encoding="utf-8")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            workspace = work / "loop-workspace"
            workspace.mkdir()
            (workspace / "plan.md").write_text(
                "Plan leaked SUPERSECRET-LOOPER-VALUE before redaction.\nOwner: host\nNo TBD\n",
                encoding="utf-8",
            )
            result = run_cmd([sys.executable, "run-loop.py"], work)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            state = json.loads((workspace / "state.json").read_text(encoding="utf-8"))
            warnings = [item for item in state.get("warnings", []) if "prompt for reviewer-1" in item]
            self.assertTrue(warnings, state.get("warnings"))
            self.assertIn("inputs/secret-a.txt", warnings[0])
            self.assertIn("inputs/secret-b.txt", warnings[0])
            run_log = (workspace / "run-log.md").read_text(encoding="utf-8")
            reviewer_events = [
                line for line in run_log.splitlines()
                if "redaction_applied" in line and "prompt for reviewer-1" in line
            ]
            self.assertEqual(len(reviewer_events), 1, run_log)

    def test_unscrubbable_flagged_file_is_surfaced_as_blind_spot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            (work / "inputs" / "huge.bin").write_text("A" * 1_100_000, encoding="utf-8")
            write_loop_yaml(work / "loop.yaml")
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            loop_yaml = (work / "loop.yaml").read_text(encoding="utf-8")
            loop_yaml = loop_yaml.replace(
                "privacy:\n  egress: []",
                "privacy:\n  egress:\n    - to: reviewer-1\n      sends: [plan, deliveries]\n"
                "      redact: [\"inputs/huge.bin\"]\n      consent: required",
            )
            (work / "loop.yaml").write_text(loop_yaml, encoding="utf-8")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = run_cmd([sys.executable, "run-loop.py"], work)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            state = json.loads((work / "loop-workspace" / "state.json").read_text(encoding="utf-8"))
            self.assertTrue(
                any("cannot be detected" in item and "huge.bin" in item for item in state.get("warnings", [])),
                state.get("warnings"),
            )
            run_log = (work / "loop-workspace" / "run-log.md").read_text(encoding="utf-8")
            self.assertIn("redaction_unscrubbable", run_log)

    def test_consent_prompt_shows_leak_warning_before_asking(self) -> None:
        # The scrub runs before the consent question, and the consent prompt
        # displays the leak warning for that member.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            (work / "inputs" / "secret.txt").write_text("SUPERSECRET-LOOPER-VALUE\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml", judge_local=False)
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            loop_yaml = (work / "loop.yaml").read_text(encoding="utf-8")
            loop_yaml = loop_yaml.replace(
                "privacy:\n  egress: []",
                "privacy:\n  egress:\n    - to: reviewer-1\n      sends: [plan, deliveries]\n"
                "      redact: [\"inputs/**\"]\n      consent: required",
            )
            (work / "loop.yaml").write_text(loop_yaml, encoding="utf-8")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            workspace = work / "loop-workspace"
            workspace.mkdir()
            (workspace / "plan.md").write_text(
                "Plan leaked SUPERSECRET-LOOPER-VALUE before redaction.\nOwner: host\nNo TBD\n",
                encoding="utf-8",
            )
            result = run_cmd([sys.executable, "run-loop.py"], work, stdin="yes\n")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            consent_block = result.stdout.split("Type 'yes' to consent")[0]
            self.assertIn("Warning: flagged content", consent_block)

    def test_fixed_passes_reviewer_gate_completes_cleanly(self) -> None:
        # Regression: the synthetic "fixed_passes reviewer pass" marker used to
        # feed the no-progress detector and fail the run before its passes
        # completed.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml")
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            loop_yaml = (work / "loop.yaml").read_text(encoding="utf-8")
            loop_yaml = loop_yaml.replace("verdict_policy: revise_until_clean", "verdict_policy: fixed_passes")
            loop_yaml = loop_yaml.replace("verdict_source: reviewer-1\n", "")
            loop_yaml = loop_yaml.replace("        criteria:", "    criteria:")
            (work / "loop.yaml").write_text(loop_yaml, encoding="utf-8")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = run_cmd([sys.executable, "run-loop.py"], work)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            state = json.loads((work / "loop-workspace" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "passed")

    def test_judge_output_with_nested_json_parses_as_pass(self) -> None:
        # Regression: the old fence regex truncated nested objects at the first
        # closing brace, degrading every structured verdict to revise.
        import importlib.util

        spec = importlib.util.spec_from_file_location("run_loop_template", RUNNER_TEMPLATE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        text = (
            "Here is my verdict.\n"
            "```json\n"
            '{"verdict": "pass", "blocking_issues": [], "confidence": 0.9,'
            ' "notes": "ok", "meta": {"scores": {"clarity": 5}}}\n'
            "```\n"
            "Trailing commentary."
        )
        verdict = module.parse_judge_output(text)
        self.assertEqual(verdict["verdict"], "pass")
        self.assertNotIn("warning", verdict)

    def test_consent_fails_closed_for_cross_vendor_member_without_egress(self) -> None:
        # Regression: a non-local member with no privacy.egress entry used to be
        # sent artifacts with no consent prompt at all.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml", judge_local=False)
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)

            # Closed stdin means consent cannot be granted: refuse and stop.
            refused = run_cmd([sys.executable, "run-loop.py"], work, stdin="")
            self.assertEqual(refused.returncode, 2, refused.stderr + refused.stdout)
            self.assertFalse((work / "judge-state.json").exists(), "judge was invoked without consent")
            state = json.loads((work / "loop-workspace" / "state.json").read_text(encoding="utf-8"))
            self.assertNotEqual(state["status"], "passed")

            # Explicit consent lets the run proceed to completion.
            shutil.rmtree(work / "loop-workspace")
            granted = run_cmd([sys.executable, "run-loop.py"], work, stdin="yes\n")
            self.assertEqual(granted.returncode, 0, granted.stderr + granted.stdout)
            self.assertTrue((work / "judge-state.json").exists())

    def test_nested_env_file_redacted_from_context_and_judge(self) -> None:
        # Regression: bare ".env" globs only matched top-level files, and
        # context gathering ignored configured redactions entirely.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            (work / "config").mkdir()
            (work / "config" / ".env").write_text("NESTED-LOOPER-SECRET-VALUE=1\n", encoding="utf-8")
            capture_file = work / "judge-stdin.txt"
            judge_script = work / "spy_judge.py"
            judge_script.write_text(
                "import json\n"
                "import pathlib\n"
                "import sys\n"
                "\n"
                "prompt = sys.stdin.read()\n"
                f"capture = pathlib.Path({str(capture_file)!r})\n"
                "previous = capture.read_text(encoding='utf-8') if capture.exists() else ''\n"
                "capture.write_text(previous + '\\n---CALL---\\n' + prompt, encoding='utf-8')\n"
                "print('```json')\n"
                "print(json.dumps({'verdict': 'pass', 'blocking_issues': [], 'confidence': 1.0, 'notes': 'ok'}))\n"
                "print('```')\n",
                encoding="utf-8",
            )
            write_loop_yaml(work / "loop.yaml", judge_script=judge_script)
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            loop_yaml = (work / "loop.yaml").read_text(encoding="utf-8")
            loop_yaml = loop_yaml.replace(
                "- file: ./inputs/process-notes.md",
                "- file: ./inputs/process-notes.md\n    - file: ./config/.env",
            )
            (work / "loop.yaml").write_text(loop_yaml, encoding="utf-8")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = run_cmd([sys.executable, "run-loop.py"], work)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            context = (work / "loop-workspace" / "context.md").read_text(encoding="utf-8")
            self.assertNotIn("NESTED-LOOPER-SECRET-VALUE", context)
            self.assertIn("[redacted]", context)
            judge_prompt = capture_file.read_text(encoding="utf-8")
            self.assertNotIn("NESTED-LOOPER-SECRET-VALUE", judge_prompt)

    def test_runner_rejects_workspace_outside_loop_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "loop"
            work.mkdir()
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml")
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            resolved = json.loads((work / "loop.resolved.json").read_text(encoding="utf-8"))
            resolved["workspace"]["dir"] = "../escaped-workspace"
            (work / "loop.resolved.json").write_text(json.dumps(resolved), encoding="utf-8")

            result = run_cmd([sys.executable, "run-loop.py"], work)
            self.assertEqual(result.returncode, 2)
            self.assertIn("escapes the loop directory", result.stderr)
            self.assertFalse((Path(tmp) / "escaped-workspace").exists())

    def test_cli_errors_are_clean_for_missing_files_and_runner_help(self) -> None:
        result = run_cmd([sys.executable, str(LOOPER), "compile", "does-not-exist.yaml"], ROOT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("looper: error: Could not read", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        help_result = run_cmd([sys.executable, str(RUNNER_TEMPLATE), "--help"], ROOT)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("Run a compiled Looper loop", help_result.stdout)

    def test_compile_rejects_invalid_specs(self) -> None:
        python = Path(sys.executable).as_posix()
        host_invoke = f'invoke: ["{python}", "{(FIXTURES / "fake_host.py").as_posix()}"]'
        duplicate_member = (
            "  - id: reviewer-1\n"
            "    role: judge\n"
            "    cli: fake-judge\n"
            "    model: fixture\n"
            '    invoke: ["echo"]\n'
            "    timeout_sec: 30\n"
        )
        cases = [
            ("empty argv", host_invoke, "invoke: []", "must not be empty"),
            ("negative budget", "wall_clock_min: 5", "wall_clock_min: -5", "positive number"),
            ("duplicate council id", "\ngates:", f"\n{duplicate_member}\ngates:", "Duplicate council member id"),
            (
                "verdict_source not a gate member",
                "members: [reviewer-1]",
                "members: []",
                "must also be listed",
            ),
            ("workspace escape", "dir: ./loop-workspace", "dir: ../escape", "relative path inside the loop directory"),
            (
                "context path traversal",
                "- file: ./inputs/process-notes.md",
                "- file: ../../outside.md",
                "relative path inside the loop directory",
            ),
            (
                "context source with file and cmd",
                "- file: ./inputs/process-notes.md",
                '- file: ./inputs/process-notes.md\n      cmd: ["ls"]',
                "exactly one of file or cmd",
            ),
        ]
        for label, old, new, expected in cases:
            with self.subTest(label):
                with tempfile.TemporaryDirectory() as tmp:
                    work = Path(tmp)
                    (work / "inputs").mkdir()
                    (work / "inputs" / "process-notes.md").write_text("Notes.\n", encoding="utf-8")
                    write_loop_yaml(work / "loop.yaml")
                    loop_yaml = (work / "loop.yaml").read_text(encoding="utf-8")
                    mutated = loop_yaml.replace(old, new, 1)
                    self.assertNotEqual(mutated, loop_yaml, f"mutation for {label} did not apply")
                    (work / "loop.yaml").write_text(mutated, encoding="utf-8")
                    result = run_cmd([sys.executable, str(LOOPER), "compile", "loop.yaml"], work)
                    self.assertEqual(result.returncode, 2, f"{label}: {result.stderr}")
                    self.assertIn(expected, result.stderr, label)
                    self.assertNotIn("Traceback", result.stderr, label)

    def test_compile_defaults_null_fields_and_timeouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Notes.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml")
            python = Path(sys.executable).as_posix()
            loop_yaml = (work / "loop.yaml").read_text(encoding="utf-8")
            loop_yaml = loop_yaml.replace(
                "- file: ./inputs/process-notes.md",
                f'- file: ./inputs/process-notes.md\n    - cmd: ["{python}", "-c", "print(1)"]',
            )
            (work / "loop.yaml").write_text(loop_yaml, encoding="utf-8")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            resolved = json.loads((work / "loop.resolved.json").read_text(encoding="utf-8"))
            self.assertEqual(resolved["goal"]["context_sources"][1]["timeout_sec"], 60)
            self.assertEqual(resolved["criteria_by_id"]["has-owner"]["timeout_sec"], 300)

            # A bare context_sources: key (YAML null) must compile, not crash.
            bare = (work / "loop.yaml").read_text(encoding="utf-8")
            head, _, tail = bare.partition("  context_sources:")
            _, _, rest = tail.partition("  definition_of_done:")
            (work / "loop.yaml").write_text(
                head + "  context_sources:\n  definition_of_done:" + rest, encoding="utf-8"
            )
            result = run_cmd([sys.executable, str(LOOPER), "compile", "loop.yaml"], work)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_session_prompt_tolerates_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Notes.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml")
            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            resolved_path = work / "loop.resolved.json"
            resolved_path.write_bytes(b"\xef\xbb\xbf" + resolved_path.read_bytes())
            rendered = run_cmd(
                [sys.executable, str(LOOPER), "session-prompt", "loop.resolved.json", "--out", "RUN.md"],
                work,
            )
            self.assertEqual(rendered.returncode, 0, rendered.stderr)

    def test_register_model_rejects_secret_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / "models.json"
            refused = run_cmd(
                [
                    sys.executable,
                    str(LOOPER),
                    "register-model",
                    "leaky",
                    "--invoke",
                    "llm",
                    "api_key=sk-abcdefghijklmnopqrstuvwx",
                    "--registry",
                    str(registry),
                ],
                Path(tmp),
            )
            self.assertEqual(refused.returncode, 2)
            self.assertIn("must never store secrets", refused.stderr)
            self.assertFalse(registry.exists())

            clean = run_cmd(
                [
                    sys.executable,
                    str(LOOPER),
                    "register-model",
                    "mycli",
                    "--invoke",
                    "mycli -p",
                    "--registry",
                    str(registry),
                ],
                Path(tmp),
            )
            self.assertEqual(clean.returncode, 0, clean.stderr)
            data = json.loads(registry.read_text(encoding="utf-8"))
            self.assertEqual(data["mycli"]["invoke"], ["mycli", "-p"])

    def test_session_prompt_command_renders_from_resolved_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            rendered = run_cmd(
                [
                    sys.executable,
                    str(LOOPER),
                    "session-prompt",
                    "loop.resolved.json",
                    "--out",
                    "RUN_IN_SESSION.md",
                ],
                work,
            )
            self.assertEqual(rendered.returncode, 0, rendered.stderr)
            prompt = (work / "RUN_IN_SESSION.md").read_text(encoding="utf-8")
            self.assertIn("Use this prompt when the user wants to run", prompt)
            self.assertIn("covers-goal", prompt)
            self.assertIn("Execution Boundary", prompt)

    def test_pattern_library_templates_compile_with_placeholder_warning(self) -> None:
        loops_dir = ROOT / "templates" / "loops"
        template_dirs = sorted(
            path for path in loops_dir.iterdir() if path.is_dir()
        )
        self.assertTrue(template_dirs, "templates/loops/ has no template directories")
        catalog = (loops_dir / "README.md").read_text(encoding="utf-8")
        for template in template_dirs:
            with self.subTest(template.name):
                self.assertTrue(
                    (template / "loop.yaml").is_file(),
                    f"{template.name} is missing loop.yaml",
                )
                self.assertTrue(
                    (template / "README.md").is_file(),
                    f"{template.name} is missing README.md",
                )
                self.assertIn(
                    f"[{template.name}]({template.name}/)",
                    catalog,
                    f"{template.name} is not listed in templates/loops/README.md",
                )
                with tempfile.TemporaryDirectory() as tmp:
                    result = run_cmd(
                        [
                            sys.executable,
                            str(LOOPER),
                            "compile",
                            str(template / "loop.yaml"),
                            "--out",
                            str(Path(tmp) / "loop.resolved.json"),
                        ],
                        ROOT,
                    )
                    self.assertEqual(result.returncode, 0, f"{template.name}: {result.stderr}")
                    self.assertIn(
                        "unresolved template placeholders remain",
                        result.stderr,
                        f"{template.name} should carry {{{{PLACEHOLDER}}}} tokens",
                    )

    def test_compile_placeholder_warning_absent_after_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Notes.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml")

            loop_yaml = (work / "loop.yaml").read_text(encoding="utf-8")
            mutated = loop_yaml.replace(
                "statement: Produce a checked LOOP.md.",
                "statement: Produce a checked LOOP.md for {{PROJECT_NAME}}.",
                1,
            )
            self.assertNotEqual(mutated, loop_yaml)
            (work / "loop.yaml").write_text(mutated, encoding="utf-8")
            with_token = run_cmd([sys.executable, str(LOOPER), "compile", "loop.yaml"], work)
            self.assertEqual(with_token.returncode, 0, with_token.stderr)
            self.assertIn("unresolved template placeholders remain", with_token.stderr)
            self.assertIn("{{PROJECT_NAME}}", with_token.stderr)

            (work / "loop.yaml").write_text(
                mutated.replace("{{PROJECT_NAME}}", "demo-project"), encoding="utf-8"
            )
            filled = run_cmd([sys.executable, str(LOOPER), "compile", "loop.yaml"], work)
            self.assertEqual(filled.returncode, 0, filled.stderr)
            self.assertNotIn("unresolved template placeholders", filled.stderr)


class LintTests(unittest.TestCase):
    def _setup_work(self, tmp: str) -> Path:
        work = Path(tmp)
        (work / "inputs").mkdir()
        (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
        write_loop_yaml(work / "loop.yaml")
        return work

    def _lint(self, work: Path, *flags: str) -> subprocess.CompletedProcess[str]:
        return run_cmd([sys.executable, str(LOOPER), "lint", "loop.yaml", *flags], work)

    def _mutate(self, work: Path, old: str, new: str, count: int = -1) -> None:
        text = (work / "loop.yaml").read_text(encoding="utf-8")
        mutated = text.replace(old, new) if count < 0 else text.replace(old, new, count)
        self.assertNotEqual(mutated, text, f"mutation did not apply: {old!r}")
        (work / "loop.yaml").write_text(mutated, encoding="utf-8")

    def test_clean_fixture_loop_has_no_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self._setup_work(tmp)
            result = self._lint(work)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("no findings", result.stdout)

    def test_all_vibe_verification_warns_and_fails_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self._setup_work(tmp)
            lines = []
            for line in (work / "loop.yaml").read_text(encoding="utf-8").splitlines():
                if "check: [" in line or "expect: exit_zero" in line:
                    continue
                if line.strip() == "type: programmatic":
                    lines.append(line.replace("programmatic", "judge"))
                    lines.append(line.replace("type: programmatic", "rubric: The artifact names an owner."))
                    continue
                lines.append(line)
            (work / "loop.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")

            result = self._lint(work)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("warning[all-vibe-verification]", result.stdout)

            strict = self._lint(work, "--strict")
            self.assertEqual(strict.returncode, 1, strict.stdout + strict.stderr)

    def test_judge_criterion_under_fixed_passes_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self._setup_work(tmp)
            self._mutate(work, "verdict_policy: revise_until_clean", "verdict_policy: fixed_passes")
            self._mutate(work, "verdict_source: reviewer-1\n", "")
            self._mutate(work, "        criteria:", "    criteria:")
            result = self._lint(work)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("error[judge-criterion-unreachable]", result.stdout)

    def test_cross_vendor_member_without_egress_is_error_and_scoped_egress_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Notes.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml", judge_local=False)

            result = self._lint(work)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("error[unscoped-egress]", result.stdout)

            self._mutate(
                work,
                "privacy:\n  egress: []",
                "privacy:\n  egress:\n    - to: reviewer-1\n      sends: [plan, deliveries]\n"
                "      redact: [\"inputs/**\"]\n      consent: required",
            )
            scoped = self._lint(work)
            self.assertEqual(scoped.returncode, 0, scoped.stdout + scoped.stderr)
            self.assertNotIn("unscoped-egress", scoped.stdout)

            self._mutate(work, "consent: required", "consent: granted")
            granted = self._lint(work)
            self.assertEqual(granted.returncode, 0, granted.stdout + granted.stderr)
            self.assertIn("warning[egress-consent-pregranted]", granted.stdout)

            # A second entry for the same member that still requires consent
            # means the runner will prompt after all; no pre-grant warning.
            self._mutate(
                work,
                "      consent: granted",
                "      consent: granted\n    - to: reviewer-1\n      sends: [reviews]\n"
                "      redact: [\"inputs/**\"]\n      consent: required",
            )
            mixed = self._lint(work)
            self.assertEqual(mixed.returncode, 0, mixed.stdout + mixed.stderr)
            self.assertNotIn("egress-consent-pregranted", mixed.stdout)

    def test_non_local_same_family_member_without_egress_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Notes.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml", judge_local=False)
            self._mutate(work, "cli: fake-judge", "cli: fake-host")
            result = self._lint(work)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("warning[non-local-member-without-egress]", result.stdout)
            self.assertIn("warning[same-family-judge]", result.stdout)

    def test_judge_criterion_under_human_verdict_source_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self._setup_work(tmp)
            self._mutate(work, "verdict_source: reviewer-1", "verdict_source: human")
            result = self._lint(work)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("error[judge-criterion-unreachable]", result.stdout)
            self.assertIn("verdict_source is human", result.stdout)

    def test_unreferenced_cross_vendor_member_warns_instead_of_erroring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Notes.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml", judge_local=False)
            # Detach the member from both gates: human verdicts, no members,
            # and no judge criteria left anywhere.
            self._mutate(work, "verdict_source: reviewer-1", "verdict_source: human")
            self._mutate(work, "members: [reviewer-1]", "members: []")
            self._mutate(work, "criteria: [covers-goal]", "criteria: []")
            self._mutate(work, "criteria: [has-owner, covers-goal]", "criteria: [has-owner]")
            result = self._lint(work)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("unscoped-egress", result.stdout)
            self.assertIn("warning[unreferenced-council-member]", result.stdout)

    def test_unhonored_human_checkpoint_warns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self._setup_work(tmp)
            self._mutate(
                work,
                "human_checkpoints: []",
                "human_checkpoints: [after_plan, before cross-vendor send]",
            )
            result = self._lint(work)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("warning[unhonored-human-checkpoint]", result.stdout)
            # Only the unrecognized name is listed; 'after_plan' is honored.
            self.assertIn("['before cross-vendor send']", result.stdout)

    def test_empty_verification_gets_dedicated_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self._setup_work(tmp)
            lines = []
            skipping = False
            for line in (work / "loop.yaml").read_text(encoding="utf-8").splitlines():
                if line.strip() == "verification:":
                    lines.append(line.replace("verification:", "verification: []"))
                    skipping = True
                    continue
                if skipping and (line.startswith("    ") or not line.strip()):
                    continue
                skipping = False
                lines.append(line)
            text = "\n".join(lines) + "\n"
            text = text.replace("criteria: [covers-goal]", "criteria: []")
            text = text.replace("criteria: [has-owner, covers-goal]", "criteria: []")
            (work / "loop.yaml").write_text(text, encoding="utf-8")
            result = self._lint(work)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("warning[no-verification-criteria]", result.stdout)
            self.assertNotIn("all-vibe-verification", result.stdout)

    def test_egress_to_unknown_member_is_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self._setup_work(tmp)
            self._mutate(
                work,
                "privacy:\n  egress: []",
                "privacy:\n  egress:\n    - to: ghost\n      sends: [plan]\n      consent: required",
            )
            result = self._lint(work)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("error[egress-unknown-member]", result.stdout)

    def test_coaching_warnings_for_caps_and_gating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self._setup_work(tmp)
            self._mutate(work, "wall_clock_min: 5", "wall_clock_min: null")
            self._mutate(work, "    max_revisions: 2\n", "", 1)
            self._mutate(work, "criteria: [has-owner, covers-goal]", "criteria: [covers-goal]")
            result = self._lint(work)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("warning[no-wall-clock-cap]", result.stdout)
            self.assertIn("warning[missing-max-revisions]", result.stdout)
            self.assertIn("warning[delivery-gate-no-programmatic]", result.stdout)

    def test_budget_caps_error_on_unmetered_invokes_and_warn_on_unknown_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self._setup_work(tmp)
            self._mutate(work, "    wall_clock_min: 5\n", "    wall_clock_min: 5\n    tokens: 1000\n    usd: 2.0\n")
            # Fixture scripts: the runner cannot judge them from argv.
            result = self._lint(work)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("budget.tokens: cannot tell from the invoke whether host, reviewer-1", result.stdout)
            # The fixture judge is local: free, so only the host is unknown for USD.
            self.assertIn("budget.usd: cannot tell from the invoke whether host report", result.stdout)
            self.assertNotIn("error[", result.stdout)

            python = Path(sys.executable).as_posix()
            host_invoke = f'invoke: ["{python}", "{(FIXTURES / "fake_host.py").as_posix()}"]'
            self._mutate(work, host_invoke, 'invoke: ["claude", "-p"]')
            result = self._lint(work)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("error[unmetered-budget]: loop_control.budget.tokens cannot be enforced for host", result.stdout)
            self.assertIn("error[unmetered-budget]: loop_control.budget.usd cannot be enforced for host", result.stdout)

            self._mutate(work, 'invoke: ["claude", "-p"]', 'invoke: ["claude", "-p", "--output-format", "json"]')
            result = self._lint(work)
            self.assertNotIn("unmetered-budget", result.stdout)
            self.assertNotIn("budget.usd", result.stdout)
            self.assertIn("budget.tokens: cannot tell from the invoke whether reviewer-1", result.stdout)

            self._mutate(work, 'invoke: ["claude", "-p", "--output-format", "json"]', 'invoke: ["codex", "exec", "--json"]')
            result = self._lint(work)
            self.assertIn("error[unmetered-budget]: loop_control.budget.usd cannot be enforced for host", result.stdout)
            self.assertNotIn("budget.tokens cannot be enforced", result.stdout)

    def test_json_output_matches_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_text("Notes.\n", encoding="utf-8")
            write_loop_yaml(work / "loop.yaml", judge_local=False)
            result = self._lint(work, "--json")
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["errors"], 1)
            self.assertEqual(
                payload["errors"] + payload["warnings"], len(payload["findings"])
            )
            checks = {item["check"] for item in payload["findings"]}
            self.assertIn("unscoped-egress", checks)

    def test_lint_rejects_uncompilable_spec_with_exit_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self._setup_work(tmp)
            self._mutate(work, "dir: ./loop-workspace", "dir: ../escape")
            result = self._lint(work)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("looper: error:", result.stderr)

    def test_shipped_templates_and_example_lint_without_errors(self) -> None:
        specs = sorted((ROOT / "templates" / "loops").glob("*/loop.yaml"))
        specs.append(ROOT / "examples" / "ai-workflow-mapping" / "loop.yaml")
        self.assertGreaterEqual(len(specs), 6)
        for spec in specs:
            with self.subTest(spec.parent.name):
                result = run_cmd([sys.executable, str(LOOPER), "lint", str(spec)], ROOT)
                self.assertEqual(result.returncode, 0, f"{spec}: {result.stdout}{result.stderr}")
                self.assertNotIn("error[", result.stdout, spec)


LOOPS = ROOT / "templates" / "loops"


class SecurityScanTemplateTests(unittest.TestCase):
    SCANNER = LOOPS / "security-scan" / "scripts" / "scan-secrets.py"

    def _sweep(self, files: dict[str, str]) -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            for name, body in files.items():
                (repo / name).write_text(body, encoding="utf-8")
            out = Path(tmp) / "cand.json"
            result = run_cmd(
                [sys.executable, str(self.SCANNER), str(repo), "--out", str(out), "--no-history"],
                ROOT,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(out.read_text(encoding="utf-8"))["candidates"]

    def test_detects_underscore_joined_credential_names(self) -> None:
        cands = self._sweep({"config.py": (
            'SECRET_KEY = "Zq9rT2vXw8LmNp4c"\n'
            "DB_PASSWORD=Corr3ctHorseBat\n"
            "client_secret: NineCharsX\n"
        )})
        self.assertEqual(len(cands), 3, cands)

    def test_value_containing_test_is_not_suppressed(self) -> None:
        cands = self._sweep({"config.py": "password = Contest2024xyz\n"})
        self.assertEqual(len(cands), 1, cands)

    def test_distinct_short_secrets_not_collapsed_by_mask(self) -> None:
        cands = self._sweep({"config.py": "password = hunter22aa\ntoken: swordfish9xy\n"})
        self.assertEqual(len(cands), 2, cands)

    def test_obvious_placeholder_values_are_suppressed(self) -> None:
        cands = self._sweep({"config.py": 'api_key = "your_key_placeholder"\n'})
        self.assertEqual(cands, [])

    def test_masked_excerpt_never_contains_full_value(self) -> None:
        cands = self._sweep({"config.py": "password = hunter22aa\n"})
        self.assertEqual(len(cands), 1)
        self.assertNotIn("hunter22aa", cands[0]["excerpt"])


class TemplateCheckerScriptTests(unittest.TestCase):
    def _run(self, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return run_cmd([sys.executable, str(script), *args], ROOT)

    def test_findings_rejects_triple_question_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "SECURITY-FINDINGS.md"
            report.write_text(
                "# Findings\n\nsecret pii vulnerability\nType: bug\nSeverity: high\n"
                "Location: a.py:1\nRemediation: ???\n",
                encoding="utf-8",
            )
            result = self._run(
                LOOPS / "security-scan" / "scripts" / "check-findings.py", str(report)
            )
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("placeholder", result.stderr)

    def test_findings_incidental_no_secrets_phrase_does_not_waive_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "SECURITY-FINDINGS.md"
            report.write_text(
                "# Report\n\nsecret pii vulnerability scan.\n"
                "No secrets were detected in git history.\n"
                "Critical issue at server.py:42 but no field labels.\n",
                encoding="utf-8",
            )
            result = self._run(
                LOOPS / "security-scan" / "scripts" / "check-findings.py", str(report)
            )
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("missing required field label", result.stderr)

    def test_findings_genuine_empty_report_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "SECURITY-FINDINGS.md"
            report.write_text(
                "# Report\n\nChecked secret, pii, vulnerability areas.\nNo findings.\n",
                encoding="utf-8",
            )
            result = self._run(
                LOOPS / "security-scan" / "scripts" / "check-findings.py", str(report)
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_citations_reject_path_outside_sources_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            loop = Path(tmp)
            (loop / "inputs" / "sources").mkdir(parents=True)
            (loop / "inputs" / "sources" / "a.md").write_text("notes\n", encoding="utf-8")
            (loop / "loop-workspace").mkdir()
            (loop / "loop-workspace" / "report-draft-1.md").write_text("draft\n", encoding="utf-8")
            report = loop / "loop-workspace" / "REPORT.md"
            checker = LOOPS / "research-synthesis" / "scripts" / "check-citations.py"

            report.write_text(
                "# A\n\nClaim supported here indeed. [source: loop-workspace/report-draft-1.md]\n",
                encoding="utf-8",
            )
            outside = run_cmd(
                [sys.executable, str(checker), "loop-workspace/REPORT.md", "--sources", "inputs/sources"],
                loop,
            )
            self.assertEqual(outside.returncode, 1, outside.stdout)

            report.write_text(
                "# A\n\nClaim supported here indeed. [source: inputs/sources/a.md]\n",
                encoding="utf-8",
            )
            inside = run_cmd(
                [sys.executable, str(checker), "loop-workspace/REPORT.md", "--sources", "inputs/sources"],
                loop,
            )
            self.assertEqual(inside.returncode, 0, inside.stderr)


class CrashStateTests(unittest.TestCase):
    def test_unexpected_crash_leaves_terminal_state_not_running(self) -> None:
        # Contract section 5: a crash must not leave state claiming
        # "running". A non-UTF-8 context file used to do exactly that.
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "inputs").mkdir()
            (work / "inputs" / "process-notes.md").write_bytes(
                "caf\xe9 notes\n".encode("latin-1")
            )
            write_loop_yaml(work / "loop.yaml")
            shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")

            compiled = run_cmd(
                [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"],
                work,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = run_cmd([sys.executable, "run-loop.py"], work)
            self.assertNotEqual(result.returncode, 0)
            state = json.loads((work / "loop-workspace" / "state.json").read_text(encoding="utf-8"))
            self.assertNotEqual(state["status"], "running", state)


def load_runner_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("run_loop_under_test", RUNNER_TEMPLATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BudgetTests(unittest.TestCase):
    def _setup_work(
        self,
        tmp: str,
        *,
        host: tuple[str, int, float] | None,
        judge: tuple[str, int, float] | None,
        budget: str,
        judge_local: bool = True,
    ) -> Path:
        work = Path(tmp)
        (work / "inputs").mkdir()
        (work / "inputs" / "process-notes.md").write_text("Need a useful loop.\n", encoding="utf-8")
        write_loop_yaml(work / "loop.yaml", judge_local=judge_local)
        python = Path(sys.executable).as_posix()
        metered = (FIXTURES / "metered.py").as_posix()
        text = (work / "loop.yaml").read_text(encoding="utf-8")
        for wrap, script in ((host, "fake_host.py"), (judge, "fake_judge.py")):
            if wrap is None:
                continue
            fmt, tokens, usd = wrap
            original = f'"{python}", "{(FIXTURES / script).as_posix()}"'
            self.assertIn(original, text)
            text = text.replace(original, f'"{python}", "{metered}", "{fmt}", "{tokens}", "{usd}", {original}')
        text = text.replace("    wall_clock_min: 5\n", f"    wall_clock_min: 5\n{budget}")
        (work / "loop.yaml").write_text(text, encoding="utf-8")
        shutil.copyfile(RUNNER_TEMPLATE, work / "run-loop.py")
        self._compile(work)
        return work

    def _compile(self, work: Path) -> None:
        compiled = run_cmd(
            [sys.executable, str(LOOPER), "compile", "loop.yaml", "--out", "loop.resolved.json"], work
        )
        self.assertEqual(compiled.returncode, 0, compiled.stderr)

    def _run(self, work: Path, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return run_cmd([sys.executable, "run-loop.py"], work, stdin=stdin)

    def _state(self, work: Path) -> dict:
        return json.loads((work / "loop-workspace" / "state.json").read_text(encoding="utf-8"))

    def _judge_calls(self, work: Path) -> int:
        path = work / "judge-state.json"
        return json.loads(path.read_text(encoding="utf-8"))["count"] if path.exists() else 0

    def test_token_cap_with_non_local_judge_stops_run_and_holds_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # host 400/call (claude JSON), non-local judge 300/call (codex
            # JSONL): plan 400, verdict 700, revision 1100 > 1000 -> stop
            # before the next verdict.
            work = self._setup_work(
                tmp,
                host=("claude", 400, 0.01),
                judge=("codex", 300, 0),
                budget="    tokens: 1000\n",
                judge_local=False,
            )
            result = self._run(work, stdin="yes\n")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("Token budget exceeded: 1100 of 1000", result.stderr)
            state = self._state(work)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["failure"], "token_budget_exceeded")
            self.assertIn("reviewer-1", state["consent"])
            self.assertEqual(state["usage"]["tokens"], 1100)
            self.assertEqual(state["usage"]["calls"], 3)
            self.assertEqual(state["usage"]["by_member"]["host"]["tokens"], 800)
            self.assertEqual(state["usage"]["by_member"]["reviewer-1"]["tokens"], 300)
            self.assertEqual(self._judge_calls(work), 1)
            self.assertFalse((work / "loop-workspace" / "delivery-1.md").exists())
            # The response text is unwrapped from the CLI's JSON envelope.
            plan = (work / "loop-workspace" / "plan.md").read_text(encoding="utf-8")
            self.assertTrue(plan.startswith("Revised artifact"), plan)
            log = (work / "loop-workspace" / "run-log.md").read_text(encoding="utf-8")
            self.assertIn('"reason": "token_budget_exceeded"', log)
            self.assertIn("`model_usage`", log)

            # Usage accrues across resumes: a rerun stops before any call.
            rerun = self._run(work)
            self.assertEqual(rerun.returncode, 2, rerun.stdout + rerun.stderr)
            self.assertIn("Token budget exceeded: 1100 of 1000", rerun.stderr)
            state = self._state(work)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["failure"], "token_budget_exceeded")
            self.assertEqual(state["usage"]["calls"], 3)
            # Neither model process ran: the judge's own counter is unchanged
            # and the host did not rewrite the plan.
            self.assertEqual(self._judge_calls(work), 1)
            self.assertEqual((work / "loop-workspace" / "plan.md").read_text(encoding="utf-8"), plan)
            log = (work / "loop-workspace" / "run-log.md").read_text(encoding="utf-8")
            self.assertEqual(log.count('"reason": "token_budget_exceeded"'), 2)
            self.assertNotIn("`host_start`", log.split("`run_start`")[-1])

    def test_usd_cap_crossed_by_non_local_judge_stops_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # host $0.10/call, non-local judge $0.40/call (both claude JSON):
            # plan .10, verdict .50, revision .60, passing verdict 1.00 > .90
            # -> the judge's spend crosses the cap and no delivery is drafted.
            work = self._setup_work(
                tmp,
                host=("claude", 10, 0.1),
                judge=("claude", 10, 0.4),
                budget="    usd: 0.9\n",
                judge_local=False,
            )
            result = self._run(work, stdin="yes\n")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("USD budget exceeded", result.stderr)
            state = self._state(work)
            self.assertEqual(state["failure"], "usd_budget_exceeded")
            self.assertAlmostEqual(state["usage"]["usd"], 1.0)
            self.assertAlmostEqual(state["usage"]["by_member"]["reviewer-1"]["usd"], 0.8)
            self.assertAlmostEqual(state["usage"]["by_member"]["host"]["usd"], 0.2)
            self.assertEqual(self._judge_calls(work), 2)
            self.assertTrue((work / "loop-workspace" / "plan.md").exists())
            self.assertFalse((work / "loop-workspace" / "delivery-1.md").exists())
            self.assertNotIn("untracked_calls", state["usage"])

    def test_usd_cap_with_local_judge_stops_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # A local judge is free; host plan + revision = $1.00 (under
            # $1.20), plan passes, delivery host call = $1.50 -> stop.
            work = self._setup_work(
                tmp, host=("claude", 10, 0.5), judge=("codex", 10, 0), budget="    usd: 1.2\n"
            )
            result = self._run(work)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("USD budget exceeded", result.stderr)
            state = self._state(work)
            self.assertEqual(state["failure"], "usd_budget_exceeded")
            self.assertAlmostEqual(state["usage"]["usd"], 1.5)
            self.assertEqual(state["usage"]["by_member"]["reviewer-1"]["usd"], 0.0)
            self.assertTrue((work / "loop-workspace" / "delivery-1.md").exists())

    def test_metered_run_within_caps_passes_and_records_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self._setup_work(
                tmp,
                host=("claude", 100, 0.02),
                judge=("codex-plain", 1234, 0),
                budget="    tokens: 100000\n    usd: 5.0\n",
            )
            result = self._run(work)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = self._state(work)
            self.assertEqual(state["status"], "passed")
            judge = state["usage"]["by_member"]["reviewer-1"]
            self.assertEqual(judge["tokens"], 1234 * judge["calls"])
            delivery = (work / "loop-workspace" / "delivery-1.md").read_text(encoding="utf-8")
            self.assertIn("Owner: host", delivery)
            self.assertNotIn('"usage"', delivery)

    def test_host_reporting_no_tokens_fails_closed_on_first_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # The plain fake host is a script the runner cannot judge from
            # argv, so it runs once; reporting nothing then ends the run.
            work = self._setup_work(tmp, host=None, judge=("codex", 10, 0), budget="    tokens: 1000\n")
            result = self._run(work)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("budget.tokens cannot be enforced: host", result.stderr)
            state = self._state(work)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["failure"], "budget_unenforceable:tokens:host")
            self.assertEqual(state["usage"]["calls"], 1)
            self.assertEqual(self._judge_calls(work), 0)

    def test_non_local_judge_reporting_no_usd_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Host reports cost; the non-local codex-format judge reports
            # tokens only, so a USD cap cannot count it: stop after its call.
            work = self._setup_work(
                tmp,
                host=("claude", 10, 0.01),
                judge=("codex", 10, 0),
                budget="    usd: 5.0\n",
                judge_local=False,
            )
            result = self._run(work, stdin="yes\n")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("budget.usd cannot be enforced: reviewer-1", result.stderr)
            state = self._state(work)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["failure"], "budget_unenforceable:usd:reviewer-1")
            self.assertEqual(self._judge_calls(work), 1)
            self.assertEqual(state["usage"]["by_member"]["reviewer-1"]["tokens"], 10)
            self.assertFalse((work / "loop-workspace" / "review-plan_gate-1.md").exists())

    def test_codex_host_with_only_usd_cap_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = self._setup_work(tmp, host=("codex", 500, 0), judge=None, budget="    usd: 5.0\n")
            result = self._run(work)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("budget.usd cannot be enforced: host", result.stderr)
            state = self._state(work)
            self.assertEqual(state["failure"], "budget_unenforceable:usd:host")
            self.assertEqual(state["usage"]["by_member"]["host"]["tokens"], 500)
            self.assertEqual(self._judge_calls(work), 0)

    def test_known_unmetered_invokes_are_refused_before_any_call(self) -> None:
        # A CLI the runner knows reports nothing is refused at preflight, so
        # nothing is spent. The stand-in executables record any invocation.
        cases = [
            ("claude", ["-p"], "    tokens: 1000\n", "budget_unenforceable:tokens:host"),
            ("codex", ["exec", "--json"], "    usd: 5.0\n", "budget_unenforceable:usd:host"),
        ]
        for exe, args, budget, failure in cases:
            with self.subTest(exe), tempfile.TemporaryDirectory() as tmp:
                work = self._setup_work(tmp, host=None, judge=None, budget=budget)
                stand_in = work / "bin" / exe
                stand_in.parent.mkdir()
                marker = (work / "invoked").as_posix()
                stand_in.write_text(
                    f"#!{Path(sys.executable).as_posix()}\nopen({marker!r}, 'w').close()\n", encoding="utf-8"
                )
                stand_in.chmod(0o755)
                python = Path(sys.executable).as_posix()
                text = (work / "loop.yaml").read_text(encoding="utf-8")
                host_invoke = f'["{python}", "{(FIXTURES / "fake_host.py").as_posix()}"]'
                self.assertIn(host_invoke, text)
                argv = ", ".join(f'"{item}"' for item in [stand_in.as_posix(), *args])
                (work / "loop.yaml").write_text(text.replace(host_invoke, f"[{argv}]"), encoding="utf-8")
                self._compile(work)

                result = self._run(work)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertFalse((work / "invoked").exists(), "model CLI was invoked despite preflight")
                state = self._state(work)
                self.assertEqual(state["status"], "blocked")
                self.assertEqual(state["failure"], failure)
                self.assertNotIn("usage", state)
                self.assertEqual(self._judge_calls(work), 0)

    def test_runner_and_lint_agree_on_which_invokes_meter(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("looper_under_test", LOOPER)
        looper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(looper)
        runner = load_runner_module()
        invokes = [
            ["claude", "-p"], ["claude", "-p", "--output-format", "json"],
            ["claude", "-p", "--output-format=stream-json"], ["claude", "-p", "--output-format", "text"],
            ["/usr/local/bin/claude.exe", "-p", "--output-format", "json"],
            ["codex", "exec"], ["codex", "exec", "--json"], ["codex"],
            ["gemini", "-p"], ["llm"], ["ollama", "run"], ["python", "wrapper.py"],
        ]
        for argv in invokes:
            for local in (False, True):
                for key in ("tokens", "usd"):
                    member = {"invoke": argv, "local": local}
                    self.assertEqual(
                        runner.invoke_meters(member, key), looper.invoke_meters(member, key), (argv, local, key)
                    )

    def _stand_in(self, work: Path, name: str, script: Path) -> Path:
        """An executable named like a real CLI (so the runner treats it as one)."""
        directory = work / "bin"
        directory.mkdir(exist_ok=True)
        if os.name == "nt":
            path = directory / f"{name}.cmd"
            path.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            path = directory / name
            path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
            path.chmod(0o755)
        return path

    def _use_fake_claude_host(self, work: Path, turns: int, usd_per_turn: float) -> Path:
        claude = self._stand_in(work, "claude", FIXTURES / "fake_claude.py")
        argv_log = work / "claude-argv.jsonl"
        python = Path(sys.executable).as_posix()
        host_invoke = f'["{python}", "{(FIXTURES / "fake_host.py").as_posix()}"]'
        invoke = [
            "-p", "--output-format", "json",
            argv_log.as_posix(), str(turns), str(usd_per_turn), "100",
            "--", python, (FIXTURES / "fake_host.py").as_posix(),
        ]
        text = (work / "loop.yaml").read_text(encoding="utf-8")
        self.assertIn(host_invoke, text)
        # Native path, YAML single-quoted (no escapes): Windows runs a .cmd
        # through cmd.exe, which can misread forward slashes as switches.
        rendered = [f"'{claude}'"] + [f'"{item}"' for item in invoke]
        (work / "loop.yaml").write_text(
            text.replace(host_invoke, "[" + ", ".join(rendered) + "]"), encoding="utf-8"
        )
        self._compile(work)
        return argv_log

    def _usd_ceilings(self, argv_log: Path) -> list[str]:
        ceilings = []
        for line in argv_log.read_text(encoding="utf-8").splitlines():
            argv = json.loads(line)
            ceilings.append(argv[argv.index("--max-budget-usd") + 1])
        return ceilings

    def test_claude_host_gets_remaining_usd_and_stops_itself_mid_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # One host call that would grind 10 turns x $0.30 = $3.00 on a
            # $1.00 budget. Handed --max-budget-usd 1.0, it stops itself after
            # the turn that reaches the ceiling ($1.20), and that failed
            # call's reported cost is still counted.
            work = self._setup_work(tmp, host=None, judge=("codex", 10, 0), budget="    usd: 1.0\n")
            argv_log = self._use_fake_claude_host(work, turns=10, usd_per_turn=0.3)
            result = self._run(work)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("--max-budget-usd ceiling", result.stderr)
            self.assertEqual(self._usd_ceilings(argv_log), ["1.000000"])
            state = self._state(work)
            self.assertEqual(state["status"], "failed")
            self.assertEqual(state["failure"], "usd_budget_exceeded")
            self.assertAlmostEqual(state["usage"]["usd"], 1.2)
            self.assertEqual(state["usage"]["by_member"]["host"]["calls"], 1)
            self.assertFalse((work / "loop-workspace" / "plan.md").exists())
            self.assertEqual(self._judge_calls(work), 0)

    def test_each_claude_call_gets_only_what_is_left(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # $0.30 per host call on $1.00: plan, revision, delivery are
            # handed 1.00, 0.70, 0.40 and the run passes at $0.90.
            work = self._setup_work(tmp, host=None, judge=("codex", 10, 0), budget="    usd: 1.0\n")
            argv_log = self._use_fake_claude_host(work, turns=1, usd_per_turn=0.3)
            result = self._run(work)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(self._usd_ceilings(argv_log), ["1.000000", "0.700000", "0.400000"])
            self.assertAlmostEqual(self._state(work)["usage"]["usd"], 0.9)

    def test_remaining_wall_clock_caps_a_hanging_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # A host that hangs far past its own 30s timeout_sec is killed
            # when the 3-second loop budget runs out, not 30s later.
            work = self._setup_work(tmp, host=None, judge=None, budget="")
            hang = work / "hang.py"
            hang.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(120)\n", encoding="utf-8")
            python = Path(sys.executable).as_posix()
            text = (work / "loop.yaml").read_text(encoding="utf-8")
            text = text.replace(
                f'["{python}", "{(FIXTURES / "fake_host.py").as_posix()}"]', f'["{python}", "{hang.as_posix()}"]'
            ).replace("wall_clock_min: 5", "wall_clock_min: 0.05")
            (work / "loop.yaml").write_text(text, encoding="utf-8")
            self._compile(work)
            started = time.monotonic()
            result = self._run(work)
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertLess(elapsed, 20, "call ran to its own timeout instead of the loop budget")
            state = self._state(work)
            self.assertEqual(state["failure"], "wall_clock_budget_exceeded")
            self.assertIn("host was stopped when the budget ran out", result.stderr)

    def _use_spawning_host(self, work: Path, mode: str, wall_clock_min: str = "5") -> Path:
        beats = work / "beats"
        python = Path(sys.executable).as_posix()
        text = (work / "loop.yaml").read_text(encoding="utf-8")
        host_invoke = f'["{python}", "{(FIXTURES / "fake_host.py").as_posix()}"]'
        self.assertIn(host_invoke, text)
        text = text.replace(
            host_invoke,
            f'["{python}", "{(FIXTURES / "spawning_host.py").as_posix()}", "{mode}", "{beats.as_posix()}"]',
        ).replace("wall_clock_min: 5", f"wall_clock_min: {wall_clock_min}")
        (work / "loop.yaml").write_text(text, encoding="utf-8")
        self._compile(work)
        return beats

    def _live_tools(self, beats: Path) -> list[str]:
        """Tool processes still heartbeating (each appends every 0.1s)."""
        def sizes() -> dict[str, int]:
            return {f.name: f.stat().st_size for f in beats.iterdir()} if beats.exists() else {}
        before = sizes()
        time.sleep(0.8)
        after = sizes()
        return sorted(name for name, size in after.items() if size > before.get(name, 0))

    def test_timeout_kill_takes_down_detached_tool_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # The host hangs with a tool running in its own session; when the
            # 3-second budget runs out, the tool must die with it rather than
            # keep working (and spending) as an orphan.
            work = self._setup_work(tmp, host=None, judge=None, budget="")
            beats = self._use_spawning_host(work, "hang", wall_clock_min="0.05")
            result = self._run(work)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(self._state(work)["failure"], "wall_clock_budget_exceeded")
            self.assertTrue(beats.exists() and any(beats.iterdir()), "tool never started")
            self.assertEqual(self._live_tools(beats), [], "tool outlived the killed host")

    @unittest.skipIf(
        sys.platform == "darwin",
        "macOS has no subreaper: a detached tool left behind by a CLI that already exited cleanly "
        "cannot be traced by parentage (documented limitation)",
    )
    def test_tools_left_behind_by_a_finished_call_are_killed_and_do_not_stall_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Every host call finishes at once but leaves a detached tool
            # holding stdout. The call must complete without waiting out the
            # host's 30s timeout_sec, and the tool must not survive it.
            work = self._setup_work(tmp, host=None, judge=("codex", 10, 0), budget="")
            beats = self._use_spawning_host(work, "finish")
            started = time.monotonic()
            result = self._run(work)
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertLess(elapsed, 25, "a leftover tool holding stdout stalled the call")
            state = self._state(work)
            self.assertEqual(state["status"], "passed")
            host_calls = state["usage"]["by_member"]["host"]["calls"]
            self.assertEqual(len(list(beats.iterdir())), host_calls)
            self.assertEqual(self._live_tools(beats), [], "tools outlived the calls that started them")

    @unittest.skipIf(os.name == "nt", "SIGTERM handling is POSIX-only")
    def test_sigterm_to_runner_kills_model_tree_and_leaves_terminal_state(self) -> None:
        import signal

        with tempfile.TemporaryDirectory() as tmp:
            work = self._setup_work(tmp, host=None, judge=None, budget="")
            beats = self._use_spawning_host(work, "hang")
            runner = subprocess.Popen(
                [sys.executable, "run-loop.py"], cwd=str(work),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                deadline = time.monotonic() + 20
                while not (beats.exists() and any(beats.iterdir())) and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertTrue(beats.exists() and any(beats.iterdir()), "tool never started")
                runner.send_signal(signal.SIGTERM)
                runner.communicate(timeout=20)
            finally:
                if runner.poll() is None:
                    runner.kill()
            self.assertEqual(runner.returncode, 128 + signal.SIGTERM)
            self.assertEqual(self._live_tools(beats), [], "tool outlived the terminated runner")
            self.assertEqual(self._state(work)["status"], "failed")

    def test_usd_ceiling_flag_handling(self) -> None:
        runner = load_runner_module()
        self.assertEqual(
            runner.with_usd_ceiling(["claude", "-p"], 0.75), ["claude", "-p", "--max-budget-usd", "0.750000"]
        )
        # Rounds down: never grant more than is left.
        self.assertEqual(runner.with_usd_ceiling(["claude"], 0.3333339)[-1], "0.333333")
        # An operator's own per-call ceiling is kept when it is lower...
        self.assertEqual(
            runner.with_usd_ceiling(["claude", "--max-budget-usd", "0.10", "-p"], 0.75),
            ["claude", "--max-budget-usd", "0.100000", "-p"],
        )
        # ...and lowered to the remaining budget when it is higher.
        self.assertEqual(
            runner.with_usd_ceiling(["claude", "--max-budget-usd=5", "-p"], 0.75),
            ["claude", "--max-budget-usd=0.750000", "-p"],
        )

    def test_parse_model_output_formats(self) -> None:
        runner = load_runner_module()
        text, usage = runner.parse_model_output("plain answer\n", "")
        self.assertEqual((text, usage), ("plain answer", None))
        # A judge printing a bare JSON verdict is not mistaken for a CLI envelope.
        text, usage = runner.parse_model_output('{"verdict": "pass"}', "")
        self.assertEqual((text, usage), ('{"verdict": "pass"}', None))
        text, usage = runner.parse_model_output("done\n", "header\ntokens used: 12,345\n")
        self.assertEqual(usage, {"tokens": 12345, "usd": None})
        claude_stream = "\n".join([
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "result", "result": "hi", "total_cost_usd": 0.25,
                        "usage": {"input_tokens": 1, "cache_read_input_tokens": 10, "output_tokens": 2}}),
        ])
        text, usage = runner.parse_model_output(claude_stream, "")
        self.assertEqual(text, "hi")
        self.assertEqual((usage["tokens"], usage["usd"]), (13, 0.25))
        with self.assertRaises(runner.RunnerError):
            runner.parse_model_output(
                json.dumps({"type": "result", "is_error": True, "result": "overloaded", "usage": {}}), ""
            )
        # Shape of a real `--max-budget-usd` stop: `usage` zeroed, the spend
        # only in modelUsage / total_cost_usd - and it must survive the error.
        budget_stop = {
            "type": "result", "subtype": "error_max_budget_usd", "is_error": True, "result": "",
            "total_cost_usd": 0.158168,
            "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0},
            "modelUsage": {
                "claude-opus-5-5": {"inputTokens": 2, "outputTokens": 570, "cacheReadInputTokens": 0,
                                    "cacheCreationInputTokens": 29352, "costUSD": 0.158168},
                "claude-haiku-5-5": {"inputTokens": 100, "outputTokens": 10, "cacheReadInputTokens": 0,
                                     "cacheCreationInputTokens": 0, "costUSD": 0.0},
            },
        }
        with self.assertRaises(runner.ModelError) as caught:
            runner.parse_model_output(json.dumps(budget_stop), "")
        self.assertEqual(caught.exception.kind, "error_max_budget_usd")
        self.assertEqual(caught.exception.usage["tokens"], 2 + 570 + 29352 + 100 + 10)
        self.assertEqual(caught.exception.usage["usd"], 0.158168)
        with self.assertRaises(runner.RunnerError):
            runner.parse_model_output(
                json.dumps({"type": "turn.failed", "error": {"message": "401"}}), ""
            )


class ConformanceTests(unittest.TestCase):
    def test_reference_runner_passes_conformance_suite(self) -> None:
        result = run_cmd(
            [
                sys.executable,
                str(ROOT / "conformance" / "check_runner.py"),
                str(RUNNER_TEMPLATE),
            ],
            ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)


if __name__ == "__main__":
    unittest.main()
