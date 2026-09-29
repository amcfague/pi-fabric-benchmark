#!/usr/bin/env python3
"""Isolated local benchmark for stock Pi, pi-subagents, and pi-fabric."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
PI_BINARY = "/usr/local/bin/pi"
PROVIDER = "openai"
MODEL = "gpt-6-sol"
THINKING = "xhigh"
CHILD_CAP = 3
DEFAULT_DEADLINE = 900
CORE_TOOLS = ("read", "grep", "find", "ls", "bash", "edit", "write")
ARMS = ("stock", "subagents", "fabric")
CASES = ("triage", "patch")
PINS = {
    "subagents": {
        "name": "pi-subagents", "version": "0.73.1",
        "integrity": "sha512-IklOqw67DtvIWQFKxFJpDFXsOGcq8RGYZEbokykD5Kvfs9aa/du1cW2NEJ5xx4GUgN2LVt3vAq5IdzEMo7YwJw==",
    },
    "fabric": {
        "name": "pi-fabric", "version": "0.100.0",
        "integrity": "sha512-tW1DBe8cZYN91yezHHud5R3nLCV8LU2/1rS69f9u0LnsuuDXBVpxbEa06o2sbiJuANMxIJ1PGf+2wGXdGWqAmg==",
    },
}


def default_cache_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "pi-parallel-benchmark"
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pi-parallel-benchmark"


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def package_root(arm: str, cache_root: Path) -> Path:
    pin = PINS[arm]
    return cache_root / "packages" / arm / "node_modules" / pin["name"]


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative + b"\0")
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def npm_integrity(spec: str) -> str:
    result = subprocess.run(
        ["npm", "view", spec, "dist.integrity", "--json"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode:
        raise RuntimeError(f"npm view failed for {spec}: {result.stderr.strip()}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"npm returned invalid integrity for {spec}") from error
    if isinstance(value, dict):
        value = value.get("dist", {}).get("integrity") or value.get("integrity")
    if not isinstance(value, str):
        raise RuntimeError(f"npm did not return an integrity for {spec}")
    return value


def inspect_package(arm: str, cache_root: Path) -> dict[str, Any]:
    pin = PINS[arm]
    root = package_root(arm, cache_root).resolve()
    metadata_path = root / "package.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"pinned package is not installed: {metadata_path}")
    metadata = json.loads(metadata_path.read_text())
    if metadata.get("name") != pin["name"] or metadata.get("version") != pin["version"]:
        raise ValueError(f"wrong package installed at {root}; expected {pin['name']}@{pin['version']}")
    extensions = metadata.get("pi", {}).get("extensions", [])
    if len(extensions) != 1:
        raise ValueError(f"{pin['name']} must declare exactly one Pi extension")
    entry = (root / extensions[0]).resolve()
    if not entry.is_file() or not entry.is_relative_to(root):
        raise ValueError(f"invalid extension entry point for {pin['name']}: {entry}")
    prefix = root.parents[1]
    lock_path = prefix / "package-lock.json"
    lock_integrity = None
    if lock_path.is_file():
        lock = json.loads(lock_path.read_text())
        package_lock = lock.get("packages", {}).get(f"node_modules/{pin['name']}", {})
        lock_integrity = package_lock.get("integrity")
        if lock_integrity and lock_integrity != pin["integrity"]:
            raise ValueError(f"npm lock integrity does not match pinned {pin['name']}@{pin['version']}")
    return {
        "name": pin["name"], "version": pin["version"],
        "integrity": pin["integrity"], "lock_integrity": lock_integrity,
        "integrity_verified": lock_integrity == pin["integrity"],
        "sha256": tree_sha256(root), "root": str(root), "entry": str(entry),
    }


def install_packages(cache_root: Path) -> dict[str, Any]:
    private_dir(cache_root)
    installed = {}
    for arm, pin in PINS.items():
        spec = f"{pin['name']}@{pin['version']}"
        actual = npm_integrity(spec)
        if actual != pin["integrity"]:
            raise RuntimeError(f"registry integrity changed for {spec}; expected pinned tarball")
        prefix = private_dir(cache_root / "packages" / arm)
        result = subprocess.run(
            ["npm", "install", "--prefix", str(prefix), "--save-exact", "--ignore-scripts",
             "--no-audit", "--no-fund", spec],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode:
            raise RuntimeError(f"npm install failed for {spec}: {result.stderr.strip()}")
        info = inspect_package(arm, cache_root)
        if not info["integrity_verified"]:
            raise RuntimeError(f"npm lock did not verify the tarball integrity for {spec}")
        info["registry_integrity_verified"] = True
        installed[arm] = info
    _write_json(cache_root / "package-manifest.json", {"version": 1, "packages": installed})
    return installed


def validate_pi_binary(binary: str, env: dict[str, str]) -> None:
    path = Path(binary)
    resolved = Path(os.path.realpath(binary))
    if path.name == "pi.real" or resolved.name == "pi.real":
        if not env.get("http_proxy") or not env.get("https_proxy"):
            raise ValueError("direct pi.real requires both lowercase http_proxy and https_proxy")
    if not path.is_file() or not os.access(path, os.X_OK):
        raise FileNotFoundError(f"Pi wrapper is not executable: {binary}")


def build_command(
    arm: str, prompt: str, session_dir: str | Path,
    extension_paths: dict[str, str | Path], pi_binary: str = PI_BINARY,
) -> list[str]:
    if arm not in ARMS:
        raise ValueError(f"unknown arm: {arm}")
    command = [
        pi_binary, "--mode", "json", "--provider", PROVIDER, "--model", MODEL,
        "--thinking", THINKING, "--session-dir", str(session_dir),
        "--tools", ",".join(CORE_TOOLS), "--no-extensions", "--no-skills",
        "--no-prompt-templates", "--no-themes", "--no-context-files", "--no-approve",
    ]
    if arm != "stock":
        entry = Path(extension_paths[arm])
        if not entry.is_absolute():
            raise ValueError(f"extension path must be absolute: {entry}")
        command.extend(["-e", str(entry)])
    command.extend(["--print", prompt])
    return command


def build_environment(
    cell_dir: Path, source: dict[str, str] | None = None,
    pi_binary: str = PI_BINARY,
) -> dict[str, str]:
    source = os.environ if source is None else source
    allowed = (
        "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE",
        "TERM", "TZ", "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY",
        "HTTPS_PROXY", "NO_PROXY", "OPENAI_API_KEY", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    )
    env = {key: source[key] for key in allowed if source.get(key)}
    binary_dir = str(Path(pi_binary).parent)
    env["PATH"] = binary_dir + (os.pathsep + env["PATH"] if env.get("PATH") else "")
    agent_dir = private_dir(cell_dir / "agent")
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    env["PI_SUBAGENT_PI_BINARY"] = pi_binary
    env["PI_FABRIC_PI_BINARY"] = pi_binary
    validate_pi_binary(pi_binary, env)
    return env


def _child_tasks(case: str, project_dir: Path) -> list[dict[str, str]]:
    files = ("catalog.py", "billing.py", "shipping.py")
    tasks = []
    for filename in files:
        module = filename.removesuffix(".py")
        if case == "triage":
            task = (
                f"Read only {filename} in {project_dir}. Report the current expression and output, "
                "the contract-correct expression and output for the input assigned here, and return "
                "one JSON object for your module only. "
                + {
                    "catalog": "Input: available_units(11, 4); expected available stock is max(0, stock-reserved).",
                    "billing": "Input: subtotal_cents(1250, 4); subtotal is integer unit price multiplied by quantity.",
                    "shipping": "Input: shipping_fee_cents(5000); free shipping applies at the threshold.",
                }[module]
            )
        else:
            contract = {
                "catalog": "subtract reservations from stock and clamp at zero",
                "billing": "multiply integer cents by quantity",
                "shipping": "waive shipping at and above the threshold",
            }[module]
            task = (
                f"Modify only {filename} in {project_dir}. Fix its behavior to {contract}; preserve "
                "the public function name, signature, and return shape. Do not edit other files."
            )
        tasks.append({"key": module, "task": task})
    return tasks


def build_prompt(case: str, arm: str, project_dir: Path, deadline_seconds: int = DEFAULT_DEADLINE) -> str:
    if case not in CASES or arm not in ARMS:
        raise ValueError("unknown case or arm")
    case_text = (ROOT / "cases" / f"{case}.md").read_text()
    base = f"{case_text}\n\nThe writable fixture is {project_dir}. Use no files outside it."
    if arm == "stock":
        return base
    tasks = _child_tasks(case, project_dir)
    child_timeout = deadline_seconds * 1000
    if arm == "subagents":
        children = [
            {
                "key": task["key"], "agent": "worker", "task": task["task"],
                "model": f"{PROVIDER}/{MODEL}:{THINKING}",
                "tools": ",".join(CORE_TOOLS), "extensions": [], "skills": [],
                "context": "fresh", "cwd": str(project_dir), "worktree": False,
                "timeoutMs": child_timeout,
            }
            for task in tasks
        ]
        script = (
            "const children = " + json.dumps(children, separators=(",", ":")) + ";\n"
            "const results = await runs.all(children);\nreturn { children: results };"
        )
        return (
            base + f"\n\nDelegate exactly these three independent tasks in parallel; child cap is {CHILD_CAP}. "
            "Use the pi-subagents workflow, never another runtime. If only subagents_enable is "
            "available, call it first, then make exactly one top-level subagent workflow call with "
            "this script and async:true; wait for its completion before answering. The workflow "
            "must use runs.all once. Each child has extensions: [] and skills: []; do not change "
            "the child model, tools, cwd, or deadline. Return the three child results to the parent.\n\n"
            "workflowScript = " + json.dumps(script) + "\n"
            f"Outer request: cwd={json.dumps(str(project_dir))}, async=true, "
            f"timeoutMs={child_timeout}, mission=false."
        )
    specs = [
        {"key": task["key"], "task": task["task"]} for task in tasks
    ]
    script = (
        "const specs = " + json.dumps(specs, separators=(",", ":")) + ";\n"
        "const children = await Promise.all(specs.map(async (spec) => {\n"
        "  const startedAt = Date.now();\n"
        "  const result = await agents.run({\n"
        f"    name: 'benchmark-' + spec.key, task: spec.task, runner: 'pi', model: '{PROVIDER}/{MODEL}', thinking: '{THINKING}',\n"
        f"    tools: {json.dumps(list(CORE_TOOLS))}, extensions: false, recursive: false,\n"
        f"    cwd: {json.dumps(str(project_dir))}, worktree: false, timeoutMs: {child_timeout},\n"
        "  });\n"
        "  return { key: spec.key, startedAt, endedAt: Date.now(), result: {\n"
        "    id: result.id, status: result.status, text: result.text, usage: result.usage,\n"
        "    turns: result.turns, toolCalls: result.toolCalls, runnerSessionId: result.runnerSessionId,\n"
        "  }};\n}));\n"
        "console.log(JSON.stringify({ children }));\nreturn { children };"
    )
    return (
        base + f"\n\nDelegate exactly these three independent tasks concurrently; child cap is {CHILD_CAP}. "
        "Call the loaded fabric_exec tool once with the following TypeScript. Do not perform the "
        "tasks in the parent. Each agents.run leaf must use extensions:false, the same model, "
        "thinking level, core tools, cwd, and deadline shown in the code. Return the child records "
        "and synthesize only after all three finish.\n\n"
        "fabric_exec code = " + json.dumps(script)
    )


def _pump(stream: Any, path: Path) -> None:
    with path.open("wb") as output:
        for line in iter(stream.readline, b""):
            output.write(line)
            output.flush()


def run_process(
    command: list[str], cwd: Path, env: dict[str, str], event_path: Path, stderr_path: Path,
    deadline_seconds: float, terminate_grace_seconds: float = 3.0,
) -> dict[str, Any]:
    event_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    process = subprocess.Popen(
        command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=(os.name == "posix"),
    )
    stdout_thread = threading.Thread(target=_pump, args=(process.stdout, event_path), daemon=True)
    stderr_thread = threading.Thread(target=_pump, args=(process.stderr, stderr_path), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    timed_out = False
    try:
        process.wait(timeout=deadline_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=terminate_grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            process.wait()
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    process.stdout.close()
    process.stderr.close()
    return {
        "exit_code": process.returncode, "timed_out": timed_out,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "termination_reason": "deadline" if timed_out else "process_exit",
    }


def read_events(path: Path) -> tuple[list[dict[str, Any]], int]:
    events, malformed = [], 0
    if not path.is_file():
        return events, 0
    for line in path.read_text(errors="replace").splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                events.append(value)
        except json.JSONDecodeError:
            malformed += 1
    return events, malformed


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _usage_record(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    source = raw
    if isinstance(raw.get("usage"), dict):
        source = raw["usage"]
    if isinstance(source.get("totalTokens"), dict):
        tokens = source["totalTokens"]
        source = {**source, **tokens}
    aliases = {
        "input_tokens": ("input", "inputTokens", "input_tokens", "prompt_tokens"),
        "output_tokens": ("output", "outputTokens", "output_tokens", "completion_tokens"),
        "cache_read_tokens": ("cacheRead", "cacheReadInputTokens", "cache_read_input_tokens"),
        "cache_write_tokens": ("cacheWrite", "cacheCreationInputTokens", "cache_creation_input_tokens"),
        "total_tokens": ("totalTokens", "total_tokens", "tokens"),
    }
    values: dict[str, Any] = {}
    for target, names in aliases.items():
        value = next((source[name] for name in names if isinstance(source.get(name), (int, float))), None)
        values[target] = value
    if values["total_tokens"] is None and values["input_tokens"] is not None and values["output_tokens"] is not None:
        values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
    cost = source.get("costUsd", source.get("cost_usd"))
    if cost is None and isinstance(source.get("cost"), dict):
        cost = source["cost"].get("total")
    if cost is None and isinstance(source.get("totalCost"), dict):
        cost = source["totalCost"].get("costUsd", source["totalCost"].get("total"))
    values["cost_usd"] = cost if isinstance(cost, (int, float)) else None
    values["known"] = values["total_tokens"] is not None
    return values


def _sum_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    fields = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens", "cost_usd")
    output: dict[str, Any] = {field: None for field in fields}
    output["known"] = bool(records) and all(item.get("known") for item in records)
    for field in fields:
        values = [item.get(field) for item in records]
        if values and all(isinstance(value, (int, float)) for value in values):
            output[field] = sum(values)
    return output


def parse_events(events: list[dict[str, Any]], exit_code: int | None) -> dict[str, Any]:
    settled = False
    agent_end = False
    assistant_messages: list[dict[str, Any]] = []
    usage_by_id: dict[str, dict[str, Any]] = {}
    for index, event in enumerate(events):
        kind = event.get("type", event.get("event"))
        if kind == "agent_settled":
            settled = True
        elif kind == "agent_end":
            agent_end = True
        message = event.get("message")
        if kind == "message_end" and isinstance(message, dict) and message.get("role") == "assistant":
            assistant_messages.append(message)
            usage = _usage_record(message.get("usage"))
            if usage:
                usage_by_id[str(message.get("id", index))] = usage
    final = assistant_messages[-1] if assistant_messages else {}
    stop = final.get("stopReason", final.get("stop_reason"))
    answer = _text(final.get("content"))
    usage = _sum_usage(list(usage_by_id.values()))
    return {
        "settled": settled, "agent_end": agent_end, "final_stop_reason": stop,
        "answer": answer, "usage": usage,
        "success": exit_code == 0 and settled and stop in ("stop", "end_turn", "endTurn"),
    }


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            yield from _walk(json.loads(value))
        except json.JSONDecodeError:
            return


def _time_value(record: dict[str, Any], *keys: str) -> int | float | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, (int, float)):
            return value
    return None


def extract_children(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for event in events:
        for record in _walk(event):
            identifier = record.get("runId", record.get("runnerSessionId", record.get("childId", record.get("key"))))
            start = _time_value(record, "startedAt", "started_at", "startTime")
            end = _time_value(record, "endedAt", "ended_at", "endTime")
            if identifier is None and isinstance(record.get("agent"), str) and start is not None:
                identifier = f"{record['agent']}:{start}"
            if not isinstance(identifier, (str, int)):
                continue
            usage = (_usage_record(record.get("usage")) or _usage_record(record.get("result"))
                     or _usage_record(record))
            if start is None or end is None or end <= start:
                continue
            item = found.setdefault(str(identifier), {"id": str(identifier)})
            item.update({key: value for key, value in record.items() if key in ("key", "agent", "status", "state", "runId", "runnerSessionId")})
            if start is not None:
                item["startedAt"] = start
            if end is not None:
                item["endedAt"] = end
            if usage is not None:
                item["usage"] = usage
    return list(found.values())


def overlap_summary(children: list[dict[str, Any]]) -> dict[str, Any]:
    intervals = []
    for child in children:
        start = _time_value(child, "startedAt", "started_at", "startTime")
        end = _time_value(child, "endedAt", "ended_at", "endTime")
        if start is not None and end is not None and end > start:
            intervals.append((start, 1))
            intervals.append((end, -1))
    active = maximum = 0
    for _, delta in sorted(intervals, key=lambda item: (item[0], item[1])):
        active += delta
        maximum = max(maximum, active)
    return {
        "interval_count": len(intervals) // 2, "max_concurrency": maximum,
        "evidenced": maximum >= 2,
    }


def _tool_calls(events: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    calls = []
    for event in events:
        kind = event.get("type", event.get("event"))
        if kind not in ("tool_execution_start", "tool_call", "tool_start"):
            continue
        name = event.get("toolName", event.get("name", event.get("tool")))
        args = event.get("args", event.get("input", event.get("arguments", event.get("parameters"))))
        if isinstance(name, str):
            calls.append((name, args))
    return calls


def _text_values(value: Any):
    if isinstance(value, str):
        yield value.replace('\\"', '"')
        if value.lstrip().startswith(("{", "[")):
            try:
                yield from _text_values(json.loads(value))
            except json.JSONDecodeError:
                pass
    elif isinstance(value, dict):
        for child in value.values():
            yield from _text_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _text_values(child)


def child_launch_evidence(arm: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    calls = _tool_calls(events)
    if arm == "subagents":
        scripts = [" ".join(_text_values(args)) for name, args in calls if name == "subagent"]
        valid = any(
            "runs.all" in script
            and len(re.findall(r"extensions[\"\']?\s*:\s*\[\s*\]", script)) >= CHILD_CAP
            and "skills" in script and "openai/gpt-6-sol:xhigh" in script
            for script in scripts
        )
        return {"verified": valid, "tool_calls": [name for name, _ in calls], "reason": "subagent workflow request with empty child extensions" if valid else "workflow child loadout not observable"}
    if arm == "fabric":
        scripts = [" ".join(_text_values(args)) for name, args in calls if name == "fabric_exec"]
        valid = any(
            "agents.run" in script and "Promise.all" in script
            and re.search(r"extensions[\"\']?\s*:\s*false", script)
            and len(re.findall(r"[\"\']key[\"\']\s*:", script)) >= CHILD_CAP
            and "openai/gpt-6-sol" in script
            for script in scripts
        )
        return {"verified": valid, "tool_calls": [name for name, _ in calls], "reason": "Fabric leaf request disables extensions" if valid else "Fabric child loadout not observable"}
    return {"verified": True, "tool_calls": [name for name, _ in calls], "reason": "stock Pi has no delegated children"}


def _grade(case: str, project: Path, answer: str) -> dict[str, Any]:
    if case == "triage":
        try:
            parsed = json.loads(answer)
        except json.JSONDecodeError as error:
            return {"passed": False, "errors": [f"final answer is not JSON: {error.msg}"]}
        from checks.triage import verify
        return verify(parsed)
    result = subprocess.run(
        [sys.executable, str(ROOT / "checks" / "patch.py"), str(project)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        grade = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"passed": False, "errors": ["patch checker did not return JSON", result.stderr[-1000:]]}
    if result.returncode and grade.get("passed"):
        return {"passed": False, "errors": ["patch checker exit status disagrees with its result"], "result": grade}
    return grade


def preflight(cache_root: Path, pi_binary: str = PI_BINARY, source_env: dict[str, str] | None = None) -> dict[str, Any]:
    source_env = os.environ if source_env is None else source_env
    errors = []
    try:
        validate_pi_binary(pi_binary, dict(source_env))
        version = subprocess.run([pi_binary, "--version"], capture_output=True, text=True, timeout=20)
        help_result = subprocess.run([pi_binary, "--help"], capture_output=True, text=True, timeout=30)
        if version.returncode or help_result.returncode:
            raise RuntimeError("Pi wrapper --version/--help failed")
        required = ("--mode", "--provider", "--model", "--thinking", "--session-dir", "--tools", "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files", "--no-approve")
        if any(flag not in help_result.stdout for flag in required):
            raise RuntimeError("Pi wrapper lacks one or more required CLI flags")
        pi_version = version.stdout.strip()
    except Exception as error:
        pi_version = None
        errors.append(f"Pi wrapper preflight: {error}")
    if not source_env.get("OPENAI_API_KEY"):
        errors.append("OPENAI_API_KEY is missing")
    packages = {}
    manifest_path = cache_root / "package-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError):
        manifest = {"packages": {}}
        errors.append("pinned package manifest is missing or invalid; run benchmark.py install")
    for arm in PINS:
        try:
            actual = npm_integrity(f"{PINS[arm]['name']}@{PINS[arm]['version']}")
            if actual != PINS[arm]["integrity"]:
                raise ValueError("registry integrity differs from the pinned tarball")
            info = inspect_package(arm, cache_root)
            info["registry_integrity_verified"] = True
            if not info["integrity_verified"]:
                raise ValueError("local npm lock does not verify the pinned tarball")
            saved = manifest.get("packages", {}).get(arm, {})
            if saved.get("sha256") != info["sha256"]:
                raise ValueError("installed package tree differs from the recorded hash; rerun install")
            if saved.get("integrity") != PINS[arm]["integrity"]:
                raise ValueError("package manifest does not match the pinned tarball")
            packages[arm] = info
        except Exception as error:
            errors.append(f"{arm} package preflight: {error}")
    return {
        "passed": not errors, "errors": errors, "pi_binary": pi_binary,
        "pi_version": pi_version, "provider": PROVIDER, "model": MODEL,
        "thinking": THINKING, "credential_present": bool(source_env.get("OPENAI_API_KEY")),
        "package_info": packages,
        "model_connectivity": "verified by first live cell; no separate billable probe",
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def _seed_hash() -> str:
    return tree_sha256(ROOT / "fixtures" / "project")


def run_cell(
    run_dir: Path, case: str, arm: str, repetition: int, deadline_seconds: int,
    packages: dict[str, dict[str, Any]], pi_binary: str = PI_BINARY,
    source_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    cell_dir = run_dir / "cells" / f"{case}-{arm}-r{repetition:02d}"
    private_dir(cell_dir)
    project = cell_dir / "project"
    shutil.copytree(ROOT / "fixtures" / "project", project)
    session_dir = private_dir(cell_dir / "session")
    env = build_environment(cell_dir, source_env, pi_binary)
    package_paths = {key: value["entry"] for key, value in packages.items()}
    prompt = build_prompt(case, arm, project, deadline_seconds)
    command = build_command(arm, prompt, session_dir, package_paths, pi_binary)
    (cell_dir / "prompt.txt").write_text(prompt)
    _write_json(cell_dir / "argv.json", command)
    environment_info = {
        "agent_dir": env["PI_CODING_AGENT_DIR"], "path_prefix": str(Path(pi_binary).parent),
        "credential_present": bool(env.get("OPENAI_API_KEY")),
        "environment_keys": sorted(key for key in env if key.endswith("_API_KEY") or "proxy" in key.lower()),
        "child_binary": pi_binary,
    }
    result = run_process(
        command, project, env, cell_dir / "events.jsonl", cell_dir / "stderr.log",
        deadline_seconds,
    )
    events, malformed = read_events(cell_dir / "events.jsonl")
    agent = parse_events(events, result["exit_code"])
    children = extract_children(events)
    overlap = overlap_summary(children)
    launches = child_launch_evidence(arm, events)
    child_usage_records = [item["usage"] for item in children if isinstance(item.get("usage"), dict)]
    child_usage = _sum_usage(child_usage_records)
    if len(child_usage_records) != len(children):
        child_usage["known"] = False
    usage = {
        "parent": agent["usage"], "children": child_usage,
        "total_tokens": None, "cost_usd": None,
        "known": agent["usage"]["known"] and (arm == "stock" or child_usage["known"]),
    }
    if usage["known"]:
        parent_total = agent["usage"].get("total_tokens") or 0
        child_total = child_usage.get("total_tokens") or 0
        usage["total_tokens"] = parent_total + child_total
    parent_cost, child_cost = agent["usage"].get("cost_usd"), child_usage.get("cost_usd")
    if isinstance(parent_cost, (int, float)) and (arm == "stock" or isinstance(child_cost, (int, float))):
        usage["cost_usd"] = parent_cost + (child_cost or 0)
    grader = _grade(case, project, agent["answer"])
    failures = []
    if result["timed_out"]:
        failures.append("deadline exceeded")
    if not agent["success"]:
        failures.append("Pi did not settle with a successful assistant stop reason")
    if not grader.get("passed"):
        failures.append("external verifier failed")
    if not usage["known"]:
        failures.append("token usage is unknown")
    if arm != "stock":
        if not launches["verified"]:
            failures.append("child extension loadout is unverified")
        if len(children) < CHILD_CAP:
            failures.append(f"expected {CHILD_CAP} child records, found {len(children)}")
        if not overlap["evidenced"]:
            failures.append("timestamped overlapping child intervals are unverified")
        if not child_usage["known"]:
            failures.append("child token usage is unknown")
    if malformed:
        failures.append(f"{malformed} malformed JSONL event(s)")
    record = {
        "case": case, "arm": arm, "repetition": repetition,
        "status": "passed" if not failures else "failed",
        "correct": bool(grader.get("passed")), "failures": failures,
        "termination_reason": result["termination_reason"],
        "exit_code": result["exit_code"], "elapsed_ms": result["elapsed_ms"],
        "settled": agent["settled"], "agent_end": agent["agent_end"],
        "final_stop_reason": agent["final_stop_reason"], "answer": agent["answer"],
        "grader": grader, "seed_sha256": _seed_hash(),
        "parent_extensions": [] if arm == "stock" else [packages[arm]["entry"]],
        "parent_loadout_verified": True, "child_loadout": launches,
        "packages": {key: value for key, value in packages.items() if key == arm},
        "children": children, "overlap": overlap, "usage": usage,
        "environment": environment_info,
        "artifacts": {
            "cell": str(cell_dir), "project": str(project),
            "events": str(cell_dir / "events.jsonl"), "stderr": str(cell_dir / "stderr.log"),
            "session": str(session_dir), "agent_dir": env["PI_CODING_AGENT_DIR"],
            "grader": grader,
        },
    }
    _write_json(cell_dir / "cell.json", record)
    return record


def _new_run_dir(cache_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return private_dir(cache_root / "runs" / f"{stamp}-{uuid.uuid4().hex[:8]}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, default=default_cache_root())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install", help="install and verify the exact pinned extension packages")
    commands.add_parser("preflight", help="check wrapper, credentials, package pins, and isolation prerequisites")
    run = commands.add_parser("run", help="run one fresh benchmark cell")
    run.add_argument("--case", choices=CASES, required=True)
    run.add_argument("--arm", choices=ARMS, required=True)
    run.add_argument("--repetition", type=int, default=1)
    run.add_argument("--deadline", type=int, default=DEFAULT_DEADLINE)
    report = commands.add_parser("report", help="summarize saved cell.json files")
    report.add_argument("run_dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cache_root = args.cache_root.expanduser().resolve()
    try:
        if args.command == "install":
            result = install_packages(cache_root)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "preflight":
            result = preflight(cache_root)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result["passed"] else 1
        if args.command == "run":
            if args.deadline < 1 or args.repetition < 1:
                raise ValueError("deadline and repetition must be positive")
            check = preflight(cache_root)
            if not check["passed"]:
                print(json.dumps(check, indent=2, sort_keys=True))
                return 1
            run_dir = _new_run_dir(cache_root)
            _write_json(run_dir / "preflight.json", check)
            record = run_cell(run_dir, args.case, args.arm, args.repetition,
                              args.deadline, check["package_info"])
            print(json.dumps(record, indent=2, sort_keys=True))
            return 0 if record["status"] == "passed" else 1
        if args.command == "report":
            cells = [json.loads(path.read_text()) for path in sorted(args.run_dir.glob("cells/*/cell.json"))]
            result = {"run_dir": str(args.run_dir.resolve()), "cells": cells,
                      "attempted": len(cells), "passed": sum(c["status"] == "passed" for c in cells)}
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
    except Exception as error:
        print(json.dumps({"passed": False, "error": str(error)}), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
