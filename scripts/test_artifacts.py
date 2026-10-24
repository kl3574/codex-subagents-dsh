"""Artifact lifecycle checks through public CLIs; no model or real user home."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from test_run_dsh import FAKE_DSH  # noqa: E402


class ArtifactCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-dsh-artifacts-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cwd = self.root / "workspace with spaces"
        self.cwd.mkdir()
        self.home = self.root / "harness-home"
        profile = self.home / "profiles" / "default"
        profile.mkdir(parents=True)
        (profile / "cordis.patch.yml").write_text("[]\n")
        self.invoked = self.root / "model-was-invoked"
        fake = self.root / "fake_dsh.py"
        fake.write_text(
            f"from pathlib import Path\nPath({str(self.invoked)!r}).touch()\n" + FAKE_DSH
        )
        self.config = self.root / "bridge.json"
        self.config.write_text(json.dumps({
            "schema_version": 2,
            "dsh_command": [sys.executable, str(fake)],
            "dsh_home": str(self.home),
            "default_profile": "default",
            "default_patches": [],
            "hard_timeout_seconds": None,
            "log_dir": str(self.root / "legacy-logs"),
        }))

    def manage(self, *args, expected=0):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "manage_dsh.py"), *args],
            capture_output=True, text=True, timeout=15,
        )
        value = json.loads(result.stdout)
        self.assertEqual(result.returncode, expected, value)
        return value

    def prepare(self):
        return self.manage("prepare", "--config", str(self.config), "--cwd", str(self.cwd))

    def run_task(self, run, task=None, *extra, expected=0):
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "run_dsh.py"), "--config", str(self.config),
             "--cwd", str(self.cwd), "--task-file", "-", "--run-dir", run["run_dir"], *extra],
            input=json.dumps(task or {"mode": "capture"}), text=True,
            capture_output=True, timeout=15,
        )
        value = json.loads(result.stdout)
        self.assertEqual(result.returncode, expected, value)
        return value

    def test_execution_uses_owned_identity_and_leaves_acceptance_pending(self):
        run = self.prepare()
        scratch = Path(run["scratch_dir"]) / "input.txt"
        scratch.write_text("recovery input")
        result = self.run_task(run)
        self.assertTrue(result["harness_completed"])
        self.assertEqual(result["run_id"], run["run_id"])
        self.assertEqual(result["artifacts"]["state"], "pending_acceptance")
        self.assertEqual(result["artifacts"]["acceptance"], "pending")
        self.assertTrue(Path(result["log_dir"]).is_relative_to(Path(run["diagnostics_dir"])))
        answer = json.loads(result["answer"])
        self.assertEqual(answer["cwd"], str(self.cwd))
        self.assertEqual(answer["task"], {"mode": "capture"})
        self.assertEqual(scratch.read_text(), "recovery input")
        manifest = json.loads((Path(run["run_dir"]) / "run.json").read_text())
        self.assertEqual(manifest["state"], "pending_acceptance")
        self.assertIsNone(manifest["closeout"])
        again = self.run_task(run, expected=2)
        self.assertEqual(again["error"]["code"], "ARTIFACT_RUN_NOT_PREPARED")

    def finalize(self, run, *extra, expected=0):
        return self.manage("finalize", "--run-dir", run["run_dir"],
                           "--disposition", "accepted", "--delivered", *extra, expected=expected)

    def test_closeout_requires_delivery_and_only_removes_scratch(self):
        run = self.prepare()
        scratch = Path(run["scratch_dir"]) / "nested"
        scratch.mkdir()
        (scratch / "input.txt").write_text("temporary")
        evidence = Path(run["evidence_dir"]) / "result.txt"
        evidence.write_text("delivered")
        borrowed = self.cwd / "borrowed.txt"
        borrowed.write_text("keep")
        result = self.run_task(run)
        session_files = list((self.home / ".codex-dsh-wrapper" / "sessions").iterdir())
        before = {p: p.read_bytes() for p in session_files}
        refused = self.manage("finalize", "--run-dir", run["run_dir"],
                             "--disposition", "accepted", expected=2)
        self.assertEqual(refused["reason"], "ARTIFACT_DELIVERY_REQUIRED")
        preview = self.finalize(run, "--dry-run")
        self.assertTrue(scratch.exists())
        closed = self.finalize(run, "--expected-plan", preview["plan_token"])
        self.assertEqual(closed["artifact_state"], "closed")
        self.assertEqual(closed["closeout"]["disposition"], "accepted")
        self.assertEqual(closed["closeout"]["cleanup"]["status"], "complete")
        self.assertEqual(list(Path(run["scratch_dir"]).iterdir()), [])
        self.assertEqual(evidence.read_text(), "delivered")
        self.assertEqual(borrowed.read_text(), "keep")
        self.assertTrue(Path(result["log_dir"]).is_dir())
        self.assertEqual(before, {p: p.read_bytes() for p in session_files})
        repeated = self.finalize(run)
        self.assertEqual(repeated["closeout"], closed["closeout"])
        self.assertLess((Path(run["run_dir"]) / "run.json").stat().st_size, 8192)

    def test_abandon_prepared_is_repeatable_and_never_invokes_model(self):
        run = self.prepare()
        (Path(run["scratch_dir"]) / "unused-task").write_text("temporary")
        args = ("finalize", "--run-dir", run["run_dir"], "--disposition", "abandoned")
        first = self.manage(*args)
        again = self.manage(*args)
        self.assertEqual(first["closeout"], again["closeout"])
        self.assertFalse(self.invoked.exists())

    def test_changed_preview_and_links_preserve_everything(self):
        run = self.prepare()
        path = Path(run["scratch_dir"]) / "temporary"
        path.write_text("v1")
        self.run_task(run)
        preview = self.finalize(run, "--dry-run")
        path.write_text("v2")
        result = self.finalize(run, "--expected-plan", preview["plan_token"], expected=2)
        self.assertEqual(result["reason"], "ARTIFACT_PLAN_CHANGED")
        target = self.cwd / "protected"
        target.write_text("keep")
        link = path.parent / "link"
        link.symlink_to(target)
        result = self.finalize(run, expected=2)
        self.assertEqual(result["reason"], "ARTIFACT_ENTRY_UNSAFE")
        link.unlink()
        os.link(target, link)
        result = self.finalize(run, expected=2)
        self.assertEqual(result["reason"], "ARTIFACT_ENTRY_UNSAFE")
        self.assertEqual(target.read_text(), "keep")
        self.assertEqual(path.read_text(), "v2")
        link.unlink()
        moved = path.parent.with_name("original-scratch")
        path.parent.rename(moved)
        path.parent.mkdir()
        (path.parent / "replacement").write_text("keep")
        result = self.finalize(run, expected=2)
        self.assertEqual(result["reason"], "ARTIFACT_TARGET_CHANGED")
        self.assertTrue((moved / "temporary").is_file())
        self.assertTrue((path.parent / "replacement").is_file())

    def test_failed_and_timed_out_runs_preserve_recovery_until_abandoned(self):
        for task, extra, code in [({"mode": "failed-turn"}, [], 8),
                                 ({"mode": "sleep-child", "pid_file": str(self.root / "pid")},
                                  ["--timeout", "0.15"], 6)]:
            with self.subTest(task=task):
                run = self.prepare()
                recovery = Path(run["scratch_dir"]) / "recovery"
                recovery.write_text("keep")
                result = self.run_task(run, task, *extra, expected=code)
                self.assertFalse(result["harness_completed"])
                self.assertEqual(result["artifacts"]["state"], "pending_acceptance")
                self.assertTrue(recovery.exists())
                closed = self.manage("finalize", "--run-dir", run["run_dir"],
                                     "--disposition", "abandoned")
                self.assertEqual(closed["closeout"]["disposition"], "abandoned")
                self.assertFalse(recovery.exists())

    def test_active_cancel_and_unknown_are_not_automatically_closed(self):
        run = self.prepare()
        recovery = Path(run["scratch_dir"]) / "recovery"
        recovery.write_text("keep")
        process = subprocess.Popen(
            [sys.executable, str(SCRIPTS / "run_dsh.py"), "--config", str(self.config),
             "--cwd", str(self.cwd), "--task-file", "-", "--run-dir", run["run_dir"]],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        process.stdin.write(json.dumps({"mode": "sleep-child", "pid_file": str(self.root / "pid")}))
        process.stdin.close()
        deadline = time.monotonic() + 5
        while not (self.root / "pid").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue((self.root / "pid").exists())
        result = self.finalize(run, expected=2)
        self.assertEqual(result["reason"], "ARTIFACT_RUN_BUSY")
        self.manage("cancel", "--status-file", run["status_file"])
        process.wait(timeout=10)
        output = json.loads(process.stdout.read())
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(process.returncode, 12, output)
        self.assertEqual(output["artifacts"]["state"], "pending_acceptance")
        self.assertTrue(recovery.is_file())
        manifest_path = Path(run["run_dir"]) / "run.json"
        doc = json.loads(manifest_path.read_text())
        doc["state"] = "running"  # Simulate crash before the durable terminal record.
        manifest_path.write_text(json.dumps(doc))
        refused = self.finalize(run, expected=2)
        self.assertEqual(refused["reason"], "ARTIFACT_RUN_UNKNOWN")
        self.assertTrue(recovery.is_file())

    def test_partial_filesystem_failure_is_retryable_without_changing_execution(self):
        run = self.prepare()
        scratch = Path(run["scratch_dir"])
        (scratch / "retry").write_text("retry")
        self.run_task(run)
        before = json.loads((Path(run["run_dir"]) / "run.json").read_text())["execution"]
        code = """import os, sys
