#!/usr/bin/env python3
"""Reproducible boundary tests. Fake Harness tests never call a model API.

Use --official-config PATH to additionally exercise the installed official
Harness config composer. That lane only runs --dump-config, never a task.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


WRAPPER = Path(__file__).with_name("run_dsh.py")
OFFICIAL_CONFIG = os.environ.get("CODEX_DSH_OFFICIAL_CONFIG")
# Optional public composition identity for the official --dump-config lane.
OFFICIAL_PROVIDER = os.environ.get("CODEX_DSH_TEST_PROVIDER", "deepseek-official")
OFFICIAL_MODEL = os.environ.get("CODEX_DSH_TEST_MODEL", "deepseek-flash")
FAKE_DSH = r'''
import json, os, pathlib, subprocess, sys, time
argv = sys.argv[1:]
task = json.loads(sys.stdin.read())
mode = task.get("mode", "normal")
sid = argv[argv.index("--session-id") + 1] if "--session-id" in argv else task.get("new_session_id", "test-session")
if mode == "surrogate-session":
    sid = "\ud800"
def emit(value):
    print(json.dumps(value), flush=True)
def session():
    emit({"type":"session", "sessionId":sid, "cwd":os.getcwd()})
def start(turn=0):
    emit({"type":"status", "phase":"turn_start", "turn":turn})
def end(reason=None, turn=0):
    emit({"type":"status", "phase":"turn_end", "turn":turn,
          "reason": reason or {"kind":"completed"}})
def final(text="done"):
    emit({"type":"final", "text":text})
if mode == "error-only":
    emit({"type":"error", "message":"driver setup failed"})
    sys.exit(1)
if mode == "invalid-json":
    print("{invalid", flush=True)
    sys.exit(0)
if mode == "invalid-utf8":
    sys.stdout.buffer.write(b'\xff\n'); sys.stdout.buffer.flush()
    sys.exit(0)
if mode == "oversized-line":
    print("x" * 100000, flush=True)
    sys.exit(0)
if mode == "flood-stderr":
    sys.stderr.write("x" * 100000); sys.stderr.flush()
    time.sleep(3)
    sys.exit(0)
if mode == "flood-stdout":
    session(); start()
    for i in range(3000):
        emit({"type":"text", "text":"x" * 100})
    sys.exit(0)
if mode == "sleep-child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    pathlib.Path(task["pid_file"]).write_text(str(child.pid))
    session(); start()
    time.sleep(120)
    sys.exit(0)
if mode == "pipe-child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    pathlib.Path(task["pid_file"]).write_text(str(child.pid))
session()
if mode in ("list-phase", "object-phase"):
    emit({"type":"status", "phase":[] if mode == "list-phase" else {}, "turn":0})
if mode == "duplicate-session":
    session()
if mode == "final-before-end":
    final(); start(); end()
    sys.exit(0)
start()
if mode == "surrogate-call":
    emit({"type":"tool_call", "callId":"\ud800", "tool":"read_file", "input":{}})
if mode in ("list-tool-status", "object-tool-status"):
    emit({"type":"tool_call", "callId":"bad-status", "tool":"read_file", "input":{}})
    emit({"type":"tool_result", "callId":"bad-status", "status":[] if mode == "list-tool-status" else {}, "result":""})
if mode == "surrogate-text":
    emit({"type":"text", "text":"\ud800"})
if mode == "tool-error":
    emit({"type":"tool_call", "callId":"call-1", "tool":"bash", "input":{"command":"false"}})
    emit({"type":"tool_result", "callId":"call-1", "status":"error", "result":"command failed"})
if mode == "tool-success":
    emit({"type":"tool_call", "callId":"call-1", "tool":"read_file", "input":{"path":"input.txt"}})
    emit({"type":"tool_result", "callId":"call-1", "status":"completed", "result":"contents"})
if mode == "incomplete":
    sys.exit(0)
if mode == "auth-error":
    end({"kind":"error", "error":{"code":"AUTH", "message":"authentication failed"}})
    final("")
    sys.exit(1)
if mode == "auth-code-only":
    end({"kind":"error", "error":{"code":"AUTH", "message":"Invalid token"}})
    final("")
    sys.exit(1)
if mode in ("max-tokens", "blocked"):
    end({"kind":mode})
    final("")
    sys.exit(1)
if mode == "failed-turn":
    end({"kind":"error", "error":{"code":"SERVER", "message":"request failed"}})
    final("")
    sys.exit(1)
if mode == "multi-turn":
    emit({"type":"text", "text":"done"})
    end(); start(1)
    emit({"type":"text", "text":"done"})
    end(turn=1); final()
    sys.exit(0)
answer = "done"
if mode == "answer-limit":
    answer = "x" * 5000
elif mode == "truncated-text":
    answer = "λ" * 6000
elif mode == "capture":
    answer = json.dumps({"argv":argv, "cwd":os.getcwd(), "dsh_home":os.environ.get("DSH_HOME"), "task":task})
if mode == "truncated-text":
    emit({"type":"text", "text":answer[:4000], "truncated":True})
else:
    emit({"type":"text", "text":answer})
end()
if mode == "no-final":
    sys.exit(0)
final("\ud800" if mode == "surrogate-final" else "different" if mode == "mismatched-final" else answer)
if mode == "duplicate-final":
    final()
if mode == "event-after-final":
    emit({"type":"text", "text":"late"})
if mode == "nonzero-completed":
    sys.exit(1)
'''


def snapshot(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def live_process(pid):
    stat = Path("/proc") / str(pid) / "stat"
    if stat.exists():
        try:
            return stat.read_text().split(") ", 1)[1][0] != "Z"
        except (OSError, IndexError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class SimulatedWrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-dsh-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "workspace with spaces"
        self.cwd.mkdir()
        self.home = self.root / "harness-home"
        for name in ("default", "review"):
            profile = self.home / "profiles" / name
            profile.mkdir(parents=True)
            (profile / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
        self.fake = self.root / "fake_dsh.py"
        self.fake.write_text(FAKE_DSH, encoding="utf-8")
        self.config = self.root / "bridge.json"
        self.settings = {
            "schema_version": 1,
            "dsh_command": [sys.executable, str(self.fake)],
            "dsh_home": str(self.home), "default_profile": "default",
            "default_patches": [], "timeout_seconds": 5,
            "log_dir": str(self.root / "logs"),
        }
        self.write_config()

    def write_config(self):
        self.config.write_text(json.dumps(self.settings), encoding="utf-8")

    def invoke(self, mode="normal", extra=(), task=None, stdin=False, expected=0):
        text = json.dumps(task or {"mode": mode}, ensure_ascii=False)
        task_file = self.root / "task.txt"
        task_file.write_text(text, encoding="utf-8")
        command = [sys.executable, str(WRAPPER), "--config", str(self.config),
                   "--cwd", str(self.cwd), "--task-file", "-" if stdin else str(task_file), *extra]
        result = subprocess.run(command, input=text if stdin else None,
                                text=True, capture_output=True, timeout=12)
        try:
            value = json.loads(result.stdout)
        except ValueError:
            self.fail(f"Expected one JSON result: code={result.returncode}, stdout={result.stdout[:500]!r}, stderr={result.stderr[:500]!r}")
        if expected is not None:
            self.assertEqual(result.returncode, expected, value)
            self.assertEqual(value["wrapper_exit_code"], expected, value)
        for key in ("harness_completed", "process_exit_code", "wrapper_exit_code", "session_id",
                    "profile", "patches", "answer", "turn_end", "tool_errors", "elapsed_seconds",
                    "log_dir", "error", "schema_version", "hard_timeout_seconds",
                    "protocol_completed", "result_available", "natural_exit", "finalization",
                    "cleanup", "supervision", "status_requested", "status_complete",
                    "run_id", "cancellation", "diagnostics", "log_complete", "log_degraded"):
            self.assertIn(key, value)
        return value

    def patch(self, name):
        path = self.root / name
        path.write_text("[]\n", encoding="utf-8")
        return str(path)

    def test_default_stdin_and_exact_task_transport(self):
        task = {"mode":"capture", "text":"中文 UTF-8; $(touch SHOULD_NOT_EXIST) `id`\nsecond line"}
        value = self.invoke(task=task, stdin=True)
        answer = json.loads(value["answer"])
        self.assertTrue(value["harness_completed"])
        self.assertEqual(answer["task"], task)
        self.assertEqual(answer["cwd"], str(self.cwd))
        self.assertEqual(answer["dsh_home"], str(self.home))
        self.assertEqual(answer["argv"], ["--profile", "default", "--json", "-"])
        self.assertFalse((self.cwd / "SHOULD_NOT_EXIST").exists())

    def test_profile_default_patches_and_ordered_call_overrides(self):
        a, b, c = self.patch("default.yml"), self.patch("first.yml"), self.patch("second.yml")
        self.settings["default_patches"] = [a]
        self.write_config()
        before = snapshot(self.home / "profiles")
        value = self.invoke("capture", extra=("--profile", "review", "--patch", b, "--patch", c))
        self.assertEqual(value["profile"], "review")
        self.assertEqual(value["patches"], [a, b, c])
        self.assertEqual(json.loads(value["answer"])["argv"],
                         ["--profile", "review", "--patch", a, "--patch", b, "--patch", c, "--json", "-"])
        self.assertEqual(snapshot(self.home / "profiles"), before)
        plain = self.invoke("capture")
        self.assertEqual(plain["profile"], "default")
        self.assertEqual(plain["patches"], [a])

    def test_profile_and_single_patch_are_one_invocation_only(self):
        patch = self.patch("once.yml")
        before = snapshot(self.home / "profiles")
        selected = self.invoke("capture", extra=("--profile", "review"))
        self.assertEqual(selected["profile"], "review")
        self.assertEqual(selected["patches"], [])
        patched = self.invoke("capture", extra=("--patch", patch))
        self.assertEqual(patched["profile"], "default")
        self.assertEqual(patched["patches"], [patch])
        plain = self.invoke("capture")
        self.assertEqual(plain["profile"], "default")
        self.assertEqual(plain["patches"], [])
        self.assertEqual(snapshot(self.home / "profiles"), before)

    def test_normal_tools_and_failed_tools_remain_separate_from_completion(self):
        good = self.invoke("tool-success")
        self.assertEqual(good["tool_errors"], [])
        bad = self.invoke("tool-error")
        self.assertTrue(bad["harness_completed"])
        self.assertEqual(len(bad["tool_errors"]), 1)

    def test_sequential_turns(self):
        self.assertTrue(self.invoke("multi-turn")["harness_completed"])

    def test_failed_turn_authentication_and_direct_failure(self):
        for mode, code in (("failed-turn", 8), ("auth-error", 4), ("auth-code-only", 4),
                           ("error-only", 8), ("max-tokens", 8), ("blocked", 8)):
            with self.subTest(mode=mode):
                value = self.invoke(mode, expected=code)
                self.assertFalse(value["harness_completed"])
                self.assertIsNotNone(value["error"])

    def test_malformed_incomplete_and_out_of_order_streams(self):
        for mode in ("invalid-json", "invalid-utf8", "duplicate-session", "final-before-end",
                     "incomplete", "no-final", "duplicate-final", "event-after-final", "nonzero-completed", "mismatched-final"):
            with self.subTest(mode=mode):
                value = self.invoke(mode, expected=5)
                self.assertFalse(value["harness_completed"])

    def test_upstream_truncated_text_keeps_the_lossless_final(self):
        value = self.invoke("truncated-text")
        self.assertTrue(value["harness_completed"])
        self.assertEqual(value["answer"], "λ" * 6000)
        self.assertTrue(value["upstream_truncated"])

    def test_adversarial_event_shapes_return_structured_protocol_errors(self):
        for mode in ("list-phase", "object-phase", "list-tool-status", "object-tool-status",
                     "surrogate-session", "surrogate-call", "surrogate-text", "surrogate-final"):
            with self.subTest(mode=mode):
                value = self.invoke(mode, expected=5)
                self.assertFalse(value["harness_completed"])
                self.assertEqual(value["error"]["category"], "protocol")

    def test_limits_are_visible_and_fail(self):
        for mode, limits in (("oversized-line", {"max_line_bytes":1024}),
                             ("answer-limit", {"max_answer_bytes":50}),
                             ("flood-stderr", {"max_stderr_bytes":1024}),
                             ("flood-stdout", {"max_stdout_bytes":2048}),
                             ("normal", {"max_events":2})):
            with self.subTest(mode=mode, limits=limits):
                self.settings.update(limits)
                self.write_config()
                value = self.invoke(mode, expected=9)
                self.assertFalse(value["harness_completed"])
                for key in limits:
                    self.settings.pop(key)

    def test_task_limit_and_invalid_utf8(self):
        self.settings["max_task_bytes"] = 10
        self.write_config()
        self.assertFalse(self.invoke("normal", expected=9)["harness_completed"])
        self.settings.pop("max_task_bytes")
        self.write_config()
        bad = self.root / "bad.txt"
        bad.write_bytes(b"\xff")
        self.invoke(extra=("--task-file", str(bad)), expected=2)

    def test_invalid_configuration_paths_and_timeouts(self):
        for extra in (("--profile", "missing"), ("--patch", "relative.yml"),
                      ("--patch", str(self.root / "absent.yml")),
                      ("--cwd", str(self.root / "absent-directory")),
                      ("--timeout", "0"), ("--timeout", "nan"),
                      ("--timeout", "inf"), ("--timeout", "-1")):
            with self.subTest(extra=extra):
                self.invoke(extra=extra, expected=2)
        for key, invalid in (("schema_version", 3), ("default_patches", "bad"),
                             ("timeout_seconds", False), ("timeout_seconds", math.inf)):
            with self.subTest(key=key):
                old = self.settings[key]
                self.settings[key] = invalid
                self.write_config()
                self.invoke(expected=2)
                self.settings[key] = old
        self.write_config()

    def test_schema2_null_deadline_and_invalid_hard_budget(self):
        self.settings["schema_version"] = 2
        self.settings.pop("timeout_seconds")
        self.write_config()
        value = self.invoke("normal", expected=0)
        self.assertIsNone(value["hard_timeout_seconds"])
        self.assertTrue(value["harness_completed"])
        self.settings["hard_timeout_seconds"] = 0
        self.write_config()
        value = self.invoke("normal", expected=2)
        self.assertEqual(value["error"]["code"], "HARD_TIMEOUT_INVALID")
        self.settings["hard_timeout_seconds"] = 0.2
        self.write_config()
        value = self.invoke("sleep-child",
                            task={"mode":"sleep-child", "pid_file":str(self.root / "schema2.pid")},
                            expected=6)
        self.assertEqual(value["hard_timeout_seconds"], 0.2)

    def test_schema2_documented_supervision_defaults(self):
        self.settings["schema_version"] = 2
        for key in ("heartbeat_seconds", "stall_warning_seconds",
                    "shutdown_grace_seconds", "finalization_grace_seconds",
                    "timeout_seconds"):
            self.settings.pop(key, None)
        self.write_config()
        value = self.invoke("normal", expected=0)
        self.assertTrue(value["harness_completed"])
        self.assertEqual(value["supervision"]["heartbeat_seconds"], 5)
        self.assertEqual(value["supervision"]["stall_warning_seconds"], 300)
        self.assertEqual(value["supervision"]["shutdown_grace_seconds"], 10)
        self.assertEqual(value["supervision"]["finalization_grace_seconds"], 10)
        self.assertIsNone(value["hard_timeout_seconds"])

    def test_full_log_rotates_without_failing_healthy_task(self):
        self.settings["max_log_bytes"] = 50
        self.write_config()
        value = self.invoke("normal", expected=0)
        self.assertTrue(value["harness_completed"])
        self.assertIsNone(value["error"])
        self.assertFalse(value["log_complete"])
        self.assertTrue(value["log_degraded"])
        self.assertTrue(value["log_truncated"] or value["log_dropped_events"])

    def test_launch_failure(self):
        self.settings["dsh_command"] = [str(self.root / "missing-executable")]
        self.write_config()
        self.invoke(expected=2)
        invalid_executable = self.root / "invalid-executable"
        invalid_executable.write_bytes(b"This file has no executable image or interpreter.\n")
        invalid_executable.chmod(0o700)
        self.settings["dsh_command"] = [str(invalid_executable)]
        self.write_config()
        self.invoke(expected=3)

    def test_session_resume_and_mismatched_selection(self):
        first = self.invoke()
        resumed = self.invoke(extra=("--session-id", first["session_id"]))
        self.assertEqual(resumed["session_id"], first["session_id"])
        self.invoke(extra=("--session-id", first["session_id"], "--profile", "review"), expected=10)

    def test_official_compatible_unicode_session_identity(self):
        session_id = "审查会话 1"
        first = self.invoke(task={"mode":"normal", "new_session_id":session_id})
        self.assertEqual(first["session_id"], session_id)
        resumed = self.invoke(extra=("--session-id", session_id))
        self.assertEqual(resumed["session_id"], session_id)

    @unittest.skipUnless(os.name == "posix", "POSIX process-group acceptance")
    def check_timeout_child(self, mode):
        pid_file = self.root / "child.pid"
        value = self.invoke(task={"mode":mode, "pid_file":str(pid_file)},
                            extra=("--timeout", "0.35"), expected=None if mode == "pipe-child" else 6)
        self.assertIn(value["wrapper_exit_code"], (0, 6))
        self.assertEqual(value["harness_completed"], value["wrapper_exit_code"] == 0)
        self.assertTrue(pid_file.exists(), value)
        pid = int(pid_file.read_text())
        self.addCleanup(lambda: os.kill(pid, signal.SIGKILL) if live_process(pid) else None)
        deadline = time.monotonic() + 2
        while live_process(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(live_process(pid), f"Owned ordinary child {pid} survived timeout")

    @unittest.skipUnless(os.name == "posix", "POSIX process-group acceptance")
    def test_timeout_terminates_owned_process_group(self):
        self.check_timeout_child("sleep-child")

    @unittest.skipUnless(os.name == "posix", "POSIX pipe lifetime acceptance")
    def test_exited_parent_cannot_leave_child_holding_pipes(self):
        self.check_timeout_child("pipe-child")

    @unittest.skipUnless(os.name == "posix", "POSIX signal acceptance")
    def test_interruption_terminates_owned_process_group(self):
        pid_file = self.root / "interrupt-child.pid"
        task_file = self.root / "interrupt-task.txt"
        task_file.write_text(json.dumps({"mode":"sleep-child", "pid_file":str(pid_file)}), encoding="utf-8")
        process = subprocess.Popen([sys.executable, str(WRAPPER), "--config", str(self.config),
                                    "--cwd", str(self.cwd), "--task-file", str(task_file)],
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 4
            while not pid_file.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.exists(), "Fixture did not reach the child-running state")
            child_pid = int(pid_file.read_text())
            self.addCleanup(lambda: os.kill(child_pid, signal.SIGKILL) if live_process(child_pid) else None)
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 7, (stdout, stderr))
            value = json.loads(stdout)
            self.assertEqual(value["wrapper_exit_code"], 7)
            self.assertFalse(value["harness_completed"])
            self.assertFalse(live_process(child_pid), "Owned ordinary child survived interruption")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_log_permissions(self):
        value = self.invoke()
        directory = Path(value["log_dir"])
        self.assertTrue(directory.is_dir())
        if os.name == "posix":
            self.assertEqual(directory.stat().st_mode & 0o077, 0)
            for path in directory.rglob("*"):
                self.assertEqual(path.stat().st_mode & 0o077, 0, str(path))


@unittest.skipUnless(OFFICIAL_CONFIG, "pass --official-config or set CODEX_DSH_OFFICIAL_CONFIG for installed Harness configuration-only checks")
class OfficialCompositionTests(unittest.TestCase):
    """Real official composer; no API task and no production profile edits."""
    def test_real_official_patch_order_and_no_persistence(self):
        settings = json.loads(Path(OFFICIAL_CONFIG).read_text(encoding="utf-8"))
        command = settings["dsh_command"]
        with tempfile.TemporaryDirectory(prefix="codex-dsh-official-compose-") as temporary:
            root = Path(temporary)
            env = dict(os.environ, DSH_HOME=str(root / "home"))
            # Initialization is restricted to this new temporary Harness home.
            initialized = subprocess.run([*command, "--profile", "compose-test", "--from-default-profile", "headless", "--dump-config"],
                                         env=env, cwd=root, text=True, capture_output=True, timeout=30)
            self.assertEqual(initialized.returncode, 0, initialized.stderr[:500])
            profile = root / "home" / "profiles" / "compose-test"
            patch_paths = []
            for index, effort in enumerate(("low", "high", "max")):
                patch = root / f"{index}.yml"
                patch.write_text("- id: agent-default-model\n  config:\n    provider: " + OFFICIAL_PROVIDER + "\n    model: " + OFFICIAL_MODEL + "\n    reasoningEffort: " + effort + "\n", encoding="utf-8")
                patch_paths.append(patch)
            before = snapshot(profile)
            # Each complete config replaces the preceding config for that row.
            for chosen in (patch_paths[:1], patch_paths[:2], patch_paths, list(reversed(patch_paths))):
                args = [*command, "--profile", "compose-test"]
                for path in chosen:
                    args.extend(("--patch", str(path)))
                result = subprocess.run([*args, "--dump-config"], env=env, cwd=root,
                                        text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr[:500])
                expected_effort = chosen[-1].read_text().split("reasoningEffort: ", 1)[1].strip()
                # Narrow evidence: model selection from official dump, not runtime API evidence.
                import re
                match = re.search(r"(?m)^([ ]*)- id: agent-default-model\s*$([\s\S]*?)(?=^\1- id:|\Z)", result.stdout)
                self.assertIsNotNone(match, result.stdout[:500])
                row = match.group(2)
                self.assertIn("provider: " + OFFICIAL_PROVIDER, row)
                self.assertIn("model: " + OFFICIAL_MODEL, row)
                self.assertIn("reasoningEffort: " + expected_effort, row)
                self.assertEqual(snapshot(profile), before)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--official-config")
    options, remaining = parser.parse_known_args()
    OFFICIAL_CONFIG = options.official_config
    # The decorator above runs at import time; activate its lane explicitly here.
    OfficialCompositionTests.__unittest_skip__ = not bool(OFFICIAL_CONFIG)
    unittest.main(argv=[sys.argv[0], *remaining])
