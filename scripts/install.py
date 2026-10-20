#!/usr/bin/env python3
"""Preserve-first installer for codex-subagents-dsh.

Standard library only. The installer never downloads packages, edits a shell
profile, copies .env files, or calls a model. It stages a complete skill tree,
backs up replaced files, and writes a private receipt for later updates.

Exit codes:
  0 success (possibly partial global routing)
  2 invalid arguments, bridge shape, or paths
  3 dsh discovery / Node validation / official profile initialization failure
  4 explicit --update or --migrate-progress is required, or a legacy global
    AGENTS.md cannot be migrated without overwriting user instructions
  5 filesystem or transaction failure
"""
from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

VERSION = "0.1.0"
SKILL_NAME = "codex-subagents-dsh"
def _default_dest() -> Path:
    return Path.home() / ".agents" / "skills" / SKILL_NAME


def _default_config_home() -> Path:
    return Path.home() / ".dsh-codex"


def _effective_paths(arguments: argparse.Namespace) -> tuple[Path, Path]:
    dest = _absolute_arg(arguments.dest) if arguments.dest else _default_dest()
    config_home = (_absolute_arg(arguments.config_home)
                   if arguments.config_home else _default_config_home())
    return dest, config_home
PROFILE_NAMES = ("codex-dsh", "codex-dsh-review")
HEADLESS_BUNDLES = ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"]
PROFILE_FILES = ("package.json", "cordis.yml", "cordis.patch.yml", "pnpm-workspace.yaml")
SAFE_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
NODE_MINIMUM = (22, 19, 0)
MODEL_PATCH = """# User-owned profile overrides; edit persistently here.
- id: agent-default-model
  config:
    provider: deepseek-official
    model: deepseek-flash
    reasoningEffort: max
"""
MAX_PATCH = """# Explicit max overlay; not referenced by default_patches.
- id: agent-default-model
  config:
    provider: deepseek-official
    model: deepseek-flash
    reasoningEffort: max
"""
AGENTS_BEGIN = "<!-- codex-subagents-dsh:begin"
AGENTS_END = "<!-- codex-subagents-dsh:end -->"
LAUNCHER_MARKER = ("# Managed by codex-subagents-dsh installer: description and "
                   "developer_instructions are updated; other settings are preserved.\n")
LEGACY_AGENTS_TITLE = "# 默认子任务委派：DeepSeek Harness"
LEGACY_AGENTS_FOOTER = "这是通过已安装技能实现的默认路由"
# Exact known pre-release block, after substituting the two installation paths
# and normalizing blank lines. Prefix matches cannot authorize deleting user rules.
LEGACY_AGENTS_SHA256 = "5404ed7b2f34d74e57d2677eb2a700c8270485506524328aca70593aca42db3b"
LEGACY_LAUNCHER_SIGNATURES = (
    "你是普通 Codex 桥接执行子 agent",
    "你是只读探索任务的 Codex 桥接执行子 agent",
    "你是实现任务的 Codex 桥接执行子 agent",
)
SOURCE_ENTRIES = ("SKILL.md", "README.md", "LICENSE", "VERSION",
                  "references", "agents", "scripts", "examples")
SKIP_DIRS = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".private"}
SKIP_SUFFIXES = (".pyc", ".pyo")
MAX_SOURCE_BYTES = 32 * 1024 * 1024


class InstallError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 2):
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


