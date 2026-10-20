#!/usr/bin/env python3
"""No-model doctor tests.

The default lane is fully simulated and needs no installed dsh, Node package,
credential, or private bridge file.  Optional official-loader checks are enabled
with --official-config PATH or CODEX_DSH_OFFICIAL_CONFIG; those checks only read
synthetic temporary .env files and never send a model request.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doctor  # noqa: E402


OFFICIAL_CONFIG = os.environ.get("CODEX_DSH_OFFICIAL_CONFIG")


class SupervisionPolicyTests(unittest.TestCase):
    """Pure policy validation, independent of any installed runtime."""

    def test_schema1_keeps_legacy_hard_timeout(self):
        result = doctor.supervision_settings({"schema_version": 1, "timeout_seconds": 5})
        self.assertEqual(result["hard_timeout_seconds"], 5)
        self.assertEqual(result["hard_timeout_source"], "legacy_timeout_seconds")
        self.assertEqual(result["capability_boundaries"]["hard_deadline"],
                         "configured_hard_budget_terminates_owned_processes")

    def test_schema2_absent_hard_timeout_has_no_implicit_deadline(self):
        result = doctor.supervision_settings({"schema_version": 2})
        self.assertIsNone(result["hard_timeout_seconds"])
        self.assertEqual(result["hard_timeout_source"], "no_implicit_hard_deadline")
        self.assertEqual(result["capability_boundaries"]["hard_deadline"],
                         "not_configured_no_implicit_wall_clock_deadline")

    def test_schema2_explicit_null_hard_timeout_is_valid(self):
        result = doctor.supervision_settings({"schema_version": 2,
                                              "hard_timeout_seconds": None})
        self.assertIsNone(result["hard_timeout_seconds"])
        self.assertEqual(result["hard_timeout_source"], "no_implicit_hard_deadline")

    def test_schema2_legacy_timeout_seconds_is_inactive(self):
        result = doctor.supervision_settings({"schema_version": 2,
                                              "timeout_seconds": "not-used"})
        self.assertIsNone(result["hard_timeout_seconds"])
        self.assertEqual(result["hard_timeout_source"], "no_implicit_hard_deadline")

    def test_schema2_explicit_hard_timeout(self):
        result = doctor.supervision_settings({"schema_version": 2,
                                              "hard_timeout_seconds": 0.5})
        self.assertEqual(result["hard_timeout_seconds"], 0.5)
        self.assertEqual(result["hard_timeout_source"], "hard_timeout_seconds")

    def test_documented_supervision_defaults(self):
        result = doctor.supervision_settings({"schema_version": 2})
        self.assertEqual(result["heartbeat_seconds"], 5)
        self.assertEqual(result["stall_warning_seconds"], 300)
        self.assertEqual(result["shutdown_grace_seconds"], 10)
        self.assertEqual(result["finalization_grace_seconds"], 10)
        self.assertTrue(all(source == "default" for source in result["sources"].values()))

    def test_finite_positive_supervision_overrides(self):
        config = {"schema_version": 2, "heartbeat_seconds": 0.05,
                  "stall_warning_seconds": 0.2, "shutdown_grace_seconds": 0.3,
                  "finalization_grace_seconds": 0.4}
        result = doctor.supervision_settings(config)
        for key, expected in (("heartbeat_seconds", 0.05),
                              ("stall_warning_seconds", 0.2),
                              ("shutdown_grace_seconds", 0.3),
                              ("finalization_grace_seconds", 0.4)):
            self.assertEqual(result[key], expected)
            self.assertEqual(result["sources"][key], "config")

    def test_capability_boundaries_are_explicit_and_not_runtime_claims(self):
        result = doctor.supervision_settings({"schema_version": 2})
        boundaries = result["capability_boundaries"]
        self.assertIn("never_terminates", boundaries["soft_stall_warning"])
        self.assertIn("not_event_or_tool_progress", boundaries["heartbeat"])
        self.assertIn("run_id", boundaries["cancel"])
        self.assertEqual(boundaries["monitoring_loss"], "never_starts_a_second_task")
        self.assertEqual(result["runtime_verification"], "NOT_RUN")
        self.assertEqual(result["status_file_acceptance"], "NOT_RUN")
        self.assertEqual(result["cancel_acceptance"], "NOT_RUN")

    def assert_invalid(self, config, code):
        with self.assertRaisesRegex(doctor.CheckError, "^" + code + "$"):
            doctor.supervision_settings(config)

    def test_schema1_invalid_timeouts(self):
        for value in (0, -1, True, False, math.inf, math.nan, "5", None, 10 ** 1000):
            with self.subTest(value=value):
                self.assert_invalid({"schema_version": 1, "timeout_seconds": value},
                                    "invalid_timeout_seconds")

    def test_schema2_invalid_hard_timeouts(self):
        for value in (0, -1, True, False, math.inf, math.nan, "5", 10 ** 1000):
            with self.subTest(value=value):
                self.assert_invalid({"schema_version": 2,
                                     "hard_timeout_seconds": value},
                                    "invalid_hard_timeout_seconds")

    def test_invalid_supervision_numbers(self):
        for key in doctor.SUPERVISION_DEFAULTS:
            for value in (0, -1, True, math.inf, math.nan, "5", 10 ** 1000):
                with self.subTest(key=key, value=value):
                    self.assert_invalid({"schema_version": 2, key: value},
                                        "invalid_" + key)

    def test_unsupported_schema_type_and_version(self):
        for schema in (0, 3, True, "2", None):
            with self.subTest(schema=schema):
                self.assert_invalid({"schema_version": schema}, "unsupported_config_schema")


class DoctorReportTests(unittest.TestCase):
    """run_doctor report behavior with all external probes replaced."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-dsh-doctor-report-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.cwd = self.base / "workspace"
        self.home = self.base / "home"
        self.logs = self.base / "logs"
        for path in (self.cwd, self.home, self.logs,
                     self.home / "profiles" / "default"):
            path.mkdir(parents=True, exist_ok=True)
        self.fake_dsh = self.base / "fake-dsh"
        self.fake_dsh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.fake_dsh.chmod(0o700)
        self.config_path = self.base / "bridge.json"

    def config(self, schema_version):
        value = {"schema_version": schema_version,
                 "dsh_command": [sys.executable, str(self.fake_dsh)],
                 "dsh_home": str(self.home), "default_profile": "default",
                 "default_patches": [], "log_dir": str(self.logs)}
        if schema_version == 1:
            value["timeout_seconds"] = 5
        return value

    def write_config(self, value):
        self.config_path.write_text(json.dumps(value), encoding="utf-8")

    def fake_probe(self, action):
        if action == "compose":
            return {"status": "PASS", "code": "composition_completed",
                    "model": {"provider": "provider", "model": "model",
                              "reasoningEffort": "max"},
                    "sandbox_provider_present": True,
                    "sandbox_custom_runner": False}
        if action == "environment":
            return {"status": "PASS", "code": "layered_environment_loaded",
                    "credential": {"present": True, "source": "launch_environment",
                                   "empty_value_shadows_fallback": False,
                                   "authentication_verified": False},
                    "dotenv_warning_count": 0,
                    "proxy": {"inputs": [], "rejected_value_count": 0,
                              "raw_values_reported": False},
                    "network": {"status": "NOT_RUN",
                                "code": "use_network_flag_for_head_request"}}
        if action == "sandbox":
            return {"status": "PASS", "code": "actual_confined_read_and_write_denial",
                    "runner": "fake", "enforcement": "test", "process_exit_code": 0,
                    "actual_command_executed": True,
                    "file_write_denial_verified": True, "policy": "read-only"}
        raise AssertionError("unexpected probe action")

    def bounded_run(self, command, *, cwd, env, timeout=30, limit=doctor.MAX_OUTPUT):
        if "--version" in command:
            return 0, b"v1.2.3\n"
        payload = json.loads(command[-1])
        return 0, json.dumps(self.fake_probe(payload["action"])).encode()

    def run_report(self, schema_version):
        self.write_config(self.config(schema_version))
        args = SimpleNamespace(config=str(self.config_path), cwd=str(self.cwd),
                               profile=None, patch=[], network=False)
        with mock.patch.object(doctor, "installation_root", return_value=self.base), \
                mock.patch.object(doctor.shutil, "which", return_value=sys.executable), \
                mock.patch.object(doctor, "bounded_run", side_effect=self.bounded_run):
            return doctor.run_doctor(args)

    def test_schema2_null_deadline_is_reported_not_rejected(self):
        report = self.run_report(2)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual(report["wrapper_exit_code"], 3)
        self.assertNotIn("invalid_timeout_seconds", report.get("failures", []))
        self.assertEqual(report["config_schema_version"], 2)
        supervision = report["supervision"]
        self.assertIsNone(supervision["hard_timeout_seconds"])
        self.assertEqual(supervision["hard_timeout_source"],
                         "no_implicit_hard_deadline")
        self.assertEqual(supervision["heartbeat_seconds"], 5)
        self.assertEqual(supervision["stall_warning_seconds"], 300)
        self.assertEqual(supervision["shutdown_grace_seconds"], 10)
        self.assertEqual(supervision["finalization_grace_seconds"], 10)
        self.assertIn("no implicit wall-clock deadline", " ".join(report["notes"]))
        self.assertEqual(report["checks"]["sandbox"]["status"], "PASS")
        self.assertTrue(report["checks"]["sandbox"]["actual_command_executed"])
        self.assertEqual(report["checks"]["environment"]["credential"]["present"], True)
        self.assertFalse(report["checks"]["environment"]["proxy"]["raw_values_reported"])

    def test_schema1_legacy_deadline_is_reported(self):
        report = self.run_report(1)
        supervision = report["supervision"]
        self.assertEqual(supervision["hard_timeout_seconds"], 5)
        self.assertEqual(supervision["hard_timeout_source"], "legacy_timeout_seconds")

    def test_schema2_custom_supervision_is_reported(self):
        value = self.config(2)
        value.update({"heartbeat_seconds": 0.1, "stall_warning_seconds": 0.2,
                      "shutdown_grace_seconds": 0.3, "finalization_grace_seconds": 0.4})
        self.write_config(value)
        args = SimpleNamespace(config=str(self.config_path), cwd=str(self.cwd),
                               profile=None, patch=[], network=False)
        with mock.patch.object(doctor, "installation_root", return_value=self.base), \
                mock.patch.object(doctor.shutil, "which", return_value=sys.executable), \
                mock.patch.object(doctor, "bounded_run", side_effect=self.bounded_run):
            report = doctor.run_doctor(args)
        supervision = report["supervision"]
        self.assertEqual(supervision["heartbeat_seconds"], 0.1)
        self.assertEqual(supervision["stall_warning_seconds"], 0.2)
        self.assertEqual(supervision["shutdown_grace_seconds"], 0.3)
        self.assertEqual(supervision["finalization_grace_seconds"], 0.4)

    def test_unsupported_schema_is_rejected_before_probes(self):
        self.write_config(self.config(3))
        args = SimpleNamespace(config=str(self.config_path), cwd=str(self.cwd),
                               profile=None, patch=[], network=False)
        with mock.patch.object(doctor, "bounded_run") as bounded:
            with self.assertRaisesRegex(doctor.CheckError, "^unsupported_config_schema$"):
                doctor.run_doctor(args)
            bounded.assert_not_called()