from unittest.mock import patch
import manage_dsh
original = os.unlink
def failing(path, *args, **kwargs):
    if path == 'retry':
        raise PermissionError('controlled filesystem failure')
    return original(path, *args, **kwargs)
with patch('os.unlink', failing):
    sys.exit(manage_dsh.main(sys.argv[1:]))
"""
        result = subprocess.run([sys.executable, "-c", code, "finalize", "--run-dir", run["run_dir"],
                                 "--disposition", "accepted", "--delivered"], cwd=SCRIPTS,
                                capture_output=True, text=True, timeout=10)
        value = json.loads(result.stdout)
        self.assertEqual(result.returncode, 6, value)
        self.assertEqual(value["artifact_state"], "closeout_pending")
        self.assertIsNone(value["closeout"]["closed_at"])
        self.assertEqual(value["closeout"]["cleanup"]["status"], "partial")
        closed = self.finalize(run)
        self.assertEqual(closed["artifact_state"], "closed")
        self.assertEqual(json.loads((Path(run["run_dir"]) / "run.json").read_text())["execution"], before)

    def test_prepare_allocates_private_unique_owned_storage_without_model(self):
        first, second = self.prepare(), self.prepare()
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["run_dir"], second["run_dir"])
        for value in (first, second):
            directory = Path(value["run_dir"])
            self.assertTrue(directory.is_relative_to(self.home))
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(value["artifact_state"], "prepared")
            for field in ("scratch_dir", "evidence_dir", "diagnostics_dir"):
                path = Path(value[field])
                self.assertTrue(path.is_dir())
                self.assertEqual(path.parent, directory)
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.invoked.exists())
        self.assertEqual(list(self.cwd.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