def _utc_stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _now_iso() -> str:
    return (_datetime.datetime.now(_datetime.timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path, limit: int = MAX_SOURCE_BYTES) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise InstallError("FILE_TOO_LARGE", "A file exceeded the installer size limit.", 2)
            digest.update(chunk)
    return digest.hexdigest()


def _read_limited(path: Path, limit: int, code: str, message: str) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        raise InstallError(code, message, 2) from None
    if len(data) > limit:
        raise InstallError(code, message, 2)
    return data


def _read_json_object(path: Path, limit: int, code: str, message: str) -> dict[str, Any]:
    data = _read_limited(path, limit, code, message)
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise InstallError(code, message, 2) from None
    if not isinstance(value, dict):
        raise InstallError(code, message, 2)
    return value


def _absolute_arg(value: str) -> Path:
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        path = Path.cwd() / path
    return Path(os.path.abspath(path))


def _absolute_json_path(value: Any, base: Path, code: str, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise InstallError(code, f"{label} must be a non-empty path string.", 2)
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        path = base / path
    return Path(os.path.abspath(path))


def _is_regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink()
    except OSError:
        return False


def _inside(root: Path, path: Path) -> bool:
    try:
        return os.path.commonpath((str(root.resolve()), str(path.resolve()))) == str(root.resolve())
    except (OSError, ValueError):
        return False


def _clean_env(home: Path) -> dict[str, str]:
    """Minimal child environment: no credentials, dotenv source, or user home."""
    keep = ("PATH", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT")
    env = {name: value for name, value in os.environ.items() if name in keep}
    env["HOME"] = str(home)
    env["DSH_HOME"] = str(home / ".dsh")
    env["DSH_TELEMETRY_DISABLED"] = "1"
    env["NO_COLOR"] = "1"
    return env


def _run_capture(command: list[str], cwd: Path, env: dict[str, str],
                 timeout: float, limit: int = 131072) -> tuple[int, bytes]:
    try:
        process = subprocess.run(command, cwd=str(cwd), env=env, shell=False,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError("COMMAND_FAILED", "A validation command could not run.", 3) from None
    if len(process.stdout) > limit:
        raise InstallError("COMMAND_OUTPUT_LIMIT", "A validation command returned too much output.", 3)
    return process.returncode, process.stdout


def _node_version(interpreter: Path) -> tuple[int, int, int]:
    with tempfile.TemporaryDirectory(prefix="codex-dsh-node-") as temporary:
        env = _clean_env(Path(temporary))
        code, output = _run_capture([str(interpreter), "--version"], Path(temporary), env, 15)
    if code != 0:
        raise InstallError("NODE_VERSION_FAILED", "The Node interpreter rejected --version.", 3)
    match = re.fullmatch(rb"v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?\s*", output)
    if not match:
        raise InstallError("NODE_VERSION_INVALID", "The Node interpreter returned an unrecognized version.", 3)
    version = tuple(int(part) for part in match.groups())
    if version < NODE_MINIMUM:
        raise InstallError(
            "NODE_VERSION_TOO_OLD",
            "Node 22.19.0 or newer is required by the supported headless launcher.", 3)
    return version  # type: ignore[return-value]


def _package_root(entry: Path) -> Path | None:
    for parent in (entry.parent.parent, entry.parent, entry.parent.parent.parent):
        candidate = parent / "package.json"
        if not candidate.is_file():
            continue
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("name") == "@deepseek-ai/dsh":
            return parent
    return None


def _validate_command(raw: Any, *, require_package: bool) -> list[str]:
    if (not isinstance(raw, list) or len(raw) != 2 or
            any(not isinstance(item, str) or not item or "\x00" in item for item in raw)):
        raise InstallError(
            "DSH_COMMAND_INVALID",
            "--dsh-command-json must be a two-item JSON array: [absolute node, absolute entry].", 2)
    interpreter_path = Path(os.path.expanduser(raw[0]))
    entry_path = Path(os.path.expanduser(raw[1]))
    if not interpreter_path.is_absolute() or not entry_path.is_absolute():
        raise InstallError("DSH_COMMAND_INVALID", "Both dsh command paths must be absolute.", 2)
    try:
        interpreter = interpreter_path.resolve(strict=True)
        entry = entry_path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise InstallError("DSH_COMMAND_INVALID", "A dsh command path does not exist.", 3) from None
    if not _is_regular(interpreter) or not os.access(interpreter, os.X_OK):
        raise InstallError("NODE_NOT_EXECUTABLE", "The configured Node interpreter is not executable.", 3)
    if interpreter.name.lower() not in {"node", "nodejs", "node.exe"}:
        raise InstallError("NODE_NOT_EXECUTABLE", "The first command item must be a Node interpreter.", 3)
    _node_version(interpreter)
    if not _is_regular(entry):
        raise InstallError("DSH_ENTRY_INVALID", "The dsh entry must be a regular file.", 3)
    if require_package and _package_root(entry) is None:
        raise InstallError("DSH_PACKAGE_INVALID", "The dsh entry is not inside @deepseek-ai/dsh.", 3)
    with tempfile.TemporaryDirectory(prefix="codex-dsh-probe-") as temporary:
        home = Path(temporary)
        env = _clean_env(home)
        # The installed dsh launcher must start; --version never loads a model.
        code, output = _run_capture([str(interpreter), str(entry), "--version"],
                                    home, env, 20)
    if code != 0 or not output.strip():
        raise InstallError("DSH_ENTRY_INVALID", "The dsh entry rejected --version.", 3)
    return [str(interpreter), str(entry)]


def _npm_roots() -> list[Path]:
    roots: list[Path] = []
    npm = shutil.which("npm")
    if npm:
        with tempfile.TemporaryDirectory(prefix="codex-dsh-npm-") as temporary:
            home = Path(temporary)
            try:
                code, output = _run_capture([npm, "root", "-g"], home,
                                            _clean_env(home), 15, 8192)
            except InstallError:
                code, output = 1, b""
        if code == 0:
            text = output.decode("utf-8", "replace").strip()
            if text and "\n" not in text:
                roots.append(Path(text))
    home = Path.home()
    roots.extend([
        home / ".npm-global" / "lib" / "node_modules",
        home / ".local" / "lib" / "node_modules",
        Path("/usr/local/lib/node_modules"),
        Path("/usr/lib/node_modules"),
    ])
    for relative in (
            ".nvm/versions/node/*/lib/node_modules",
            ".volta/tools/image/node/*/lib/node_modules",
            ".fnm/node-versions/*/installation/lib/node_modules",
    ):
        try:
            roots.extend(Path(path) for path in home.glob(relative))
        except OSError:
            pass
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = os.path.abspath(str(root))
        if key not in seen:
            seen.add(key)
            unique.append(Path(key))
    return unique


def _discover_command() -> list[str]:
    node = shutil.which("node")
    if not node:
        raise InstallError(
            "NODE_NOT_FOUND",
            "Node 22.19.0+ was not found; install dsh first or pass --dsh-command-json.", 3)
    node_path = Path(node).resolve()
    _node_version(node_path)
    for root in _npm_roots():
        entry = root / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
        if not entry.is_file():
            continue
        try:
            candidate = _validate_command([str(node_path), str(entry)], require_package=True)
        except InstallError:
            continue
        return candidate
    dsh = shutil.which("dsh")
    if dsh:
        shim = Path(dsh)
        try:
            if shim.is_symlink():
                target = shim.resolve(strict=True)
                if target.name == "bin.js" and _package_root(target) is not None:
                    return _validate_command([str(node_path), str(target)], require_package=True)
            elif shim.is_file():
                sample = shim.read_bytes()[:65536]
                if b"DSH_HOME" not in sample:
                    target = shim.resolve(strict=True)
                    if target.name == "bin.js" and _package_root(target) is not None:
                        return _validate_command([str(node_path), str(target)], require_package=True)
        except (OSError, RuntimeError):
            pass
    raise InstallError(
        "DSH_NOT_FOUND",
        "No real @deepseek-ai/dsh npm entry was found (desktop shims that set DSH_HOME are skipped). "
        "Install dsh first or pass --dsh-command-json.", 3)


def _collect_source(root: Path) -> tuple[dict[str, bytes], dict[str, int]]:
    files: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    total = 0
    candidates: list[Path] = []
    for name in SOURCE_ENTRIES:
        path = root / name
        if path.is_file() and not path.is_symlink():
            candidates.append(path)
        elif path.is_dir() and not path.is_symlink():
            for directory, dirs, names in os.walk(path, followlinks=False):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                for child in sorted(names):
                    item = Path(directory) / child
                    if child.startswith("."):
                        # Private receipt/backup metadata must never be published.
                        continue
                    if child.endswith(SKIP_SUFFIXES):
                        continue
                    if item.is_symlink() or not item.is_file():
                        continue
                    candidates.append(item)
    for item in sorted(set(candidates)):
        rel = item.relative_to(root).as_posix()
        if rel.startswith("../") or "/../" in rel:
            raise InstallError("SOURCE_PATH_INVALID", "Source discovery escaped the skill root.", 5)
        try:
            data = item.read_bytes()
        except OSError:
            raise InstallError("SOURCE_READ_FAILED", "A source file could not be read.", 5) from None
        total += len(data)
        if total > MAX_SOURCE_BYTES:
            raise InstallError("SOURCE_TOO_LARGE", "The source tree exceeded the installer size limit.", 5)
        files[rel] = data
        modes[rel] = 0o755 if rel.startswith("scripts/") and rel.endswith(".py") else 0o644
    required = {"SKILL.md", "references/usage.md", "agents/openai.yaml",
                "scripts/run_dsh.py", "scripts/doctor.py", "scripts/supervision.py",
                "scripts/manage_dsh.py"}
    if not required.issubset(files):
        raise InstallError("SOURCE_INCOMPLETE", "The source tree is missing required skill files.", 5)
    return files, modes


def _reject_dotenv_in_destination(dest: Path) -> None:
    """Refuse to copy a pre-existing .env into staging or backups."""
    if not dest.exists() or not dest.is_dir():
        return
    for directory, dirs, files in os.walk(dest, followlinks=False):
        for name in list(dirs) + list(files):
            if name.startswith(".env") and name != ".env.example":
                raise InstallError(
                    "DOTENV_IN_DEST",
                    "An existing skill destination contains an .env file; the installer will not copy "
                    "or back up dotenv secrets. Move it out of the skill directory and retry.", 4)


def _load_receipt(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = _read_json_object(path, 2 * 1024 * 1024,
                                  "RECEIPT_INVALID", "The installation receipt is not valid JSON.")
    except InstallError:
        return None
    skill = value.get("skill")
    if not isinstance(skill, dict) or not isinstance(skill.get("files"), dict):
        return None
    if not isinstance(skill.get("dest"), str):
        return None
    return value


@dataclass
class BridgePlan:
    path: Path
    old: dict[str, Any] | None
    old_bytes: bytes | None
    new: dict[str, Any]
    new_bytes: bytes
    changed: bool
    migrated: bool
    command: list[str]


def _number(value: Any, label: str, *, none: bool = False,
            positive: bool = False) -> int | float | None:
    if none and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InstallError("BRIDGE_FIELD_INVALID", f"{label} has an invalid type.", 2)
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise InstallError("BRIDGE_FIELD_INVALID", f"{label} is not finite.", 2) from None
    if not math.isfinite(number):
        raise InstallError("BRIDGE_FIELD_INVALID", f"{label} is not finite.", 2)
    if positive and value <= 0:
        raise InstallError("BRIDGE_FIELD_INVALID", f"{label} must be positive.", 2)
    if not positive and value < 0:
        raise InstallError("BRIDGE_FIELD_INVALID", f"{label} must not be negative.", 2)
    return value


def _validate_bridge_fields(bridge: dict[str, Any], base: Path) -> None:
    if bridge.get("schema_version") != 2 or type(bridge.get("schema_version")) is not int:
        raise InstallError("BRIDGE_SCHEMA_INVALID", "The bridge must use schema_version 2.", 2)
    command = bridge.get("dsh_command")
    _validate_command(command, require_package=False)
    for key in ("dsh_home", "log_dir"):
        if not isinstance(bridge.get(key), str) or not Path(bridge[key]).is_absolute():
            raise InstallError("BRIDGE_PATH_INVALID", f"{key} must be absolute.", 2)
    profile = bridge.get("default_profile")
    if not isinstance(profile, str) or not SAFE_PROFILE.fullmatch(profile):
        raise InstallError("BRIDGE_PROFILE_INVALID", "default_profile is invalid.", 2)
    patches = bridge.get("default_patches")
    if not isinstance(patches, list) or any(not isinstance(item, str) or not item for item in patches):
        raise InstallError("BRIDGE_PATCHES_INVALID", "default_patches must be a list of paths.", 2)
    for patch in patches:
        if not Path(patch).is_absolute():
            raise InstallError("BRIDGE_PATCHES_INVALID", "default_patches entries must be absolute.", 2)
    _number(bridge.get("hard_timeout_seconds"), "hard_timeout_seconds", none=True, positive=True)
    heartbeat = _number(bridge.get("heartbeat_seconds"), "heartbeat_seconds", positive=True)
    _number(bridge.get("stall_warning_seconds"), "stall_warning_seconds", positive=True)
    _number(bridge.get("shutdown_grace_seconds"), "shutdown_grace_seconds", positive=True)
    _number(bridge.get("finalization_grace_seconds"), "finalization_grace_seconds", positive=True)
    if heartbeat is None:
        raise InstallError("BRIDGE_FIELD_INVALID", "heartbeat_seconds is required.", 2)


def _bridge_command(bridge: dict[str, Any] | None, config_home: Path,
                    explicit: Any) -> list[str]:
    if explicit is not None:
        return _validate_command(explicit, require_package=False)
    if bridge is not None and "dsh_command" in bridge:
        raw = bridge.get("dsh_command")
        if isinstance(raw, list):
            expanded = []
            for item in raw:
                if isinstance(item, str):
                    path = Path(os.path.expanduser(item))
                    expanded.append(str(Path(os.path.abspath(path)) if path.is_absolute() else item))
                else:
                    expanded.append(item)
            return _validate_command(expanded, require_package=False)
        raise InstallError("BRIDGE_COMMAND_INVALID", "bridge.dsh_command is invalid.", 2)
    return _discover_command()


def _normalize_bridge(path: Path, config_home: Path, migrate: bool,
                      explicit_command: Any) -> BridgePlan:
    old_bytes: bytes | None = None
    old: dict[str, Any] | None = None
    if path.exists():
        if not _is_regular(path):
            raise InstallError("BRIDGE_NOT_FILE", "bridge.json is not a regular file.", 2)
        old_bytes = _read_limited(path, 2 * 1024 * 1024, "BRIDGE_UNREADABLE",
                                  "bridge.json could not be read.")
        try:
            value = json.loads(old_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise InstallError("BRIDGE_JSON_INVALID", "bridge.json is not valid JSON.", 2) from None
        if not isinstance(value, dict):
            raise InstallError("BRIDGE_JSON_INVALID", "bridge.json must contain an object.", 2)
        schema = value.get("schema_version")
        if type(schema) is not int or schema not in (1, 2):
            raise InstallError("BRIDGE_SCHEMA_UNSUPPORTED", "bridge.json must use schema_version 1 or 2.", 2)
        if schema == 1 and not migrate:
            raise InstallError(
                "MIGRATION_REQUIRED",
                "bridge schema 1 requires explicit --migrate-progress; no files were changed.", 4)
        old = value
    migrated = bool(old is not None and old.get("schema_version") == 1)
    command = _bridge_command(old, config_home, explicit_command)
    if old is None:
        new: dict[str, Any] = {
            "schema_version": 2,
            "dsh_command": command,
            "dsh_home": str(config_home),
            "default_profile": "codex-dsh",
            "default_patches": [],
            "hard_timeout_seconds": None,
            "heartbeat_seconds": 5,
            "stall_warning_seconds": 300,
            "shutdown_grace_seconds": 10,
            "finalization_grace_seconds": 10,
            "log_dir": str(config_home / "logs"),
        }
    else:
        new = {"schema_version": 2}
        for key, value in old.items():
            if key in {"schema_version", "timeout_seconds"}:
                continue
            new[key] = value
        new["dsh_command"] = command
        new["dsh_home"] = str(_absolute_json_path(
            old.get("dsh_home", str(config_home)), config_home,
            "BRIDGE_PATH_INVALID", "dsh_home"))
        new["default_profile"] = old.get("default_profile", "codex-dsh")
        patches = old.get("default_patches", [])
        if not isinstance(patches, list):
            raise InstallError("BRIDGE_PATCHES_INVALID", "default_patches must be a list.", 2)
        new["default_patches"] = [
            str(_absolute_json_path(item, config_home, "BRIDGE_PATCHES_INVALID",
                                    "default_patches entry"))
            for item in patches
        ]
        legacy_timeout = old.get("timeout_seconds")
        if "hard_timeout_seconds" not in new:
            if legacy_timeout is None or legacy_timeout == 300:
                new["hard_timeout_seconds"] = None
            else:
                new["hard_timeout_seconds"] = _number(
                    legacy_timeout, "timeout_seconds", positive=True)
        else:
            new["hard_timeout_seconds"] = _number(
                new["hard_timeout_seconds"], "hard_timeout_seconds", none=True, positive=True)
        for key, default in (("heartbeat_seconds", 5), ("stall_warning_seconds", 300),
                             ("shutdown_grace_seconds", 10), ("finalization_grace_seconds", 10)):
            if key not in new:
                new[key] = default
        new["log_dir"] = str(_absolute_json_path(
            old.get("log_dir", str(Path(new["dsh_home"]) / "logs")), config_home,
            "BRIDGE_PATH_INVALID", "log_dir"))
    _validate_bridge_fields(new, config_home)
    new_bytes = (json.dumps(new, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    changed = old != new
    return BridgePlan(path=path, old=old, old_bytes=old_bytes, new=new,
                      new_bytes=new_bytes, changed=changed, migrated=migrated,
                      command=command)


@dataclass
class ProfilePlan:
    name: str
    directory: Path
    files: dict[str, bytes]


@dataclass
class ConfigPlan:
    config_home: Path
    dsh_home: Path
    log_dir: Path
    bridge: BridgePlan
    profiles: list[ProfilePlan] = field(default_factory=list)
    preserved_profiles: list[str] = field(default_factory=list)
    max_patch: Path | None = None
    max_patch_bytes: bytes | None = None


def _official_profile(command: list[str], profile: str) -> dict[str, bytes]:
    if not SAFE_PROFILE.fullmatch(profile):
        raise InstallError("PROFILE_NAME_INVALID", "Invalid profile name.", 2)
    with tempfile.TemporaryDirectory(prefix="codex-dsh-template-") as temporary:
        root = Path(temporary)
        home = root / "home"
        home.mkdir()
        dsh_home = root / "dsh"
        env = _clean_env(home)
        env["DSH_HOME"] = str(dsh_home)
        process = None
        try:
            process = subprocess.run(
                command + ["--profile", profile, "--from-default-profile", "headless", "--dump-config"],
                cwd=str(root), env=env, shell=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40)
        except (OSError, subprocess.TimeoutExpired):
            raise InstallError(
                "PROFILE_INIT_FAILED",
                "The installed dsh could not initialize the official headless template.", 3) from None
        if process.returncode != 0:
            raise InstallError(
                "PROFILE_INIT_FAILED",
                "The installed dsh could not initialize the official headless template.", 3)
        profile_dir = dsh_home / "profiles" / profile
        values: dict[str, bytes] = {}
        for name in PROFILE_FILES:
            path = profile_dir / name
            if not _is_regular(path):
                raise InstallError(
                    "PROFILE_TEMPLATE_INCOMPLETE",
                    "The official headless template did not create all expected files.", 3)
            values[name] = path.read_bytes()
        try:
            manifest = json.loads(values["package.json"].decode("utf-8"))
            bundles = manifest["dsh"]["profile"]["bundles"]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            raise InstallError(
                "PROFILE_TEMPLATE_INVALID",
                "The generated profile manifest is not the official headless template.", 3) from None
        if bundles != HEADLESS_BUNDLES:
            raise InstallError(
                "PROFILE_TEMPLATE_MISMATCH",
                "The installed dsh headless template no longer matches the supported bundle list.", 3)
        values["cordis.patch.yml"] = MODEL_PATCH.encode("utf-8")
        return values


def _prepare_config(config_home: Path, bridge: BridgePlan) -> ConfigPlan:
    dsh_home = Path(bridge.new["dsh_home"])
    log_dir = Path(bridge.new["log_dir"])
    for path, label in ((dsh_home, "dsh_home"), (log_dir, "log_dir")):
        if path.exists() and not path.is_dir():
            raise InstallError("BRIDGE_PATH_INVALID", f"{label} is not a directory.", 2)
    plan = ConfigPlan(config_home=config_home, dsh_home=dsh_home,
                      log_dir=log_dir, bridge=bridge)
    profiles_root = dsh_home / "profiles"
    if profiles_root.exists() and not profiles_root.is_dir():
        raise InstallError("PROFILE_ROOT_INVALID", "The profiles path is not a directory.", 2)
    for name in PROFILE_NAMES:
        directory = profiles_root / name
        if directory.exists():
            if not directory.is_dir():
                raise InstallError("PROFILE_PATH_INVALID", f"Profile {name} is not a directory.", 2)
            plan.preserved_profiles.append(name)
            continue
        values = _official_profile(bridge.command, name)
        plan.profiles.append(ProfilePlan(name=name, directory=directory, files=values))
    max_patch = dsh_home / "patches" / "max.yml"
    if max_patch.exists():
        if not _is_regular(max_patch):
            raise InstallError("PATCH_PATH_INVALID", "patches/max.yml is not a regular file.", 2)
    else:
        plan.max_patch = max_patch
        plan.max_patch_bytes = MAX_PATCH.encode("utf-8")
    return plan


def _ensure_dir(path: Path, mode: int) -> None:
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    if not path.is_dir():
        raise InstallError("DIRECTORY_INVALID", "A required path is not a directory.", 5)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    _ensure_dir(path.parent, 0o755)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _write_new_file(path: Path, data: bytes, mode: int) -> None:
    _ensure_dir(path.parent, 0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        raise InstallError("FILE_EXISTS", "A file appeared during installation.", 5) from None
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, mode)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


class BackupSession:
    def __init__(self, config_home: Path):
        self.config_home = config_home
        self.root: Path | None = None
        self.records: list[dict[str, str]] = []

    def _ensure(self) -> Path:
        if self.root is None:
            _ensure_dir(self.config_home, 0o700)
            backups = self.config_home / "backups"
            _ensure_dir(backups, 0o700)
            self.root = backups / f"install-{_utc_stamp()}-{secrets.token_hex(3)}"
            self.root.mkdir(mode=0o700)
        return self.root

    def backup_file(self, path: Path, label: str) -> Path | None:
        if not path.exists():
            return None
        if path.is_symlink() or not _is_regular(path):
            raise InstallError("BACKUP_UNSAFE", "A file to back up is not a regular file.", 5)
        destination = self._ensure() / label
        _ensure_dir(destination.parent, 0o700)
        shutil.copy2(path, destination)
        self.records.append({"path": str(path), "backup": str(destination)})
        return destination

    def backup_tree(self, path: Path, label: str) -> Path | None:
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_dir():
            raise InstallError("BACKUP_UNSAFE", "The skill destination is not a safe directory.", 5)
        destination = self._ensure() / label
        _ensure_dir(destination.parent, 0o700)
        shutil.copytree(path, destination, symlinks=True)
        self.records.append({"path": str(path), "backup": str(destination)})
        return destination

    def write_manifest(self, payload: dict[str, Any]) -> None:
        if self.root is None:
            return
        data = {"schema_version": 1, "created_at": _now_iso(),
                "records": self.records, **payload}
        _atomic_write(self.root / "manifest.json",
                      (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                      0o600)


class SkillReplacement:
    def __init__(self, dest: Path, source: dict[str, bytes], modes: dict[str, int],
                 previous_managed: set[str], update: bool):
        self.dest = dest
        self.source = source
        self.modes = modes
        self.previous_managed = previous_managed
        self.update = update
        self.staging: Path | None = None
        self.old_dir: Path | None = None
        self.committed = False
        self.changed_files: list[str] = []
        self.removed_files: list[str] = []

    def _target(self, rel: str) -> Path:
        if rel.startswith("/") or rel.startswith("../") or "/../" in rel or rel == "..":
            raise InstallError("SOURCE_PATH_INVALID", "An unsafe source path was rejected.", 5)
        assert self.staging is not None
        target = self.staging / rel
        if not _inside(self.staging, target):
            raise InstallError("SOURCE_PATH_INVALID", "A source path escaped the staging directory.", 5)
        return target

    def _replace_path(self, target: Path, data: bytes, mode: int) -> None:
        parent = target.parent
        if not _inside(self.staging, parent):
            raise InstallError("SYMLINK_ESCAPE", "A staging parent escapes through a symlink.", 5)
        _ensure_dir(parent, 0o755)
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)
        _atomic_write(target, data, mode)
        self.changed_files.append(target.relative_to(self.staging).as_posix())

    def prepare(self) -> bool:
        _ensure_dir(self.dest.parent, 0o755)
        root_mode = (stat.S_IMODE(self.dest.stat().st_mode)
                     if self.dest.exists() and self.dest.is_dir() and not self.dest.is_symlink()
                     else 0o755)
        staging = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}.staging-", dir=str(self.dest.parent)))
        self.staging = staging
        try:
            if self.dest.exists():
                if self.dest.is_symlink() or not self.dest.is_dir():
                    raise InstallError("DEST_INVALID", "The skill destination is not a safe directory.", 2)
                shutil.copytree(self.dest, staging, symlinks=True, dirs_exist_ok=True)
            changed = not self.dest.exists()
            for rel, data in sorted(self.source.items()):
                target = self._target(rel)
                if not target.exists() or target.is_symlink() or not target.is_file():
                    changed = True
                else:
                    try:
                        changed = changed or _sha256_file(target) != _sha256_bytes(data)
                    except InstallError:
                        changed = True
                if (not target.exists() or target.is_symlink() or not target.is_file() or
                        _sha256_file(target) != _sha256_bytes(data)):
                    self._replace_path(target, data, self.modes[rel])
            for rel in sorted(self.previous_managed - set(self.source)):
                target = self._target(rel)
                if not target.exists() and not target.is_symlink():
                    continue
                changed = True
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    raise InstallError("MANAGED_PATH_INVALID",
                                       "A removed managed path became a directory; refusing to delete it.", 5)
                self.removed_files.append(rel)
            if not changed:
                shutil.rmtree(staging, ignore_errors=True)
                self.staging = None
                return False
            os.chmod(staging, root_mode)
            return True
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            self.staging = None
            raise

    def commit(self, backups: BackupSession) -> None:
        if self.staging is None:
            return
        if self.dest.exists():
            backups.backup_tree(self.dest, "skill")
            self.old_dir = self.dest.parent / f".{self.dest.name}.old-{secrets.token_hex(4)}"
            if self.old_dir.exists():
                raise InstallError("TRANSACTION_FAILED", "A temporary rollback path already exists.", 5)
            os.replace(self.dest, self.old_dir)
        try:
            os.replace(self.staging, self.dest)
        except BaseException as error:
            if self.old_dir is not None and not self.dest.exists():
                try:
                    os.replace(self.old_dir, self.dest)
                    self.old_dir = None
                except OSError:
                    pass
            if isinstance(error, OSError):
                raise InstallError(
                    "TRANSACTION_FAILED",
                    "The staged skill could not replace the destination.", 5) from None
            raise
        self.staging = None
        self.committed = True

    def finalize(self) -> None:
        if self.old_dir is not None and self.old_dir.exists():
            shutil.rmtree(self.old_dir, ignore_errors=True)
        self.old_dir = None

    def rollback(self) -> None:
        if self.committed:
            if self.dest.exists():
                if self.dest.is_symlink() or not self.dest.is_dir():
                    raise OSError("rollback destination changed")
                else:
                    shutil.rmtree(self.dest)
            if self.old_dir is not None and self.old_dir.exists():
                os.replace(self.old_dir, self.dest)
                self.old_dir = None
            self.committed = False
        elif self.staging is not None:
            shutil.rmtree(self.staging)
            self.staging = None


@dataclass
class GlobalPlan:
    path: Path
    old: bytes | None
    new: bytes
    mode: int
    label: str


@dataclass
class GlobalResult:
    status: str
    plans: list[GlobalPlan] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)


def _template(root: Path, relative: str) -> str:
    path = root / relative
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise InstallError("TEMPLATE_MISSING", f"Missing public template {relative}.", 5) from None
    return text


def _render(text: str, dest: Path, config_home: Path) -> str:
    return (text.replace("{{SKILL_MD}}", str(dest / "SKILL.md"))
                .replace("{{RUN_DSH}}", str(dest / "scripts" / "run_dsh.py"))
                .replace("{{BRIDGE_JSON}}", str(config_home / "bridge.json"))
                .replace("{{SKILL_DIR}}", str(dest))
                .replace("{{CONFIG_HOME}}", str(config_home)))


def _find_toml_value(text: str, key: str) -> tuple[int, int] | None:
    pattern = re.compile(rf"(?m)^{re.escape(key)}[ \t]*=")
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        return None
    start = matches[0].start()
    index = matches[0].end()
    while index < len(text) and text[index] in " \t":
        index += 1
    if text.startswith('"""', index):
        end = text.find('"""', index + 3)
        if end < 0:
            return None
        end += 3
    elif text.startswith("'''", index):
        end = text.find("'''", index + 3)
        if end < 0:
            return None
        end += 3
    elif index < len(text) and text[index] == '"':
        cursor = index + 1
        escaped = False
        while cursor < len(text):
            char = text[cursor]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                cursor += 1
                break
            cursor += 1
        else:
            return None
        end = cursor
    else:
        end = text.find("\n", index)
        if end < 0:
            end = len(text)
    return start, end


def _replace_toml_value(text: str, key: str, block: str) -> str:
    found = _find_toml_value(text, key)
    if found is None:
        return text.rstrip("\n") + "\n\n" + block + "\n"
    start, end = found
    return text[:start] + block + text[end:]


def _launcher_recognized(text: str) -> bool:
    return (LAUNCHER_MARKER.split(";")[0] in text or
            any(signature in text for signature in LEGACY_LAUNCHER_SIGNATURES))


def _prepare_global(root: Path, dest: Path, config_home: Path) -> GlobalResult:
    codex_home = Path.home() / ".codex"
    plans: list[GlobalPlan] = []
    warnings: list[str] = []
    agents_path = codex_home / "AGENTS.md"
    block = _render(_template(root, "examples/global-agents.block.md"), dest, config_home).rstrip("\n") + "\n"
    old_agents: bytes | None = None
    if agents_path.exists():
        if agents_path.is_symlink() or not _is_regular(agents_path):
            raise InstallError("GLOBAL_AGENTS_UNSAFE",
                               "The global AGENTS.md is not a regular file; no global routing was changed.", 4)
        old_agents = agents_path.read_bytes()
        try:
            existing = old_agents.decode("utf-8")
        except UnicodeDecodeError:
            raise InstallError("GLOBAL_AGENTS_ENCODING",
                               "The global AGENTS.md is not UTF-8; no global routing was changed.", 4) from None
        new_agents = _merge_agents(existing, block)
    else:
        new_agents = block
    if old_agents != new_agents.encode("utf-8"):
        plans.append(GlobalPlan(agents_path, old_agents, new_agents.encode("utf-8"),
                                stat.S_IMODE(agents_path.stat().st_mode) if old_agents is not None else 0o644,
                                "global/AGENTS.md"))
    launchers = codex_home / "agents"
    for role in ("default", "explorer", "worker"):
        path = launchers / f"{role}.toml"
        template = _render(_template(root, f"examples/launchers/{role}.toml"), dest, config_home)
        old: bytes | None = None
        if path.exists():
            if path.is_symlink() or not _is_regular(path):
                warnings.append(f"Skipped unsafe launcher path {path.name}.")
                continue
            old = path.read_bytes()
            try:
                existing = old.decode("utf-8")
            except UnicodeDecodeError:
                warnings.append(f"Skipped non-UTF-8 launcher {path.name}.")
                continue
            if not _launcher_recognized(existing):
                warnings.append(f"Preserved unrecognized launcher {path.name}; update it manually.")
                continue
            description = _template_value(template, "description")
            instructions = _template_value(template, "developer_instructions")
            if description is None or instructions is None:
                raise InstallError("TEMPLATE_INVALID", "A launcher template is missing managed fields.", 5)
            updated = _replace_toml_value(existing, "description", description)
            updated = _replace_toml_value(updated, "developer_instructions", instructions)
            if LAUNCHER_MARKER.split(";")[0] not in updated:
                updated = LAUNCHER_MARKER + updated
            new = updated
        else:
            new = template
        encoded = new.encode("utf-8")
        if old != encoded:
            plans.append(GlobalPlan(path, old, encoded,
                                    stat.S_IMODE(path.stat().st_mode) if old is not None else 0o644,
                                    f"global/launchers/{role}.toml"))
    status = "PARTIAL" if warnings else "PASS"
    return GlobalResult(status=status, plans=plans, warnings=warnings)


def _template_value(template: str, key: str) -> str | None:
    found = _find_toml_value(template, key)
    if found is None:
        return None
    start, end = found
    return template[start:end]


def _merge_agents(existing: str, block: str) -> str:
    starts = [match.start() for match in re.finditer(re.escape(AGENTS_BEGIN), existing)]
    ends = [match.start() for match in re.finditer(re.escape(AGENTS_END), existing)]
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
            raise InstallError("GLOBAL_AGENTS_MARKERS",
                               "The global AGENTS.md has ambiguous managed markers; no changes were made.", 4)
        end = ends[0] + len(AGENTS_END)
        while end < len(existing) and existing[end] in "\r\n":
            end += 1
        return existing[:starts[0]] + block + existing[end:]
    if LEGACY_AGENTS_TITLE in existing:
        return _migrate_legacy_agents(existing, block)
    if not existing.strip():
        return block
    return existing.rstrip("\n") + "\n\n" + block


def _migrate_legacy_agents(existing: str, block: str) -> str:
    lines = existing.splitlines(keepends=True)
    header_index = next((index for index, line in enumerate(lines)
                         if line.strip() == LEGACY_AGENTS_TITLE), None)
    if header_index is None:
        return existing.rstrip("\n") + "\n\n" + block
    footer_index = next((index for index, line in enumerate(lines)
                         if line.strip().startswith(LEGACY_AGENTS_FOOTER)), None)
    if footer_index is None or footer_index <= header_index:
        raise InstallError(
            "GLOBAL_AGENTS_LEGACY_AMBIGUOUS",
            "A legacy global AGENTS.md title was found, but its managed extent is ambiguous; "
            "the file was not changed. Remove the legacy section manually or add managed markers.", 4)
    section = "".join(lines[header_index:footer_index + 1])
    section = re.sub(r"`[^`\n]+/codex-subagents-dsh/SKILL\.md`", "`{{SKILL_MD}}`", section)
    section = re.sub(r"`[^`\n]+/bridge\.json`", "`{{BRIDGE_JSON}}`", section)
    canonical = "\n".join(line.strip() for line in section.splitlines() if line.strip())
    if _sha256_bytes(canonical.encode("utf-8")) != LEGACY_AGENTS_SHA256:
        raise InstallError(
            "GLOBAL_AGENTS_LEGACY_AMBIGUOUS",
            "The legacy global AGENTS.md contains unrecognized instructions inside the old section; "
            "the file was not changed. Add managed markers manually after reviewing it.", 4)
    return "".join(lines[:header_index]) + block + "".join(lines[footer_index + 1:])


def _restore_global(undo: list[tuple[Path, bytes | None, int | None]]) -> bool:
    restored = True
    for path, old, mode in reversed(undo):
        try:
            if old is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, old, mode if mode is not None else 0o644)
        except OSError:
            restored = False
    return restored


def _rollback_failure() -> InstallError:
    return InstallError(
        "ROLLBACK_INCOMPLETE",
        "Installation failed and rollback could not restore every path. "
        "Preserve the backups under config-home/backups and inspect the affected installation.", 5)


def _apply_global(result: GlobalResult, backups: BackupSession) -> list[tuple[Path, bytes | None, int | None]]:
    undo: list[tuple[Path, bytes | None, int | None]] = []
    try:
        for plan in result.plans:
            if plan.old is not None:
                backups.backup_file(plan.path, plan.label)
                old_mode = stat.S_IMODE(plan.path.stat().st_mode)
            else:
                old_mode = None
            _atomic_write(plan.path, plan.new, plan.mode)
            undo.append((plan.path, plan.old, old_mode))
            result.applied.append(str(plan.path))
        return undo
    except BaseException:
        if not _restore_global(undo):
            raise _rollback_failure() from None
        raise


def _apply_config(plan: ConfigPlan, backups: BackupSession) -> Callable[[], bool]:
    undo: list[Callable[[], None]] = []
    try:
        _ensure_dir(plan.config_home, 0o700)
        if plan.dsh_home != plan.config_home:
            _ensure_dir(plan.dsh_home, 0o700)
        _ensure_dir(plan.log_dir, 0o700)
        bridge = plan.bridge
        if bridge.changed:
            if bridge.old_bytes is not None:
                backups.backup_file(bridge.path, "config/bridge.json")
            _atomic_write(bridge.path, bridge.new_bytes, 0o600)

            def restore_bridge() -> None:
                if bridge.old_bytes is None:
                    bridge.path.unlink(missing_ok=True)
                else:
                    _atomic_write(bridge.path, bridge.old_bytes, 0o600)
            undo.append(restore_bridge)
        for profile in plan.profiles:
            _ensure_dir(profile.directory.parent, 0o700)
            created = False
            try:
                profile.directory.mkdir(mode=0o700)
                created = True
            except FileExistsError:
                if not profile.directory.is_dir():
                    raise InstallError("PROFILE_PATH_INVALID", "A profile path became invalid.", 5)
            if not created:
                continue
            try:
                for name, data in profile.files.items():
                    _atomic_write(profile.directory / name, data, 0o644)
            except BaseException:
                try:
                    shutil.rmtree(profile.directory)
                except OSError:
                    raise _rollback_failure() from None
                raise
            undo.append(lambda directory=profile.directory: shutil.rmtree(directory))
        if plan.max_patch is not None and plan.max_patch_bytes is not None:
            _write_new_file(plan.max_patch, plan.max_patch_bytes, 0o644)
            undo.append(lambda path=plan.max_patch: path.unlink(missing_ok=True))
        return lambda: _rollback_actions(undo)
    except BaseException:
        if not _rollback_actions(undo):
            raise _rollback_failure() from None
        raise


def _rollback_actions(undo: Iterable[Callable[[], None]]) -> bool:
    restored = True
    for action in reversed(list(undo)):
        try:
            action()
        except OSError:
            restored = False
    return restored


def _receipt_payload(dest: Path, config_home: Path, source: dict[str, bytes],
                     modes: dict[str, int], bridge: BridgePlan,
                     backups: BackupSession, global_result: GlobalResult | None,
                     adopted: bool, initialized: list[str], preserved: list[str],
                     removed: list[str]) -> dict[str, Any]:
    files = {}
    for rel, data in sorted(source.items()):
        digest = _sha256_bytes(data)
        files[rel] = {"sha256": digest, "source_sha256": digest,
                      "installed_sha256": digest, "size": len(data)}
    tree_digest = hashlib.sha256()
    for rel in sorted(files):
        tree_digest.update(rel.encode("utf-8") + b"\0" +
                           files[rel]["sha256"].encode("ascii") + b"\n")
    return {
        "schema_version": 1,
        "installer_version": VERSION,
        "installed_at": _now_iso(),
        "skill": {
            "name": SKILL_NAME,
            "dest": str(dest),
            "source_tree_sha256": tree_digest.hexdigest(),
            "files": files,
            "modes": {rel: modes[rel] for rel in sorted(modes)},
        },
        "config": {
            "home": str(config_home),
            "bridge": str(bridge.path),
            "bridge_schema_version": 2,
            "bridge_sha256": _sha256_bytes(bridge.new_bytes),
        },
        "backups": backups.records,
        "backup_root": str(backups.root) if backups.root is not None else None,
        "global_routing": {
            "enabled": global_result is not None,
            "status": global_result.status if global_result is not None else "DISABLED",
            "warnings": global_result.warnings if global_result is not None else [],
            "applied": global_result.applied if global_result is not None else [],
        },
        "profiles": {"initialized": initialized, "preserved": preserved},
        "files_removed": removed,
        "adopted_without_receipt": adopted,
    }


def _report_base(dest: Path, config_home: Path, bridge_path: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "installer_version": VERSION,
        "status": "FAIL",
        "dest": str(dest),
        "config_home": str(config_home),
        "bridge_path": str(bridge_path),
        "error": None,
        "warnings": [],
        "backups": [],
        "global_routing": {"status": "DISABLED", "warnings": [], "applied": []},
    }


def install(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if os.name != "posix":
        raise InstallError("PLATFORM_NOT_SUPPORTED",
                           "This installer supports Linux/POSIX only; the Windows runtime is not verified.", 2)
    dest, config_home = _effective_paths(arguments)
    bridge_path = config_home / "bridge.json"
    source_root = Path(__file__).resolve().parent.parent
    report = _report_base(dest, config_home, bridge_path)
    if dest == config_home:
        raise InstallError("DEST_INVALID", "The skill destination must differ from config-home.", 2)
    for path, label in ((dest, "dest"), (config_home, "config-home")):
        if path.exists() and not path.is_dir():
            raise InstallError("PATH_INVALID", f"{label} is not a directory.", 2)
        if path.is_symlink():
            raise InstallError("PATH_INVALID", f"{label} must not be a symlink.", 2)
    source, modes = _collect_source(source_root)
    _reject_dotenv_in_destination(dest)
    explicit = None
    if arguments.dsh_command_json is not None:
        try:
            explicit = json.loads(arguments.dsh_command_json)
        except ValueError:
            raise InstallError("DSH_COMMAND_INVALID", "--dsh-command-json is not valid JSON.", 2) from None
    bridge = _normalize_bridge(bridge_path, config_home, arguments.migrate_progress, explicit)
    receipt_path = config_home / "install-receipt.json"
    receipt = _load_receipt(receipt_path)
    adopted = False
    previous_managed: set[str] = set()
    if receipt is not None and receipt.get("skill", {}).get("dest") == str(dest):
        for rel, metadata in receipt["skill"]["files"].items():
            if not isinstance(rel, str) or not isinstance(metadata, dict) or not isinstance(metadata.get("sha256"), str):
                receipt = None
                break
            previous_managed.add(rel)
    else:
        receipt = None
    if receipt is None and dest.exists():
        adopted = True
    conflicts: list[str] = []
    if dest.exists():
        for rel in sorted(source):
            target = dest / rel
            if not target.exists() and not target.is_symlink():
                continue
            if target.is_symlink() or not target.is_file():
                conflicts.append(rel)
                continue
            try:
                current = _sha256_file(target)
            except InstallError:
                conflicts.append(rel)
                continue
            if receipt is not None and rel in previous_managed:
                # A clean file tracked by the receipt may receive a source update.
                if current != receipt["skill"]["files"][rel].get("sha256"):
                    conflicts.append(rel)
            elif current != _sha256_bytes(source[rel]):
                # No receipt, or a new managed path colliding with user content.
                conflicts.append(rel)
        for rel in sorted(previous_managed - set(source)):
            target = dest / rel
            if not target.exists() and not target.is_symlink():
                continue
            if target.is_symlink() or not target.is_file():
                conflicts.append(rel)
                continue
            try:
                current = _sha256_file(target)
            except InstallError:
                conflicts.append(rel)
                continue
            if current != receipt["skill"]["files"][rel].get("sha256"):
                conflicts.append(rel)
    if conflicts:
        unique = sorted(set(conflicts))
        if not arguments.update:
            raise InstallError(
                "SKILL_UPDATE_CONFLICT",
                "Existing managed or conflicting files differ from the source or receipt: "
                + ", ".join(unique) + ". Re-run with --update to back up and replace them.", 4)
    config_plan = _prepare_config(config_home, bridge)
    global_result = None
    if arguments.enable_global_routing:
        global_result = _prepare_global(source_root, dest, config_home)
    skill = SkillReplacement(dest, source, modes, previous_managed, arguments.update)
    has_skill_changes = skill.prepare()
    backups = BackupSession(config_home)
    config_undo: Callable[[], bool] = lambda: True
    global_undo: list[tuple[Path, bytes | None, int | None]] = []
    try:
        skill.commit(backups)
        config_undo = _apply_config(config_plan, backups)
        if global_result is not None:
            global_undo = _apply_global(global_result, backups)
        initialized = [profile.name for profile in config_plan.profiles]
        receipt_payload = _receipt_payload(
            dest, config_home, source, modes, bridge, backups, global_result,
            adopted, initialized, config_plan.preserved_profiles, skill.removed_files)
        _atomic_write(receipt_path, (json.dumps(receipt_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), 0o600)
    except BaseException as error:
        restored = _restore_global(global_undo)
        restored = config_undo() and restored
        try:
            skill.rollback()
        except OSError:
            restored = False
        if not restored:
            error = _rollback_failure()
        elif not isinstance(error, InstallError):
            error = InstallError("TRANSACTION_FAILED",
                                 "Installation failed and the previous state was restored.", 5)
        error.backup_root = str(backups.root) if backups.root is not None else None
        error.backups = backups.records
        raise error from None
    skill.finalize()
    report.update({
        "status": "PARTIAL" if (global_result is not None and global_result.status == "PARTIAL") else "PASS",
        "bridge_schema_version": 2,
        "bridge_changed": bridge.changed,
        "bridge_migrated": bridge.migrated,
        "dsh_command": bridge.command,
        "profiles_initialized": [profile.name for profile in config_plan.profiles],
        "profiles_preserved": config_plan.preserved_profiles,
        "skill_changed": has_skill_changes,
        "files_installed": sorted(source) if has_skill_changes else [],
        "files_removed": skill.removed_files,
        "files_unchanged": not has_skill_changes,
        "adopted_without_receipt": adopted,
        "backups": backups.records,
        "backup_root": str(backups.root) if backups.root is not None else None,
        "receipt": str(receipt_path),
        "global_routing": {
            "status": global_result.status if global_result is not None else "DISABLED",
            "warnings": global_result.warnings if global_result is not None else [],
            "applied": global_result.applied if global_result is not None else [],
        },
    })
    if global_result is not None:
        report["warnings"].extend(global_result.warnings)
    try:
        backups.write_manifest({
            "status": report["status"],
            "dest": str(dest),
            "config_home": str(config_home),
            "installer_version": VERSION,
        })
    except OSError:
        # The install and receipt already succeeded; backup manifest is metadata.
        report["warnings"].append("The backup manifest could not be written.")
    return report, 0


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(description=__doc__,
                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dest", help="skill destination (default: ~/.agents/skills/codex-subagents-dsh)")
    parser.add_argument("--config-home", help="private config directory (default: ~/.dsh-codex)")
    parser.add_argument("--dsh-command-json",
                        help='JSON array [absolute node, absolute @deepseek-ai/dsh entry]')
    parser.add_argument("--update", action="store_true",
                        help="authorize replacing files that differ from the receipt or source")
    parser.add_argument("--migrate-progress", action="store_true",
                        help="explicitly migrate bridge schema 1 to schema 2")
    parser.add_argument("--enable-global-routing", action="store_true",
                        help="update the managed global AGENTS.md block and three launcher files")
    parser.add_argument("--json", action="store_true", help="print one machine-readable summary")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    report: dict[str, Any]
    try:
        report, code = install(arguments)
    except InstallError as error:
        dest, config_home = _effective_paths(arguments)
        report = _report_base(dest, config_home, config_home / "bridge.json")
        report["error"] = {"code": error.code, "message": error.message}
        report["backup_root"] = getattr(error, "backup_root", None)
        report["backups"] = getattr(error, "backups", [])
        code = error.exit_code
    except Exception:
        dest, config_home = _effective_paths(arguments)
        report = _report_base(dest, config_home, config_home / "bridge.json")
        report["error"] = {"code": "INTERNAL_ERROR",
                           "message": "An unexpected installer error occurred; no raw environment was reported."}
        code = 5
    if arguments.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if report.get("error"):
            print(f"FAIL {report['error']['code']}: {report['error']['message']}", file=sys.stderr)
        else:
            print(f"{report['status']} codex-subagents-dsh {VERSION}")
            print(f"  skill:  {report['dest']}")
            print(f"  config: {report['bridge_path']}")
            print(f"  dsh:    {' '.join(report.get('dsh_command', []))}")
            print(f"  global: {report['global_routing']['status']}")
            for warning in report.get("warnings", []):
                print(f"  warning: {warning}", file=sys.stderr)
            if report.get("backup_root"):
                print(f"  backup: {report['backup_root']}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
