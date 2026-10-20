#!/usr/bin/env python3
"""Bounded, credential-safe Harness readiness checks; never calls a model.

The small Node probes use the selected installation's own config, environment,
proxy and sandbox implementations. No .env content, composed configuration,
raw subprocess errors, proxy URLs, or authentication headers are reported.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time


MAX_OUTPUT = 65536
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SUPERVISION_DEFAULTS = {
    "heartbeat_seconds": 5,
    "stall_warning_seconds": 300,
    "shutdown_grace_seconds": 10,
    "finalization_grace_seconds": 10,
}


class CheckError(Exception):
    """A fixed, public diagnostic; never constructed from raw exception text."""


def stop_process(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def bounded_run(command: list[str], *, cwd: Path, env: dict[str, str],
                timeout: float = 30, limit: int = MAX_OUTPUT) -> tuple[int, bytes]:
    """Drain stdout into a bounded buffer; discard raw stderr at the source."""
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, shell=False,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL,
                                   start_new_session=os.name == "posix")
    except OSError:
        raise CheckError("process_start_failed") from None
    output = bytearray()
    overflow = threading.Event()

    def collect() -> None:
        assert process.stdout is not None
        with process.stdout as stream:
            while True:
                part = stream.read(8192)
                if not part:
                    break
                if len(output) + len(part) > limit:
                    overflow.set()
                    stop_process(process)
                    break
                output.extend(part)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_process(process)
        process.wait()
        reader.join(timeout=2)
        raise CheckError("probe_timeout") from None
    reader.join(timeout=2)
    if reader.is_alive():
        stop_process(process)
        reader.join(timeout=2)
        raise CheckError("probe_output_incomplete")
    if overflow.is_set():
        raise CheckError("probe_output_limit")
    return code, bytes(output)


# Every catch emits a fixed diagnostic. Raw library errors and stderr may contain
# .env values or proxy credentials and deliberately never reach our report.
NODE_PROBE = r"""
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
import {mkdtempSync,writeFileSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {spawnSync} from 'node:child_process';
const request=JSON.parse(process.argv[1]);
const root=request.root;
const req=createRequire(join(root,'package.json'));
const dependency=async name=>import(pathToFileURL(req.resolve(name)).href);
const emit=data=>process.stdout.write(JSON.stringify(data));
try {
  if(request.action==='compose') {
    const child=spawnSync(request.command[0],request.command.slice(1),{
      cwd:request.cwd,env:process.env,encoding:'utf8',timeout:20000,
      maxBuffer:2*1024*1024,stdio:['ignore','pipe','ignore']});
    if(child.error || child.status!==0) {
      emit({status:'FAIL',code:child.error?.code==='ETIMEDOUT'?'composition_timeout':'composition_failed',process_exit_code:child.status});
    } else {
      const yaml=req('js-yaml');
      const inert=yaml.JSON_SCHEMA.extend(new yaml.Type('tag:yaml.org,2002:js',{
        kind:'scalar',construct:value=>({__unevaluatedExpression:true})}));
      const entries=yaml.load(child.stdout,{schema:inert});
      if(!Array.isArray(entries)) throw Error('shape');
      const rows=[];
      const walk=items=>{for(const row of items){if(!row || typeof row!=='object')continue;
        rows.push(row);if(row.group && Array.isArray(row.config))walk(row.config);}};
      walk(entries);
      const models=rows.filter(row=>row.id==='agent-default-model' && !row.disabled);
      const model=models.length===1?models[0].config:undefined;
      const literal=value=>typeof value==='string' && /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/.test(value)?value:null;
      const selected=model && typeof model==='object'?{
        provider:literal(model.provider),model:literal(model.model),
        reasoningEffort:literal(model.reasoningEffort)}:null;
      const sandbox=rows.find(row=>!row.disabled && typeof row.name==='string' && row.name.includes('dsh-sandbox-local'));
      // Only the sandbox provider's documented startup options cross the pipe.
      const cfg=sandbox && sandbox.config && typeof sandbox.config==='object'?sandbox.config:{};
      const sandboxConfig={runnerCommand:Array.isArray(cfg.runnerCommand)?cfg.runnerCommand:[],
        runnerFailureSignatures:Array.isArray(cfg.runnerFailureSignatures)?cfg.runnerFailureSignatures:[],
        probeTimeoutMs:typeof cfg.probeTimeoutMs==='number'?Math.min(cfg.probeTimeoutMs,5000):5000};
      // Do not export configured runner argv (it can include private data).
      emit({status:selected?.provider && selected?.model && selected?.reasoningEffort?'PASS':'FAIL',
        code:selected?'composition_completed':'model_entry_missing_or_ambiguous',model:selected,
        sandbox_provider_present:Boolean(sandbox),
        sandbox_custom_runner:sandboxConfig.runnerCommand.length>0});
    }
  } else if(request.action==='environment') {
    const {loadLayeredEnv}=await dependency('@deepseek-ai/dsh-app-boot');
    let warnings=0;
    const snapshot=loadLayeredEnv('doctor',request.cwd,()=>{warnings++;});
    const key=snapshot.get('DEEPSEEK_API_KEY');
    const present=Boolean(key?.value?.trim());
    const source=key?.source??'missing';
    const category={process:'launch_environment','project-env':'cwd_dotenv','user-env':'dsh_home_dotenv',missing:'missing'}[source];
    const proxyNames=['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy','NO_PROXY','no_proxy'];
    const proxyInputs=proxyNames.flatMap(name=>{const entry=snapshot.get(name);return entry?[{
      variable:name,source:{process:'launch_environment','project-env':'cwd_dotenv','user-env':'dsh_home_dotenv'}[entry.source],
      nonempty:Boolean(entry.value.trim())}]:[];});
    let network={status:'NOT_RUN',code:'use_network_flag_for_head_request'};
    let rejectedProxyValues=0;
    const {installProxyFromEnvironment,proxyRouteFor}=await dependency('@deepseek-ai/dsh-http-proxy');
    const dispose=await installProxyFromEnvironment(snapshot,()=>{rejectedProxyValues++;});
    try {
      const url=new URL('https://api.deepseek.com/');
      const proxied=proxyRouteFor(url).proxied;
      if(request.network) {
        try {
          const response=await fetch(url,{method:'HEAD',redirect:'manual',signal:AbortSignal.timeout(10000)});
          network={status:'PASS',code:'http_endpoint_reachable',http_status:response.status,
            proxied,method:'HEAD',endpoint:'https://api.deepseek.com/',model_request:false,
            authentication_verified:false};
          await response.body?.cancel();
        }catch{network={status:'FAIL',code:'connection_failed_or_timed_out',proxied,
          method:'HEAD',endpoint:'https://api.deepseek.com/',model_request:false,
          cause:'not_classified_without_private_diagnostics'};}
      } else network.proxied=proxied;
    }finally{await dispose();}
    emit({status:warnings?'FAIL':'PASS',code:warnings?'dotenv_read_warning':'layered_environment_loaded',
      credential:{present,source:category,empty_value_shadows_fallback:Boolean(key && !present),
        authentication_verified:false},dotenv_warning_count:warnings,
      proxy:{inputs:proxyInputs,rejected_value_count:rejectedProxyValues,
        raw_values_reported:false},network});
  } else if(request.action==='sandbox') {
    const {Context}=await dependency('@deepseek-ai/cordis');
    const {LocalSandboxProvider}=await dependency('@deepseek-ai/dsh-sandbox-local');
    const context=new Context();
    const provider=new LocalSandboxProvider(context,{
      runnerCommand:[],runnerFailureSignatures:[],probeTimeoutMs:5000});
    let temporary;
    try {
      temporary=mkdtempSync(join(tmpdir(),'codex-dsh-doctor-'));
      const target=join(temporary,'read-only-probe.txt');
      writeFileSync(target,'sandbox-read-probe',{mode:0o600});
      const code="const fs=require('node:fs');const p=process.argv[1];"+
        "if(fs.readFileSync(p,'utf8')!=='sandbox-read-probe')process.exit(9);"+
        "try{fs.appendFileSync(p,'denied-write');process.exit(8)}catch(e){"+
        "if(!['EACCES','EPERM','EROFS'].includes(e.code))process.exit(7);"+
        "process.stdout.write('READ_OK_WRITE_DENIED')}";
      const wrapped=await provider.confine([process.execPath,'-e',code,target],{
        mode:'read-only',workspaceRoot:request.cwd});
      const child=spawnSync(wrapped.argv[0],wrapped.argv.slice(1),{
        cwd:request.cwd,env:process.env,timeout:10000,maxBuffer:1024,
        encoding:'utf8',stdio:['ignore','pipe','ignore']});
      const passed=child.status===0 && child.stdout==='READ_OK_WRITE_DENIED' &&
        readFileSync(target,'utf8')==='sandbox-read-probe';
      const selected=provider.selectedRunner;
      emit({status:passed?'PASS':'FAIL',code:passed?'actual_confined_read_and_write_denial':'sandbox_execution_failed',
        runner:selected?.runner??'unidentified',enforcement:wrapped.enforcement,
        process_exit_code:child.status,actual_command_executed:passed,
        file_write_denial_verified:passed,policy:'read-only',
        scope:'installed_Harness_builtin_backend_no_profile_or_model_boot'});
    }catch(error){emit({status:'FAIL',code:error?.code==='SANDBOX_UNAVAILABLE'?'SANDBOX_UNAVAILABLE':'sandbox_probe_failed',
      actual_command_executed:false,policy:'read-only'});}
    finally{if(temporary)rmSync(temporary,{recursive:true,force:true});await context.fiber.dispose();}
  } else throw Error('action');
}catch{emit({status:'FAIL',code:request.action==='environment'?'layered_environment_rejected_or_unavailable':'probe_failed'});}
"""


def absolute_path(value: object, label: str, *, directory: bool = False) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise CheckError(label + "_must_be_absolute")
    path = Path(value)
    if directory and not path.is_dir():
        raise CheckError(label + "_directory_missing")
    return path


def installation_root(command: list[str]) -> Path:
    for argument in command:
        candidate = Path(argument)
        if not candidate.is_absolute() or not candidate.is_file():
            continue
        candidate = candidate.resolve()
        for parent in list(candidate.parents)[:4]:
            package = parent / "package.json"
            if not package.is_file():
                continue
            try:
                metadata = json.loads(package.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if metadata.get("name") == "@deepseek-ai/dsh":
                return parent
    raise CheckError("installed_dsh_package_not_resolved")


def check_access(path: Path) -> dict:
    """A real owned temporary file verifies directory write access."""
    result = {"path": str(path), "status": "FAIL", "readable": False,
              "writable": False, "searchable": False}
    if not path.is_dir():
        result["code"] = "directory_missing"
        return result
    result.update(readable=os.access(path, os.R_OK), searchable=os.access(path, os.X_OK))
    try:
        with tempfile.TemporaryFile(dir=path) as probe:
            probe.write(b"codex-dsh-access-check")
            probe.flush()
            probe.seek(0)
            result["writable"] = probe.read() == b"codex-dsh-access-check"
    except OSError:
        result["code"] = "directory_write_probe_failed"
    if all(result[key] for key in ("readable", "writable", "searchable")):
        result.update(status="PASS", code="directory_access_verified")
    return result


def positive_number(value: object) -> bool:
    """Finite, strictly positive JSON numbers only; booleans are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value)) and value > 0
    except (OverflowError, ValueError):
        return False


