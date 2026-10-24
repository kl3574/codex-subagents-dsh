"""Owned run storage and artifact closeout, separate from process cleanup."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import json
import fcntl
import hashlib
import math
import os
from pathlib import Path
import shutil
import stat
import time
import uuid
from typing import Any

import supervision as sup

MANIFEST = "run.json"
SCHEMA_VERSION = 1
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
RETENTION_SECONDS = 7 * 86400
AUTO_GC_LIMIT = 4


class ArtifactError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _home(config_path: str | Path) -> Path:
    try:
        path = Path(config_path).expanduser().resolve(strict=True)
        with path.open("rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError
        config = json.loads(raw)
        if (not isinstance(config, dict)
                or type(config.get("schema_version")) is not int
                or config["schema_version"] not in {1, 2}):
            raise ValueError
        home = Path(config["dsh_home"])
        if not home.is_absolute():
            raise ValueError
        home = home.resolve(strict=True)
        if not home.is_dir():
            raise ValueError
        return home
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        raise ArtifactError("ARTIFACT_CONFIG_INVALID") from None


def _private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ArtifactError("ARTIFACT_ROOT_UNSAFE")
    except OSError:
        raise ArtifactError("ARTIFACT_ROOT_UNAVAILABLE") from None


def _new_json(path: Path, value: dict[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _identity(info: os.stat_result) -> dict[str, int]:
    return {"device": info.st_dev, "inode": info.st_ino}


def _open_directory(path: Path) -> int:
    """Open every component without following links; callers own the returned fd."""
    if not path.is_absolute() or ".." in path.parts:
        raise ArtifactError("ARTIFACT_PATH_UNSAFE")
    fd = os.open("/", DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


class RunLease:
    """Exclusive, non-inherited lease shared by execution and housekeeping."""
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.fd = self.lock = None
        try:
            self.fd = _open_directory(self.path)
            info = os.fstat(self.fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ArtifactError("ARTIFACT_ROOT_UNSAFE")
            self.lock = os.open("lease.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=self.fd)
            lock_info = os.fstat(self.lock)
            if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                    or lock_info.st_uid != os.getuid() or lock_info.st_mode & 0o077):
                raise ArtifactError("ARTIFACT_LOCK_UNSAFE")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ArtifactError("ARTIFACT_RUN_BUSY") from None
            self.document = self._read()
            doc = self.document
            if (type(doc.get("schema_version")) is not int or doc["schema_version"] != SCHEMA_VERSION
                    or doc.get("run_id") != self.path.name
                    or len(self.path.name) != 32
                    or any(c not in "0123456789abcdef" for c in self.path.name)
                    or self.path.parent != Path(doc.get("dsh_home", "")) / ".codex-dsh-wrapper" / "runs"
                    or doc.get("identity") != _identity(info)
                    or not isinstance(doc.get("directories"), dict)
                    or doc.get("state") not in {"prepared", "running", "pending_acceptance",
                                                 "unknown", "closeout_pending", "closed"}
                    or (doc.get("closeout") is not None and not isinstance(doc["closeout"], dict))):
                raise ArtifactError("ARTIFACT_MANIFEST_INVALID")
            closeout = doc.get("closeout")
            if closeout is not None:
                cleanup = closeout.get("cleanup")
                if (closeout.get("disposition") not in {"accepted", "abandoned"}
                        or type(closeout.get("delivered")) is not bool
                        or (closeout["disposition"] == "accepted" and not closeout["delivered"])
                        or not isinstance(cleanup, dict)
                        or cleanup.get("status") not in {"pending", "partial", "complete"}
                        or any(type(cleanup.get(k)) is not int or cleanup[k] < 0 for k in ("files", "bytes"))):
                    raise ArtifactError("ARTIFACT_MANIFEST_INVALID")
                for field in ("decided_at", "closed_at"):
                    value = closeout.get(field)
                    if value is None and field == "closed_at" and doc["state"] != "closed":
                        continue
                    if type(value) not in {int, float} or not math.isfinite(value):
                        raise ArtifactError("ARTIFACT_MANIFEST_INVALID")
            elif doc["state"] in {"closed", "closeout_pending"}:
                raise ArtifactError("ARTIFACT_MANIFEST_INVALID")
            if doc.get("gc") is not None and not isinstance(doc["gc"], dict):
                raise ArtifactError("ARTIFACT_MANIFEST_INVALID")
            self.verify()
        except BaseException as exc:
            self.close()
            if isinstance(exc, (OSError, ValueError, TypeError, KeyError, RecursionError)):
                raise ArtifactError("ARTIFACT_RUN_UNSAFE") from None
            raise

    def _read(self) -> dict[str, Any]:
        fd = os.open(MANIFEST, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.getuid() or info.st_mode & 0o077):
                raise ArtifactError("ARTIFACT_MANIFEST_UNSAFE")
            raw = handle.read(65537)
            self.manifest_stat = _fingerprint(os.fstat(handle.fileno()))
        if len(raw) > 65536:
            raise ArtifactError("ARTIFACT_MANIFEST_INVALID")
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            raise ArtifactError("ARTIFACT_MANIFEST_INVALID")
        return doc

    def verify(self) -> None:
        fd = _open_directory(self.path)
        try:
            if _identity(os.fstat(fd)) != self.document["identity"]:
                raise ArtifactError("ARTIFACT_TARGET_CHANGED")
            if _fingerprint(os.stat(MANIFEST, dir_fd=fd, follow_symlinks=False)) != self.manifest_stat:
                raise ArtifactError("ARTIFACT_TARGET_CHANGED")
            if _identity(os.stat("lease.lock", dir_fd=fd, follow_symlinks=False)) != _identity(os.fstat(self.lock)):
                raise ArtifactError("ARTIFACT_TARGET_CHANGED")
            for name in ("scratch", "evidence", "diagnostics"):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (not stat.S_ISDIR(info.st_mode)
                        or _identity(info) != self.document["directories"].get(name)):
                    raise ArtifactError("ARTIFACT_TARGET_CHANGED")
        finally:
            os.close(fd)

    def save(self) -> None:
        self.verify()
        name = ".run-" + uuid.uuid4().hex + ".tmp"
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.fd)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.document, handle, ensure_ascii=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, MANIFEST, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            self.manifest_stat = _fingerprint(os.stat(MANIFEST, dir_fd=self.fd, follow_symlinks=False))
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(name, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def begin(self, home: Path, cwd: Path, identity: dict[str, Any] | None) -> None:
        if self.document["dsh_home"] != str(home) or self.document["cwd"] != str(cwd):
            raise ArtifactError("ARTIFACT_SELECTION_MISMATCH")
        if self.document["state"] != "prepared":
            raise ArtifactError("ARTIFACT_RUN_NOT_PREPARED")
        if identity is None or identity.get("identity_kind") != "linux_proc_starttime":
            raise ArtifactError("ARTIFACT_IDENTITY_UNAVAILABLE")
        self.document.update(state="running", owner=identity)
        self.save()

    def finish(self, result: dict[str, Any]) -> dict[str, Any]:
        stopped = result["cleanup"].get("status") in {
            "ok", "terminated_gracefully", "forced_kill", "not_needed", "not_run"}
        # A missing process exit is safe only when no child was ever launched.
        if result["process_exit_code"] is None and result["cleanup"].get("status") == "ok":
            stopped = False
        self.document["state"] = "pending_acceptance" if stopped else "unknown"
        self.document["execution"] = {
            "harness_completed": result["harness_completed"],
            "wrapper_exit_code": result["wrapper_exit_code"],
            "session_id": result["session_id"],
            "cleanup_status": result["cleanup"].get("status"),
            "stopped": stopped,
        }
        self.save()
        return {"state": self.document["state"], "run_dir": str(self.path),
                "acceptance": "pending", "housekeeping_error": None}

    def close(self) -> None:
        for field in ("lock", "fd"):
            fd = getattr(self, field)
            if fd is not None:
                os.close(fd)
                setattr(self, field, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _stopped(lease: RunLease) -> None:
    doc = lease.document
    if doc["state"] not in {"prepared", "pending_acceptance", "closeout_pending", "closed"}:
        raise ArtifactError("ARTIFACT_RUN_UNKNOWN")
    if doc["state"] == "prepared" or (doc.get("execution") is None and doc.get("owner") is None
                                      and (doc.get("closeout") or {}).get("disposition") == "abandoned"):
        return
    if not isinstance(doc.get("execution"), dict) or doc["execution"].get("stopped") is not True:
        raise ArtifactError("ARTIFACT_RUN_UNKNOWN")
    owner = doc.get("owner")
    if (not isinstance(owner, dict) or owner.get("identity_kind") != "linux_proc_starttime"
            or type(owner.get("start_time_ticks")) is not int):
        raise ArtifactError("ARTIFACT_RUN_UNKNOWN")
    state, _ = sup.identity_state(owner)
    if state == "gone" and sup.process_identity(owner["pid"]) is None and Path("/proc", str(owner["pid"])).exists():
        raise ArtifactError("ARTIFACT_OWNER_LIVE_OR_UNKNOWN")
    if state not in {"gone", "mismatch"}:
        raise ArtifactError("ARTIFACT_OWNER_LIVE_OR_UNKNOWN")


def _fingerprint(info: os.stat_result) -> list[int]:
    return [info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _snapshot(fd: int, device: int, depth: int = 0,
              budget: list[int] | None = None) -> dict[str, Any]:
    """Bounded no-follow inventory. Refuse links/special files before any deletion."""
    budget = [10000] if budget is None else budget
    if depth > 64:
        raise ArtifactError("ARTIFACT_TREE_LIMIT")
    entries = {}
    with os.scandir(fd) as iterator:
        for item in iterator:
            budget[0] -= 1
            if budget[0] < 0:
                raise ArtifactError("ARTIFACT_TREE_LIMIT")
            info = item.stat(follow_symlinks=False)
            if info.st_dev != device or info.st_uid != os.getuid():
                raise ArtifactError("ARTIFACT_ENTRY_UNSAFE")
            entry: dict[str, Any] = {"stat": _fingerprint(info)}
            if stat.S_ISDIR(info.st_mode):
                child = os.open(item.name, DIRECTORY_FLAGS, dir_fd=fd)
                try:
                    if _identity(os.fstat(child)) != _identity(info):
                        raise ArtifactError("ARTIFACT_TARGET_CHANGED")
                    entry["children"] = _snapshot(child, device, depth + 1, budget)
                finally:
                    os.close(child)
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ArtifactError("ARTIFACT_ENTRY_UNSAFE")
            entries[item.name] = entry
    return entries


def _remove_snapshot(fd: int, entries: dict[str, Any], counts: dict[str, int]) -> None:
    for name, entry in entries.items():
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if _fingerprint(info) != entry["stat"]:
            raise ArtifactError("ARTIFACT_TARGET_CHANGED")
        if "children" in entry:
            child = os.open(name, DIRECTORY_FLAGS, dir_fd=fd)
            try:
                if _identity(os.fstat(child)) != _identity(info):
                    raise ArtifactError("ARTIFACT_TARGET_CHANGED")
                _remove_snapshot(child, entry["children"], counts)
                if _identity(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _identity(info):
                    raise ArtifactError("ARTIFACT_TARGET_CHANGED")
                os.rmdir(name, dir_fd=fd)
            finally:
                os.close(child)
        else:
            os.unlink(name, dir_fd=fd)
            counts["files"] += 1
            counts["bytes"] += info.st_size


def _plan_token(document: dict[str, Any], entries: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([document, entries], sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def finalize_run(path: str | Path, disposition: str, delivered: bool = False,
                 dry_run: bool = False, expected_plan: str | None = None) -> dict[str, Any]:
    if disposition not in {"accepted", "abandoned"}:
        raise ArtifactError("ARTIFACT_DISPOSITION_INVALID")
    if disposition == "accepted" and not delivered:
        raise ArtifactError("ARTIFACT_DELIVERY_REQUIRED")
    try:
        with RunLease(path) as lease:
            _stopped(lease)
            doc = lease.document
            if disposition == "accepted" and doc["state"] == "prepared":
                raise ArtifactError("ARTIFACT_EXECUTION_REQUIRED")
            closeout = doc.get("closeout")
            if closeout is not None and closeout["disposition"] != disposition:
                raise ArtifactError("ARTIFACT_DISPOSITION_CONFLICT")
            base = {"ok": True, "tool": "manage_dsh", "action": "finalize",
                    "run_id": doc["run_id"], "run_dir": str(lease.path), "dry_run": dry_run}
            if doc["state"] == "closed":
                return {**base, "artifact_state": "closed", "closeout": closeout}
            fd = os.open("scratch", DIRECTORY_FLAGS, dir_fd=lease.fd)
            try:
                entries = _snapshot(fd, doc["identity"]["device"])
                token = _plan_token(doc, entries)
                if expected_plan is not None and expected_plan != token:
                    raise ArtifactError("ARTIFACT_PLAN_CHANGED")
                if dry_run:
                    return {**base, "artifact_state": doc["state"], "plan_token": token,
                            "scope": "scratch_only", "closeout": closeout}
                # Persist the decision before deleting. A crash can be retried with
                # the same disposition, without inventing acceptance from execution.
                if closeout is None:
                    closeout = {"disposition": disposition, "delivered": delivered,
                                "decided_at": time.time(), "closed_at": None,
                                "cleanup": {"status": "pending", "files": 0, "bytes": 0,
                                            "error": None}}
                    doc["closeout"] = closeout
                doc["state"] = "closeout_pending"
                lease.save()
                counts = closeout["cleanup"]
                try:
                    lease.verify()
                    _remove_snapshot(fd, entries, counts)
                    if os.listdir(fd):
                        raise ArtifactError("ARTIFACT_TARGET_CHANGED")
                    lease.verify()
                    counts.update(status="complete", error=None)
                    closeout["closed_at"] = time.time()
                    doc["state"] = "closed"
                except (OSError, ArtifactError) as exc:
                    counts.update(status="partial", error=getattr(exc, "code", "ARTIFACT_CLEANUP_FAILED"))
                    base["ok"] = False
                lease.save()
                return {**base, "artifact_state": doc["state"], "closeout": closeout}
            finally:
                os.close(fd)
    except OSError:
        raise ArtifactError("ARTIFACT_IO_FAILED") from None


@contextmanager
def _index(home: Path, create: bool = False):
    """Fixed 32-byte IDs + an 8-byte cursor: bounded reads, fair round robin.

    The index is only a candidate queue, never deletion authority. Every selected
    run still needs its own manifest, lease, identity and eligibility checks.
    """
    root = home / ".codex-dsh-wrapper" / "runs"
    try:
        parent = _open_directory(root)
    except FileNotFoundError:
        raise ArtifactError("ARTIFACT_INDEX_MISSING") from None
    fd = None
    try:
        flags = os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY)
        try:
            fd = os.open("gc-index", flags, 0o600, dir_fd=parent)
        except FileNotFoundError:
            raise ArtifactError("ARTIFACT_INDEX_MISSING") from None
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size % 32):
            raise ArtifactError("ARTIFACT_INDEX_UNSAFE")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ArtifactError("ARTIFACT_GC_BUSY") from None
        yield root, parent, fd
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _enqueue(home: Path, run_id: str) -> None:
    with _index(home, create=True) as (_, _, fd):
        os.lseek(fd, 0, os.SEEK_END)
        if os.write(fd, run_id.encode("ascii")) != 32:
            raise ArtifactError("ARTIFACT_INDEX_WRITE_FAILED")
        os.fsync(fd)


def _gc_reason(lease: RunLease, now: float) -> str:
    doc = lease.document
    if doc["state"] != "closed":
        return "unknown" if doc["state"] in {"running", "unknown"} else "not_closed"
    _stopped(lease)
    closeout = doc.get("closeout")
    if not isinstance(closeout, dict):
        return "unknown"
    closed_at = closeout.get("closed_at")
    if type(closed_at) not in {int, float} or not math.isfinite(closed_at):
        return "unknown"
    if now - closed_at < RETENTION_SECONDS:
        return "retention_not_elapsed"
    if (doc.get("gc") or {}).get("status") == "complete":
        return "already_collected"
    fd = os.open("diagnostics", DIRECTORY_FLAGS, dir_fd=lease.fd)
    try:
        try:
            os.stat(".keep", dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            return "protected"
    finally:
        os.close(fd)
    return "eligible"


def gc_runs(home: Path, dry_run: bool = False, limit: int = 16,
            expected_plan: str | None = None) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ArtifactError("ARTIFACT_GC_LIMIT_INVALID")
    response: dict[str, Any] = {"ok": True, "tool": "manage_dsh", "action": "gc",
                               "dry_run": dry_run, "runs": [], "warnings": []}
    try:
        with _index(home) as (root, parent, index), ExitStack() as stack:
            total = os.fstat(index).st_size // 32
            cursor = 0
            try:
                cursor_fd = os.open("gc-cursor", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                pass
            else:
                with os.fdopen(cursor_fd, "rb") as handle:
                    info = os.fstat(handle.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 8:
                        raise ArtifactError("ARTIFACT_CURSOR_UNSAFE")
                    cursor = int.from_bytes(handle.read(8), "big")
            cursor = cursor % total if total else 0
            plans = []
            candidates = []
            now = time.time()
            for offset in range(min(limit, total)):
                os.lseek(index, ((cursor + offset) % total) * 32, os.SEEK_SET)
                run_id = os.read(index, 32).decode("ascii")
                if len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
                    raise ArtifactError("ARTIFACT_INDEX_UNSAFE")
                row = {"run_id": run_id, "reason": "unknown"}
                response["runs"].append(row)
                try:
                    lease = stack.enter_context(RunLease(root / run_id))
                    row["reason"] = _gc_reason(lease, now)
                    entries = None
                    if row["reason"] == "eligible":
                        fd = os.open("diagnostics", DIRECTORY_FLAGS, dir_fd=lease.fd)
                        stack.callback(os.close, fd)
                        entries = _snapshot(fd, lease.document["identity"]["device"])
                        candidates.append((lease, fd, entries, row))
                    plans.append([run_id, row["reason"], lease.document, entries])
                except (ArtifactError, OSError) as exc:
                    row["reason"] = getattr(exc, "code", "ARTIFACT_IO_FAILED")
                    plans.append([run_id, row["reason"]])
            token = hashlib.sha256(json.dumps([cursor, plans], sort_keys=True,
                                              separators=(",", ":")).encode()).hexdigest()
            response["plan_token"] = token
            if expected_plan is not None and expected_plan != token:
                raise ArtifactError("ARTIFACT_PLAN_CHANGED")
            if dry_run:
                return response
            # Revalidate the queue's pathname before committing any sweep effects.
            if _identity(os.stat("gc-index", dir_fd=parent, follow_symlinks=False)) != _identity(os.fstat(index)):
                raise ArtifactError("ARTIFACT_TARGET_CHANGED")
            for lease, fd, entries, row in candidates:
                counts = {"files": 0, "bytes": 0}
                try:
                    lease.verify()
                    if _gc_reason(lease, time.time()) != "eligible" or _snapshot(fd, lease.document["identity"]["device"]) != entries:
                        raise ArtifactError("ARTIFACT_PLAN_CHANGED")
                    _remove_snapshot(fd, entries, counts)
                    if os.listdir(fd):
                        raise ArtifactError("ARTIFACT_TARGET_CHANGED")
                    lease.verify()
                    lease.document["gc"] = {**counts, "status": "complete", "collected_at": time.time()}
                    lease.save()
                    row.update(reason="collected", **counts)
                except (ArtifactError, OSError) as exc:
                    row.update(reason="partial", error=getattr(exc, "code", "ARTIFACT_GC_FAILED"), **counts)
                    response["ok"] = False
                    response["warnings"].append({"run_id": row["run_id"], "code": row["error"]})
            next_cursor = (cursor + len(response["runs"])) % total if total else 0
            name = ".cursor-" + uuid.uuid4().hex
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(next_cursor.to_bytes(8, "big"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, "gc-cursor", src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
        return response
    except ArtifactError as exc:
        if exc.code == "ARTIFACT_INDEX_MISSING":
            # No managed queue yet: do not adopt historical directories.
            return response
        raise
    except (OSError, ValueError, UnicodeError):
        raise ArtifactError("ARTIFACT_GC_IO_FAILED") from None


def opportunistic_gc(home: Path) -> dict[str, Any]:
    try:
        return gc_runs(home, limit=AUTO_GC_LIMIT)
    except ArtifactError as exc:
        return {"ok": False, "warnings": [{"code": exc.code}]}


def prepare_run(config_path: str | Path, cwd: str | Path) -> dict[str, Any]:
    home = _home(config_path)
    try:
        workspace = Path(cwd).expanduser().resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError
    except (OSError, ValueError, RuntimeError):
        raise ArtifactError("ARTIFACT_CWD_INVALID") from None
    parent = home / ".codex-dsh-wrapper"
    _private_directory(parent)
    parent = parent / "runs"
    _private_directory(parent)
    run_id = uuid.uuid4().hex
    directory = parent / run_id
    created = False
    try:
        directory.mkdir(mode=0o700)
        created = True
        for name in ("scratch", "evidence", "diagnostics"):
            (directory / name).mkdir(mode=0o700)
        info = directory.stat()
        _new_json(directory / MANIFEST, {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "dsh_home": str(home),
            "cwd": str(workspace),
            "identity": {"device": info.st_dev, "inode": info.st_ino},
            "directories": {name: _identity((directory / name).stat())
                            for name in ("scratch", "evidence", "diagnostics")},
            "created_at": time.time(),
            "state": "prepared",
            "execution": None,
            "closeout": None,
        })
        fd = os.open(directory / "lease.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except OSError:
        if created:
            shutil.rmtree(directory)
        raise ArtifactError("ARTIFACT_PREPARE_FAILED") from None
    warnings = []
    try:
        _enqueue(home, run_id)
    except (ArtifactError, OSError) as exc:
        warnings.append({"code": getattr(exc, "code", "ARTIFACT_INDEX_WRITE_FAILED")})
    housekeeping = opportunistic_gc(home)
    housekeeping["warnings"] = warnings + housekeeping.get("warnings", [])
    if warnings:
        housekeeping["ok"] = False
    return {
        "ok": True,
        "tool": "manage_dsh",
        "action": "prepare",
        "run_id": run_id,
        "run_dir": str(directory),
        "scratch_dir": str(directory / "scratch"),
        "evidence_dir": str(directory / "evidence"),
        "diagnostics_dir": str(directory / "diagnostics"),
        "status_file": str(directory / "diagnostics" / "status.json"),
        "artifact_state": "prepared",
        "housekeeping": housekeeping,
    }
