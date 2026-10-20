#!/usr/bin/env python3
"""Isolated installer tests: temporary HOME/dest/config and fake dsh only.

These tests never read the real user's ~/.codex or ~/.dsh-codex, never call a
model, and never install an npm package. Run with:

    python3 scripts/test_install.py -v
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = Path(__file__).resolve().parent.parent
INSTALLER = Path(__file__).with_name("install.py")
FAKE_NODE = r'''#!/usr/bin/env python3
import runpy
import sys
from pathlib import Path

args = sys.argv[1:]
version_file = Path(__file__).with_name("node-version.txt")
if args == ["--version"]:
    print(version_file.read_text(encoding="utf-8").strip() if version_file.exists() else "v22.19.0")
    raise SystemExit(0)
if not args:
    raise SystemExit(2)
entry = args[0]
sys.argv = [entry] + args[1:]
runpy.run_path(entry, run_name="__main__")
'''

FAKE_DSH = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("0.2.1-test")
    raise SystemExit(0)
if Path(__file__).with_name("FAIL_PROFILE").exists():
    print("fake dsh: forced profile failure", file=sys.stderr)
    raise SystemExit(1)
if "--from-default-profile" in args:
    try:
        profile = args[args.index("--profile") + 1]
        assert args[args.index("--from-default-profile") + 1] == "headless"
    except (ValueError, IndexError, AssertionError):
        raise SystemExit(2)
    home = Path(os.environ["DSH_HOME"])
    directory = home / "profiles" / profile
    directory.mkdir(parents=True)
    manifest = {
        "name": "dsh-profile-" + profile,
        "private": True,
        "dependencies": {},
        "dsh": {"profile": {"bundles": [
            "@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"]}},
    }
    (directory / "package.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (directory / "cordis.yml").write_text(
        "# dsh profile root -- an empty entry list.\n[]\n", encoding="utf-8")
    (directory / "cordis.patch.yml").write_text(
        "# Your patch layer for this dsh profile:\n[]\n", encoding="utf-8")
    (directory / "pnpm-workspace.yaml").write_text(
        "packages:\n  - .\n\nnodeLinker: hoisted\nautoInstallPeers: false\n",
        encoding="utf-8")
    print("[]")
    raise SystemExit(0)
print("fake dsh: unsupported invocation", file=sys.stderr)
raise SystemExit(2)
'''

FAKE_NPM = r'''#!/usr/bin/env python3
from pathlib import Path
print(Path(__file__).with_name("npm-root.txt").read_text(encoding="utf-8").strip())
'''

FAKE_SHIM = '''#!/bin/sh
export DSH_HOME="/tmp/codex-dsh-desktop-shim-must-be-skipped"
exit 99
'''

LEGACY_AGENTS = """# 默认子任务委派：DeepSeek Harness

这是用户的全局 Codex 委派偏好。所有需要委派的任务，无论探索、分析、规划、实现、调试、测试、审查、研究或文档，默认使用 `codex-subagents-dsh`。主 agent 负责协调和最终独立验收；简单工作仍可由主 agent 直接完成。

- 开始委派前读取 `/example/.agents/skills/codex-subagents-dsh/SKILL.md`。使用普通原生 Codex subagent 作为启动器，由它直接运行技能中的 `scripts/run_dsh.py`，实际子任务由独立 Harness 的模型、上下文、工具和 session 执行。
- 配置使用 `/example/.dsh-codex/bridge.json`；默认 profile 为 `codex-dsh`，当前选择为 `deepseek-official / deepseek-flash / max`。以实际配置文件为准。只有用户明确指定时，才为本次任务改变执行方式、profile 或 patches。
- 此路由覆盖所有原生或自定义 agent 类型。优先使用普通 `default` 启动器；使用 `explorer`、`worker` 或其他角色时也遵循同一路由，把角色约束传给 Harness。探索/只读审查保持只读；实现任务只修改用户授权的文件。
- 支持时用 `fork_turns="none"`，消息给全技能路径、配置路径、绝对 cwd、完整任务、允许的操作、验收条件及可选参数。只使用当前工具 schema 真正支持的字段。
- 执行子 agent 只做启动所需的范围整理、任务文件准备、脚本调用、等待和结果回传。实际分析、实现、审查或计算交给 Harness。它不再递归委派，也不自行代写目标产物。Harness 内部执行任务时直接完成该任务，不重新进入 Codex 桥接流程。
- 缺少工具、凭据、网络或权限，以及 Harness 失败或超时，均明确回报失败/阻塞。不得静默改为 GPT 子 agent 或启动器自行完成子任务；任务已有运行句柄时先确认其实际状态，模型业务请求不自动重试。
- 主 agent 独立检查实际文件、差异或命令结果，分别报告 Harness 完成状态、任务验收状态和未解决的工具错误。`harness_completed=true` 本身不证明任务通过。