def supervision_settings(config: dict) -> dict:
    """Validate the schema-specific hard budget and supervision overrides.

    schema1 keeps its historical mandatory timeout_seconds hard budget. schema2
    has no implicit deadline: absent or null hard_timeout_seconds is valid and
    means the launcher waits for completion, a cancellation, or an explicit
    --timeout. Soft stall thresholds are advisory and never convert a live run
    into a failure by themselves.
    """
    schema_version = config.get("schema_version")
    if type(schema_version) is not int:
        raise CheckError("unsupported_config_schema")
    if schema_version == 1:
        timeout = config.get("timeout_seconds")
        if not positive_number(timeout):
            raise CheckError("invalid_timeout_seconds")
        hard_timeout: float | None = float(timeout)
        hard_timeout_source = "legacy_timeout_seconds"
    elif schema_version == 2:
        hard_timeout = config.get("hard_timeout_seconds")
        if hard_timeout is None:
            hard_timeout_source = "no_implicit_hard_deadline"
        else:
            if not positive_number(hard_timeout):
                raise CheckError("invalid_hard_timeout_seconds")
            hard_timeout = float(hard_timeout)
            hard_timeout_source = "hard_timeout_seconds"
    else:
        raise CheckError("unsupported_config_schema")

    values: dict[str, float] = {}
    sources: dict[str, str] = {}
    for key, default in SUPERVISION_DEFAULTS.items():
        if key in config:
            if not positive_number(config[key]):
                raise CheckError("invalid_" + key)
            values[key] = float(config[key])
            sources[key] = "config"
        else:
            values[key] = float(default)
            sources[key] = "default"

    return {
        "schema_version": schema_version,
        "hard_timeout_seconds": hard_timeout,
        "hard_timeout_source": hard_timeout_source,
        **values,
        "sources": sources,
        "capability_boundaries": {
            "soft_stall_warning": "advisory_only_never_terminates_or_marks_failure",
            "heartbeat": "liveness_signal_not_event_or_tool_progress",
            "status_file": "optional_private_atomic_observability_snapshot",
            "cancel": "requires_matching_run_id_and_process_identity",
            "finalization": "post_final_hang_is_independent_cleanup_anomaly_answer_preserved",
            "monitoring_loss": "never_starts_a_second_task",
            "hard_deadline": ("not_configured_no_implicit_wall_clock_deadline"
                              if hard_timeout is None
                              else "configured_hard_budget_terminates_owned_processes"),
        },
        "runtime_verification": "NOT_RUN",
        "status_file_acceptance": "NOT_RUN",
        "cancel_acceptance": "NOT_RUN",
    }


