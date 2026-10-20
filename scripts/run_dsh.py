#!/usr/bin/env python3
"""Bounded, secret-minimizing launcher for an independent dsh headless process.

Python 3.10+, standard library only. Public helpers for doctor/tests:
  build_parser(), prepare(namespace) -> Prepared (validation only),
  execute(namespace) -> JSON-serializable result, ProtocolParser.feed(line),
  ProtocolParser.finish(exit_code) -> diagnostic or None.

Configuration schema 1 keeps the old explicit ``timeout_seconds`` hard budget.
Schema 2 adds ``hard_timeout_seconds`` (default null: no total-duration kill)
and supervision timings. ``--timeout`` is always an explicit hard budget.

Exit codes: 0 Harness completed; 2 configuration; 3 launch; 4 authentication;
5 protocol; 6 timeout; 7 interruption; 8 Harness failure; 9 size limit;
10 wrapper-known session selection mismatch; 11 local I/O; 12 cancellation;
13 cleanup/finalization issue (any captured answer is preserved).

Installed protocol reference: @deepseek-ai/dsh-headless 0.2.1-rc.2,
lib/json-stream-BA-F3lfb.js and lib/index.js. Raw task, stderr, tool inputs,
tool results, reasoning and intermediate text are NEVER written to wrapper
logs or status files. The bounded final answer is returned to the caller, not
saved in logs. This is collection minimization, not a claim that arbitrary
final answers or Harness's own durable session storage can be universally
secret-redacted.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
import supervision as sup  # noqa: E402  (same directory; script/direct import)

EXIT_CODES = {"config": 2, "launch": 3, "authentication": 4, "protocol": 5,
              "timeout": 6, "interrupted": 7, "harness": 8, "limit": 9,
              "session": 10, "io": 11, "cancelled": 12, "cleanup": 13}
LIMIT_DEFAULTS = {"max_task_bytes": 1024 * 1024,
                  "max_stdout_bytes": 16 * 1024 * 1024,
                  "max_stderr_bytes": 4 * 1024 * 1024,
                  "max_line_bytes": 1024 * 1024,
                  "max_answer_bytes": 256 * 1024,
                  "max_log_bytes": 2 * 1024 * 1024,
                  "max_events": 100000}
SUPERVISION_DEFAULTS = {"heartbeat_seconds": 5.0,
                        "stall_warning_seconds": 300.0,
                        "shutdown_grace_seconds": 10.0,
                        "finalization_grace_seconds": 10.0}
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SAFE_TOOL = re.compile(r"[A-Za-z0-9_./:-]{1,128}\Z")
_CONTROL_CHECK_INTERVAL = 0.5
_MAX_DIAGNOSTICS = 32


def diagnostic(category: str, code: str) -> dict[str, str]:
    """Only static codes enter diagnostics; never attach exception messages."""
    messages = {
        "config": "Invalid launcher configuration, arguments, or input paths.",
        "launch": "The configured Harness process could not be started.",
        "authentication": "Harness reported a missing or rejected credential.",
        "protocol": "Harness output did not form a complete compatible event stream.",
        "timeout": "The execution deadline expired; owned processes were stopped.",
        "interrupted": "Execution was interrupted; owned processes were stopped.",
        "harness": "Harness reported an unsuccessful run.",
        "limit": "A configured size or event limit was exceeded; output is incomplete.",
        "session": "Saved wrapper session selection does not match this invocation.",
        "io": "A required local read, write, operation, or supervision channel failed.",
        "cancelled": "Execution was cancelled by request; owned processes were stopped.",
        "cleanup": "Owned-process finalization or cleanup did not complete cleanly; any captured result is preserved.",
    }
    try:
        message = messages[category]
    except KeyError:
        category, message = "io", messages["io"]
    return {"category": category, "code": code, "message": message}


def observation_warning(code: str, silent_seconds: float) -> dict[str, Any]:
    """A non-failure observation: no semantic progress is inferred."""
    return {"category": "unknown", "code": code, "severity": "warning",
            "message": ("No allowlisted NDJSON event was observed before the stall "
                        "threshold. This is an observation warning only; the same "
                        "process continues and no semantic progress is inferred."),
            "silent_seconds": round(max(0.0, silent_seconds), 3)}


class BridgeError(Exception):
    def __init__(self, category: str, code: str):
        self.diagnostic = diagnostic(category, code)
        super().__init__(code)


class Interrupted(Exception):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BridgeError("config", "ARGUMENT_INVALID")


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--task-file", required=True, help="UTF-8 file, or - for stdin")
    parser.add_argument("--profile")
    parser.add_argument("--patch", action="append", default=[])
    parser.add_argument("--session-id")
    parser.add_argument("--timeout", type=str,
                        help="explicit hard execution budget in positive seconds")
    parser.add_argument("--status-file",
                        help="absolute path for a new 0600 bounded status JSON file")
    return parser


def _absolute(value: Any, code: str, *, strict: bool = True) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise BridgeError("config", code)
    path = Path(value)
    if not path.is_absolute():
        raise BridgeError("config", code)
    try:
        return path.resolve(strict=strict)
    except (OSError, RuntimeError):
        raise BridgeError("config", code) from None


def _identifier(value: Any) -> bool:
    # Official SessionId is an opaque branded string, not a UUID. Preserve
    # compatible Unicode/spaces while bounding retained protocol identifiers.
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= 4096
    except UnicodeEncodeError:
        return False


def _positive_number(value: Any, code: str) -> float:
    if isinstance(value, bool):
        raise BridgeError("config", code)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise BridgeError("config", code) from None
    if not math.isfinite(number) or number <= 0:
        raise BridgeError("config", code)
    return number


def _hard_timeout(config: dict[str, Any], args: argparse.Namespace,
                  schema_version: int) -> float | None:
    if args.timeout is not None:
        return _positive_number(args.timeout, "TIMEOUT_INVALID")
    if schema_version == 1:
        if "timeout_seconds" not in config:
            raise BridgeError("config", "TIMEOUT_INVALID")
        return _positive_number(config.get("timeout_seconds"), "TIMEOUT_INVALID")
    # Schema 2: timeout_seconds is a schema-1 field and is intentionally not
    # reinterpreted as a total-duration kill. Only hard_timeout_seconds or the
    # explicit --timeout controls a hard budget.
    if "hard_timeout_seconds" not in config or config.get("hard_timeout_seconds") is None:
        return None
    return _positive_number(config.get("hard_timeout_seconds"), "HARD_TIMEOUT_INVALID")


def _supervision_settings(config: dict[str, Any]) -> dict[str, float]:
    settings = dict(SUPERVISION_DEFAULTS)
    for key in settings:
        if key in config:
            settings[key] = _positive_number(config[key], "SUPERVISION_SETTING_INVALID")
    return settings


def _read_limited(path: Path, limit: int, code: str) -> bytes:
    try:
        with path.open("rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise BridgeError("config", code)
            data = handle.read(limit + 1)
    except OSError:
        raise BridgeError("config", code) from None
    if len(data) > limit:
        raise BridgeError("limit", "INPUT_SIZE_LIMIT")
    return data


def _json_no_constants(value: str) -> None:
    raise ValueError("non-finite JSON number")


def _json_object(data: bytes, code: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), parse_constant=_json_no_constants)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise BridgeError("config", code) from None
    if not isinstance(value, dict):
        raise BridgeError("config", code)
    return value


def source_hashes(config_path: Path, profile_dir: Path,
                  patches: list[Path]) -> dict[str, str]:
    """Read source bytes only for hashing; cap file count and total bytes."""
    paths = [config_path] + patches
    try:
        for directory, dirs, files in os.walk(profile_dir, followlinks=False):
            dirs.sort()
            for name in sorted(files):
                path = Path(directory) / name
                if not path.is_file():
                    raise BridgeError("config", "PROFILE_SOURCE_INVALID")
                paths.append(path)
                if len(paths) > 2048:
                    raise BridgeError("limit", "SOURCE_FILE_COUNT_LIMIT")
        result: dict[str, str] = {}
        total = 0
        for path in paths:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise BridgeError("config", "CONFIG_SOURCE_INVALID")
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 64 * 1024 * 1024:
                        raise BridgeError("limit", "SOURCE_SIZE_LIMIT")
                    digest.update(chunk)
            result[str(path)] = digest.hexdigest()
        return result
    except OSError:
        raise BridgeError("io", "SOURCE_HASH_FAILED") from None


@dataclass
class Prepared:
    config_path: Path
    config: dict[str, Any]
    cwd: Path
    dsh_home: Path
    log_root: Path
    profile: str
    patches: list[Path]
    session_id: str | None
    timeout: float | None
    task: bytes
    command: list[str]
    env: dict[str, str]
    limits: dict[str, int]
    source_hashes: dict[str, str]
    resume_metadata: str
    session_metadata_path: Path | None
    schema_version: int = 1
    heartbeat_seconds: float = 5.0
    stall_warning_seconds: float = 300.0
    shutdown_grace_seconds: float = 10.0
    finalization_grace_seconds: float = 10.0
    status_file: Path | None = None

    def selection(self) -> dict[str, Any]:
        return {"dsh_home": str(self.dsh_home), "cwd": str(self.cwd),
                "profile": self.profile, "patches": [str(p) for p in self.patches]}


def prepare(args: argparse.Namespace) -> Prepared:
    """Validate without starting Harness or modifying a profile."""
    try:
        config_path = Path(args.config).expanduser().resolve(strict=True)
        cwd = Path(args.cwd).expanduser().resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        raise BridgeError("config", "CONFIG_OR_CWD_INVALID") from None
    if not cwd.is_dir() or not os.access(cwd, os.R_OK | os.X_OK):
        raise BridgeError("config", "CWD_INACCESSIBLE")
    config = _json_object(_read_limited(config_path, 1024 * 1024, "CONFIG_UNREADABLE"),
                          "CONFIG_JSON_INVALID")
    schema_version = config.get("schema_version")
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise BridgeError("config", "SCHEMA_VERSION_UNSUPPORTED")
    limits = dict(LIMIT_DEFAULTS)
    for key in limits:
        if key in config:
            value = config[key]
            if type(value) is not int or not 1 <= value <= 1024 * 1024 * 1024:
                raise BridgeError("config", "SIZE_LIMIT_INVALID")
            limits[key] = value
    dsh_home = _absolute(config.get("dsh_home"), "DSH_HOME_INVALID")
    if not dsh_home.is_dir() or not os.access(dsh_home, os.R_OK | os.W_OK | os.X_OK):
        raise BridgeError("config", "DSH_HOME_INACCESSIBLE")
    log_root = _absolute(config.get("log_dir"), "LOG_DIR_INVALID", strict=False)
    if log_root.exists() and not log_root.is_dir():
        raise BridgeError("config", "LOG_DIR_INVALID")
    command = config.get("dsh_command")
    if (not isinstance(command, list) or not command or
            any(not isinstance(part, str) or not part or "\x00" in part for part in command)):
        raise BridgeError("config", "DSH_COMMAND_INVALID")
    entrypoint = _absolute(command[0], "DSH_ENTRYPOINT_INVALID")
    if not entrypoint.is_file() or not os.access(entrypoint, os.X_OK):
        raise BridgeError("config", "DSH_ENTRYPOINT_NOT_EXECUTABLE")
    command = list(command)
    command[0] = str(entrypoint)
    profile = args.profile if args.profile is not None else config.get("default_profile")
    if not isinstance(profile, str) or not _SAFE_NAME.fullmatch(profile):
        raise BridgeError("config", "PROFILE_NAME_INVALID")
    profile_dir = dsh_home / "profiles" / profile
    if not profile_dir.is_dir() or not os.access(profile_dir, os.R_OK | os.X_OK):
        raise BridgeError("config", "PROFILE_NOT_FOUND")
    defaults = config.get("default_patches")
    if not isinstance(defaults, list):
        raise BridgeError("config", "DEFAULT_PATCHES_INVALID")
    patches = []
    for item in defaults + list(args.patch or []):
        patch = _absolute(item, "PATCH_PATH_INVALID")
        if not patch.is_file() or not os.access(patch, os.R_OK):
            raise BridgeError("config", "PATCH_UNREADABLE")
        patches.append(patch)
    timeout = _hard_timeout(config, args, schema_version)
    supervision = _supervision_settings(config)
    status_file = None
    if getattr(args, "status_file", None) is not None:
        try:
            status_file = sup.validate_status_destination(args.status_file)
        except sup.SupervisionError as exc:
            raise BridgeError(exc.category, exc.code) from None
    session_id = args.session_id
    if session_id is not None and not _identifier(session_id):
        raise BridgeError("config", "SESSION_ID_INVALID")
    if args.task_file == "-":
        try:
            task = sys.stdin.buffer.read(limits["max_task_bytes"] + 1)
        except (OSError, AttributeError):
            raise BridgeError("config", "STDIN_UNREADABLE") from None
        if len(task) > limits["max_task_bytes"]:
            raise BridgeError("limit", "TASK_SIZE_LIMIT")
    else:
        try:
            task_path = Path(args.task_file).expanduser().resolve(strict=True)
        except (OSError, ValueError, RuntimeError):
            raise BridgeError("config", "TASK_FILE_INVALID") from None
        task = _read_limited(task_path, limits["max_task_bytes"], "TASK_FILE_UNREADABLE")
    try:
        task_text = task.decode("utf-8")
    except UnicodeDecodeError:
        raise BridgeError("config", "TASK_UTF8_INVALID") from None
    if not task_text.strip():
        raise BridgeError("config", "TASK_EMPTY")
    env = dict(os.environ)
    env["DSH_HOME"] = str(dsh_home)
    launch = command + ["--profile", profile]
    for patch in patches:
        launch += ["--patch", str(patch)]
    launch += ["--json"]
    if session_id is not None:
        launch += ["--session-id", session_id]
    launch += ["-"]
    hashes = source_hashes(config_path, profile_dir, patches)
    metadata_path = None
    resume_metadata = "not_requested"
    if session_id is not None:
        metadata_path = (dsh_home / ".codex-dsh-wrapper" / "sessions" /
                         (hashlib.sha256(session_id.encode()).hexdigest() + ".json"))
        if metadata_path.exists():
            saved = _json_object(_read_limited(metadata_path, 1024 * 1024,
                                               "SESSION_METADATA_UNREADABLE"),
                                 "SESSION_METADATA_INVALID")
            selection = {"dsh_home": str(dsh_home), "cwd": str(cwd),
                         "profile": profile, "patches": [str(p) for p in patches]}
            if saved.get("session_id") != session_id or saved.get("selection") != selection:
                raise BridgeError("session", "SESSION_SELECTION_MISMATCH")
            resume_metadata = "matched_official_check_required"
        else:
            resume_metadata = "not_found_official_check_required"
    return Prepared(config_path, config, cwd, dsh_home, log_root, profile,
                    patches, session_id, timeout, task, launch, env, limits,
                    hashes, resume_metadata, metadata_path,
                    schema_version=schema_version,
                    heartbeat_seconds=supervision["heartbeat_seconds"],
                    stall_warning_seconds=supervision["stall_warning_seconds"],
                    shutdown_grace_seconds=supervision["shutdown_grace_seconds"],
                    finalization_grace_seconds=supervision["finalization_grace_seconds"],
                    status_file=status_file)


def _classify_text(value: Any) -> dict[str, str] | None:
    """Inspect bounded text transiently. Return static categories, never raw text."""
    if not isinstance(value, str):
        return None
    lowered = value.lower()
    if (re.search(r"\b401\b|unauthenticated|unauthorized|authentication.failed", lowered)
            or re.search(r"(?:missing|invalid|empty|unset|not.set|not.configured|requires?).{0,80}(?:api.?key|credential)", lowered)
            or re.search(r"(?:api.?key|credential).{0,80}(?:missing|invalid|empty|unset|not.set|not.configured|not.found|required)", lowered)):
        return diagnostic("authentication", "AUTHENTICATION_FAILED")
    return None


class ProtocolParser:
    """Ordered NDJSON state machine for the installed official headless schema.

    No tool input/result/text/thinking is retained. feed returns an allowlisted
    event summary for logs. Sequential completed turns are supported because
    the official driver may await child-agent followups before finalization.
    """
    def __init__(self, expected_session_id: str | None = None,
                 expected_cwd: str | None = None,
                 max_answer_bytes: int = 256 * 1024):
        self.expected_session_id = expected_session_id
        self.expected_cwd = expected_cwd
        self.max_answer_bytes = max_answer_bytes
        self.session_id: str | None = None
        self.answer = ""
        self.final_text_accepted = False
        self.turn_end: dict[str, Any] | None = None
        self.tool_errors: list[dict[str, Any]] = []
        self.tool_counts: dict[str, int] = {}
        self.event_counts: dict[str, int] = {}
        self.error: dict[str, str] | None = None
        self.harness_error: dict[str, str] | None = None
        self.turn_error: dict[str, str] | None = None
        self._step_text = ""
        self._step_text_bytes = 0
        self._step_has_text = False
        self._step_text_complete = True
        self._last_text = ""
        self._last_text_complete = True
        self.answer_consistency = "not_checked"
        self.upstream_truncated = False
        self.final_seen = False
        self.events = 0
        self.active_turn: int | None = None
        self.last_turn: int | None = None
        self.active_step: int | None = None
        self.turns_completed = 0
        self.calls: dict[str, str] = {}
        self.seen_calls: set[str] = set()

    def _fail(self, code: str, category: str = "protocol") -> None:
        if self.error is None:
            self.error = diagnostic(category, code)

    def _reset_step_text(self) -> None:
        self._step_text = ""
        self._step_text_bytes = 0
        self._step_has_text = False
        self._step_text_complete = True

    def _commit_step_text(self) -> None:
        # summarize() selects the last nonempty assistant message. The
        # projection emits each message's text blocks contiguously in its
        # step. Keep at most the configured answer bound and never log it.
        if self._step_has_text:
            self._last_text = self._step_text
            self._last_text_complete = self._step_text_complete

    def feed(self, line: str) -> dict[str, Any]:
        self.events += 1
        try:
            event = json.loads(line, parse_constant=_json_no_constants)
        except (ValueError, RecursionError):
            self._fail("INVALID_JSON")
            return {"type": "invalid_json"}
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            self._fail("EVENT_SHAPE_INVALID")
            return {"type": "invalid_event"}
        kind = event["type"]
        known = {"session", "status", "thinking", "text", "tool_call", "tool_result", "error", "final"}
        if kind not in known:
            self._fail("EVENT_TYPE_UNSUPPORTED")
            return {"type": "unsupported_event"}
        self.event_counts[kind] = self.event_counts.get(kind, 0) + 1
        summary: dict[str, Any] = {"type": kind, "index": self.events}
        if self.final_seen:
            self._fail("EVENT_AFTER_FINAL")
            return summary
        truncated = event.get("truncated") is True
        if truncated:
            self.upstream_truncated = True
            summary["upstream_truncated"] = True
        if kind == "error":
            self.harness_error = (_classify_text(event.get("message")) or
                                  diagnostic("harness", "HARNESS_ERROR_EVENT"))
            summary["error_category"] = self.harness_error["category"]
            return summary
        if kind == "session":
            sid = event.get("sessionId")
            if self.session_id is not None or self.events != 1:
                self._fail("SESSION_ORDER_INVALID")
            if not _identifier(sid):
                self._fail("SESSION_ID_INVALID")
            else:
                self.session_id = sid
                summary["session_id"] = sid
                if self.expected_session_id is not None and sid != self.expected_session_id:
                    self._fail("SESSION_ID_MISMATCH")
            cwd = event.get("cwd")
            if not isinstance(cwd, str) or not os.path.isabs(cwd):
                self._fail("SESSION_CWD_INVALID")
            elif self.expected_cwd is not None and os.path.realpath(cwd) != os.path.realpath(self.expected_cwd):
                self._fail("SESSION_CWD_MISMATCH")
            if truncated:
                self._fail("SESSION_TRUNCATED")
            return summary
        if self.session_id is None:
            self._fail("SESSION_MISSING")
            return summary
        if kind == "status":
            phase = event.get("phase")
            turn = event.get("turn")
            if not isinstance(phase, str) or phase not in {"turn_start", "turn_end", "step_start", "step_end"}:
                self._fail("STATUS_PHASE_INVALID")
                return summary
            summary["phase"] = phase
            if type(turn) is not int or turn < 0:
                self._fail("TURN_ID_INVALID")
                return summary
            summary["turn"] = turn
            if truncated:
                self._fail("STATUS_TRUNCATED")
            if phase == "turn_start":
                if self.active_turn is not None or (self.last_turn is not None and turn <= self.last_turn):
                    self._fail("TURN_START_ORDER_INVALID")
                self.active_turn = turn
                self.active_step = None
                self._reset_step_text()
            elif self.active_turn != turn:
                self._fail("TURN_ID_MISMATCH")
            elif phase == "turn_end":
                reason = event.get("reason")
                reason_kind = reason.get("kind") if isinstance(reason, dict) else reason
                if not isinstance(reason_kind, str) or reason_kind not in {
                        "completed", "error", "blocked", "max-tokens", "forked",
                        "cancelled", "canceled", "aborted", "interrupted", "stopped"}:
                    self._fail("TURN_REASON_INVALID")
                    reason_kind = "unknown"
                if reason_kind == "completed" and self.active_step is not None:
                    self._fail("STEP_NOT_ENDED")
                self._commit_step_text()
                if reason_kind == "completed" and self.calls:
                    self._fail("TOOL_RESULT_MISSING")
                self.turn_end = {"turn": turn, "reason": {"kind": reason_kind}}
                summary["reason"] = {"kind": reason_kind}
                if reason_kind != "completed":
                    raw_error = reason.get("error") if isinstance(reason, dict) else None
                    raw_message = raw_error.get("message") if isinstance(raw_error, dict) else None
                    raw_code = raw_error.get("code") if isinstance(raw_error, dict) else None
                    auth_code = raw_code in {"AUTH", "AUTHENTICATION", "AUTHENTICATION_ERROR", "UNAUTHORIZED", "INVALID_API_KEY"} if isinstance(raw_code, str) else False
                    self.turn_error = (diagnostic("authentication", "AUTHENTICATION_FAILED") if auth_code else
                                       _classify_text(raw_message) or diagnostic("harness", "TURN_NOT_COMPLETED"))
                    self.turn_end["error"] = self.turn_error
                else:
                    self.turn_error = None
                self.last_turn = turn
                self.active_turn = None
                self.turns_completed += 1
            else:
                step = event.get("step")
                if type(step) is not int or step < 0:
                    self._fail("STEP_ID_INVALID")
                elif phase == "step_start":
                    if self.active_step is not None:
                        self._fail("STEP_START_ORDER_INVALID")
                    self.active_step = step
                    self._reset_step_text()
                elif self.active_step != step:
                    self._fail("STEP_ID_MISMATCH")
                else:
                    self._commit_step_text()
                    self.active_step = None
            return summary
        if kind == "final":
            self.final_seen = True
            if self.active_turn is not None or self.turn_end is None:
                self._fail("FINAL_ORDER_INVALID")
            text = event.get("text")
            if not isinstance(text, str):
                self._fail("FINAL_TEXT_INVALID")
            elif any(0xD800 <= ord(char) <= 0xDFFF for char in text):
                self._fail("FINAL_TEXT_UTF8_INVALID")
            elif len(text.encode("utf-8")) > self.max_answer_bytes:
                self._fail("ANSWER_SIZE_LIMIT", "limit")
            else:
                self.answer = text
                self.final_text_accepted = True
                summary["answer_bytes"] = len(text.encode("utf-8"))
                if self._last_text_complete:
                    self.answer_consistency = "matched" if text == self._last_text else "mismatched"
                    if text != self._last_text and self.turn_end is not None and self.turn_end["reason"]["kind"] == "completed":
                        self._fail("FINAL_TEXT_MISMATCH")
                else:
                    self.answer_consistency = "unverified_upstream_truncation_or_intermediate_size"
                summary["answer_consistency"] = self.answer_consistency
            if truncated:
                self._fail("FINAL_TRUNCATED")
            return summary
        if self.active_turn is None:
            self._fail("EVENT_OUTSIDE_TURN")
            return summary
        if kind in {"text", "thinking"}:
            text = event.get("text")
            if not isinstance(text, str):
                self._fail("TEXT_EVENT_INVALID")
            elif any(0xD800 <= ord(char) <= 0xDFFF for char in text):
                self._fail("TEXT_EVENT_UTF8_INVALID")
            elif kind == "text":
                self._step_has_text = self._step_has_text or bool(text)
                self._step_text_bytes += len(text.encode("utf-8"))
                if truncated or self._step_text_bytes > self.max_answer_bytes:
                    self._step_text_complete = False
                    self._step_text = ""
                elif self._step_text_complete:
                    self._step_text += text
            return summary
        call_id = event.get("callId")
        if not _identifier(call_id):
            self._fail("TOOL_CALL_ID_INVALID")
            return summary
        summary["call_ref"] = hashlib.sha256(call_id.encode()).hexdigest()[:16]
        if kind == "tool_call":
            tool = event.get("tool")
            if not isinstance(tool, str) or not _SAFE_TOOL.fullmatch(tool):
                self._fail("TOOL_NAME_INVALID")
                return summary
            if call_id in self.seen_calls:
                self._fail("TOOL_CALL_DUPLICATE")
            self.seen_calls.add(call_id)
            self.calls[call_id] = tool
            self.tool_counts[tool] = self.tool_counts.get(tool, 0) + 1
            summary["tool"] = tool
        else:
            tool = self.calls.pop(call_id, None)
            if tool is None:
                self._fail("TOOL_RESULT_WITHOUT_CALL")
            status = event.get("status")
            if not isinstance(status, str) or status not in {"completed", "error"}:
                self._fail("TOOL_RESULT_STATUS_INVALID")
            else:
                summary["status"] = status
                summary["tool"] = tool
                if status == "error":
                    self.tool_errors.append({"tool": tool, "call_ref": summary["call_ref"],
                                             "status": "error"})
            # Upstream may truncate tool input/result text. Their required
            # identifiers and actual status are validated above; raw text is
            # intentionally irrelevant to execution-layer completion.
        return summary

    def finish(self, process_exit_code: int) -> dict[str, str] | None:
        if self.error is not None:
            return self.error
        if self.harness_error is not None:
            return self.harness_error
        if self.turn_error is not None:
            return self.turn_error
        if process_exit_code != 0:
            if self.turn_end is not None and self.turn_end["reason"]["kind"] == "completed" and self.final_seen:
                return diagnostic("protocol", "COMPLETED_EXIT_INCONSISTENT")
            return diagnostic("harness", "HARNESS_PROCESS_NONZERO")
        if (self.session_id is None or not self.final_seen or self.active_turn is not None
                or self.turn_end is None or self.turn_end["reason"]["kind"] != "completed"):
            return diagnostic("protocol", "STREAM_INCOMPLETE")
        return None


class BoundedLog:
    """Bounded event rotation plus bounded summary documents.

    Log retention is deliberately decoupled from task execution: exhausting a
    log budget sets degradation markers and drops retention, but never changes
    the task's exit category or stops a healthy process.
    """
    def __init__(self, root: Path, limit: int):
        self.limit = max(1, int(limit))
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix=time.strftime("run-%Y%m%dT%H%M%S-"), dir=root))
        os.chmod(self.path, 0o700)
        self.event_budget = self.limit // 2
        self.document_budget = self.limit - self.event_budget
        self._per_event_file = max(1, self.event_budget // 2) if self.event_budget else 0
        self._event_file_used = 0
        self._document_used = 0
        self._event_handle = None
        self._event_name = "events.current.ndjson"
        self._closed = False
        self._summary_written = False
        self.degraded = False
        self.truncated = False
        self.rotations = 0
        self.dropped_events = 0
        self.diagnostics: list[str] = []
        self._open_event_file()

    @staticmethod
    def _encoded(data: Any) -> bytes:
        return (json.dumps(data, ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")

    def _degrade(self, code: str) -> None:
        if code not in self.diagnostics:
            self.diagnostics.append(code)
        self.degraded = True

    def _open_event_file(self) -> None:
        self._event_handle = None
        self._event_file_used = 0
        if self.event_budget <= 0 or self._per_event_file <= 0:
            self._degrade("LOG_SIZE_LIMIT")
            return
        try:
            fd = os.open(self.path / self._event_name,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            self._event_handle = os.fdopen(fd, "wb")
        except OSError:
            self._degrade("LOG_WRITE_FAILED")

    def _rotate(self) -> None:
        if self._event_handle is not None:
            try:
                self._event_handle.close()
            except OSError:
                pass
            self._event_handle = None
        current = self.path / self._event_name
        previous = self.path / "events.previous.ndjson"
        try:
            os.replace(current, previous)
        except OSError:
            self._degrade("LOG_WRITE_FAILED")
            return
        self.rotations += 1
        self.truncated = True
        self._degrade("LOG_TRUNCATED")
        self._open_event_file()

    def event(self, data: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            encoded = self._encoded(data)
        except (TypeError, ValueError, RecursionError):
            self._degrade("LOG_ENCODE_FAILED")
            return
        if self._event_handle is None:
            self.dropped_events += 1
            self.truncated = True
            return
        if len(encoded) > self._per_event_file:
            self.dropped_events += 1
            self.truncated = True
            self._degrade("LOG_EVENT_TOO_LARGE")
            return
        if self._event_file_used + len(encoded) > self._per_event_file:
            self._rotate()
            if self._event_handle is None:
                self.dropped_events += 1
                self.truncated = True
                return
        try:
            self._event_handle.write(encoded)
            self._event_handle.flush()
        except OSError:
            self._degrade("LOG_WRITE_FAILED")
            try:
                self._event_handle.close()
            except OSError:
                pass
            self._event_handle = None
            return
        self._event_file_used += len(encoded)

    def document(self, name: str, data: dict[str, Any]) -> bool:
        if Path(name).name != name or not name:
            self._degrade("LOG_NAME_INVALID")
            return False
        try:
            encoded = self._encoded(data)
        except (TypeError, ValueError, RecursionError):
            self._degrade("LOG_ENCODE_FAILED")
            return False
        if (self._document_used + len(encoded) > self.document_budget
                or len(encoded) > self.limit):
            self._degrade("LOG_DOCUMENT_LIMIT")
            return False
        destination = self.path / name
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            self._degrade("LOG_WRITE_FAILED")
            return False
        self._document_used += len(encoded)
        return True

    def close(self) -> None:
        if self._closed:
            return
        if self._event_handle is not None:
            try:
                self._event_handle.close()
            except OSError:
                pass
            self._event_handle = None
        self._closed = True
        if self.degraded and not self._summary_written:
            self._summary_written = True
            self.document("summary.json", {
                "schema_version": 1,
                "log_complete": False,
                "degraded": True,
                "truncated": self.truncated,
                "rotations": self.rotations,
                "dropped_events": self.dropped_events,
                "diagnostics": list(self.diagnostics),
            })

    @property
    def complete(self) -> bool:
        return not self.degraded


def _subreaper(enable: bool | int) -> int | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        previous = ctypes.c_int()
        if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
            return None
        if libc.prctl(36, int(enable), 0, 0, 0) != 0:
            return None
        return previous.value
    except (OSError, AttributeError):
        return None


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True


def _drain_streams(process: subprocess.Popen[bytes]) -> None:
    """Discard already-buffered cleanup output without retaining raw bytes."""
    for stream in (process.stdout, process.stderr):
        if stream is None or stream.closed:
            continue
        for _ in range(32):
            try:
                chunk = os.read(stream.fileno(), 65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            if not chunk:
                break


def _reap_owned(process: subprocess.Popen[bytes], timeout: float = 0.0) -> bool:
    """Reap direct/adopted ordinary children in the owned process group."""
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            child, status = os.waitpid(-process.pid, os.WNOHANG)
        except ChildProcessError:
            return True
        except OSError:
            return False
        if child == 0:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
            continue
        if child == process.pid and process.returncode is None:
            try:
                process.returncode = os.waitstatus_to_exitcode(status)
            except (ValueError, OSError):
                pass


def _cleanup_owned(process: subprocess.Popen[bytes] | None, grace: float,
                   on_wait: Any = None) -> dict[str, Any]:
    """Stop this run's process group/ordinary descendants with SIGTERM grace.

    ``process`` was started with start_new_session=True, so its PID is the
    owned process-group id. Escaped descendants that call setsid/setpgid are
    outside this guarantee, matching the launcher's existing ownership model.
    """
    info: dict[str, Any] = {
        "status": "ok", "signals": [], "direct_signaled": False,
        "group_signaled": False, "forced": False, "reaped": False,
        "diagnostics": [], "duration_seconds": 0.0,
    }
    started = time.monotonic()
    if process is None:
        info["status"] = "not_needed"
        return info
    try:
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
    except OSError:
        pass
    try:
        direct_alive = process.poll() is None
        group_alive = _group_exists(process.pid)
        if not direct_alive and not group_alive:
            info["reaped"] = process.returncode is not None
            info["duration_seconds"] = round(time.monotonic() - started, 6)
            return info

        term_sent = False
        if group_alive:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                term_sent = True
                info["group_signaled"] = True
            except ProcessLookupError:
                group_alive = False
            except OSError:
                info["diagnostics"].append("PROCESS_GROUP_SIGNAL_FAILED")
        if direct_alive:
            try:
                process.send_signal(signal.SIGTERM)
                info["direct_signaled"] = True
            except ProcessLookupError:
                direct_alive = False
            except OSError:
                info["diagnostics"].append("PROCESS_SIGNAL_FAILED")
        if term_sent or info["direct_signaled"]:
            info["signals"].append("SIGTERM")

        deadline = time.monotonic() + max(0.0, grace)
        last_wait = 0.0
        while time.monotonic() < deadline:
            _drain_streams(process)
            _reap_owned(process, 0.0)
            now = time.monotonic()
            if process.poll() is not None and not _group_exists(process.pid):
                break
            if on_wait is not None and now - last_wait >= 0.25:
                last_wait = now
                try:
                    on_wait()
                except Exception:
                    pass
            time.sleep(0.02)

        if process.poll() is None or _group_exists(process.pid):
            info["forced"] = True
            if _group_exists(process.pid):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    info["group_signaled"] = True
                except ProcessLookupError:
                    pass
                except OSError:
                    info["diagnostics"].append("PROCESS_GROUP_SIGNAL_FAILED")
            if process.poll() is None:
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    info["diagnostics"].append("PROCESS_SIGNAL_FAILED")
            if "SIGKILL" not in info["signals"]:
                info["signals"].append("SIGKILL")
            kill_deadline = time.monotonic() + 1.0
            while time.monotonic() < kill_deadline:
                _drain_streams(process)
                _reap_owned(process, 0.0)
                if process.poll() is not None and not _group_exists(process.pid):
                    break
                time.sleep(0.01)

        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            info["diagnostics"].append("PROCESS_REAP_FAILED")
        except OSError:
            info["diagnostics"].append("PROCESS_REAP_FAILED")
        _reap_owned(process, 0.25)
        info["reaped"] = process.returncode is not None
        if not info["reaped"]:
            if "PROCESS_REAP_FAILED" not in info["diagnostics"]:
                info["diagnostics"].append("PROCESS_REAP_FAILED")
        if _group_exists(process.pid):
            if "OWNED_PROCESS_GROUP_REMAINS" not in info["diagnostics"]:
                info["diagnostics"].append("OWNED_PROCESS_GROUP_REMAINS")
        if info["diagnostics"]:
            info["status"] = "failed"
        elif info["forced"]:
            info["status"] = "forced_kill"
        elif info["signals"]:
            info["status"] = "terminated_gracefully"
        else:
            info["status"] = "ok"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        info["status"] = "failed"
        if "CLEANUP_FAILED" not in info["diagnostics"]:
            info["diagnostics"].append("CLEANUP_FAILED")
        try:
            info["reaped"] = process.returncode is not None
        except (OSError, AttributeError):
            info["reaped"] = False
    info["duration_seconds"] = round(time.monotonic() - started, 6)
    _drain_streams(process)
    return info


def _save_session(prepared: Prepared, session_id: str) -> None:
    parent = prepared.dsh_home / ".codex-dsh-wrapper" / "sessions"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(parent.parent, 0o700)
    os.chmod(parent, 0o700)
    destination = parent / (hashlib.sha256(session_id.encode()).hexdigest() + ".json")
    payload = {"schema_version": 1, "session_id": session_id,
               "selection": prepared.selection(), "source_hashes": prepared.source_hashes}
    fd, temporary = tempfile.mkstemp(prefix=".session-", dir=parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _base_result() -> dict[str, Any]:
    return {
        "harness_completed": False, "process_exit_code": None,
        "wrapper_exit_code": None, "session_id": None, "profile": None,
        "patches": [], "answer": "", "turn_end": None, "tool_errors": [],
        "elapsed_seconds": 0.0, "log_dir": None, "error": None,
        "output_complete": False, "log_complete": False,
        "schema_version": None, "hard_timeout_seconds": None,
        "protocol_completed": False, "result_available": False,
        "natural_exit": None, "finalization": {"status": "not_started",
                                                "waited_seconds": 0.0,
                                                "timed_out": False},
        "cleanup": {"status": "not_run", "signals": [], "diagnostics": []},
        "supervision": None, "observation": _observation_boundary(),
        "status_file": None, "status_requested": False, "status_complete": None,
        "status_error": None, "control_error": None, "run_id": None,
        "cancellation": {"requested": False, "reason": None, "source": None,
                         "requested_at": None, "acted": False},
        "diagnostics": [], "log_degraded": False, "log_truncated": False,
        "log_dropped_events": 0, "log_diagnostics": [],
    }


def _observation_boundary() -> dict[str, Any]:
    return {
        "progress": "unknown",
        "basis": "allowlisted NDJSON lifecycle observations only",
        "heartbeat_is_progress": False,
        "process_liveness_is_progress": False,
        "cpu_activity_is_progress": False,
        "semantic_progress": "unknown",
        "tokens_realtime": False,
        "sdk_control": False,
        "raw_reasoning_or_tool_payload_retained": False,
        "note": ("A heartbeat, live PID, or CPU activity proves only that something is "
                 "alive. It is not task progress; no real-time token or SDK control "
                 "information is available in this headless interface."),
    }


def _new_run_state(prepared: Prepared) -> dict[str, Any]:
    return {
        "phase": "launching",
        "created_at": sup.iso_time(),
        "heartbeats": 0,
        "last_event_epoch": None,
        "last_event_mono": None,
        "last_tool_completed_epoch": None,
        "last_step_completed_epoch": None,
        "last_turn_completed_epoch": None,
        "stall": {"suspected": False, "active": False, "warning_count": 0,
                  "last_warning_at": None, "recovered_at": None,
                  "silent_seconds": 0.0,
                  "note": "observation only; the same process continues"},
        "cancellation": {"requested": False, "reason": None, "source": None,
                         "requested_at": None, "acted": False},
        "finalization": {"status": "not_started",
                         "grace_seconds": prepared.finalization_grace_seconds,
                         "waited_seconds": 0.0, "timed_out": False,
                         "started_at": None, "completed_at": None},
        "cleanup": {"status": "not_run", "signals": [], "forced": False,
                    "reaped": False, "direct_signaled": False,
                    "diagnostics": []},
        "diagnostics": [],
        "status_error": None,
        "control_error": None,
        "status_disabled": False,
        "status_dirty": False,
        "next_status_mono": 0.0,
        "next_control_mono": 0.0,
        "next_dirty_status_mono": 0.0,
        "last_publish_mono": 0.0,
        "control_checked": False,
        "error": None,
    }


def _safe_diagnostic(value: Any, severity: str | None = None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    category = value.get("category")
    code = value.get("code")
    message = value.get("message")
    if not isinstance(category, str) or not isinstance(code, str) or not isinstance(message, str):
        return None
    entry: dict[str, Any] = {"category": category[:64], "code": code[:96],
                             "message": message[:512],
                             "severity": severity or value.get("severity", "error")}
    if entry["severity"] not in {"warning", "error"}:
        entry["severity"] = "error"
    for key in ("tool", "call_ref"):
        item = value.get(key)
        if isinstance(item, str):
            entry[key] = item[:160]
    silent = value.get("silent_seconds")
    if isinstance(silent, (int, float)) and not isinstance(silent, bool):
        if math.isfinite(float(silent)):
            entry["silent_seconds"] = round(max(0.0, float(silent)), 3)
    return entry


def _append_diagnostic(state: dict[str, Any], value: Any,
                       severity: str | None = None) -> None:
    item = _safe_diagnostic(value, severity)
    if item is None:
        return
    key = (item["category"], item["code"], item["message"], item.get("tool"),
           item.get("call_ref"))
    existing = {(d["category"], d["code"], d["message"], d.get("tool"),
                 d.get("call_ref")) for d in state["diagnostics"]}
    if key not in existing:
        state["diagnostics"].append(item)
        if len(state["diagnostics"]) > _MAX_DIAGNOSTICS:
            del state["diagnostics"][0:len(state["diagnostics"]) - _MAX_DIAGNOSTICS]


def _active_tool_summary(parser: ProtocolParser | None) -> dict[str, Any]:
    if parser is None:
        return {"active_calls": 0, "distinct_tools": 0, "tools": [], "truncated": False}
    counts: dict[str, int] = {}
    for tool in parser.calls.values():
        counts[tool] = counts.get(tool, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    names = [{"tool": tool, "count": count} for tool, count in ordered[:32]]
    return {"active_calls": len(parser.calls), "distinct_tools": len(counts),
            "tools": names, "truncated": len(ordered) > 32}


def _tool_error_summaries(parser: ProtocolParser | None) -> list[dict[str, Any]]:
    if parser is None:
        return []
    result = []
    for entry in parser.tool_errors[:8]:
        item = {"category": "tool", "code": "TOOL_ERROR",
                "message": "A tool call completed with an error status.",
                "severity": "error"}
        tool = entry.get("tool")
        call_ref = entry.get("call_ref")
        if isinstance(tool, str):
            item["tool"] = tool[:160]
        if isinstance(call_ref, str):
            item["call_ref"] = call_ref[:160]
        result.append(item)
    return result


def _diagnostics_snapshot(state: dict[str, Any], parser: ProtocolParser | None,
                          primary: dict[str, Any] | None = None,
                          extras: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    values: list[Any] = list(state.get("diagnostics", []))
    for key in ("status_error", "control_error"):
        if state.get(key) is not None:
            values.append(state[key])
    if parser is not None:
        for value in (parser.harness_error, parser.turn_error):
            if value is not None:
                values.append(value)
        values.extend(_tool_error_summaries(parser))
    if extras:
        values.extend(extras)
    if primary is not None:
        values.append(primary)
    seen: set[tuple[Any, ...]] = set()
    result = []
    for value in values:
        item = _safe_diagnostic(value)
        if item is None:
            continue
        key = (item["category"], item["code"], item["message"], item.get("tool"),
               item.get("call_ref"))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= _MAX_DIAGNOSTICS:
            break
    return result


def _known_errors(state: dict[str, Any], parser: ProtocolParser | None,
                  terminal: dict[str, Any] | None) -> list[dict[str, Any]]:
    values = [item for item in _diagnostics_snapshot(state, parser)
              if item.get("severity") != "warning"]
    if terminal is not None and terminal.get("error") is not None:
        item = _safe_diagnostic(terminal["error"])
        if item is not None:
            values.append(item)
    seen: set[tuple[Any, ...]] = set()
    result = []
    for item in values:
        key = (item["category"], item["code"], item["message"], item.get("tool"),
               item.get("call_ref"))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= 16:
            break
    return result


def _running_terminal() -> dict[str, Any]:
    return {"state": "running", "protocol_completed": False,
            "result_available": False, "natural_exit": None,
            "harness_completed": False, "output_complete": False,
            "cleanup_status": "not_run", "error": None}


def _status_payload(prepared: Prepared, run_id: str, state: dict[str, Any],
                    parser: ProtocolParser | None,
                    supervisor_identity: dict[str, Any] | None,
                    child_identity: dict[str, Any] | None,
                    phase: str, terminal: dict[str, Any],
                    logs: BoundedLog | None) -> dict[str, Any]:
    now = time.time()
    stale = sup.stale_after_seconds(prepared.heartbeat_seconds)
    log_state = {
        "created": logs is not None,
        "degraded": bool(logs.degraded) if logs is not None else True,
        "truncated": bool(logs.truncated) if logs is not None else False,
        "dropped_events": logs.dropped_events if logs is not None else 0,
        "rotations": logs.rotations if logs is not None else 0,
    }
    return {
        "schema_version": sup.STATUS_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": state["created_at"],
        "heartbeat_at": sup.iso_time(now),
        "heartbeat_epoch": round(now, 6),
        "stale_after_seconds": stale,
        "phase": phase,
        "supervisor": supervisor_identity,
        "child": child_identity,
        "session_id": parser.session_id if parser is not None else None,
        "last_event_at": (sup.iso_time(state["last_event_epoch"])
                          if state.get("last_event_epoch") is not None else None),
        "last_tool_completed_at": (sup.iso_time(state["last_tool_completed_epoch"])
                                   if state.get("last_tool_completed_epoch") is not None else None),
        "last_step_completed_at": (sup.iso_time(state["last_step_completed_epoch"])
                                   if state.get("last_step_completed_epoch") is not None else None),
        "last_turn_completed_at": (sup.iso_time(state["last_turn_completed_epoch"])
                                   if state.get("last_turn_completed_epoch") is not None else None),
        "active_tools": _active_tool_summary(parser),
        "known_errors": _known_errors(state, parser, terminal),
        "diagnostics": _diagnostics_snapshot(state, parser, terminal.get("error")),
        "cancellation": state["cancellation"],
        "finalization": state["finalization"],
        "cleanup": state["cleanup"],
        "supervision": {
            "config_schema_version": prepared.schema_version,
            "hard_timeout_seconds": prepared.timeout,
            "heartbeat_seconds": prepared.heartbeat_seconds,
            "stall_warning_seconds": prepared.stall_warning_seconds,
            "shutdown_grace_seconds": prepared.shutdown_grace_seconds,
            "finalization_grace_seconds": prepared.finalization_grace_seconds,
            "heartbeats_published": state["heartbeats"],
            "stall": state["stall"],
            "progress_semantics": "unknown_liveness_only",
            "semantic_progress": False,
            "tokens_realtime": False,
            "sdk_control": False,
        },
        "observation": _observation_boundary(),
        "logs": log_state,
        "status_error": state.get("status_error"),
        "control_error": state.get("control_error"),
        "terminal": terminal,
    }


def _publish_status(publisher: sup.StatusPublisher | None, prepared: Prepared,
                    run_id: str, state: dict[str, Any], parser: ProtocolParser | None,
                    supervisor_identity: dict[str, Any] | None,
                    child_identity: dict[str, Any] | None,
                    phase: str, terminal: dict[str, Any],
                    logs: BoundedLog | None) -> bool:
    if publisher is None or state.get("status_disabled"):
        return False
    state["phase"] = phase
    try:
        payload = _status_payload(prepared, run_id, state, parser, supervisor_identity,
                                  child_identity, phase, terminal, logs)
    except (TypeError, ValueError, RuntimeError, RecursionError):
        error = diagnostic("io", "STATUS_PAYLOAD_INVALID")
        state["status_error"] = error
        state["status_disabled"] = True
        _append_diagnostic(state, error)
        return False
    try:
        publisher.publish(payload)
    except sup.SupervisionError as exc:
        error = diagnostic("io", exc.code)
        state["status_error"] = error
        state["status_disabled"] = True
        _append_diagnostic(state, error)
        return False
    state["heartbeats"] = int(state.get("heartbeats", 0)) + 1
    state["last_publish_mono"] = time.monotonic()
    state["status_dirty"] = False
    return True


def _capture_child_identity(process: subprocess.Popen[bytes]) -> dict[str, Any]:
    identity = sup.process_identity(process.pid)
    if identity is None:
        identity = {"pid": process.pid, "identity_kind": "linux_proc_starttime",
                    "boot_id": sup.boot_id(), "start_time_ticks": None,
                    "pgrp": None, "state": None, "alive": True}
    try:
        identity["pgid"] = os.getpgid(process.pid)
    except OSError:
        identity["pgid"] = process.pid
    identity["captured_at"] = sup.iso_time()
    return identity


def _supervise(prepared: Prepared, process: subprocess.Popen[bytes],
               parser: ProtocolParser, logs: BoundedLog | None,
               publisher: sup.StatusPublisher | None, run_id: str,
               state: dict[str, Any],
               supervisor_identity: dict[str, Any] | None,
               stop_signal: dict[str, int | None]) -> dict[str, Any]:
    """Stream, observe, warn, enforce only explicit budgets, then clean up.

    Silence/heartbeat/stall observations never fail or stop this process. The
    only automatic process stops here are an explicit hard budget, a caller
    signal, a validated run-scoped cancel request, a finalization pipe/exit
    bound, or a stream/resource failure that already ended the run.
    """
    started_mono = time.monotonic()
    hard_deadline = (started_mono + prepared.timeout) if prepared.timeout is not None else None
    state["phase"] = "running"
    state["last_event_mono"] = started_mono
    state["last_event_epoch"] = time.time()
    state["next_status_mono"] = started_mono + prepared.heartbeat_seconds
    state["next_control_mono"] = started_mono
    state["next_dirty_status_mono"] = started_mono
    child_identity = _capture_child_identity(process)
    primary: dict[str, Any] | None = None
    protocol_violation = False
    termination_started = False
    natural_exit: bool | None = None
    process_exit_mono: float | None = None
    process_exit_code: int | None = None
    finalization_started_mono: float | None = None
    finalization_deadline: float | None = None
    stdout_buffer = bytearray()
    received = {"stdout": 0, "stderr": 0}
    stdin_offset = 0
    stdout_eof = False
    stderr_eof = False
    stderr_auth = None
    selector = None
    finish_error: dict[str, str] | None = None
    output_complete = False

    def publish_now(phase: str | None = None) -> bool:
        return _publish_status(publisher, prepared, run_id, state, parser,
                               supervisor_identity, child_identity,
                               phase or state["phase"], _running_terminal(), logs)

    def note_event(now_mono: float) -> None:
        state["last_event_mono"] = now_mono
        state["last_event_epoch"] = time.time()
        state["status_dirty"] = True
        if state["stall"]["suspected"]:
            state["stall"]["suspected"] = False
            state["stall"]["active"] = False
            state["stall"]["recovered_at"] = sup.iso_time()
            state["next_dirty_status_mono"] = now_mono

    def consume_control() -> None:
        if state.get("control_checked") or prepared.status_file is None:
            return
        try:
            request = sup.read_control_request(prepared.status_file, run_id,
                                               supervisor_identity)
        except sup.SupervisionError as exc:
            error = diagnostic("io", exc.code)
            state["control_error"] = error
            state["control_checked"] = True
            _append_diagnostic(state, error)
            if logs is not None:
                logs.event({"type": "supervisor_control_error",
                            "code": exc.code, "severity": "error"})
            publish_now()
            return
        if request is None:
            return
        state["control_checked"] = True
        try:
            sup.remove_control_request(prepared.status_file)
        except sup.SupervisionError as exc:
            error = diagnostic("io", exc.code)
            state["control_error"] = error
            _append_diagnostic(state, error)
        requested_at = request.get("requested_at")
        if (not isinstance(requested_at, str) or len(requested_at) > 64
                or not re.fullmatch(r"[0-9T:.+Z-]+", requested_at)):
            requested_at = None
        state["cancellation"] = {
            "requested": True, "reason": "user_cancelled",
            "source": "run_id_scoped_control_file",
            "requested_at": requested_at,
            "acted": True,
        }
        state["phase"] = "stopping"
        state["next_dirty_status_mono"] = time.monotonic()
        if logs is not None:
            logs.event({"type": "supervisor_control", "action": "cancel",
                        "reason": "user_cancelled"})
        publish_now("stopping")

    def maintenance(now_mono: float, allow_control: bool = True) -> None:
        last_event = state.get("last_event_mono")
        if last_event is not None and not state["stall"]["suspected"]:
            silent = now_mono - last_event
            if silent >= prepared.stall_warning_seconds:
                warning = observation_warning("SUSPECTED_STALL", silent)
                state["stall"].update({
                    "suspected": True, "active": True,
                    "warning_count": int(state["stall"]["warning_count"]) + 1,
                    "last_warning_at": sup.iso_time(),
                    "silent_seconds": round(silent, 3),
                })
                _append_diagnostic(state, warning, severity="warning")
                if logs is not None:
                    logs.event({"type": "supervisor_warning",
                                "code": "SUSPECTED_STALL", "severity": "warning"})
                publish_now()
        if allow_control and prepared.status_file is not None:
            if now_mono >= state["next_control_mono"]:
                consume_control()
                state["next_control_mono"] = now_mono + _CONTROL_CHECK_INTERVAL
        if now_mono >= state["next_status_mono"]:
            publish_now()
            state["next_status_mono"] = now_mono + prepared.heartbeat_seconds
        if state.get("status_dirty") and now_mono >= state["next_dirty_status_mono"]:
            publish_now()
            state["next_dirty_status_mono"] = now_mono + max(
                0.2, min(1.0, prepared.heartbeat_seconds))

    try:
        selector = selectors.DefaultSelector()
        for stream, name, event in ((process.stdin, "stdin", selectors.EVENT_WRITE),
                                    (process.stdout, "stdout", selectors.EVENT_READ),
                                    (process.stderr, "stderr", selectors.EVENT_READ)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, event, name)
        publish_now("running")
        while True:
            now = time.monotonic()
            if stop_signal.get("signum") is not None:
                primary = diagnostic("interrupted", "EXECUTION_INTERRUPTED")
                termination_started = True
                state["phase"] = "stopping"
                publish_now("stopping")
                break
            if state["cancellation"]["requested"]:
                primary = diagnostic("cancelled", "USER_CANCELLED")
                termination_started = True
                state["phase"] = "stopping"
                publish_now("stopping")
                break
            if hard_deadline is not None and now >= hard_deadline:
                primary = diagnostic("timeout", "EXECUTION_TIMEOUT")
                termination_started = True
                state["phase"] = "stopping"
                publish_now("stopping")
                break
            if parser.final_seen and finalization_started_mono is None:
                finalization_started_mono = now
                finalization_deadline = now + prepared.finalization_grace_seconds
                state["finalization"].update({
                    "status": "waiting_for_exit",
                    "started_at": sup.iso_time(),
                    "grace_seconds": prepared.finalization_grace_seconds,
                })
                state["phase"] = "finalizing"
                publish_now("finalizing")
            code = process.poll()
            if code is not None and process_exit_mono is None:
                process_exit_mono = now
                process_exit_code = code
                natural_exit = not termination_started
                pipe_grace = min(1.0, prepared.finalization_grace_seconds)
                if finalization_started_mono is None:
                    finalization_started_mono = now
                    finalization_deadline = now + pipe_grace
                    state["finalization"].update({
                        "status": ("parent_exited_no_final" if not parser.final_seen
                                   else "waiting_for_pipes"),
                        "started_at": sup.iso_time(),
                    })
                else:
                    finalization_deadline = min(
                        finalization_deadline or (now + pipe_grace), now + pipe_grace)
                publish_now(state["phase"])
            if finalization_deadline is not None and now >= finalization_deadline:
                if process.poll() is None:
                    cleanup_diag = diagnostic("cleanup", "FINALIZATION_GRACE_EXPIRED")
                    state["finalization"]["status"] = "forced_cleanup"
                    state["finalization"]["timed_out"] = True
                    _append_diagnostic(state, cleanup_diag)
                    termination_started = True
                    state["phase"] = "stopping"
                    if primary is None:
                        primary = cleanup_diag
                    publish_now("stopping")
                    break
                if selector.get_map():
                    cleanup_diag = diagnostic("cleanup", "ORPHANED_DESCENDANT_PIPES")
                    state["finalization"]["status"] = "orphaned_pipes_cleanup"
                    state["finalization"]["timed_out"] = True
                    _append_diagnostic(state, cleanup_diag)
                    termination_started = True
                    state["phase"] = "stopping"
                    if primary is None:
                        primary = cleanup_diag
                    publish_now("stopping")
                    break
                state["finalization"]["status"] = (
                    "natural_exit" if parser.final_seen else "parent_exited_no_final")
                break
            if stdout_eof and not parser.final_seen and process.poll() is None:
                # EOF is final: no future final event can arrive on stdout.
                # A still-running child is therefore a broken protocol stream,
                # not ordinary event silence; stop it under the normal bounded
                # shutdown grace instead of waiting for an unrelated deadline.
                disconnect = diagnostic("protocol", "STDOUT_EOF_WITHOUT_FINAL")
                protocol_violation = True
                _append_diagnostic(state, disconnect)
                candidate = (parser.error or parser.harness_error
                             or parser.turn_error or disconnect)
                if (stderr_auth is not None
                        and candidate["category"] in {"harness", "protocol"}):
                    candidate = stderr_auth
                primary = candidate
                termination_started = True
                state["phase"] = "stopping"
                if logs is not None:
                    logs.event({"type": "supervisor_protocol_disconnect",
                                "code": "STDOUT_EOF_WITHOUT_FINAL",
                                "severity": "error"})
                publish_now("stopping")
                break
            if process.poll() is not None and not selector.get_map():
                break
            maintenance(now)
            if stop_signal.get("signum") is not None or state["cancellation"]["requested"]:
                continue
            now = time.monotonic()
            timeout = 0.5
            if hard_deadline is not None:
                timeout = min(timeout, max(0.0, hard_deadline - now))
            if finalization_deadline is not None:
                timeout = min(timeout, max(0.0, finalization_deadline - now))
            timeout = min(timeout, max(0.0, state["next_status_mono"] - now))
            if prepared.status_file is not None:
                # Without a status file there is no control request to poll and
                # next_control_mono never advances; using it here would pin the
                # selector timeout to zero and busy-spin.
                timeout = min(timeout, max(0.0, state["next_control_mono"] - now))
            if state.get("status_dirty"):
                timeout = min(timeout, max(0.0, state["next_dirty_status_mono"] - now))
            for key, mask in selector.select(timeout):
                stream = key.fileobj
                name = key.data
                if name == "stdin":
                    try:
                        written = os.write(stream.fileno(),
                                           prepared.task[stdin_offset:stdin_offset + 65536])
                        stdin_offset += written
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    except OSError:
                        raise BridgeError("io", "TASK_STDIN_WRITE_FAILED") from None
                    if stdin_offset >= len(prepared.task):
                        selector.unregister(stream)
                        stream.close()
                    continue
                try:
                    chunk = os.read(stream.fileno(), 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    raise BridgeError("io", "OUTPUT_READ_FAILED") from None
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    if name == "stdout":
                        stdout_eof = True
                    else:
                        stderr_eof = True
                    continue
                received[name] += len(chunk)
                if received[name] > prepared.limits["max_" + name + "_bytes"]:
                    raise BridgeError("limit", name.upper() + "_SIZE_LIMIT")
                if name == "stderr":
                    # Transient classification only; neither raw bytes nor a
                    # stderr tail is retained. Cross-chunk phrases may be
                    # unknown, never misreported as a clean authentication pass.
                    stderr_auth = stderr_auth or _classify_text(
                        chunk.decode("utf-8", errors="replace"))
                    continue
                stdout_buffer.extend(chunk)
                while True:
                    newline = stdout_buffer.find(b"\n")
                    if newline < 0:
                        if len(stdout_buffer) > prepared.limits["max_line_bytes"]:
                            raise BridgeError("limit", "LINE_SIZE_LIMIT")
                        break
                    if newline > prepared.limits["max_line_bytes"]:
                        raise BridgeError("limit", "LINE_SIZE_LIMIT")
                    raw_line = bytes(stdout_buffer[:newline])
                    del stdout_buffer[:newline + 1]
                    try:
                        line = raw_line.decode("utf-8")
                    except UnicodeDecodeError:
                        raise BridgeError("protocol", "OUTPUT_UTF8_INVALID") from None
                    if parser.events >= prepared.limits["max_events"]:
                        raise BridgeError("limit", "EVENT_COUNT_LIMIT")
                    summary = parser.feed(line)
                    note_event(time.monotonic())
                    if summary.get("type") == "tool_result":
                        state["last_tool_completed_epoch"] = time.time()
                    if summary.get("phase") == "step_end":
                        state["last_step_completed_epoch"] = time.time()
                    if summary.get("phase") == "turn_end":
                        state["last_turn_completed_epoch"] = time.time()
                    if logs is not None:
                        logs.event(summary)
                    if parser.error is not None:
                        raise BridgeError(parser.error["category"], parser.error["code"])
            if process.poll() is not None and not selector.get_map():
                break
    except BridgeError as exc:
        primary = exc.diagnostic
        if primary["category"] == "protocol":
            protocol_violation = True
        termination_started = termination_started or (process.poll() is None)
        state["phase"] = "stopping"
    except (KeyboardInterrupt, Interrupted):
        primary = diagnostic("interrupted", "EXECUTION_INTERRUPTED")
        termination_started = True
        state["phase"] = "stopping"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        primary = diagnostic("io", "LOCAL_OPERATION_FAILED")
        termination_started = termination_started or (process.poll() is None)
        state["phase"] = "stopping"
    finally:
        if parser.error is not None and primary is None:
            primary = parser.error
            if primary["category"] == "protocol":
                protocol_violation = True
        if process.poll() is not None and process_exit_mono is None:
            process_exit_mono = time.monotonic()
            process_exit_code = process.returncode
            natural_exit = not termination_started
        state["phase"] = "stopping" if (process.poll() is None or primary is not None) else "finalizing"
        publish_now(state["phase"])
        if selector is not None:
            try:
                selector.close()
            except OSError:
                pass
            selector = None
        cleanup_info = _cleanup_owned(
            process, prepared.shutdown_grace_seconds,
            on_wait=lambda: maintenance(time.monotonic(), allow_control=False))
        state["cleanup"] = {
            "status": cleanup_info["status"],
            "signals": list(cleanup_info["signals"]),
            "forced": bool(cleanup_info["forced"]),
            "reaped": bool(cleanup_info["reaped"]),
            "direct_signaled": bool(cleanup_info["direct_signaled"]),
            "duration_seconds": cleanup_info["duration_seconds"],
            "diagnostics": list(cleanup_info["diagnostics"]),
        }
        for code in cleanup_info["diagnostics"]:
            cleanup_diag = diagnostic("cleanup", code)
            _append_diagnostic(state, cleanup_diag)
            if primary is None:
                primary = cleanup_diag
        process_exit_code = process.returncode
        if process_exit_mono is None and process_exit_code is not None:
            process_exit_mono = time.monotonic()
            natural_exit = not termination_started
        if cleanup_info["direct_signaled"]:
            natural_exit = False
        elif natural_exit is None and not termination_started:
            natural_exit = process_exit_code is not None
        if process_exit_mono is not None:
            state["finalization"]["waited_seconds"] = round(
                max(0.0, process_exit_mono - finalization_started_mono), 6
            ) if finalization_started_mono is not None else 0.0
        state["finalization"]["completed_at"] = sup.iso_time()
        if state["finalization"]["status"] in {"waiting_for_exit", "waiting_for_pipes"}:
            if natural_exit:
                state["finalization"]["status"] = (
                    "natural_exit" if parser.final_seen else "parent_exited_no_final")
            else:
                state["finalization"]["status"] = "terminated"
        state["finalization"]["timed_out"] = bool(state["finalization"].get("timed_out"))
        # Result-preserving post-stop checks.
        if primary is None and stdout_buffer:
            primary = diagnostic("protocol", "UNTERMINATED_JSON_LINE")
            protocol_violation = True
        if process_exit_code is not None:
            finish_error = parser.finish(process_exit_code)
        if primary is None and finish_error is not None:
            candidate = finish_error
            if (stderr_auth is not None and candidate["category"] in {"harness", "protocol"}
                    and not termination_started):
                candidate = stderr_auth
            primary = candidate
            if primary["category"] == "protocol":
                protocol_violation = True
        if primary is None and stdin_offset != len(prepared.task):
            primary = diagnostic("protocol", "TASK_STDIN_INCOMPLETE")
            protocol_violation = True
        output_complete = bool(stdout_eof and stderr_eof and parser.error is None
                               and not stdout_buffer)
        state["error"] = primary
    return {
        "error": primary,
        "protocol_violation": protocol_violation,
        "stdout_eof": stdout_eof,
        "stderr_eof": stderr_eof,
        "stdout_buffer_pending": bool(stdout_buffer),
        "stdin_offset": stdin_offset,
        "received": dict(received),
        "natural_exit": natural_exit,
        "termination_started": termination_started,
        "finalization": state["finalization"],
        "cleanup": state["cleanup"],
        "process_exit_code": process.returncode,
        "finish_error": finish_error,
        "output_complete": output_complete,
        "child_identity": child_identity,
        "stderr_auth": stderr_auth,
    }


def _terminal_state(error: dict[str, Any] | None) -> str:
    if error is None:
        return "completed"
    return {"cancelled": "cancelled", "timeout": "timed_out",
            "interrupted": "interrupted", "cleanup": "cleanup_failed"}.get(
                error.get("category"), "failed")


def execute(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    result = _base_result()
    prepared: Prepared | None = None
    process: subprocess.Popen[bytes] | None = None
    logs: BoundedLog | None = None
    parser: ProtocolParser | None = None
    publisher: sup.StatusPublisher | None = None
    state: dict[str, Any] | None = None
    supervisor_identity: dict[str, Any] | None = None
    child_identity: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    old_signals: dict[int, Any] = {}
    old_subreaper = None
    stop_signal: dict[str, int | None] = {"signum": None}
    error: dict[str, Any] | None = None
    log_init_error = None
    supervision_completed = False
    run_id = None
    try:
        prepared = prepare(args)
        result.update({
            "profile": prepared.profile,
            "patches": [str(p) for p in prepared.patches],
            "resume_metadata": prepared.resume_metadata,
            "source_hashes_before": prepared.source_hashes,
            "configuration_selection": prepared.selection(),
            "process_cleanup": ("posix_process_group_and_linux_subreaper"
                                if sys.platform.startswith("linux")
                                else "posix_process_group"),
            "schema_version": prepared.schema_version,
            "hard_timeout_seconds": prepared.timeout,
            "status_file": str(prepared.status_file) if prepared.status_file else None,
            "status_requested": prepared.status_file is not None,
        })
        if os.name != "posix":
            raise BridgeError("config", "PLATFORM_NOT_VERIFIED")
        run_id = uuid.uuid4().hex
        result["run_id"] = run_id
        state = _new_run_state(prepared)
        supervisor_identity = sup.process_identity(os.getpid())
        if prepared.status_file is not None and supervisor_identity is None:
            raise BridgeError("io", "STATUS_IDENTITY_UNAVAILABLE")
        if prepared.status_file is not None:
            initial = _status_payload(prepared, run_id, state, None,
                                      supervisor_identity, None, "launching",
                                      _running_terminal(), None)
            try:
                publisher = sup.StatusPublisher(prepared.status_file, initial)
            except sup.SupervisionError as exc:
                status_error = diagnostic(
                    exc.category if exc.code in {
                        "STATUS_FILE_EXISTS", "STATUS_INIT_FAILED",
                        "STATUS_SYMLINK_REFUSED", "STATUS_PARENT_INACCESSIBLE"
                    } else "io", exc.code)
                state["status_error"] = status_error
                result["status_error"] = status_error
                result["status_complete"] = False
                raise BridgeError(status_error["category"], status_error["code"]) from None
        try:
            logs = BoundedLog(prepared.log_root, prepared.limits["max_log_bytes"])
            result["log_dir"] = str(logs.path)
        except (OSError, ValueError):
            logs = None
            log_init_error = "LOG_DIR_UNAVAILABLE"
            result["log_dir"] = None
        if logs is not None:
            logs.document("metadata.json", {
                "schema_version": 1,
                "run_id": run_id,
                "configuration_schema_version": prepared.schema_version,
                "selection": prepared.selection(),
                "resume_metadata": prepared.resume_metadata,
                "source_hashes_before": prepared.source_hashes,
                "timeout_seconds": (prepared.timeout
                                    if prepared.schema_version == 1 else None),
                "hard_timeout_seconds": prepared.timeout,
                "timeout_semantics": ("v1_explicit_timeout_seconds"
                                      if prepared.schema_version == 1
                                      else "v2_hard_timeout_seconds_or_cli_only"),
                "supervision": {
                    "heartbeat_seconds": prepared.heartbeat_seconds,
                    "stall_warning_seconds": prepared.stall_warning_seconds,
                    "shutdown_grace_seconds": prepared.shutdown_grace_seconds,
                    "finalization_grace_seconds": prepared.finalization_grace_seconds,
                },
                "limits": prepared.limits,
                "status_file_requested": prepared.status_file is not None,
                "log_policy": "bounded_rotation_allowlisted_summaries_no_raw_content",
            })
        parser = ProtocolParser(prepared.session_id, str(prepared.cwd),
                                prepared.limits["max_answer_bytes"])
        if threading.current_thread() is threading.main_thread():
            def on_signal(signum, frame):
                if stop_signal["signum"] is None:
                    stop_signal["signum"] = signum
            for signum in (signal.SIGTERM, signal.SIGINT):
                old_signals[signum] = signal.signal(signum, on_signal)
        old_subreaper = _subreaper(True)
        try:
            process = subprocess.Popen(prepared.command, cwd=prepared.cwd, env=prepared.env,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, shell=False,
                                       start_new_session=True, bufsize=0)
        except OSError:
            raise BridgeError("launch", "PROCESS_START_FAILED") from None
        outcome = _supervise(prepared, process, parser, logs, publisher, run_id,
                             state, supervisor_identity, stop_signal)
        supervision_completed = True
        error = outcome.get("error")
        child_identity = outcome.get("child_identity")
    except BridgeError as exc:
        error = exc.diagnostic
    except (KeyboardInterrupt, Interrupted):
        error = diagnostic("interrupted", "EXECUTION_INTERRUPTED")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        error = diagnostic("io", "LOCAL_OPERATION_FAILED")
    except Exception:
        # No exception text is retained.  The finally block still owns any
        # already-started child and emits one structured result line.
        error = diagnostic("io", "LOCAL_OPERATION_FAILED")
    finally:
        if process is not None and not supervision_completed:
            for signum in old_signals:
                signal.signal(signum, signal.SIG_IGN)
            if state is None:
                state = _new_run_state(prepared) if prepared is not None else None
            if state is not None:
                state["phase"] = "stopping"
                publish_now = _publish_status(
                    publisher, prepared, run_id, state, parser, supervisor_identity,
                    child_identity, "stopping", _running_terminal(), logs) if (
                        prepared is not None and run_id is not None) else False
            fallback_cleanup = _cleanup_owned(
                process, prepared.shutdown_grace_seconds if prepared is not None else 10.0)
            if state is not None:
                state["cleanup"] = {
                    "status": fallback_cleanup["status"],
                    "signals": list(fallback_cleanup["signals"]),
                    "forced": bool(fallback_cleanup["forced"]),
                    "reaped": bool(fallback_cleanup["reaped"]),
                    "direct_signaled": bool(fallback_cleanup["direct_signaled"]),
                    "diagnostics": list(fallback_cleanup["diagnostics"]),
                    "duration_seconds": fallback_cleanup["duration_seconds"],
                }
                for code in fallback_cleanup["diagnostics"]:
                    cleanup_diag = diagnostic("cleanup", code)
                    _append_diagnostic(state, cleanup_diag)
                    if error is None:
                        error = cleanup_diag
                if fallback_cleanup["direct_signaled"]:
                    state["natural_exit"] = False
                elif process.returncode is not None:
                    state["natural_exit"] = not bool(
                        state.get("cancellation", {}).get("requested")) and error is None
            supervision_completed = True
        for stream in ((process.stdin, process.stdout, process.stderr)
                       if process is not None else ()):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass
        if old_subreaper is not None:
            try:
                _subreaper(old_subreaper)
            except (OSError, AttributeError):
                pass
        for signum in old_signals:
            signal.signal(signum, signal.SIG_IGN)
        if parser is not None:
            result.update({"session_id": parser.session_id, "answer": parser.answer,
                           "turn_end": parser.turn_end, "tool_errors": parser.tool_errors,
                           "tool_counts": parser.tool_counts, "event_counts": parser.event_counts,
                           "upstream_truncated": parser.upstream_truncated,
                           "turns_completed": parser.turns_completed,
                           "answer_consistency": parser.answer_consistency})
        if state is None and prepared is not None:
            state = _new_run_state(prepared)
        if outcome is not None:
            natural_exit = outcome.get("natural_exit")
            finalization = outcome.get("finalization") or {}
            cleanup = outcome.get("cleanup") or {}
            output_complete = bool(outcome.get("output_complete"))
            protocol_violation = bool(outcome.get("protocol_violation"))
            finish_error = outcome.get("finish_error")
            process_exit_code = outcome.get("process_exit_code")
        else:
            natural_exit = (state.get("natural_exit") if state is not None else None)
            finalization = (state.get("finalization") if state is not None else
                            {"status": "not_started", "waited_seconds": 0.0,
                             "timed_out": False})
            cleanup = (state.get("cleanup") if state is not None else
                       {"status": "not_run", "signals": [], "diagnostics": []})
            output_complete = False
            protocol_violation = bool(error and error.get("category") == "protocol")
            finish_error = None
            process_exit_code = process.returncode if process is not None else None
        if prepared is not None:
            try:
                after = source_hashes(prepared.config_path,
                                      prepared.dsh_home / "profiles" / prepared.profile,
                                      prepared.patches)
                result["source_hashes_after"] = after
                result["source_files_unchanged"] = after == prepared.source_hashes
                if after != prepared.source_hashes:
                    source_error = diagnostic("config", "CONFIG_SOURCE_CHANGED_DURING_RUN")
                    _append_diagnostic(state, source_error) if state is not None else None
                    if error is None:
                        error = source_error
                if parser is not None and parser.session_id is not None:
                    try:
                        _save_session(prepared, parser.session_id)
                    except (BridgeError, OSError):
                        session_error = diagnostic("io", "METADATA_OR_SOURCE_READBACK_FAILED")
                        if state is not None:
                            _append_diagnostic(state, session_error)
                        if error is None:
                            error = session_error
            except (BridgeError, OSError):
                readback_error = diagnostic("io", "METADATA_OR_SOURCE_READBACK_FAILED")
                if state is not None:
                    _append_diagnostic(state, readback_error)
                if error is None:
                    error = readback_error
        # Supervision/channel errors are independent failures, never swallowed
        # into a successful wrapper exit.
        if state is not None:
            for channel in ("status_error", "control_error"):
                channel_error = state.get(channel)
                if channel_error is not None and error is None:
                    error = channel_error
            state["error"] = error
        result["process_exit_code"] = process_exit_code
        result["natural_exit"] = natural_exit
        result["finalization"] = finalization
        result["cleanup"] = cleanup
        result["output_complete"] = bool(output_complete)
        if state is not None:
            result["cancellation"] = state["cancellation"]
            result["finalization"] = state["finalization"]
            result["cleanup"] = state["cleanup"]
            result["diagnostics"] = _diagnostics_snapshot(state, parser, error)
            result["supervision"] = {
                "hard_timeout_seconds": prepared.timeout if prepared is not None else None,
                "heartbeat_seconds": prepared.heartbeat_seconds if prepared is not None else None,
                "stall_warning_seconds": prepared.stall_warning_seconds if prepared is not None else None,
                "shutdown_grace_seconds": prepared.shutdown_grace_seconds if prepared is not None else None,
                "finalization_grace_seconds": prepared.finalization_grace_seconds if prepared is not None else None,
                "heartbeats_published": state["heartbeats"],
                "stall": state["stall"],
                "progress_semantics": "unknown_liveness_only",
                "semantic_progress": False,
                "tokens_realtime": False,
                "sdk_control": False,
            }
            result["status_error"] = state.get("status_error")
            result["control_error"] = state.get("control_error")
        elif not result["diagnostics"]:
            result["diagnostics"] = _diagnostics_snapshot({}, parser, error)
        # Structural distinction: protocol_completed is the strict completed
        # turn + final stream, while result_available means a valid final answer
        # was captured even if a later exit/cleanup/protocol-consistency check
        # failed. Neither claims task acceptance.
        turn_completed = bool(parser is not None and parser.turn_end is not None
                              and parser.turn_end.get("reason", {}).get("kind") == "completed")
        protocol_completed = bool(
            parser is not None and parser.session_id is not None
            and parser.final_text_accepted and turn_completed
            and parser.error is None and not protocol_violation)
        result_available = bool(parser is not None and parser.final_text_accepted
                                and turn_completed)
        result["protocol_completed"] = protocol_completed
        result["result_available"] = result_available
        result["error"] = error
        result["wrapper_exit_code"] = EXIT_CODES[error["category"]] if error else 0
        result["harness_completed"] = bool(
            error is None and protocol_completed and process_exit_code == 0
            and (parser.finish(process_exit_code) is None if parser is not None else False))
        if error is not None:
            result["output_complete"] = False
        # Publish a true terminal status before the (also public) final stdout
        # line. A status-channel failure is itself reported as a wrapper error.
        status_complete: bool | None
        if prepared is not None and prepared.status_file is not None:
            if publisher is None or (state is not None and state.get("status_disabled")):
                status_complete = False
                if result["status_error"] is None:
                    status_error = diagnostic("io", "STATUS_UPDATE_FAILED")
                    result["status_error"] = status_error
                    if error is None:
                        error = status_error
            else:
                terminal = {
                    "state": _terminal_state(error),
                    "protocol_completed": protocol_completed,
                    "result_available": result_available,
                    "natural_exit": natural_exit,
                    "harness_completed": result["harness_completed"],
                    "output_complete": result["output_complete"],
                    "cleanup_status": result["cleanup"].get("status"),
                    "error": error,
                }
                if state is None:
                    state = _new_run_state(prepared)
                published = _publish_status(
                    publisher, prepared, run_id, state, parser, supervisor_identity,
                    child_identity, "finished", terminal, logs)
                if published:
                    status_complete = True
                else:
                    status_complete = False
                    status_error = (state.get("status_error") if state is not None else None)
                    if status_error is None:
                        status_error = diagnostic("io", "STATUS_UPDATE_FAILED")
                    result["status_error"] = status_error
                    if error is None:
                        error = status_error
                    result["error"] = error
                    result["wrapper_exit_code"] = EXIT_CODES[error["category"]]
                    result["harness_completed"] = False
                    result["output_complete"] = False
        else:
            status_complete = None
        result["status_complete"] = status_complete
        # A requested status channel that failed earlier remains false even if
        # the task itself produced an answer.
        if prepared is not None and prepared.status_file is not None:
            if result["status_error"] is not None:
                result["status_complete"] = False
        result["elapsed_seconds"] = round(time.monotonic() - started, 6)
        result["diagnostics"] = _diagnostics_snapshot(
            state or {}, parser, error, [finish_error] if (
                finish_error is not None and error is not finish_error) else None)
        # Logs are retention, not execution control: failures degrade the log
        # and never rewrite a healthy task result into a log-limit failure.
        if logs is not None:
            predicted_complete = bool(logs.complete)
            safe_result = {key: value for key, value in result.items() if key != "answer"}
            safe_result["answer_bytes_returned"] = len(result["answer"].encode("utf-8"))
            safe_result["log_complete"] = predicted_complete
            wrote = logs.document("result.json", safe_result)
            result["log_complete"] = bool(wrote and logs.complete)
            result["log_degraded"] = bool(logs.degraded)
            result["log_truncated"] = bool(logs.truncated or logs.rotations
                                           or logs.dropped_events)
            result["log_dropped_events"] = logs.dropped_events
            result["log_diagnostics"] = list(logs.diagnostics)
            try:
                logs.close()
            except (OSError, ValueError):
                result["log_complete"] = False
        else:
            result["log_complete"] = False
            result["log_degraded"] = True
            result["log_diagnostics"] = ([log_init_error] if log_init_error else
                                         ["LOG_DIR_UNAVAILABLE"])
        for signum, previous in old_signals.items():
            signal.signal(signum, previous)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        result = execute(args)
    except BridgeError as exc:
        result = _base_result()
        result["error"] = exc.diagnostic
        result["wrapper_exit_code"] = EXIT_CODES[exc.diagnostic["category"]]
    except Exception:
        result = _base_result()
        result["error"] = diagnostic("io", "LOCAL_OPERATION_FAILED")
        result["wrapper_exit_code"] = EXIT_CODES["io"]
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return result["wrapper_exit_code"]


if __name__ == "__main__":
    sys.exit(main())