这是通过已安装技能实现的默认路由，并非修改 Codex 原生执行后端。保持现有主模型、权限、审批和代理策略。用户对某次任务的明确覆盖优先；无法按路由执行时报告具体缺口。
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-dsh-installer-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.node = self.bin / "node"
        self.node.write_text(FAKE_NODE, encoding="utf-8")
        self.node.chmod(0o755)
        self.entry = self.bin / "fake-dsh.py"
        self.entry.write_text(FAKE_DSH, encoding="utf-8")
        self.entry.chmod(0o755)
        self.npm = self.bin / "npm"
        self.npm.write_text(FAKE_NPM, encoding="utf-8")
        self.npm.chmod(0o755)
        self.shim = self.bin / "dsh"
        self.shim.write_text(FAKE_SHIM, encoding="utf-8")
        self.shim.chmod(0o755)
        self.npm_root = self.root / "npm-global" / "lib" / "node_modules"
        package = self.npm_root / "@deepseek-ai" / "dsh"
        (package / "lib").mkdir(parents=True)
        shutil.copy2(self.entry, package / "lib" / "bin.js")
        (package / "package.json").write_text(
            json.dumps({"name": "@deepseek-ai/dsh", "version": "0.2.1-test"}) + "\n",
            encoding="utf-8")
        (self.bin / "npm-root.txt").write_text(str(self.npm_root) + "\n", encoding="utf-8")
        self.dest = self.root / "dest" / "codex-subagents-dsh"
        self.config = self.root / "config"
        self.source = self.copy_source("source-a")
        self.command_json = json.dumps([str(self.node), str(self.entry)])

    def copy_source(self, name: str) -> Path:
        target = self.root / name
        shutil.copytree(
            REPO, target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".private"))
        return target

    def env(self) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8",
        }
        return env

    def run_install(self, *extra: str, source: Path | None = None,
                    expected: int | None = None) -> tuple[subprocess.CompletedProcess, dict]:
        source = source or self.source
        command = [
            sys.executable, str(source / "scripts" / "install.py"),
            "--dest", str(self.dest), "--config-home", str(self.config),
            "--json", *extra,
        ]
        result = subprocess.run(command, env=self.env(), text=True,
                                capture_output=True, timeout=90)
        try:
            report = json.loads(result.stdout)
        except ValueError:
            self.fail(f"installer did not print JSON: code={result.returncode}\n"
                      f"stdout={result.stdout[:2000]}\nstderr={result.stderr[:2000]}")
        if expected is not None:
            self.assertEqual(result.returncode, expected,
                             f"report={json.dumps(report, ensure_ascii=False)[:3000]}\n"
                             f"stderr={result.stderr[:2000]}")
        return result, report

    def command_args(self) -> list[str]:
        return ["--dsh-command-json", self.command_json]

    def bridge(self) -> dict:
        return json.loads((self.config / "bridge.json").read_text(encoding="utf-8"))

    def write_bridge(self, value: dict) -> None:
        self.config.mkdir(parents=True, exist_ok=True)
        (self.config / "bridge.json").write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def assert_mode(self, path: Path, mode: int) -> None:
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)

    # ------------------------------------------------------------------ fresh
    def test_fresh_install_schema2_profiles_and_receipt(self):
        _, report = self.run_install(*self.command_args(), expected=0)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["bridge_schema_version"], 2)
        bridge = self.bridge()
        self.assertEqual(bridge["schema_version"], 2)
        self.assertIsNone(bridge["hard_timeout_seconds"])
        self.assertEqual(bridge["heartbeat_seconds"], 5)
        self.assertEqual(bridge["stall_warning_seconds"], 300)
        self.assertEqual(bridge["shutdown_grace_seconds"], 10)
        self.assertEqual(bridge["finalization_grace_seconds"], 10)
        self.assertEqual(bridge["default_profile"], "codex-dsh")
        self.assertEqual(bridge["default_patches"], [])
        self.assertEqual(bridge["dsh_command"], [str(self.node), str(self.entry)])
        self.assertEqual(Path(bridge["dsh_home"]), self.config)
        self.assertEqual(Path(bridge["log_dir"]), self.config / "logs")
        self.assert_mode(self.config / "bridge.json", 0o600)
        self.assert_mode(self.config, 0o700)
        for profile in ("codex-dsh", "codex-dsh-review"):
            directory = self.config / "profiles" / profile
            self.assertTrue(directory.is_dir())
            manifest = json.loads((directory / "package.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["dsh"]["profile"]["bundles"],
                             ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"])
            patch = (directory / "cordis.patch.yml").read_text(encoding="utf-8")
            self.assertIn("provider: deepseek-official", patch)
            self.assertIn("model: deepseek-flash", patch)
            self.assertIn("reasoningEffort: max", patch)
        self.assertTrue((self.config / "patches" / "max.yml").is_file())
        self.assertTrue((self.dest / "SKILL.md").is_file())
        self.assertTrue((self.dest / "README.md").is_file())
        self.assert_mode(self.dest, 0o755)
        self.assertTrue((self.dest / "examples" / "global-agents.block.md").is_file())
        self.assert_mode(self.dest / "scripts" / "install.py", 0o755)
        receipt = json.loads((self.config / "install-receipt.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["skill"]["dest"], str(self.dest))
        self.assertFalse(receipt["adopted_without_receipt"])
        self.assert_mode(self.config / "install-receipt.json", 0o600)
        self.assertEqual(report["backups"], [])

    def test_existing_profile_and_unknown_user_file_preserved(self):
        profile = self.config / "profiles" / "codex-dsh"
        profile.mkdir(parents=True)
        custom = b"# user-owned\n- id: custom\n"
        (profile / "cordis.patch.yml").write_bytes(custom)
        (profile / "package.json").write_text('{"custom": true}\n', encoding="utf-8")
        self.run_install(*self.command_args(), expected=0)
        self.assertEqual((profile / "cordis.patch.yml").read_bytes(), custom)
        self.assertEqual((profile / "package.json").read_text(encoding="utf-8"),
                         '{"custom": true}\n')
        self.assertTrue((self.config / "profiles" / "codex-dsh-review" / "package.json").is_file())
        (self.dest / "user-notes.txt").write_text("keep me\n", encoding="utf-8")
        self.run_install(*self.command_args(), expected=0)
        self.assertEqual((self.dest / "user-notes.txt").read_text(encoding="utf-8"), "keep me\n")

    def test_default_paths_are_resolved_under_home(self):
        command = [sys.executable, str(self.source / "scripts" / "install.py"),
                   "--dsh-command-json", self.command_json, "--json"]
        result = subprocess.run(command, env=self.env(), text=True,
                                capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        default_dest = self.home / ".agents" / "skills" / "codex-subagents-dsh"
        default_config = self.home / ".dsh-codex"
        self.assertEqual(Path(report["dest"]), default_dest)
        self.assertEqual(Path(report["bridge_path"]), default_config / "bridge.json")
        self.assertTrue((default_dest / "SKILL.md").is_file())
        self.assertTrue((default_config / "profiles" / "codex-dsh" / "package.json").is_file())
        self.assertEqual((default_config / "bridge.json").stat().st_mode & 0o777, 0o600)

    # ------------------------------------------------------------ source update
    def test_source_update_and_modified_file_requires_update(self):
        self.run_install(*self.command_args(), expected=0)
        source_skill = self.source / "SKILL.md"
        source_skill.write_text("# changed source\n", encoding="utf-8")
        _, report = self.run_install(*self.command_args(), expected=0)
        self.assertEqual((self.dest / "SKILL.md").read_text(encoding="utf-8"), "# changed source\n")
        self.assertTrue(report["backups"])
        backup_root = Path(report["backup_root"])
        self.assertTrue((backup_root / "skill" / "SKILL.md").is_file())
        (self.dest / "SKILL.md").write_text("# local modification\n", encoding="utf-8")
        source_skill.write_text("# changed again\n", encoding="utf-8")
        _, report = self.run_install(*self.command_args(), expected=4)
        self.assertEqual(report["error"]["code"], "SKILL_UPDATE_CONFLICT")
        self.assertEqual((self.dest / "SKILL.md").read_text(encoding="utf-8"),
                         "# local modification\n")
        _, report = self.run_install("--update", *self.command_args(), expected=0)
        self.assertEqual((self.dest / "SKILL.md").read_text(encoding="utf-8"), "# changed again\n")
        backup_root = Path(report["backup_root"])
        self.assertEqual((backup_root / "skill" / "SKILL.md").read_text(encoding="utf-8"),
                         "# local modification\n")

    def test_no_receipt_conflict_and_safe_adoption(self):
        self.dest.mkdir(parents=True)
        (self.dest / "SKILL.md").write_text("# pre-existing\n", encoding="utf-8")
        (self.dest / "unknown.txt").write_text("user file\n", encoding="utf-8")
        before = sha256(self.dest / "SKILL.md")
        _, report = self.run_install(*self.command_args(), expected=4)
        self.assertEqual(report["error"]["code"], "SKILL_UPDATE_CONFLICT")
        self.assertEqual(sha256(self.dest / "SKILL.md"), before)
        self.assertFalse(self.config.exists())
        self.run_install("--update", *self.command_args(), expected=0)
        self.assertNotEqual(sha256(self.dest / "SKILL.md"), before)
        self.assertEqual((self.dest / "unknown.txt").read_text(encoding="utf-8"), "user file\n")
        receipt = json.loads((self.config / "install-receipt.json").read_text(encoding="utf-8"))
        self.assertTrue(receipt["adopted_without_receipt"])

    def test_removed_managed_file_and_unknown_file(self):
        self.run_install(*self.command_args(), expected=0)
        (self.dest / "obsolete.txt").write_text("old managed\n", encoding="utf-8")
        # Make it managed by reinstalling with a receipt entry that includes it.
        receipt_path = self.config / "install-receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["skill"]["files"]["obsolete.txt"] = {
            "sha256": sha256(self.dest / "obsolete.txt"),
            "size": (self.dest / "obsolete.txt").stat().st_size,
        }
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
        (self.dest / "keep.txt").write_text("unknown\n", encoding="utf-8")
        self.run_install(*self.command_args(), expected=0)
        self.assertFalse((self.dest / "obsolete.txt").exists())
        self.assertEqual((self.dest / "keep.txt").read_text(encoding="utf-8"), "unknown\n")

    # --------------------------------------------------------------- migration
    def test_schema1_requires_explicit_migration_and_is_idempotent(self):
        old = {
            "schema_version": 1,
            "dsh_command": [str(self.node), str(self.entry)],
            "dsh_home": str(self.config),
            "default_profile": "custom",
            "default_patches": [],
            "timeout_seconds": 300,
            "log_dir": str(self.config / "logs"),
            "user_unknown": {"keep": True},
        }
        self.write_bridge(old)
        before = (self.config / "bridge.json").read_bytes()
        _, report = self.run_install(expected=4)
        self.assertEqual(report["error"]["code"], "MIGRATION_REQUIRED")
        self.assertEqual((self.config / "bridge.json").read_bytes(), before)
        _, report = self.run_install("--migrate-progress", expected=0)
        self.assertTrue(report["bridge_migrated"])
        bridge = self.bridge()
        self.assertEqual(bridge["schema_version"], 2)
        self.assertNotIn("timeout_seconds", bridge)
        self.assertIsNone(bridge["hard_timeout_seconds"])
        self.assertEqual(bridge["default_profile"], "custom")
        self.assertEqual(bridge["user_unknown"], {"keep": True})
        backup = Path(report["backup_root"]) / "config" / "bridge.json"
        self.assertEqual(backup.read_bytes(), before)
        migrated_bytes = (self.config / "bridge.json").read_bytes()
        _, report = self.run_install("--migrate-progress", expected=0)
        self.assertFalse(report["bridge_changed"])
        self.assertEqual((self.config / "bridge.json").read_bytes(), migrated_bytes)
        self.assertEqual(report["backups"], [])

    def test_custom_legacy_timeout_is_preserved(self):
        old = {
            "schema_version": 1,
            "dsh_command": [str(self.node), str(self.entry)],
            "dsh_home": str(self.config),
            "default_profile": "codex-dsh",
            "default_patches": [],
            "timeout_seconds": 120,
            "log_dir": str(self.config / "logs"),
        }
        self.write_bridge(old)
        self.run_install("--migrate-progress", expected=0)
        self.assertEqual(self.bridge()["hard_timeout_seconds"], 120)

    # ----------------------------------------------------------- global routes
    def test_global_routing_is_opt_in_and_preserves_native_launcher_models(self):
        codex = self.home / ".codex"
        agents = codex / "AGENTS.md"
        agents.parent.mkdir(parents=True)
        agents.write_text("# My own global rules\n\nKeep this line.\n", encoding="utf-8")
        launchers = codex / "agents"
        launchers.mkdir()
        old_explorer = textwrap.dedent('''\
            name = "explorer"
            description = "old explorer"
            developer_instructions = """
            你是只读探索任务的 Codex 桥接执行子 agent。旧说明。
            """
            model = "native-model"
            model_reasoning_effort = "xhigh"
            unknown_key = "keep"
            ''')
        (launchers / "explorer.toml").write_text(old_explorer, encoding="utf-8")
        before = agents.read_bytes()
        self.run_install(*self.command_args(), expected=0)
        self.assertEqual(agents.read_bytes(), before)
        self.assertEqual((launchers / "explorer.toml").read_text(encoding="utf-8"), old_explorer)
        _, report = self.run_install("--enable-global-routing", *self.command_args(), expected=0)
        self.assertEqual(report["global_routing"]["status"], "PASS")
        migrated = agents.read_text(encoding="utf-8")
        self.assertIn("Keep this line.", migrated)
        self.assertIn("<!-- codex-subagents-dsh:begin", migrated)
        self.assertIn("<!-- codex-subagents-dsh:end -->", migrated)
        launcher = (launchers / "explorer.toml").read_text(encoding="utf-8")
        self.assertIn('model = "native-model"', launcher)
        self.assertIn('model_reasoning_effort = "xhigh"', launcher)
        self.assertIn('unknown_key = "keep"', launcher)
        self.assertIn("codex-subagents-dsh", launcher)
        self.assertTrue((launchers / "default.toml").is_file())
        self.assertTrue((launchers / "worker.toml").is_file())
        self.assertTrue((Path(report["backup_root"]) / "global" / "AGENTS.md").is_file())
        self.assertTrue((Path(report["backup_root"]) / "global" / "launchers" / "explorer.toml").is_file())
        snapshot = agents.read_bytes()
        snapshot_launcher = launcher
        _, report = self.run_install("--enable-global-routing", *self.command_args(), expected=0)
        self.assertEqual(agents.read_bytes(), snapshot)
        self.assertEqual((launchers / "explorer.toml").read_text(encoding="utf-8"), snapshot_launcher)
        self.assertEqual(report["global_routing"]["applied"], [])

    def test_legacy_global_agents_is_migrated_without_duplicate(self):
        codex = self.home / ".codex"
        codex.mkdir(parents=True)
        (codex / "AGENTS.md").write_text(LEGACY_AGENTS, encoding="utf-8")
        self.run_install("--enable-global-routing", *self.command_args(), expected=0)
        text = (codex / "AGENTS.md").read_text(encoding="utf-8")
        self.assertNotIn("# 默认子任务委派：DeepSeek Harness", text)
        self.assertEqual(text.count("<!-- codex-subagents-dsh:begin"), 1)
        self.assertEqual(text.count("<!-- codex-subagents-dsh:end -->"), 1)

    def test_content_before_legacy_title_is_preserved(self):
        codex = self.home / ".codex"
        codex.mkdir(parents=True)
        prefix = "    Keep this indented custom section.\n\n# Team policy\n\n"
        suffix = "\n    Preserve this trailing code block.\n\n"
        (codex / "AGENTS.md").write_text(prefix + LEGACY_AGENTS + suffix, encoding="utf-8")
        self.run_install("--enable-global-routing", *self.command_args(), expected=0)
        text = (codex / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith(prefix))
        self.assertTrue(text.endswith(suffix))
        self.assertIn("<!-- codex-subagents-dsh:begin", text)
        self.assertNotIn("# 默认子任务委派：DeepSeek Harness", text)

    def test_ambiguous_legacy_agents_is_not_overwritten(self):
        codex = self.home / ".codex"
        codex.mkdir(parents=True)
        variants = (
            LEGACY_AGENTS.replace("\n- 配置使用", "\n- 未知的自定义说明不要覆盖。\n- 配置使用"),
            LEGACY_AGENTS.replace("\n- 支持时", "\n- 支持时：发布前必须运行安全审查。\n- 支持时"),
            LEGACY_AGENTS.rstrip("\n") + "始终先运行安全审查。\n",
        )
        for index, ambiguous in enumerate(variants):
            with self.subTest(variant=index):
                (codex / "AGENTS.md").write_text(ambiguous, encoding="utf-8")
                before = (codex / "AGENTS.md").read_bytes()
                _, report = self.run_install("--enable-global-routing", *self.command_args(), expected=4)
                self.assertEqual(report["error"]["code"], "GLOBAL_AGENTS_LEGACY_AMBIGUOUS")
                self.assertEqual((codex / "AGENTS.md").read_bytes(), before)
                self.assertFalse(self.dest.exists())
                self.assertFalse(self.config.exists())

    def test_unrecognized_launcher_is_preserved_and_reported_partial(self):
        launchers = self.home / ".codex" / "agents"
        launchers.mkdir(parents=True)
        custom = 'name = "default"\ndeveloper_instructions = "custom"\nmodel = "native"\n'
        (launchers / "default.toml").write_text(custom, encoding="utf-8")
        _, report = self.run_install("--enable-global-routing", *self.command_args(), expected=0)
        self.assertEqual(report["global_routing"]["status"], "PARTIAL")
        self.assertTrue(any("default.toml" in warning for warning in report["warnings"]))
        self.assertEqual((launchers / "default.toml").read_text(encoding="utf-8"), custom)

    # ---------------------------------------------------------------- discovery
    def test_auto_discovery_skips_dsh_home_shim_and_finds_npm_entry(self):
        _, report = self.run_install(expected=0)
        bridge = self.bridge()
        self.assertEqual(bridge["dsh_command"], [str(self.node), str(self.npm_root / "@deepseek-ai" / "dsh" / "lib" / "bin.js")])
        self.assertNotEqual(Path(bridge["dsh_command"][1]), self.shim)

    def test_invalid_node_and_entry_fail_without_writes(self):
        (self.bin / "node-version.txt").write_text("v18.0.0\n", encoding="utf-8")
        _, report = self.run_install(*self.command_args(), expected=3)
        self.assertEqual(report["error"]["code"], "NODE_VERSION_TOO_OLD")
        self.assertFalse(self.config.exists())
        (self.bin / "node-version.txt").write_text("v22.19.0\n", encoding="utf-8")
        bad = json.dumps([str(self.node), str(self.root / "missing.js")])
        _, report = self.run_install("--dsh-command-json", bad, expected=3)
        self.assertEqual(report["error"]["code"], "DSH_COMMAND_INVALID")

    # ------------------------------------------------------------------- rollback
    def test_profile_initialization_failure_leaves_existing_state_untouched(self):
        self.run_install(*self.command_args(), expected=0)
        bridge_before = (self.config / "bridge.json").read_bytes()
        skill_before = sha256(self.dest / "SKILL.md")
        (self.source / "SKILL.md").write_text("# newer source\n", encoding="utf-8")
        shutil.rmtree(self.config / "profiles" / "codex-dsh-review")
        (self.entry.parent / "FAIL_PROFILE").write_text("fail\n", encoding="utf-8")
        _, report = self.run_install(*self.command_args(), expected=3)
        self.assertEqual(report["error"]["code"], "PROFILE_INIT_FAILED")
        self.assertEqual((self.config / "bridge.json").read_bytes(), bridge_before)
        self.assertEqual(sha256(self.dest / "SKILL.md"), skill_before)
        self.assertFalse((self.config / "profiles" / "codex-dsh-review").exists())

    def test_receipt_write_failure_rolls_back_skill_and_bridge(self):
        self.run_install(*self.command_args(), expected=0)
        bridge_before = (self.config / "bridge.json").read_bytes()
        skill_before = (self.dest / "SKILL.md").read_bytes()
        receipt_path = self.config / "install-receipt.json"
        receipt_path.unlink()
        receipt_path.mkdir()
        (self.source / "SKILL.md").write_text("# rollback target\n", encoding="utf-8")
        _, report = self.run_install("--update", *self.command_args(), expected=5)
        self.assertEqual(report["error"]["code"], "TRANSACTION_FAILED")
        self.assertEqual((self.dest / "SKILL.md").read_bytes(), skill_before)
        self.assertEqual((self.config / "bridge.json").read_bytes(), bridge_before)
        self.assertTrue(receipt_path.is_dir())

    def test_failed_bridge_rollback_is_reported_and_skill_still_restored(self):
        self.run_install(*self.command_args(), expected=0)
        skill_before = (self.dest / "SKILL.md").read_bytes()
        bridge = self.bridge()
        bridge["schema_version"] = 1
        bridge["timeout_seconds"] = 300
        self.write_bridge(bridge)
        (self.source / "SKILL.md").write_text("# replacement\n", encoding="utf-8")
        probe = textwrap.dedent("""
            import sys
            sys.path.insert(0, sys.argv[1])
            import install
            original = install._atomic_write
            failed_receipt = False
            def write(path, data, mode):
                global failed_receipt
                if path.name == "install-receipt.json":
                    failed_receipt = True
                    raise OSError("injected receipt failure")
                if failed_receipt and path.name == "bridge.json":
                    raise OSError("injected rollback failure")
                return original(path, data, mode)
            install._atomic_write = write
            raise SystemExit(install.main(sys.argv[2:]))
        """)
        result = subprocess.run(
            [sys.executable, "-c", probe, str(self.source / "scripts"),
             "--dest", str(self.dest), "--config-home", str(self.config),
             "--migrate-progress", "--update", "--json", *self.command_args()],
            env=self.env(), text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 5, result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual(report["error"]["code"], "ROLLBACK_INCOMPLETE")
        self.assertNotIn("previous state was restored", report["error"]["message"])
        self.assertTrue(Path(report["backup_root"]).is_dir())
        self.assertTrue(report["backups"])
        self.assertEqual((self.dest / "SKILL.md").read_bytes(), skill_before)
        self.assertEqual(self.bridge()["schema_version"], 2)
        self.assertTrue(list((self.config / "backups").glob("*/config/bridge.json")))

    def test_dotenv_in_existing_destination_is_refused_without_copy(self):
        self.dest.mkdir(parents=True)
        (self.dest / ".env").write_text("SENTINEL_SECRET=destination-secret\n",
                                        encoding="utf-8")
        _, report = self.run_install("--update", *self.command_args(), expected=4)
        self.assertEqual(report["error"]["code"], "DOTENV_IN_DEST")
        self.assertEqual((self.dest / ".env").read_text(encoding="utf-8"),
                         "SENTINEL_SECRET=destination-secret\n")
        self.assertFalse(self.config.exists())

    # ---------------------------------------------------------------------- env
    def test_dotenv_is_not_copied_or_read(self):
        (self.source / ".env").write_text("SENTINEL_SECRET=source-secret\n", encoding="utf-8")
        self.config.mkdir(parents=True)
        (self.config / ".env").write_text("SENTINEL_SECRET=config-secret\n", encoding="utf-8")
        self.run_install(*self.command_args(), expected=0)
        self.assertFalse((self.dest / ".env").exists())
        self.assertEqual((self.config / ".env").read_text(encoding="utf-8"),
                         "SENTINEL_SECRET=config-secret\n")
        receipt = (self.config / "install-receipt.json").read_text(encoding="utf-8")
        self.assertNotIn("source-secret", receipt)
        self.assertNotIn("config-secret", receipt)


if __name__ == "__main__":
    unittest.main()