def run_doctor(arguments: argparse.Namespace) -> dict:
    config_path = absolute_path(arguments.config, "config")
    cwd = absolute_path(arguments.cwd, "cwd", directory=True)
    try:
        if config_path.stat().st_size > MAX_OUTPUT:
            raise CheckError("config_size_limit")
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CheckError("config_unreadable_or_invalid_json") from None
    schema_version = config.get("schema_version") if isinstance(config, dict) else None
    if type(schema_version) is not int or schema_version not in (1, 2):
        raise CheckError("unsupported_config_schema")
    command = config.get("dsh_command")
    if not isinstance(command, list) or not command or any(not isinstance(item, str) or not item or "\0" in item for item in command):
        raise CheckError("invalid_dsh_command")
    executable = absolute_path(command[0], "dsh_executable")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise CheckError("dsh_entry_not_executable")
    home = absolute_path(config.get("dsh_home"), "dsh_home", directory=True)
    log_dir = absolute_path(config.get("log_dir"), "log_dir", directory=True)
    default_profile = config.get("default_profile")
    if not isinstance(default_profile, str) or not SAFE_NAME.fullmatch(default_profile):
        raise CheckError("invalid_default_profile")
    profile = arguments.profile or default_profile
    if not SAFE_NAME.fullmatch(profile):
        raise CheckError("invalid_profile")
    defaults = config.get("default_patches")
    if not isinstance(defaults, list):
        raise CheckError("invalid_default_patches")
    patches = defaults + arguments.patch
    for patch in patches:
        path = absolute_path(patch, "patch")
        if not path.is_file() or not os.access(path, os.R_OK):
            raise CheckError("patch_unreadable")
    supervision = supervision_settings(config)
    root = installation_root(command)
    node = executable if executable.name in ("node", "node.exe", "nodejs") else Path(shutil.which("node") or "")
    if not node.is_absolute() or not node.is_file() or not os.access(node, os.X_OK):
        raise CheckError("node_not_executable")
    env = os.environ.copy()
    env["DSH_HOME"] = str(home)
    report = {"schema_version": 1, "config_schema_version": schema_version,
              "scope": "readiness_only_no_model_request",
              "config": str(config_path), "cwd": str(cwd), "dsh_home": str(home),
              "selection": {"profile": profile, "patches": patches,
                            "default_patches": defaults, "call_patches": arguments.patch},
              "supervision": supervision,
              "checks": {}, "configurations": []}
    checks = report["checks"]

    def probe(action: str, **extra: object) -> dict:
        payload = {"action": action, "root": str(root), "cwd": str(cwd), **extra}
        try:
            code, data = bounded_run([str(node), "--input-type=module", "-e", NODE_PROBE,
                                      json.dumps(payload)], cwd=cwd, env=env)
            value = json.loads(data)
            if code != 0 or not isinstance(value, dict):
                raise CheckError("probe_failed")
            return value
        except (CheckError, ValueError) as error:
            return {"status": "FAIL", "code": str(error) if isinstance(error, CheckError) else "probe_invalid_response"}

    checks["python"] = {"status": "PASS", "path": sys.executable,
                        "version": ".".join(map(str, sys.version_info[:3]))}
    for name, version_command in (("node", [str(node), "--version"]),
                                  ("dsh", command + ["--version"])):
        try:
            code, data = bounded_run(version_command, cwd=cwd, env=env, timeout=15, limit=8192)
            versions = re.findall(rb"\bv?\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?\b", data)
            checks[name] = {"status": "PASS" if code == 0 and versions else "FAIL",
                            "path": str(node) if name == "node" else command,
                            "version": versions[-1].decode("ascii") if versions else None,
                            "process_exit_code": code}
        except CheckError as error:
            checks[name] = {"status": "FAIL", "code": str(error)}
    checks["directories"] = [check_access(path) for path in (cwd, home, log_dir)]
    # Compose only the selected configuration; no local profile or model names
    # are assumed. A caller that wants extra combinations can pass patches.
    combinations = [("default", default_profile, list(defaults)),
                    ("requested", profile, patches)]
    seen_combinations = set()
    for label, selected_profile, overlays in combinations:
        key = (selected_profile, tuple(overlays))
        if key in seen_combinations:
            continue
        seen_combinations.add(key)
        profile_path = home / "profiles" / selected_profile
        if not profile_path.is_dir():
            result = {"status": "FAIL", "code": "profile_directory_missing"}
        else:
            dump_command = command + ["--profile", selected_profile]
            for overlay in overlays:
                dump_command += ["--patch", overlay]
            result = probe("compose", command=dump_command + ["--dump-config"])
        result.update(label=label, profile=selected_profile, patches=overlays)
        report["configurations"].append(result)
    checks["environment"] = probe("environment", network=arguments.network)
    selected = report["configurations"][-1]
    if selected.get("sandbox_custom_runner"):
        checks["sandbox"] = {"status": "NOT_RUN", "code": "custom_profile_runner_requires_separate_probe",
                             "scope": "builtin_probe_would_not_verify_custom_runner"}
    elif not selected.get("sandbox_provider_present"):
        checks["sandbox"] = {"status": "FAIL", "code": "configured_sandbox_provider_missing"}
    else:
        checks["sandbox"] = probe("sandbox")
    failures = []
    for name, value in checks.items():
        if name == "directories":
            if any(item["status"] == "FAIL" for item in value):
                failures.append("directories")
        elif value.get("status") == "FAIL":
            failures.append(name)
    if any(item["status"] == "FAIL" for item in report["configurations"]):
        failures.append("configuration_composition")
    environment = checks["environment"]
    if environment.get("credential", {}).get("present") is not True:
        failures.append("credential_missing_or_shadowed_or_unverified")
    if environment.get("network", {}).get("status") == "FAIL":
        failures.append("network")
    unverified = []
    if environment.get("network", {}).get("status") != "PASS":
        unverified.append("network_reachability")
    if checks["sandbox"].get("status") != "PASS":
        unverified.append("command_sandbox")
    report.update(ready_for_model_attempt=not failures and not unverified,
                  status="FAIL" if failures else "PARTIAL" if unverified else "PASS",
                  failures=failures, unverified=unverified,
                  real_deepseek_api_acceptance="NOT_RUN",
                  credential_authentication="NOT_RUN",
                  runtime_model_parameters="NOT_RUN",
                  notes=["Dump evidence proves composition only.",
                         "HTTP reachability does not verify credentials or model availability.",
                         "Sandbox probe uses the installed builtin provider without booting a model.",
                         "Supervision values are validated policy metadata, not proof of an active status file.",
                         "A soft stall warning never terminates a live run or turns it into a failure.",
                         "schema2 without a configured hard timeout has no implicit wall-clock deadline."])
    report["wrapper_exit_code"] = 2 if failures else 3 if unverified else 0
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--patch", action="append", default=[])
    parser.add_argument("--network", action="store_true", help="send an unauthenticated HEAD with the Harness proxy policy")
    args = parser.parse_args()
    started = time.monotonic()
    try:
        report = run_doctor(args)
    except CheckError as error:
        report = {"schema_version": 1, "status": "FAIL", "scope": "readiness_only_no_model_request",
                  "error": str(error), "wrapper_exit_code": 2,
                  "real_deepseek_api_acceptance": "NOT_RUN"}
    except (OSError, ValueError, TypeError):
        report = {"schema_version": 1, "status": "FAIL", "scope": "readiness_only_no_model_request",
                  "error": "doctor_internal_or_io_error", "wrapper_exit_code": 2,
                  "real_deepseek_api_acceptance": "NOT_RUN"}
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report["wrapper_exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
