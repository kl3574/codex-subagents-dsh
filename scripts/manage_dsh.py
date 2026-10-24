#!/usr/bin/env python3
"""Manage owned artifacts, inspect execution status, or request cancellation.

This tool deliberately never signals or kills a PID.  A cancel request is a
single-use JSON file next to the status file, scoped to the exact run_id and
verified supervisor identity.  Only the wrapper that owns the child consumes
it and performs the graceful shutdown.

Commands:
  manage_dsh.py prepare --config PATH --cwd PATH
  manage_dsh.py finalize --run-dir PATH --disposition accepted --delivered
  manage_dsh.py gc --config PATH --dry-run
  manage_dsh.py status --status-file PATH
  manage_dsh.py cancel --status-file PATH --reason user_cancelled

Output is one JSON object on one stdout line.  Exit codes:
  0 = status read / cancel request written (status may still say UNKNOWN)
  2 = argument/configuration error
  3 = status file missing, malformed, unreadable, or unsafe
  4 = cancel refused because the run is not verified live/fresh (UNKNOWN/terminal)
  5 = cancel control request could not be written or already exists
  6 = partial artifact cleanup (safe to retry the same operation)
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
import supervision as sup  # noqa: E402
import artifacts  # noqa: E402

MANAGE_EXIT = {"ok": 0, "usage": 2, "status": 3, "not_live": 4, "control": 5}


class ManageError(Exception):
    def __init__(self, code: str, category: str = "io"):
        self.code = code
        self.category = category
        super().__init__(code)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ManageError("ARGUMENT_INVALID", "config")


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="manage_dsh.py", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True,
                                       parser_class=JsonArgumentParser)
    prepare = subparsers.add_parser("prepare", help="create an owned run directory without starting Harness")
    prepare.add_argument("--config", required=True)
    prepare.add_argument("--cwd", required=True)
    finalize = subparsers.add_parser("finalize", help="record main-agent closeout and reclaim owned scratch")
    finalize.add_argument("--run-dir", required=True)
    finalize.add_argument("--disposition", required=True, choices=["accepted", "abandoned"])
    finalize.add_argument("--delivered", action="store_true")
    finalize.add_argument("--dry-run", action="store_true")
    finalize.add_argument("--expected-plan")
    gc = subparsers.add_parser("gc", help="preview or collect closed-run diagnostics after seven days")
    gc.add_argument("--config", required=True)
    gc.add_argument("--dry-run", action="store_true")
    gc.add_argument("--limit", type=int, default=16)
    gc.add_argument("--expected-plan")
    status = subparsers.add_parser("status", help="read and verify a status file")
    status.add_argument("--status-file", required=True)
    cancel = subparsers.add_parser("cancel", help="write a run_id-scoped cancel request")
    cancel.add_argument("--status-file", required=True)
    cancel.add_argument("--reason", default=sup.CANCEL_REASON,
                        choices=[sup.CANCEL_REASON])
    return parser


def _safe_error(error: Any) -> dict[str, str] | None:
    if not isinstance(error, Exception):
        return None
    category = getattr(error, "category", "io")
    if category not in {"config", "io"}:
        category = "io"
    code = getattr(error, "code", "MANAGE_FAILED")
    if not isinstance(code, str) or not code:
        code = "MANAGE_FAILED"
    return {"category": category, "code": code[:96]}


def _status_response(path: Path, document: dict[str, Any],
                     verification: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "tool": "manage_dsh",
        "action": "status",
        "status_file": str(path),
        "state": verification["state"],
        "reason": verification["reason"],
        "identity_verified": verification["identity_verified"],
        "fresh": verification["fresh"],
        "verification": verification,
        "run_id": document.get("run_id"),
        "phase": document.get("phase"),
        "session_id": document.get("session_id"),
        "heartbeat_at": document.get("heartbeat_at"),
        "last_event_at": document.get("last_event_at"),
        "last_tool_completed_at": document.get("last_tool_completed_at"),
        "last_step_completed_at": document.get("last_step_completed_at"),
        "active_tools": document.get("active_tools"),
        "known_errors": document.get("known_errors"),
        "supervision": document.get("supervision"),
        "terminal": document.get("terminal"),
        "finalization": document.get("finalization"),
        "cleanup": document.get("cleanup"),
        "cancellation": document.get("cancellation"),
        "observation": document.get("observation"),
        "control_file": str(sup.control_path(path)),
        "status": document,
    }


def command_status(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    try:
        path, document = sup.read_status_document(args.status_file)
        verification = sup.verify_status_document(document)
    except sup.SupervisionError as exc:
        return {
            "ok": False,
            "tool": "manage_dsh",
            "action": "status",
            "status_file": os.path.abspath(os.path.expanduser(args.status_file)),
            "state": "UNKNOWN",
            "reason": exc.code,
            "identity_verified": False,
            "error": _safe_error(exc),
        }, MANAGE_EXIT["status"]
    return _status_response(path, document, verification), MANAGE_EXIT["ok"]


def command_cancel(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    try:
        path, document = sup.read_status_document(args.status_file)
        verification = sup.verify_status_document(document)
    except sup.SupervisionError as exc:
        return {
            "ok": False,
            "tool": "manage_dsh",
            "action": "cancel",
            "status_file": os.path.abspath(os.path.expanduser(args.status_file)),
            "state": "UNKNOWN",
            "reason": exc.code,
            "identity_verified": False,
            "control_written": False,
            "error": _safe_error(exc),
        }, MANAGE_EXIT["status"]
    base = {
        "ok": False,
        "tool": "manage_dsh",
        "action": "cancel",
        "status_file": str(path),
        "state": verification["state"],
        "reason": verification["reason"],
        "identity_verified": verification["identity_verified"],
        "fresh": verification["fresh"],
        "run_id": document.get("run_id"),
        "phase": document.get("phase"),
        "control_written": False,
        "execution": "wrapper_owned",
        "cancel_reason": sup.CANCEL_REASON,
    }
    if (verification["state"] != "RUNNING"
            or not verification["identity_verified"]
            or not verification["fresh"]):
        base["reason"] = verification["reason"] or "RUN_NOT_LIVE"
        base["error"] = {"category": "io", "code": "CANCEL_REFUSED_STATUS_NOT_LIVE"}
        return base, MANAGE_EXIT["not_live"]
    try:
        control = sup.write_control_request(path, document, sup.CANCEL_REASON)
    except sup.SupervisionError as exc:
        base["reason"] = exc.code
        base["error"] = _safe_error(exc)
        return base, MANAGE_EXIT["control"]
    base.update({
        "ok": True,
        "reason": "CANCEL_REQUEST_WRITTEN",
        "control_written": True,
        "control_file": str(control),
        "requested_reason": sup.CANCEL_REASON,
        "run_id": document.get("run_id"),
    })
    return base, MANAGE_EXIT["ok"]


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command == "prepare":
            try:
                response, code = artifacts.prepare_run(args.config, args.cwd), 0
            except artifacts.ArtifactError as exc:
                raise ManageError(exc.code, "config") from None
        elif args.command == "status":
            response, code = command_status(args)
        elif args.command == "finalize":
            response = artifacts.finalize_run(args.run_dir, args.disposition, args.delivered,
                                              args.dry_run, args.expected_plan)
            code = 0 if response["ok"] else 6
            if not args.dry_run and response["artifact_state"] == "closed":
                response["housekeeping"] = artifacts.opportunistic_gc(Path(response["dsh_home"]), args.run_dir)
        elif args.command == "gc":
            response = artifacts.gc_runs(artifacts._home(args.config), args.dry_run,
                                         args.limit, args.expected_plan)
            code = 0 if response["ok"] else 6
        elif args.command == "cancel":
            response, code = command_cancel(args)
        else:
            raise ManageError("ARGUMENT_INVALID", "config")
    except (ManageError, artifacts.ArtifactError) as exc:
        response = {
            "ok": False,
            "tool": "manage_dsh",
            "action": "unknown",
            "state": "UNKNOWN",
            "reason": exc.code,
            "identity_verified": False,
            "error": _safe_error(exc),
        }
        code = MANAGE_EXIT["usage"]
    print(json.dumps(response, ensure_ascii=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())
