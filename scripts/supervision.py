#!/usr/bin/env python3
"""Bounded status/control support for run_dsh.py and manage_dsh.py.

The status channel is deliberately an observation channel only:

* a heartbeat proves that this supervisor process can still publish;
* process liveness and CPU activity are not task progress;
* NDJSON lifecycle events are weak, allowlisted observations; the wrapper does
  not claim to know semantic progress, live token counts, or hidden SDK state.

No raw task text, reasoning, tool input/result, environment, command line, or
exception message is written to a status/control document.  Status writes are
atomic regular-file replacements with mode 0600.  Control requests are
single-use, run_id-scoped files that only request cancellation; the process
that owns the child performs the cancellation.
"""
from __future__ import annotations

import datetime
import functools
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Any

STATUS_SCHEMA_VERSION = 1
CONTROL_SCHEMA_VERSION = 1
STATUS_MAX_BYTES = 64 * 1024
CONTROL_MAX_BYTES = 8 * 1024
CANCEL_REASON = "user_cancelled"
_HEARTBEAT_DEFAULT = 5.0
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_TERMINAL_STATES = {
    "completed": "COMPLETED",
    "failed": "FAILED",
    "cancelled": "CANCELLED",
    "timed_out": "TIMED_OUT",
    "interrupted": "INTERRUPTED",
    "cleanup_failed": "CLEANUP_FAILED",
}


class SupervisionError(Exception):
    """A safe, static-code-only status/control failure."""

    def __init__(self, code: str, category: str = "io"):
        self.code = code
        self.category = category
        super().__init__(code)


