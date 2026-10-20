#!/usr/bin/env python3
"""Pure-Python supervision tests: no model API, no installed dsh, no secrets.

The integration lane drives scripts/run_dsh.py with a fake Harness.  Status and
control documents are inspected only through their public schemas; raw task,
tool, reasoning, and environment content is never expected in those files.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import supervision  # noqa: E402


class StatusDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-dsh-supervision-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.status_path = self.root / "run.status.json"
        self.now = time.time()

    def status_document(self, **overrides):
        document = {
            "schema_version": supervision.STATUS_SCHEMA_VERSION,
            "run_id": "run-test-0001",
            "supervisor": supervision.process_identity(os.getpid()),
            "child": None,
            "session_id": "session-test",
            "phase": "running",
            "heartbeat_epoch": time.time(),
            "stale_after_seconds": 2.0,
            "supervision": {"heartbeat_seconds": 0.1,
                            "stall_warning_seconds": 0.4,
                            "shutdown_grace_seconds": 0.3,
                            "finalization_grace_seconds": 0.3},
            "last_event_epoch": self.now,
            "last_tool_step_epoch": self.now,
            "active_tools": [],
            "error": None,
            "progress_boundaries": ["heartbeat_is_liveness_not_task_progress"],
            "terminal": {"state": "running"},
        }
        document.update(overrides)
        return document

    def publish(self, **overrides):
        document = self.status_document(**overrides)
        publisher = supervision.StatusPublisher(self.status_path, document)
        return document, publisher

    @unittest.skipUnless(supervision.sys_is_linux(), "Linux process identity semantics")
    def test_publisher_is_private_atomic_and_verifiable(self):
        document, publisher = self.publish()
        self.assertEqual(self.status_path.stat().st_mode & 0o777, 0o600)
        path, read = supervision.read_status_document(str(self.status_path))
        self.assertEqual(path, self.status_path.resolve())
        self.assertEqual(read["run_id"], document["run_id"])
        verification = supervision.verify_status_document(read)
        self.assertEqual(verification["state"], "RUNNING")
        self.assertTrue(verification["identity_verified"])
        self.assertTrue(verification["fresh"])
        publisher.publish(self.status_document(heartbeat_epoch=time.time() + 1))
        _, read = supervision.read_status_document(str(self.status_path))
        self.assertEqual(supervision.verify_status_document(read)["state"], "RUNNING")

    def test_existing_status_file_is_not_overwritten(self):
        self.status_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(supervision.SupervisionError,
                                    "^STATUS_FILE_EXISTS$"):
            supervision.StatusPublisher(self.status_path, self.status_document())

    def test_symlink_status_destination_is_refused_without_following(self):
        target = self.root / "target.json"
        target.write_text("untouched", encoding="utf-8")
        self.status_path.symlink_to(target)
        with self.assertRaisesRegex(supervision.SupervisionError,
                                    "^STATUS_SYMLINK_REFUSED$"):
            supervision.canonical_status_path(str(self.status_path))
        self.assertEqual(target.read_text(encoding="utf-8"), "untouched")

    def test_relative_or_missing_parent_status_path_is_invalid(self):
        with self.assertRaisesRegex(supervision.SupervisionError, "^STATUS_PATH_INVALID$"):
            supervision.validate_status_destination("relative.json")
        with self.assertRaisesRegex(supervision.SupervisionError, "^STATUS_PARENT_INVALID$"):
            supervision.validate_status_destination(str(self.root / "missing" / "status.json"))

    def test_publisher_rejects_replaced_status_file(self):
        _, publisher = self.publish()
        self.status_path.unlink()
        self.status_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(supervision.SupervisionError,
                                    "^STATUS_FILE_REPLACED$"):
            publisher.publish(self.status_document())

    def test_publisher_rejects_changed_content_even_with_the_same_inode(self):
        _, publisher = self.publish()
        inode = self.status_path.stat().st_ino
        self.status_path.write_text("{}", encoding="utf-8")
        self.assertEqual(self.status_path.stat().st_ino, inode)
        with self.assertRaisesRegex(supervision.SupervisionError,
                                    "^STATUS_FILE_REPLACED$"):
            publisher.publish(self.status_document())
        self.assertEqual(self.status_path.read_text(encoding="utf-8"), "{}")

    def test_terminal_completed_is_not_a_failure(self):
        _, publisher = self.publish()
        publisher.publish(self.status_document(
            terminal={"state": "completed", "protocol_completed": True,
                      "result_available": True, "cleanup": "clean"}))
        _, read = supervision.read_status_document(str(self.status_path))
        verification = supervision.verify_status_document(read)
        self.assertEqual(verification["state"], "COMPLETED")
        self.assertTrue(verification["terminal"])
        self.assertIsNone(verification["reason"])

    def test_stale_heartbeat_is_unknown_not_failed(self):
        _, publisher = self.publish()
        publisher.publish(self.status_document(heartbeat_epoch=time.time() - 100))
        _, read = supervision.read_status_document(str(self.status_path))
        verification = supervision.verify_status_document(read)
        self.assertEqual(verification["state"], "UNKNOWN")
        self.assertEqual(verification["reason"], "HEARTBEAT_EXPIRED")
        self.assertFalse(verification["fresh"])
        self.assertFalse(verification["terminal"])

    def test_supervisor_pid_reuse_is_unknown(self):
        supervisor = supervision.process_identity(os.getpid())
        self.assertIsNotNone(supervisor)
        forged = dict(supervisor)
        forged["start_time_ticks"] = int(supervisor["start_time_ticks"]) + 1
        _, publisher = self.publish(supervisor=forged)
        _, read = supervision.read_status_document(str(self.status_path))
        verification = supervision.verify_status_document(read)
        self.assertEqual(verification["state"], "UNKNOWN")
        self.assertEqual(verification["reason"], "PID_REUSE_DETECTED")
        self.assertFalse(verification["identity_verified"])

    def test_concurrent_atomic_readers_never_see_partial_json(self):
        document = self.status_document()
        publisher = supervision.StatusPublisher(self.status_path, document)
        stop = threading.Event()
        errors = []

        def reader():
            while not stop.is_set():
                try:
                    path, value = supervision.read_status_document(str(self.status_path))
                    if value.get("run_id") != document["run_id"]:
                        errors.append("run_id changed")
                except supervision.SupervisionError as exc:
                    errors.append(str(exc))
                time.sleep(0.001)

        threads = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
        for thread in threads:
            thread.start()
        try:
            for sequence in range(30):
                publisher.publish(self.status_document(heartbeat_epoch=time.time(),
                                                       phase="running",
                                                       heartbeat_sequence=sequence))
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(errors, [])


class ControlRequestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-dsh-control-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.status_path = self.root / "run.status.json"
        self.status_path.write_text("{}\n", encoding="utf-8")
        self.supervisor = supervision.process_identity(os.getpid())
        self.document = {
            "schema_version": supervision.STATUS_SCHEMA_VERSION,
            "run_id": "run-control-0001",
            "supervisor": self.supervisor,
            "heartbeat_epoch": time.time(),
            "terminal": {"state": "running"},
        }

    def test_cancel_request_is_run_id_and_identity_scoped(self):
        path = supervision.write_control_request(self.status_path, self.document,
                                                 "user_cancelled")
        self.assertEqual(path, supervision.control_path(self.status_path))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        request = supervision.read_control_request(self.status_path,
                                                   "run-control-0001",
                                                   self.supervisor)
        self.assertIsNotNone(request)
        self.assertEqual(request["reason"], "user_cancelled")
        self.assertEqual(request["run_id"], "run-control-0001")
        self.assertEqual(request["target"]["pid"], os.getpid())
        with self.assertRaisesRegex(supervision.SupervisionError,
                                    "^CONTROL_FILE_EXISTS$"):
            supervision.write_control_request(self.status_path, self.document)

    def test_wrong_run_id_and_identity_are_rejected(self):
        supervision.write_control_request(self.status_path, self.document)
        with self.assertRaisesRegex(supervision.SupervisionError,
                                    "^CONTROL_RUN_MISMATCH$"):
            supervision.read_control_request(self.status_path, "run-other",
                                             self.supervisor)
        forged = dict(self.supervisor)
        forged["start_time_ticks"] = int(self.supervisor["start_time_ticks"]) + 1
        with self.assertRaisesRegex(supervision.SupervisionError,
                                    "^CONTROL_TARGET_MISMATCH$"):
            supervision.read_control_request(self.status_path, "run-control-0001",
                                             forged)

    def test_only_user_cancelled_reason_is_accepted(self):
        with self.assertRaisesRegex(supervision.SupervisionError,
                                    "^CONTROL_REASON_INVALID$"):
            supervision.write_control_request(self.status_path, self.document,
                                              "operator_kill")

    def test_control_symlink_is_not_followed(self):
        target = self.root / "target.json"
        target.write_text("untouched", encoding="utf-8")
        supervision.control_path(self.status_path).symlink_to(target)
        with self.assertRaisesRegex(supervision.SupervisionError,
                                    "^CONTROL_FILE_EXISTS$"):
            supervision.write_control_request(self.status_path, self.document)
        self.assertEqual(target.read_text(encoding="utf-8"), "untouched")
        with self.assertRaisesRegex(supervision.SupervisionError,
                                    "^CONTROL_SYMLINK_REFUSED$"):
            supervision.read_control_request(self.status_path, "run-control-0001",
                                             self.supervisor)



SCRIPTS = Path(__file__).resolve().parent
RUN_DSH = SCRIPTS / "run_dsh.py"
MANAGE = SCRIPTS / "manage_dsh.py"

FAKE_HARNESS = r'''
import json, os, pathlib, subprocess, sys, time

argv = sys.argv[1:]
if "--early-eof" in argv:
    sys.stdin.buffer.read(1)
    sys.exit(0)
task = json.loads(sys.stdin.buffer.read().decode("utf-8"))
mode = task.get("mode", "normal")
secret = task.get("secret", "SYNTHETIC-STATUS-MARKER")
answer = task.get("answer", "fake-answer")


def emit(value):
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


def session():
    emit({"type": "session", "sessionId": "fake-session", "cwd": os.getcwd()})


def turn_start(turn=0):
    emit({"type": "status", "phase": "turn_start", "turn": turn})


def turn_end(turn=0, reason=None):
    emit({"type": "status", "phase": "turn_end", "turn": turn,
          "reason": reason or {"kind": "completed"}})


def step_start(step=0):
    emit({"type": "status", "phase": "step_start", "turn": 0, "step": step})


def step_end(step=0):
    emit({"type": "status", "phase": "step_end", "turn": 0, "step": step})


def final(text):
    emit({"type": "final", "text": text})


def record():
    own = task.get("harness_pid_file")
    if own:
        pathlib.Path(own).write_text(str(os.getpid()), encoding="utf-8")
    counter = task.get("start_counter")
    if counter:
        with open(counter, "a", encoding="utf-8") as handle:
            handle.write(str(os.getpid()) + "\n")


def spawn_sleeper():
    child = subprocess.Popen([sys.executable, "-c",
                              "import time; time.sleep(120)"])
    child_path = task.get("child_pid_file")
    if child_path:
        pathlib.Path(child_path).write_text(str(child.pid), encoding="utf-8")
    return child


record()
if mode == "invalid-json":
    print("{invalid", flush=True)
    sys.exit(0)
if mode == "protocol-error":
    session()
    turn_start()
    emit({"type": "not-a-protocol-event"})
    sys.exit(0)
session()
turn_start()
if mode == "normal":
    emit({"type": "text", "text": answer})
    turn_end()
    final(answer)
elif mode == "silent":
    time.sleep(float(task.get("hold", 0.5)))
    emit({"type": "text", "text": answer})
    turn_end()
    final(answer)
elif mode == "active-tool":
    emit({"type": "thinking", "text": secret})
    step_start()
    emit({"type": "tool_call", "callId": "call-active", "tool": "bash",
          "input": {"command": secret}})
    time.sleep(float(task.get("hold", 0.5)))
    emit({"type": "tool_result", "callId": "call-active",
          "status": "completed", "result": secret})
    step_end()
    emit({"type": "text", "text": answer})
    turn_end()
    final(answer)
elif mode == "tool-error-flow":
    step_start()
    emit({"type": "tool_call", "callId": "call-error", "tool": "bash",
          "input": {"command": secret}})
    emit({"type": "tool_result", "callId": "call-error", "status": "error",
          "result": secret})
    step_end()
    emit({"type": "text", "text": answer})
    turn_end()
    final(answer)
elif mode == "failed-turn":
    emit({"type": "text", "text": answer})
    turn_end(reason={"kind": "error", "error": {"code": "SERVER",
                                                "message": "synthetic failure"}})
    final(answer)
    sys.exit(1)
elif mode == "finalization-hang":
    emit({"type": "text", "text": answer})
    turn_end()
    final(answer)
    sys.stdout.flush()
    time.sleep(float(task.get("hold", 120)))
elif mode == "final-orphan-pipes":
    emit({"type": "text", "text": answer})
    turn_end()
    final(answer)
    sys.stdout.flush()
    spawn_sleeper()
    sys.exit(0)
elif mode == "timeout-child":
    spawn_sleeper()
    time.sleep(120)
elif mode == "cancel-wait":
    step_start()
    emit({"type": "tool_call", "callId": "call-cancel", "tool": "bash",
          "input": {"command": secret}})
    spawn_sleeper()
    time.sleep(120)
elif mode == "pipe-orphan":
    spawn_sleeper()
    sys.exit(0)
elif mode == "stdout-eof":
    sys.stdout.flush()
    os.close(1)
    time.sleep(120)
elif mode == "flood-text":
    for index in range(24):
        step_start(index)
        emit({"type": "text", "text": secret + "-" + str(index)})
        step_end(index)
    step_start(24)
    emit({"type": "text", "text": answer})
    step_end(24)
    turn_end()
    final(answer)
elif mode == "early-eof":
    sys.stdin.buffer.read(1)
    sys.exit(0)
else:
    emit({"type": "text", "text": answer})
    turn_end()
    final(answer)
'''


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


class IntegrationBase(unittest.TestCase):
    """Fake Harness process boundary; no model or installed dsh is used."""

    heartbeat = 0.05
    stall = 0.2
    shutdown = 0.25
    finalization = 0.25

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-dsh-sup-int-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.home = self.root / "home"
        for name in ("default", "review"):
            profile = self.home / "profiles" / name
            profile.mkdir(parents=True)
            (profile / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
        self.fake = self.root / "fake_harness.py"
        self.fake.write_text(FAKE_HARNESS, encoding="utf-8")
        self.config_path = self.root / "bridge.json"
        self.log_root = self.root / "logs"
        self.started = []
        self._sequence = 0

    def settings(self, schema=2, **overrides):
        value = {
            "schema_version": schema,
            "dsh_command": [sys.executable, str(self.fake)],
            "dsh_home": str(self.home),
            "default_profile": "default",
            "default_patches": [],
            "log_dir": str(self.log_root),
            "heartbeat_seconds": self.heartbeat,
            "stall_warning_seconds": self.stall,
            "shutdown_grace_seconds": self.shutdown,
            "finalization_grace_seconds": self.finalization,
        }
        if schema == 1:
            value["timeout_seconds"] = 5
        value.update(overrides)
        return value

    def write_config(self, value):
        self.config_path.write_text(json.dumps(value), encoding="utf-8")

    def task_file(self, task):
        self._sequence += 1
        path = self.root / ("task-%d.json" % self._sequence)
        path.write_text(json.dumps(task), encoding="utf-8")
        return path

    def command(self, task, extra=(), status=None, config=None):
        if config is None:
            config = self.settings()
        self.write_config(config)
        command = [sys.executable, str(RUN_DSH), "--config", str(self.config_path),
                   "--cwd", str(self.cwd), "--task-file", str(self.task_file(task)),
                   *extra]
        if status is not None:
            command.extend(["--status-file", str(status)])
        return command

    def start(self, task, extra=(), status=None, config=None):
        process = subprocess.Popen(self.command(task, extra, status, config),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        self.started.append(process)
        return process

    def finish(self, process, expected=None, timeout=12):
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            self.fail("wrapper did not finish: stdout=%r stderr=%r" % (
                stdout[:1000], stderr[:1000]))
        try:
            value = json.loads(stdout)
        except ValueError:
            self.fail("expected one JSON result: code=%r stdout=%r stderr=%r" % (
                process.returncode, stdout[:1000], stderr[:1000]))
        if expected is not None:
            self.assertEqual(process.returncode, expected, value)
            self.assertEqual(value["wrapper_exit_code"], expected, value)
        return value

    def invoke(self, task, extra=(), status=None, config=None, expected=None):
        return self.finish(self.start(task, extra, status, config), expected)

    def wait_status(self, path, predicate=None, timeout=6):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                last = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(0.01)
                continue
            if predicate is None or predicate(last):
                return last
            time.sleep(0.01)
        self.fail("status condition not met: %r" % (last,))

    def manage(self, *arguments, timeout=12):
        process = subprocess.run([sys.executable, str(MANAGE), *arguments],
                                 text=True, capture_output=True, timeout=timeout)
        try:
            value = json.loads(process.stdout)
        except ValueError:
            self.fail("manage output was not JSON: code=%r stdout=%r stderr=%r" % (
                process.returncode, process.stdout[:1000], process.stderr[:1000]))
        return process.returncode, value

    def wait_dead(self, pid, timeout=5):
        deadline = time.monotonic() + timeout
        while live_process(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(live_process(pid), "owned child %r survived" % pid)

    def tearDown(self):
        for process in self.started:
            if process.poll() is None:
                process.kill()
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self.started.clear()
        for pid_file in self.root.glob("*.pid"):
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            if pid != os.getpid() and live_process(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass


class SchemaPolicyTests(IntegrationBase):
    def test_schema2_legacy_timeout_seconds_is_not_an_implicit_deadline(self):
        config = self.settings(timeout_seconds=0.12)
        value = self.invoke({"mode": "silent", "hold": 0.4, "answer": "schema2-ok"},
                            config=config, expected=0)
        self.assertIsNone(value["hard_timeout_seconds"])
        self.assertTrue(value["harness_completed"])
        self.assertEqual(value["answer"], "schema2-ok")

    def test_schema1_timeout_seconds_remains_a_hard_budget(self):
        config = self.settings(schema=1, timeout_seconds=0.15)
        value = self.invoke({"mode": "silent", "hold": 2.0},
                            config=config, expected=6)
        self.assertEqual(value["hard_timeout_seconds"], 0.15)
        self.assertEqual(value["error"]["category"], "timeout")

    def test_schema2_hard_timeout_and_explicit_timeout_are_hard_budgets(self):
        for label, config, extra in (
                ("hard_timeout_seconds",
                 self.settings(hard_timeout_seconds=0.15), ()),
                ("explicit_timeout",
                 self.settings(), ("--timeout", "0.15"))):
            with self.subTest(label=label):
                value = self.invoke({"mode": "silent", "hold": 2.0},
                                    extra=extra, config=config, expected=6)
                self.assertEqual(value["error"]["category"], "timeout")

    def test_schema2_null_hard_timeout_is_reported(self):
        value = self.invoke({"mode": "normal"}, config=self.settings(), expected=0)
        self.assertIsNone(value["hard_timeout_seconds"])
        self.assertEqual(value["schema_version"], 2)
        self.assertEqual(value["supervision"]["heartbeat_seconds"], self.heartbeat)



class StatusChannelTests(IntegrationBase):
    @unittest.skipUnless(supervision.sys_is_linux(), "Linux process CPU accounting")
    def test_quiet_run_without_status_file_does_not_busy_spin(self):
        marker = self.root / "quiet.pid"
        process = self.start({"mode": "silent", "hold": 1.5,
                              "harness_pid_file": str(marker)})
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(marker.exists())

        def cpu_seconds():
            raw = Path(f"/proc/{process.pid}/stat").read_text()
            fields = raw.rsplit(")", 1)[1].split()
            return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")

        before = cpu_seconds()
        time.sleep(1)
        used = cpu_seconds() - before
        value = self.finish(process, expected=0)
        self.assertTrue(value["harness_completed"])
        self.assertLess(used, 0.4, "quiet supervisor consumed a busy CPU")

    def test_silent_run_beyond_stall_continues_and_heartbeat_is_not_progress(self):
        status = self.root / "silent.status.json"
        harness_file = self.root / "silent-harness.pid"
        process = self.start({"mode": "silent", "hold": 0.8,
                              "answer": "silent-answer",
                              "secret": "SILENT-TASK-MARKER",
                              "harness_pid_file": str(harness_file)}, status=status)
        stalled = self.wait_status(
            status, lambda d: d.get("supervision", {}).get("stall", {}).get("suspected") is True)
        deadline = time.monotonic() + 5
        while not harness_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(harness_file.exists())
        harness_pid = int(harness_file.read_text(encoding="utf-8"))
        self.assertEqual(stalled["child"]["pid"], harness_pid)
        self.assertEqual(supervision.identity_state(stalled["child"])[0], "verified")
        self.assertEqual(supervision.identity_state(stalled["supervisor"])[0], "verified")
        self.assertEqual(stalled["session_id"], "fake-session")
        time.sleep(0.12)
        later = self.wait_status(
            status, lambda d: d.get("heartbeat_epoch", 0) > stalled["heartbeat_epoch"])
        self.assertEqual(later["last_event_at"], stalled["last_event_at"])
        self.assertGreater(later["heartbeat_epoch"], stalled["heartbeat_epoch"])
        self.assertGreater(later["supervision"]["heartbeats_published"],
                           stalled["supervision"]["heartbeats_published"])
        self.assertIsNone(stalled["last_tool_completed_at"])
        self.assertIsNone(stalled["last_step_completed_at"])
        self.assertEqual(stalled["active_tools"]["active_calls"], 0)
        boundary = stalled["observation"]
        self.assertIsInstance(boundary, dict)
        self.assertFalse(boundary["heartbeat_is_progress"])
        self.assertEqual(boundary["semantic_progress"], "unknown")
        self.assertEqual(stalled["terminal"]["state"], "running")
        self.assertFalse(stalled["terminal"]["result_available"])
        self.assertIn("SUSPECTED_STALL",
                      [item.get("code") for item in stalled["diagnostics"]])
        self.assertNotIn("SILENT-TASK-MARKER", json.dumps(stalled))
        self.assertEqual(status.stat().st_mode & 0o777, 0o600)
        code, managed = self.manage("status", "--status-file", str(status))
        self.assertEqual(code, 0)
        self.assertEqual(managed["state"], "RUNNING")
        self.assertIsNone(managed["reason"])
        self.assertTrue(managed["identity_verified"])
        value = self.finish(process, expected=0)
        self.assertTrue(value["harness_completed"])
        self.assertEqual(value["answer"], "silent-answer")
        self.assertTrue(value["protocol_completed"])
        self.assertTrue(value["result_available"])
        self.assertTrue(value["status_complete"])
        self.assertGreaterEqual(value["supervision"]["stall"]["warning_count"], 1)
        final = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(final["run_id"], value["run_id"])
        self.assertEqual(final["terminal"]["state"], "completed")
        self.assertTrue(final["terminal"]["protocol_completed"])
        self.assertTrue(final["terminal"]["result_available"])
        self.assertIsNone(final["terminal"]["error"])
        self.assertNotIn("silent-answer", json.dumps(final))
        self.assertNotIn("SILENT-TASK-MARKER", json.dumps(final))

    def test_active_tool_summary_is_separate_from_step_completion(self):
        status = self.root / "active.status.json"
        process = self.start({"mode": "active-tool", "hold": 0.8,
                              "answer": "tool-answer",
                              "secret": "TOOL-INPUT-MARKER"}, status=status)
        active = self.wait_status(
            status, lambda d: d.get("active_tools", {}).get("active_calls") == 1
            and d.get("supervision", {}).get("stall", {}).get("suspected") is True)
        summary = active["active_tools"]
        self.assertEqual(summary["distinct_tools"], 1)
        self.assertEqual(summary["tools"][0]["tool"], "bash")
        self.assertIsNone(active["last_tool_completed_at"])
        self.assertIsNone(active["last_step_completed_at"])
        self.assertEqual(active["terminal"]["state"], "running")
        self.assertNotIn("TOOL-INPUT-MARKER", json.dumps(active))
        value = self.finish(process, expected=0)
        self.assertTrue(value["harness_completed"])
        self.assertEqual(value["tool_errors"], [])
        self.assertEqual(value["answer"], "tool-answer")
        final = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(final["active_tools"]["active_calls"], 0)
        self.assertIsNotNone(final["last_tool_completed_at"])
        self.assertIsNotNone(final["last_step_completed_at"])
        self.assertEqual(final["terminal"]["state"], "completed")
        self.assertNotIn("TOOL-INPUT-MARKER", json.dumps(final))

    def test_failure_flow_status_distinguishes_tool_error_from_failed_protocol(self):
        tool_status = self.root / "tool-error.status.json"
        tool_value = self.invoke({"mode": "tool-error-flow", "answer": "after-tool-error",
                                  "secret": "TOOL-ERROR-MARKER"},
                                 status=tool_status, expected=0)
        self.assertTrue(tool_value["harness_completed"])
        self.assertEqual(len(tool_value["tool_errors"]), 1)
        self.assertTrue(tool_value["status_complete"])
        final = json.loads(tool_status.read_text(encoding="utf-8"))
        self.assertEqual(final["terminal"]["state"], "completed")
        self.assertTrue(any(item["code"] == "TOOL_ERROR"
                            for item in final["known_errors"]))
        protocol_status = self.root / "protocol-error.status.json"
        protocol_value = self.invoke({"mode": "protocol-error"},
                                     status=protocol_status, expected=5)
        self.assertFalse(protocol_value["harness_completed"])
        self.assertTrue(protocol_value["status_complete"])
        final = json.loads(protocol_status.read_text(encoding="utf-8"))
        self.assertEqual(final["terminal"]["state"], "failed")
        self.assertFalse(final["terminal"]["protocol_completed"])
        self.assertFalse(final["terminal"]["result_available"])
        self.assertEqual(final["terminal"]["error"]["category"], "protocol")

    def test_status_channel_failure_is_not_swallowed_as_success(self):
        status = self.root / "channel.status.json"
        target = self.root / "channel-target.json"
        target.write_text("untouched", encoding="utf-8")
        process = self.start({"mode": "silent", "hold": 1.0,
                              "answer": "answer-preserved"}, status=status)
        self.wait_status(status, lambda d: d.get("phase") == "running")
        Path(status).unlink()
        Path(status).symlink_to(target)
        value = self.finish(process, expected=11)
        self.assertFalse(value["harness_completed"])
        self.assertFalse(value["status_complete"])
        self.assertIsNotNone(value["status_error"])
        self.assertEqual(value["status_error"]["category"], "io")
        self.assertEqual(value["answer"], "answer-preserved")
        self.assertEqual(target.read_text(encoding="utf-8"), "untouched")
        self.assertIsNone(value["control_error"])



class CleanupOutcomeTests(IntegrationBase):
    def test_explicit_timeout_cleans_descendants_and_reports_timed_out(self):
        status = self.root / "timeout.status.json"
        child_file = self.root / "timeout-child.pid"
        harness_file = self.root / "timeout-harness.pid"
        value = self.invoke({"mode": "timeout-child", "child_pid_file": str(child_file),
                             "harness_pid_file": str(harness_file)},
                            extra=("--timeout", "0.3"), status=status, expected=6)
        self.assertEqual(value["error"]["category"], "timeout")
        self.assertFalse(value["harness_completed"])
        self.assertFalse(value["result_available"])
        self.assertTrue(value["status_complete"])
        self.assertTrue(value["cleanup"]["reaped"])
        self.wait_dead(int(child_file.read_text()))
        self.wait_dead(int(harness_file.read_text()))
        final = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(final["terminal"]["state"], "timed_out")
        self.assertEqual(final["terminal"]["error"]["category"], "timeout")
        self.assertFalse(final["terminal"]["result_available"])

    def test_finalization_hang_preserves_answer_with_cleanup_failure(self):
        status = self.root / "finalization.status.json"
        harness_file = self.root / "finalization-harness.pid"
        value = self.invoke({"mode": "finalization-hang", "hold": 120,
                             "answer": "answer-preserved",
                             "harness_pid_file": str(harness_file)},
                            status=status, expected=13)
        self.assertTrue(value["protocol_completed"])
        self.assertTrue(value["result_available"])
        self.assertFalse(value["harness_completed"])
        self.assertFalse(value["natural_exit"])
        self.assertEqual(value["answer"], "answer-preserved")
        self.assertEqual(value["error"]["category"], "cleanup")
        self.assertEqual(value["error"]["code"], "FINALIZATION_GRACE_EXPIRED")
        self.assertTrue(value["finalization"]["timed_out"])
        self.assertTrue(value["status_complete"])
        self.wait_dead(int(harness_file.read_text()))
        final = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(final["terminal"]["state"], "cleanup_failed")
        self.assertTrue(final["terminal"]["protocol_completed"])
        self.assertTrue(final["terminal"]["result_available"])
        self.assertEqual(final["terminal"]["error"]["category"], "cleanup")
        self.assertNotIn("answer-preserved", json.dumps(final))

    def test_final_orphan_pipe_is_independent_cleanup_and_keeps_answer(self):
        status = self.root / "orphan.status.json"
        child_file = self.root / "orphan-child.pid"
        value = self.invoke({"mode": "final-orphan-pipes",
                             "answer": "orphan-answer",
                             "child_pid_file": str(child_file)},
                            status=status, expected=13)
        self.assertTrue(value["protocol_completed"])
        self.assertTrue(value["result_available"])
        self.assertFalse(value["harness_completed"])
        self.assertEqual(value["answer"], "orphan-answer")
        self.assertEqual(value["error"]["category"], "cleanup")
        self.assertEqual(value["error"]["code"], "ORPHANED_DESCENDANT_PIPES")
        self.assertTrue(value["finalization"]["timed_out"])
        self.wait_dead(int(child_file.read_text()))
        final = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(final["terminal"]["state"], "cleanup_failed")
        self.assertTrue(final["terminal"]["result_available"])

    def test_sigint_reports_interrupted_and_cleans_owned_child(self):
        status = self.root / "sigint.status.json"
        child_file = self.root / "sigint-child.pid"
        process = self.start({"mode": "cancel-wait", "child_pid_file": str(child_file),
                              "secret": "SIGINT-MARKER"}, status=status)
        self.wait_status(status, lambda d: d.get("phase") == "running"
                         and d.get("child") is not None)
        deadline = time.monotonic() + 5
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(child_file.exists())
        child_pid = int(child_file.read_text())
        process.send_signal(signal.SIGINT)
        value = self.finish(process, expected=7)
        self.assertEqual(value["error"]["category"], "interrupted")
        self.assertFalse(value["harness_completed"])
        self.assertEqual(value["cancellation"]["requested"], False)
        self.assertTrue(value["status_complete"])
        self.wait_dead(child_pid)
        final = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(final["terminal"]["state"], "interrupted")
        self.assertEqual(final["cancellation"]["requested"], False)

    def test_controlled_early_stdin_eof_does_not_hang(self):
        config = self.settings()
        config["dsh_command"] = [sys.executable, str(self.fake), "--early-eof"]
        started = time.monotonic()
        value = self.invoke({"mode": "normal"}, config=config, expected=5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(value["harness_completed"])
        self.assertIn(value["error"]["category"], {"protocol", "io"})

    def test_stdout_eof_without_final_is_a_protocol_failure_without_timeout(self):
        started = time.monotonic()
        value = self.invoke({"mode": "stdout-eof"}, expected=5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNone(value["supervision"]["hard_timeout_seconds"])
        self.assertEqual(value["error"]["category"], "protocol")
        self.assertEqual(value["error"]["code"], "STDOUT_EOF_WITHOUT_FINAL")
        self.assertTrue(value["cleanup"]["reaped"])



class CancellationAndStatusSafetyTests(IntegrationBase):
    def _write_forged_status(self, path, supervisor, run_id="run-forged",
                             heartbeat_epoch=None):
        document = {
            "schema_version": supervision.STATUS_SCHEMA_VERSION,
            "run_id": run_id,
            "supervisor": supervisor,
            "child": None,
            "session_id": "forged-session",
            "phase": "running",
            "heartbeat_epoch": time.time() if heartbeat_epoch is None else heartbeat_epoch,
            "stale_after_seconds": 30.0,
            "supervision": {"heartbeat_seconds": 0.1},
            "terminal": {"state": "running"},
        }
        supervision.StatusPublisher(path, document)
        return document

    def test_cancel_roundtrip_is_run_id_bound_and_cleans_children(self):
        status = self.root / "cancel.status.json"
        child_file = self.root / "cancel-child.pid"
        process = self.start({"mode": "cancel-wait", "child_pid_file": str(child_file),
                              "secret": "CANCEL-MARKER"}, status=status)
        running = self.wait_status(status, lambda d: d.get("phase") == "running"
                                   and d.get("child") is not None)
        run_id = running["run_id"]
        deadline = time.monotonic() + 5
        while not child_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(child_file.exists())
        child_pid = int(child_file.read_text())
        code, managed = self.manage("status", "--status-file", str(status))
        self.assertEqual(code, 0)
        self.assertEqual(managed["state"], "RUNNING")
        self.assertEqual(managed["run_id"], run_id)
        self.assertTrue(managed["identity_verified"])
        code, cancelled = self.manage("cancel", "--status-file", str(status),
                                      "--reason", "user_cancelled")
        self.assertEqual(code, 0)
        self.assertTrue(cancelled["control_written"])
        self.assertEqual(cancelled["run_id"], run_id)
        self.assertEqual(cancelled["execution"], "wrapper_owned")
        value = self.finish(process, expected=12)
        self.assertEqual(value["error"]["category"], "cancelled")
        self.assertEqual(value["error"]["code"], "USER_CANCELLED")
        self.assertTrue(value["cancellation"]["requested"])
        self.assertTrue(value["cancellation"]["acted"])
        self.assertEqual(value["cancellation"]["reason"], "user_cancelled")
        self.assertFalse(value["harness_completed"])
        self.assertTrue(value["status_complete"])
        self.wait_dead(child_pid)
        self.assertFalse(supervision.control_path(status).exists())
        final = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(final["terminal"]["state"], "cancelled")
        self.assertEqual(final["terminal"]["error"]["category"], "cancelled")
        self.assertEqual(final["cancellation"]["reason"], "user_cancelled")

    def test_cancel_refuses_completed_run_and_pid_reuse(self):
        status = self.root / "terminal.status.json"
        self.invoke({"mode": "normal"}, status=status, expected=0)
        code, cancelled = self.manage("cancel", "--status-file", str(status),
                                      "--reason", "user_cancelled")
        self.assertEqual(code, 4)
        self.assertFalse(cancelled["control_written"])
        self.assertEqual(cancelled["state"], "COMPLETED")
        self.assertFalse(supervision.control_path(status).exists())
        forged = self.root / "reused.status.json"
        identity = supervision.process_identity(os.getpid())
        forged_identity = dict(identity)
        forged_identity["start_time_ticks"] = int(identity["start_time_ticks"]) + 1
        self._write_forged_status(forged, forged_identity)
        code, managed = self.manage("status", "--status-file", str(forged))
        self.assertEqual(code, 0)
        self.assertEqual(managed["state"], "UNKNOWN")
        self.assertEqual(managed["reason"], "PID_REUSE_DETECTED")
        code, cancelled = self.manage("cancel", "--status-file", str(forged),
                                      "--reason", "user_cancelled")
        self.assertEqual(code, 4)
        self.assertFalse(cancelled["control_written"])
        self.assertFalse(supervision.control_path(forged).exists())

    def test_cancel_writes_request_but_never_kills_a_stale_or_unrelated_pid(self):
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()) if sleeper.poll() is None else None)
        identity = supervision.process_identity(sleeper.pid)
        self.assertIsNotNone(identity)
        live_status = self.root / "unrelated-live.status.json"
        self._write_forged_status(live_status, identity)
        code, cancelled = self.manage("cancel", "--status-file", str(live_status),
                                      "--reason", "user_cancelled")
        self.assertEqual(code, 0)
        self.assertTrue(cancelled["control_written"])
        time.sleep(0.1)
        self.assertTrue(live_process(sleeper.pid),
                        "manage_dsh must not signal a PID from a status file")
        sleeper.terminate()
        sleeper.wait(timeout=5)
        dead_identity = supervision.process_identity(sleeper.pid)
        self.assertTrue(dead_identity is None or not dead_identity["alive"])
        dead_status = self.root / "stale.status.json"
        self._write_forged_status(dead_status, identity)
        code, managed = self.manage("status", "--status-file", str(dead_status))
        self.assertEqual(code, 0)
        self.assertEqual(managed["state"], "UNKNOWN")
        self.assertEqual(managed["reason"], "PROCESS_NOT_RUNNING")
        code, cancelled = self.manage("cancel", "--status-file", str(dead_status),
                                      "--reason", "user_cancelled")
        self.assertEqual(code, 4)
        self.assertFalse(cancelled["control_written"])
        self.assertFalse(supervision.control_path(dead_status).exists())

    def test_status_path_must_be_absolute_and_have_an_existing_parent(self):
        value = self.invoke({"mode": "normal"},
                            extra=("--status-file", "relative.status.json"), expected=2)
        self.assertEqual(value["error"]["code"], "STATUS_PATH_INVALID")
        missing = self.root / "missing-parent" / "status.json"
        value = self.invoke({"mode": "normal"},
                            extra=("--status-file", str(missing)), expected=2)
        self.assertEqual(value["error"]["code"], "STATUS_PARENT_INVALID")

    def test_status_symlink_and_existing_path_are_refused(self):
        target = self.root / "symlink-target.json"
        target.write_text("untouched", encoding="utf-8")
        link = self.root / "symlink.status.json"
        link.symlink_to(target)
        code, managed = self.manage("status", "--status-file", str(link))
        self.assertEqual(code, 3)
        self.assertEqual(managed["state"], "UNKNOWN")
        self.assertEqual(managed["reason"], "STATUS_SYMLINK_REFUSED")
        code, cancelled = self.manage("cancel", "--status-file", str(link),
                                      "--reason", "user_cancelled")
        self.assertEqual(code, 3)
        self.assertFalse(cancelled["control_written"])
        self.assertEqual(target.read_text(encoding="utf-8"), "untouched")
        value = self.finish(self.start({"mode": "normal"}, status=link), expected=2)
        self.assertEqual(value["error"]["code"], "STATUS_FILE_EXISTS")
        self.assertEqual(target.read_text(encoding="utf-8"), "untouched")
        existing = self.root / "existing.status.json"
        existing.write_text("{}", encoding="utf-8")
        value = self.invoke({"mode": "normal"}, status=existing, expected=2)
        self.assertEqual(value["error"]["code"], "STATUS_FILE_EXISTS")

    def test_duplicate_status_owner_cannot_start_a_second_task(self):
        status = self.root / "exclusive.status.json"
        counter = self.root / "start-counter.txt"
        first = self.start({"mode": "silent", "hold": 1.2,
                            "answer": "first-answer",
                            "start_counter": str(counter)}, status=status)
        self.wait_status(status, lambda d: d.get("phase") == "running")
        deadline = time.monotonic() + 5
        while (not counter.exists()) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(counter.exists())
        self.assertEqual(len(counter.read_text().splitlines()), 1)
        second = self.invoke({"mode": "normal", "start_counter": str(counter)},
                             status=status, expected=2)
        self.assertEqual(second["error"]["code"], "STATUS_FILE_EXISTS")
        self.assertEqual(len(counter.read_text().splitlines()), 1)
        code, cancelled = self.manage("cancel", "--status-file", str(status),
                                      "--reason", "user_cancelled")
        self.assertEqual(code, 0)
        value = self.finish(first, expected=12)
        self.assertFalse(value["harness_completed"])
        self.assertEqual(len(counter.read_text().splitlines()), 1)

    def test_missing_or_stale_status_is_unknown_without_starting_work(self):
        missing = self.root / "missing.status.json"
        code, managed = self.manage("status", "--status-file", str(missing))
        self.assertEqual(code, 3)
        self.assertEqual(managed["state"], "UNKNOWN")
        self.assertEqual(managed["reason"], "STATUS_NOT_FOUND")
        stale = self.root / "stale-heartbeat.status.json"
        identity = supervision.process_identity(os.getpid())
        self._write_forged_status(stale, identity, heartbeat_epoch=time.time() - 100)
        code, managed = self.manage("status", "--status-file", str(stale))
        self.assertEqual(code, 0)
        self.assertEqual(managed["state"], "UNKNOWN")
        self.assertEqual(managed["reason"], "HEARTBEAT_EXPIRED")
        code, cancelled = self.manage("cancel", "--status-file", str(stale),
                                      "--reason", "user_cancelled")
        self.assertEqual(code, 4)
        self.assertFalse(cancelled["control_written"])
        self.assertFalse(supervision.control_path(stale).exists())



class LogRetentionTests(IntegrationBase):
    def test_log_budget_rotates_and_degrades_without_killing_task(self):
        status = self.root / "logging.status.json"
        value = self.invoke({"mode": "flood-text", "answer": "log-answer",
                             "secret": "LOG-RAW-MARKER"},
                            status=status, config=self.settings(max_log_bytes=1200),
                            expected=0)
        self.assertTrue(value["harness_completed"])
        self.assertEqual(value["answer"], "log-answer")
        self.assertFalse(value["log_complete"])
        self.assertTrue(value["log_degraded"])
        self.assertTrue(value["log_truncated"])
        self.assertTrue(value["status_complete"])
        log_dir = Path(value["log_dir"])
        names = sorted(path.name for path in log_dir.iterdir())
        self.assertIn("events.previous.ndjson", names)
        self.assertIn("summary.json", names)
        self.assertIn("LOG_TRUNCATED", value["log_diagnostics"])
        for path in log_dir.rglob("*"):
            if path.is_file():
                self.assertEqual(path.stat().st_mode & 0o077, 0, str(path))
        self.assertEqual(log_dir.stat().st_mode & 0o077, 0)
        final = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(final["terminal"]["state"], "completed")
        self.assertTrue(final["terminal"]["result_available"])
        self.assertGreater(final["logs"]["rotations"], 0)

    def test_logs_and_status_hold_no_raw_task_tool_reasoning_or_answer(self):
        status = self.root / "safe.status.json"
        markers = ("TASK-RAW-MARKER", "TOOL-INPUT-MARKER", "THINKING-RAW-MARKER",
                   "ANSWER-RAW-MARKER")
        value = self.invoke({"mode": "active-tool", "hold": 0.2,
                             "secret": "TOOL-INPUT-MARKER",
                             "answer": "ANSWER-RAW-MARKER"},
                            status=status, expected=0)
        self.assertEqual(value["answer"], "ANSWER-RAW-MARKER")
        self.assertTrue(value["harness_completed"])
        log_dir = Path(value["log_dir"])
        blobs = [status.read_bytes()] + [path.read_bytes()
                                          for path in log_dir.rglob("*") if path.is_file()]
        combined = b"\n".join(blobs)
        for marker in markers:
            with self.subTest(marker=marker):
                self.assertNotIn(marker.encode("ascii"), combined)
        result_blob = (log_dir / "result.json").read_bytes()
        self.assertIn(b'"answer_bytes_returned"', result_blob)
        self.assertNotIn(b'"answer":', result_blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