@unittest.skipUnless(OFFICIAL_CONFIG, "pass --official-config or set CODEX_DSH_OFFICIAL_CONFIG")
class OfficialEnvironmentLoaderTests(unittest.TestCase):
    """Real installed layer loader; no dsh task and no model request."""

    @classmethod
    def setUpClass(cls):
        cls.bridge = Path(OFFICIAL_CONFIG)
        cls.config = json.loads(cls.bridge.read_text(encoding="utf-8"))
        cls.root = doctor.installation_root(cls.config["dsh_command"])
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("Node executable unavailable for official loader checks")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-dsh-doctor-env-")
        self.base = Path(self.temporary.name)
        self.cwd = self.base / "workspace"
        self.home = self.base / "home"
        self.cwd.mkdir()
        self.home.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def environment(self, process_value, project_value, user_value):
        for path, value in ((self.cwd / ".env", project_value),
                            (self.home / ".env", user_value)):
            if value is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text("DEEPSEEK_API_KEY=" + value + "\n", encoding="utf-8")
        env = os.environ.copy()
        env["DSH_HOME"] = str(self.home)
        if process_value is None:
            env.pop("DEEPSEEK_API_KEY", None)
        else:
            env["DEEPSEEK_API_KEY"] = process_value
        payload = {"action": "environment", "root": str(self.root),
                   "cwd": str(self.cwd), "network": False}
        code, output = doctor.bounded_run(
            [self.node, "--input-type=module", "-e", doctor.NODE_PROBE,
             json.dumps(payload)], cwd=self.cwd, env=env)
        self.assertEqual(code, 0)
        for marker in (b"synthetic-home-marker", b"synthetic-project-marker",
                       b"synthetic-process-marker"):
            self.assertNotIn(marker, output)
        report = json.loads(output)
        self.assertEqual(report["network"]["status"], "NOT_RUN")
        self.assertFalse(report["proxy"]["raw_values_reported"])
        self.assertTrue(all(isinstance(item["nonempty"], bool)
                            for item in report["proxy"]["inputs"]))
        return report["credential"]

    def assert_credential(self, actual, source, present, shadow=False):
        self.assertEqual(actual, {"source": source, "present": present,
                                  "empty_value_shadows_fallback": shadow,
                                  "authentication_verified": False})

    def test_launch_environment_wins(self):
        self.assert_credential(self.environment("synthetic-process-marker",
                                                "synthetic-project-marker",
                                                "synthetic-home-marker"),
                               "launch_environment", True)

    def test_project_environment_wins_over_home(self):
        self.assert_credential(self.environment(None, "synthetic-project-marker",
                                                "synthetic-home-marker"),
                               "cwd_dotenv", True)

    def test_home_environment_fallback(self):
        self.assert_credential(self.environment(None, None, "synthetic-home-marker"),
                               "dsh_home_dotenv", True)

    def test_empty_export_shadows_dotenv(self):
        self.assert_credential(self.environment("", "synthetic-project-marker",
                                                "synthetic-home-marker"),
                               "launch_environment", False, True)

    def test_empty_project_shadows_home(self):
        self.assert_credential(self.environment(None, "", "synthetic-home-marker"),
                               "cwd_dotenv", False, True)

    def test_missing_credential(self):
        self.assert_credential(self.environment(None, None, None), "missing", False)

    def test_dotenv_launch_control_rejected_without_raw_error(self):
        (self.cwd / ".env").write_text("DSH_HOME=synthetic-rejected-control\n",
                                       encoding="utf-8")
        env = os.environ.copy()
        env["DSH_HOME"] = str(self.home)
        payload = {"action": "environment", "root": str(self.root),
                   "cwd": str(self.cwd), "network": False}
        code, output = doctor.bounded_run(
            [self.node, "--input-type=module", "-e", doctor.NODE_PROBE,
             json.dumps(payload)], cwd=self.cwd, env=env)
        self.assertEqual(code, 0)
        self.assertNotIn(b"synthetic-rejected-control", output)
        self.assertEqual(json.loads(output), {"status": "FAIL",
                                               "code": "layered_environment_rejected_or_unavailable"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--official-config")
    options, remaining = parser.parse_known_args()
    if options.official_config:
        OFFICIAL_CONFIG = options.official_config
        OfficialEnvironmentLoaderTests.__unittest_skip__ = False
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