def iso_time(epoch: float | None = None) -> str:
    """Return a UTC ISO-8601 timestamp with millisecond precision."""
    moment = time.time() if epoch is None else float(epoch)
    return datetime.datetime.fromtimestamp(
        moment, tz=datetime.timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z")


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def stale_after_seconds(heartbeat_seconds: Any) -> float:
    """Bound how long a running status remains fresh without a heartbeat."""
    heartbeat = _finite(heartbeat_seconds)
    if heartbeat is None or heartbeat <= 0:
        heartbeat = _HEARTBEAT_DEFAULT
    return round(max(3.0 * heartbeat, heartbeat + 2.0, 2.0), 6)


@functools.lru_cache(maxsize=1)
def boot_id() -> str | None:
    """Read the Linux boot identifier only; no environment/config data."""
    try:
        with open("/proc/sys/kernel/random/boot_id", "rb") as handle:
            value = handle.read(256).decode("ascii", errors="strict").strip()
    except (OSError, UnicodeError):
        return None
    return value or None


def _proc_stat(pid: Any) -> tuple[str, int, int] | None:
    """Return (state, start_time_ticks, pgrp) from /proc/PID/stat only."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            data = handle.read(16384)
    except OSError:
        return None
    end = data.rfind(b")")
    if end < 0 or end + 2 > len(data):
        return None
    fields = data[end + 2:].split()
    if len(fields) < 20:
        return None
    try:
        state = fields[0].decode("ascii", errors="strict")
        pgrp = int(fields[2])
        start_time = int(fields[19])
    except (UnicodeDecodeError, ValueError, IndexError):
        return None
    return state, start_time, pgrp


def process_identity(pid: Any) -> dict[str, Any] | None:
    """Capture a real process identity without reading its command/environment.

    On Linux the pair (boot_id, /proc/PID/stat starttime) is the identity.
    A bare PID is never enough for manage_dsh.py to trust a status document.
    """
    stat_value = _proc_stat(pid)
    if stat_value is not None:
        state, start_time, pgrp = stat_value
        return {
            "pid": int(pid),
            "identity_kind": "linux_proc_starttime",
            "boot_id": boot_id(),
            "start_time_ticks": start_time,
            "pgrp": pgrp,
            "state": state,
            "alive": state not in {"Z", "X", "x"},
        }
    if not sys_is_linux():
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError, TypeError):
            return None
        return {"pid": int(pid), "identity_kind": "pid_only", "boot_id": None,
                "start_time_ticks": None, "pgrp": None, "state": None,
                "alive": True}
    return None


def sys_is_linux() -> bool:
    return os.uname().sysname == "Linux" if hasattr(os, "uname") else False


def identity_state(recorded: Any) -> tuple[str, str | None]:
    """Compare a recorded identity with the real process now.

    Returns one of verified / gone / mismatch / unverifiable plus a static code.
    """
    if not isinstance(recorded, dict):
        return "unverifiable", "IDENTITY_RECORD_INVALID"
    pid = recorded.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return "unverifiable", "IDENTITY_RECORD_INVALID"
    current = process_identity(pid)
    if current is None:
        return "gone", "PROCESS_NOT_RUNNING"
    if not current["alive"]:
        return "gone", "PROCESS_NOT_RUNNING"
    recorded_kind = recorded.get("identity_kind")
    if recorded_kind != "linux_proc_starttime" or current["identity_kind"] != "linux_proc_starttime":
        # A bare PID cannot disprove reuse, so never call it verified.
        return "unverifiable", "START_IDENTITY_UNAVAILABLE"
    recorded_boot = recorded.get("boot_id")
    current_boot = current.get("boot_id")
    if recorded_boot is not None and current_boot != recorded_boot:
        return "mismatch", "BOOT_ID_MISMATCH"
    recorded_start = recorded.get("start_time_ticks")
    if isinstance(recorded_start, bool) or not isinstance(recorded_start, int):
        return "unverifiable", "START_IDENTITY_UNAVAILABLE"
    if recorded_start != current["start_time_ticks"]:
        return "mismatch", "PID_REUSE_DETECTED"
    return "verified", None


def identity_equal(left: Any, right: Any) -> bool:
    """Compare two already-recorded Linux identities without trusting a PID."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if left.get("pid") != right.get("pid"):
        return False
    if left.get("boot_id") is not None and right.get("boot_id") is not None:
        if left.get("boot_id") != right.get("boot_id"):
            return False
    return left.get("start_time_ticks") == right.get("start_time_ticks")


def _normalized_path(value: Any) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise SupervisionError("STATUS_PATH_INVALID", "config")
    try:
        path = Path(value).expanduser()
    except (OSError, ValueError, RuntimeError):
        raise SupervisionError("STATUS_PATH_INVALID", "config") from None
    if not path.is_absolute():
        raise SupervisionError("STATUS_PATH_INVALID", "config")
    normalized = Path(os.path.normpath(str(path)))
    if normalized.name in {"", ".", ".."}:
        raise SupervisionError("STATUS_PATH_INVALID", "config")
    return normalized


def validate_status_destination(value: Any) -> Path:
    """Validate a new status path without following a final symlink."""
    raw = _normalized_path(value)
    try:
        parent = raw.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SupervisionError("STATUS_PARENT_INVALID", "config") from None
    if not parent.is_dir():
        raise SupervisionError("STATUS_PARENT_INVALID", "config")
    final = parent / raw.name
    if os.path.lexists(final):
        raise SupervisionError("STATUS_FILE_EXISTS", "config")
    if os.path.lexists(control_path(final)):
        raise SupervisionError("STATUS_CONTROL_EXISTS", "config")
    if not os.access(parent, os.W_OK | os.X_OK):
        raise SupervisionError("STATUS_PARENT_INACCESSIBLE", "config")
    return final


def canonical_status_path(value: Any) -> Path:
    """Canonicalize an existing status path without following its final link."""
    raw = _normalized_path(value)
    try:
        before = os.lstat(raw)
    except FileNotFoundError:
        raise SupervisionError("STATUS_NOT_FOUND", "io") from None
    except OSError:
        raise SupervisionError("STATUS_UNREADABLE", "io") from None
    if stat.S_ISLNK(before.st_mode):
        raise SupervisionError("STATUS_SYMLINK_REFUSED", "io")
    try:
        parent = raw.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SupervisionError("STATUS_UNREADABLE", "io") from None
    final = parent / raw.name
    try:
        after = os.lstat(final)
    except OSError:
        raise SupervisionError("STATUS_UNREADABLE", "io") from None
    if stat.S_ISLNK(after.st_mode):
        raise SupervisionError("STATUS_SYMLINK_REFUSED", "io")
    if not stat.S_ISREG(after.st_mode):
        raise SupervisionError("STATUS_NOT_REGULAR", "io")
    # Atomic replacement by the owner is expected.  Do not require the inode
    # sampled a moment ago to still match: that would make a healthy heartbeat
    # update look like an unknown run.  _read_regular_json re-checks the opened
    # descriptor with O_NOFOLLOW and regular-file validation.
    return final


def control_path(status_path: Path | str) -> Path:
    return Path(str(status_path) + ".control.json")


def _read_regular_json(path: Path, limit: int, prefix: str) -> dict[str, Any]:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise SupervisionError(prefix + "_NOT_FOUND", "io") from None
    except OSError:
        raise SupervisionError(prefix + "_UNREADABLE", "io") from None
    if stat.S_ISLNK(before.st_mode):
        raise SupervisionError(prefix + "_SYMLINK_REFUSED", "io")
    if not stat.S_ISREG(before.st_mode):
        raise SupervisionError(prefix + "_NOT_REGULAR", "io")
    if before.st_size > limit:
        raise SupervisionError(prefix + "_TOO_LARGE", "io")
    flags = os.O_RDONLY | _O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        raise SupervisionError(prefix + "_UNREADABLE", "io") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SupervisionError(prefix + "_NOT_REGULAR", "io")
        if info.st_size > limit:
            raise SupervisionError(prefix + "_TOO_LARGE", "io")
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    except OSError:
        raise SupervisionError(prefix + "_UNREADABLE", "io") from None
    finally:
        os.close(fd)
    if len(data) > limit:
        raise SupervisionError(prefix + "_TOO_LARGE", "io")
    try:
        value = json.loads(
            data.decode("utf-8"),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise SupervisionError(prefix + "_INVALID", "io") from None
    if not isinstance(value, dict):
        raise SupervisionError(prefix + "_INVALID", "io")
    return value


def read_status_document(value: Any) -> tuple[Path, dict[str, Any]]:
    path = canonical_status_path(value)
    document = _read_regular_json(path, STATUS_MAX_BYTES, "STATUS")
    if document.get("schema_version") != STATUS_SCHEMA_VERSION:
        raise SupervisionError("STATUS_SCHEMA_UNSUPPORTED", "io")
    run_id = document.get("run_id")
    if not isinstance(run_id, str) or not run_id or len(run_id.encode("utf-8")) > 256:
        raise SupervisionError("STATUS_RUN_ID_INVALID", "io")
    return path, document


def _heartbeat_value(document: dict[str, Any]) -> tuple[float | None, float]:
    value = _finite(document.get("heartbeat_epoch"))
    stale = _finite(document.get("stale_after_seconds"))
    if stale is None or stale <= 0 or stale > 7 * 24 * 3600:
        supervision = document.get("supervision")
        heartbeat = supervision.get("heartbeat_seconds") if isinstance(supervision, dict) else None
        stale = stale_after_seconds(heartbeat)
    return value, float(stale)


def verify_status_document(document: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    """Verify a status document against real process identity and freshness."""
    current_time = time.time() if now is None else float(now)
    supervisor_state, supervisor_reason = identity_state(document.get("supervisor"))
    child_state = "not_recorded"
    child_reason = None
    child = document.get("child")
    if isinstance(child, dict) and child.get("pid") is not None:
        child_state, child_reason = identity_state(child)
    heartbeat, stale = _heartbeat_value(document)
    heartbeat_age = None if heartbeat is None else round(current_time - heartbeat, 3)
    fresh = bool(
        heartbeat is not None
        and heartbeat <= current_time + 60.0
        and (current_time - heartbeat) <= stale
    )
    if heartbeat is None:
        fresh_reason = "HEARTBEAT_INVALID"
    elif heartbeat > current_time + 60.0:
        fresh_reason = "HEARTBEAT_IN_FUTURE"
    elif not fresh:
        fresh_reason = "HEARTBEAT_EXPIRED"
    else:
        fresh_reason = None

    terminal = document.get("terminal") if isinstance(document.get("terminal"), dict) else {}
    terminal_raw = terminal.get("state", "running")
    terminal_name = terminal_raw if isinstance(terminal_raw, str) else "unknown"
    terminal_state = _TERMINAL_STATES.get(terminal_name)
    if terminal_state is not None:
        state = terminal_state
        reason = None
    elif terminal_name not in {"running", "preparing", "launching"}:
        state = "UNKNOWN"
        reason = "TERMINAL_STATE_INVALID"
    elif supervisor_state != "verified":
        state = "UNKNOWN"
        reason = supervisor_reason or "SUPERVISOR_IDENTITY_UNVERIFIED"
    elif not fresh:
        state = "UNKNOWN"
        reason = fresh_reason
    elif child_state == "mismatch":
        state = "UNKNOWN"
        reason = "CHILD_IDENTITY_MISMATCH"
    else:
        state = "RUNNING"
        reason = None
    return {
        "state": state,
        "reason": reason,
        "identity_verified": supervisor_state == "verified",
        "supervisor_identity": supervisor_state,
        "supervisor_reason": supervisor_reason,
        "child_identity": child_state,
        "child_reason": child_reason,
        "fresh": fresh,
        "heartbeat_age_seconds": heartbeat_age,
        "stale_after_seconds": stale,
        "terminal": terminal_state is not None,
    }


def _encode_document(document: dict[str, Any], limit: int) -> bytes:
    try:
        encoded = (json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n").encode(
            "utf-8")
    except (TypeError, ValueError, RecursionError):
        raise SupervisionError("STATUS_ENCODE_FAILED", "io") from None
    if len(encoded) > limit:
        raise SupervisionError("STATUS_SIZE_LIMIT", "io")
    return encoded


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class StatusPublisher:
    """Create a new 0600 status file and atomically replace only that file."""

    def __init__(self, path: Path | str, first_document: dict[str, Any],
                 limit: int = STATUS_MAX_BYTES):
        self.path = Path(path)
        self.limit = limit
        self._device: int | None = None
        self._inode: int | None = None
        self._last_document_bytes: bytes | None = None
        self._create(first_document)

    def _encode(self, document: dict[str, Any]) -> bytes:
        return _encode_document(document, self.limit)

    def _create(self, document: dict[str, Any]) -> None:
        data = self._encode(document)
        try:
            fd, temporary = tempfile.mkstemp(
                prefix="." + self.path.name + ".tmp-", dir=self.path.parent)
        except OSError:
            raise SupervisionError("STATUS_INIT_FAILED", "io") from None
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, data)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            try:
                # Atomic create-if-absent; readers never observe a partial
                # initial document and another run's final path is not replaced.
                os.link(temporary, self.path)
            except FileExistsError:
                raise SupervisionError("STATUS_FILE_EXISTS", "config") from None
            except OSError:
                raise SupervisionError("STATUS_INIT_FAILED", "io") from None
            info = os.lstat(self.path)
            if not stat.S_ISREG(info.st_mode):
                raise SupervisionError("STATUS_FILE_REPLACED", "io")
            self._device = info.st_dev
            self._inode = info.st_ino
            self._last_document_bytes = data
        except SupervisionError:
            raise
        except OSError:
            raise SupervisionError("STATUS_INIT_FAILED", "io") from None
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(temporary)
            except OSError:
                pass
        _fsync_directory(self.path.parent)

    def publish(self, document: dict[str, Any]) -> None:
        data = self._encode(document)
        try:
            before = os.lstat(self.path)
        except OSError:
            raise SupervisionError("STATUS_FILE_REPLACED", "io") from None
        if (not stat.S_ISREG(before.st_mode)
                or before.st_dev != self._device or before.st_ino != self._inode):
            raise SupervisionError("STATUS_FILE_REPLACED", "io")
        # A filesystem may reuse an unlinked inode immediately. Also verify the
        # last committed document so reuse or in-place edits cannot impersonate
        # this publisher's previous snapshot. Reads remain bounded/no-follow.
        current = _read_regular_json(self.path, self.limit, "STATUS")
        if self._encode(current) != self._last_document_bytes:
            raise SupervisionError("STATUS_FILE_REPLACED", "io")
        try:
            fd, temporary = tempfile.mkstemp(
                prefix="." + self.path.name + ".tmp-", dir=self.path.parent)
        except OSError:
            raise SupervisionError("STATUS_UPDATE_FAILED", "io") from None
        try:
            os.fchmod(fd, 0o600)
            _write_all(fd, data)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            # Atomic replacement does not follow a final symlink: any late
            # link swap is replaced as a directory entry, never written through.
            os.replace(temporary, self.path)
            temporary = None
            info = os.lstat(self.path)
            if not stat.S_ISREG(info.st_mode):
                raise SupervisionError("STATUS_FILE_REPLACED", "io")
            self._device = info.st_dev
            self._inode = info.st_ino
            self._last_document_bytes = data
        except SupervisionError:
            raise
        except OSError:
            raise SupervisionError("STATUS_UPDATE_FAILED", "io") from None
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        _fsync_directory(self.path.parent)


def write_control_request(status_path: Path | str, document: dict[str, Any],
                          reason: str = CANCEL_REASON) -> Path:
    """Write one single-use, run_id-scoped cancellation request (0600)."""
    if reason != CANCEL_REASON:
        raise SupervisionError("CONTROL_REASON_INVALID", "config")
    run_id = document.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise SupervisionError("CONTROL_RUN_ID_INVALID", "io")
    supervisor = document.get("supervisor")
    if not isinstance(supervisor, dict):
        raise SupervisionError("CONTROL_TARGET_INVALID", "io")
    target = {
        "pid": supervisor.get("pid"),
        "identity_kind": supervisor.get("identity_kind"),
        "boot_id": supervisor.get("boot_id"),
        "start_time_ticks": supervisor.get("start_time_ticks"),
    }
    request = {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "action": "cancel",
        "reason": CANCEL_REASON,
        "run_id": run_id,
        "requested_at": iso_time(),
        "target": target,
    }
    data = _encode_document(request, CONTROL_MAX_BYTES)
    path = control_path(status_path)
    # Publish a complete request atomically.  A wrapper polling this path must
    # never observe an empty or partially written JSON object.  os.link is an
    # atomic create-if-absent on this local filesystem; the final rename would
    # overwrite a request for another run and is therefore not used.
    try:
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".tmp-",
                                         dir=path.parent)
    except OSError:
        raise SupervisionError("CONTROL_WRITE_FAILED", "io") from None
    try:
        os.fchmod(fd, 0o600)
        _write_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise SupervisionError("CONTROL_FILE_EXISTS", "io") from None
        except OSError as exc:
            import errno
            if exc.errno == errno.ELOOP:
                raise SupervisionError("CONTROL_SYMLINK_REFUSED", "io") from None
            raise SupervisionError("CONTROL_WRITE_FAILED", "io") from None
    except SupervisionError:
        raise
    except OSError:
        raise SupervisionError("CONTROL_WRITE_FAILED", "io") from None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
    _fsync_directory(path.parent)
    return path


def read_control_request(status_path: Path | str, expected_run_id: str,
                         supervisor: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Read and validate a control request without following a symlink."""
    path = control_path(status_path)
    try:
        request = _read_regular_json(path, CONTROL_MAX_BYTES, "CONTROL")
    except SupervisionError as exc:
        if exc.code == "CONTROL_NOT_FOUND":
            return None
        raise
    if request.get("schema_version") != CONTROL_SCHEMA_VERSION:
        raise SupervisionError("CONTROL_SCHEMA_UNSUPPORTED", "io")
    if request.get("action") != "cancel":
        raise SupervisionError("CONTROL_ACTION_INVALID", "io")
    if request.get("reason") != CANCEL_REASON:
        raise SupervisionError("CONTROL_REASON_INVALID", "io")
    if request.get("run_id") != expected_run_id:
        raise SupervisionError("CONTROL_RUN_MISMATCH", "io")
    target = request.get("target")
    if target is not None:
        if not isinstance(target, dict) or not identity_equal(target, supervisor):
            raise SupervisionError("CONTROL_TARGET_MISMATCH", "io")
    return request


def remove_control_request(status_path: Path | str) -> None:
    path = control_path(status_path)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError:
        raise SupervisionError("CONTROL_REMOVE_FAILED", "io") from None
