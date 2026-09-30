#!/usr/bin/env python3
"""Isolated local benchmark for stock Pi, two subagents packages, and pi-fabric."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import tempfile
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
CHILD_CAP = 4
DEFAULT_DEADLINE = 900
CORE_TOOLS = ("read", "grep", "find", "ls", "bash", "edit", "write")
ARMS = ("stock", "subagents", "tintin-subagents", "fabric")
ARM_EXTENSION_TOOLS = {
    "subagents": ("subagents_enable", "subagent"),
    "tintin-subagents": ("Agent",),
    "fabric": ("fabric_exec",),
}
CASES = ("triage", "patch", "control", "debug", "integration")
PINS = {
    "subagents": {
        "name": "pi-subagents", "version": "0.73.1",
        "integrity": "sha512-IklOqw67DtvIWQFKxFJpDFXsOGcq8RGYZEbokykD5Kvfs9aa/du1cW2NEJ5xx4GUgN2LVt3vAq5IdzEMo7YwJw==",
    },
    "tintin-subagents": {
        "name": "@tintinweb/pi-subagents", "version": "0.19.0",
        "integrity": "sha512-DZsU33Urfb9dhEsJmsmpx0dayIHMYUbxng6AA7B6+bIgspNcTckcSnQRsbrv+QCS7jVKYJTzHIT/j1SU2B/nVQ==",
    },
    "fabric": {
        "name": "pi-fabric", "version": "0.100.0",
        "integrity": "sha512-tW1DBe8cZYN91yezHHud5R3nLCV8LU2/1rS69f9u0LnsuuDXBVpxbEa06o2sbiJuANMxIJ1PGf+2wGXdGWqAmg==",
    },
}
HOST_RUNTIME_ARMS = ("subagents", "tintin-subagents")
SUBAGENT_RUNTIME_PIN = {
    "name": "@earendil-works/pi-coding-agent", "version": "0.87.1",
    "integrity": "sha512-m8ArJUtVcQMSe1lLE/Ei7vX/JV7O39sWmWBsXV2NOU70F0qCp8GubA24pT3LnwTmM6LL2xV80/h6sQg85n69ew==",
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
    prefix = cache_root / "packages" / arm
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


def inspect_host_runtime(cache_root: Path, package_arm: str) -> dict[str, Any]:
    pin = SUBAGENT_RUNTIME_PIN
    prefix = cache_root / "packages" / package_arm
    root = prefix / "node_modules" / "@earendil-works" / "pi-coding-agent"
    metadata_path = root / "package.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"pinned Pi host runtime is not installed: {metadata_path}")
    metadata = json.loads(metadata_path.read_text())
    if metadata.get("name") != pin["name"] or metadata.get("version") != pin["version"]:
        raise ValueError(f"wrong Pi host runtime installed at {root}; expected {pin['name']}@{pin['version']}")
    lock_path = prefix / "package-lock.json"
    lock_integrity = None
    if lock_path.is_file():
        lock = json.loads(lock_path.read_text())
        package_lock = lock.get("packages", {}).get("node_modules/@earendil-works/pi-coding-agent", {})
        lock_integrity = package_lock.get("integrity")
        if lock_integrity and lock_integrity != pin["integrity"]:
            raise ValueError("npm lock integrity does not match the pinned Pi host runtime")
    return {
        "name": pin["name"], "version": pin["version"],
        "integrity": pin["integrity"], "lock_integrity": lock_integrity,
        "integrity_verified": lock_integrity == pin["integrity"],
        "sha256": tree_sha256(root), "root": str(root.resolve()),
    }


def install_packages(cache_root: Path) -> dict[str, Any]:
    private_dir(cache_root)
    installed = {}
    for arm, pin in PINS.items():
        spec = f"{pin['name']}@{pin['version']}"
        actual = npm_integrity(spec)
        if actual != pin["integrity"]:
            raise RuntimeError(f"registry integrity changed for {spec}; expected pinned tarball")
        specs = [spec]
        if arm in HOST_RUNTIME_ARMS:
            runtime = SUBAGENT_RUNTIME_PIN
            runtime_spec = f"{runtime['name']}@{runtime['version']}"
            if npm_integrity(runtime_spec) != runtime["integrity"]:
                raise RuntimeError(f"registry integrity changed for {runtime_spec}; expected pinned tarball")
            specs.append(runtime_spec)
        prefix = private_dir(cache_root / "packages" / arm)
        result = subprocess.run(
            ["npm", "install", "--prefix", str(prefix), "--save-exact", "--ignore-scripts",
             "--no-audit", "--no-fund", *specs],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode:
            raise RuntimeError(f"npm install failed for {', '.join(specs)}: {result.stderr.strip()}")
        info = inspect_package(arm, cache_root)
        if not info["integrity_verified"]:
            raise RuntimeError(f"npm lock did not verify the tarball integrity for {spec}")
        info["registry_integrity_verified"] = True
        if arm in HOST_RUNTIME_ARMS:
            host_runtime = inspect_host_runtime(cache_root, arm)
            if not host_runtime["integrity_verified"]:
                raise RuntimeError(f"npm lock did not verify the tarball integrity for {runtime_spec}")
            host_runtime["registry_integrity_verified"] = True
            info["host_runtime"] = host_runtime
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
        "--tools", ",".join(CORE_TOOLS + ARM_EXTENSION_TOOLS.get(arm, ())), "--no-skills",
        "--no-prompt-templates", "--no-themes", "--no-context-files", "--no-approve",
    ]
    if arm == "stock":
        command.append("--no-extensions")
    else:
        entry = Path(extension_paths[arm])
        if not entry.is_absolute():
            raise ValueError(f"extension path must be absolute: {entry}")
        command.extend(["-e", str(entry)])
    command.extend(["--print", prompt])
    return command


def build_environment(
    cell_dir: Path, source: dict[str, str] | None = None,
    pi_binary: str = PI_BINARY, subagents_runtime_root: str | Path | None = None,
) -> dict[str, str]:
    source = os.environ if source is None else source
    allowed = (
        "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE",
        "TERM", "TZ", "http_proxy", "https_proxy", "no_proxy", "HTTP_PROXY",
        "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    )
    env = {key: source[key] for key in allowed if source.get(key)}
    binary_dir = str(Path(pi_binary).parent)
    env["PATH"] = binary_dir + (os.pathsep + env["PATH"] if env.get("PATH") else "")
    agent_dir = private_dir(cell_dir / "agent")
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    env["PI_SUBAGENT_PI_BINARY"] = pi_binary
    env["PI_FABRIC_PI_BINARY"] = pi_binary
    if subagents_runtime_root is not None:
        _write_json(agent_dir / "settings.json", {
            "subagents": {"agentOverrides": {"worker": {"thinking": THINKING}}},
        })
        env["PI_SUBAGENTS_PI_CODING_AGENT_PACKAGE_ROOT"] = str(Path(subagents_runtime_root).resolve())
    validate_pi_binary(pi_binary, env)
    return env


def _child_tasks(case: str, project_dir: Path) -> list[dict[str, str]]:
    if case == "control":
        return [{
            "key": "billing",
            "task": (
                f"Read only billing.py in {project_dir}. Evaluate subtotal_cents(1250, 4) "
                "from the fixture and return one JSON object with numeric fields "
                "observed_cents and required_cents. Do not edit files."
            ),
        }]
    if case == "integration":
        return [
            {"key": "catalog", "task": (
                f"Modify only catalog.py in {project_dir}. Fix inventory to subtract "
                "reservations and clamp at zero. Preserve its public API."
            )},
            {"key": "billing", "task": (
                f"Modify only billing.py in {project_dir}. Fix subtotal to multiply "
                "integer unit-price cents by quantity. Preserve its public API."
            )},
            {"key": "shipping-fee", "task": (
                f"Modify only shipping_fee_cents in shipping.py in {project_dir}. "
                "Waive shipping at and above the free threshold; preserve quote_order."
            )},
            {"key": "checkout-total", "task": (
                f"Modify only quote_order in shipping.py in {project_dir}. Add "
                "grand_total_cents as subtotal plus shipping fee; preserve existing keys "
                "and shipping_fee_cents."
            )},
        ]
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
            if case == "debug":
                test = {
                    "catalog": "test_available_units_subtracts_reservations",
                    "billing": "test_subtotal_multiplies_unit_cents",
                    "shipping": "test_free_shipping_includes_threshold",
                }[module]
                task += f" Read test_contracts.py and fix the cause of {test}; do not edit tests."
        tasks.append({"key": module, "task": task})
    return tasks


def _fabric_script(tasks: list[dict[str, str]], project_dir: Path, child_timeout: int) -> str:
    specs = [{"key": task["key"], "task": task["task"]} for task in tasks]
    return (
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


def _tintin_agent_specs(tasks: list[dict[str, str]]) -> list[dict[str, Any]]:
    return [{
        "prompt": task["task"], "description": f"Handle {task['key']} task",
        "name": task["key"], "subagent_type": "general-purpose",
        "run_in_background": False, "isolated": True, "inherit_context": False,
        "isolation": "off",
    } for task in tasks]


def build_prompt(case: str, arm: str, project_dir: Path, deadline_seconds: int = DEFAULT_DEADLINE) -> str:
    if case not in CASES or arm not in ARMS:
        raise ValueError("unknown case or arm")
    case_text = (ROOT / "cases" / f"{case}.md").read_text()
    base = f"{case_text}\n\nThe writable fixture is {project_dir}. Use no files outside it."
    if arm == "stock":
        return base
    tasks = _child_tasks(case, project_dir)
    task_count = len(tasks)
    task_description = f"{task_count} independent task{'s' if task_count != 1 else ''}"
    task_records = f"{task_count} returned child record{'s' if task_count != 1 else ''}"
    delegation_mode = "in parallel" if task_count > 1 else "as one delegated child"
    child_timeout = deadline_seconds * 1000
    if arm == "subagents":
        children = [
            {
                "key": task["key"], "agent": "worker", "task": task["task"],
                "async": False, "tools": ",".join(CORE_TOOLS), "extensions": [], "skills": [],
                "acceptance": {
                    "level": "none",
                    "reason": "Benchmark output is checked by the parent grader",
                },
                "context": "fresh", "cwd": str(project_dir), "worktree": False,
                "timeoutMs": child_timeout,
            }
            for task in tasks
        ]
        script = (
            "const children = " + json.dumps(children, separators=(",", ":")) + ";\n"
            "const results = await runs.all(children);\nconsole.log(JSON.stringify({ children: results }));\nreturn { children: results };"
        )
        return (
            base + f"\n\nDelegate exactly {task_description} {delegation_mode}; child cap is {task_count}. "
            "Use the pi-subagents workflow, never another runtime. Call subagents_enable first, "
            "then call subagent({action:\"list\",capabilities:true}) and make exactly one top-level workflow call with this script and async:false. "
            f"Wait for the workflow tool to finish. Synthesize from its {task_records}; if inline output is empty, read only that child's returned sessionFile for its final assistant response. Never read fixture source in the parent. "
            "The workflow must use runs.all once. Each child runs foreground with async:false and inherits the parent provider and model; private cell worker settings match the parent thinking level. Pass no per-run model or thinking override. "
            "Each child sets acceptance to none because the parent grader checks each benchmark result. Children have extensions: [] and skills: []; do not change their tools, cwd, or deadline.\n\n"
            "workflowScript = " + json.dumps(script) + "\n"
            f"Outer request: cwd={json.dumps(str(project_dir))}, async=false, "
            f"timeoutMs={child_timeout}, mission=false."
        )
    if arm == "tintin-subagents":
        return (
            base + f"\n\nDelegate exactly {task_description} {delegation_mode}; child cap is {task_count}. "
            "Use the loaded Agent tool from @tintinweb/pi-subagents, not the other subagents package. "
            "Invoke each argument object below once in the same assistant tool-call batch. Settle every call inline; "
            "do not use workflows, background agents, or any other tools. The isolated general-purpose agents "
            "have only built-in tools and fresh context. Leave model and thinking unset so they inherit the parent. "
            "Use only child results; do not inspect fixture files in the parent. Synthesize after all children finish.\n\n"
            "Agent arguments = " + json.dumps(_tintin_agent_specs(tasks), indent=2)
        )
    script = _fabric_script(tasks, project_dir, child_timeout)
    return (
        base + f"\n\nDelegate exactly {task_description} {delegation_mode}; child cap is {task_count}. "
        "Call the loaded fabric_exec tool once with the following TypeScript. Do not perform the "
        "tasks in the parent. Each agents.run leaf must use extensions:false, the same model, "
        "thinking level, core tools, cwd, and deadline shown in the code. Return the child records "
        "and synthesize only after every child finishes.\n\n"
        "fabric_exec code = " + json.dumps(script)
    )


def _pump(
    stream: Any, path: Path, tool_call_times: dict[str, dict[str, float]] | None = None,
    observed_tools: tuple[str, ...] = (),
) -> None:
    with path.open("wb") as output:
        for line in iter(stream.readline, b""):
            if tool_call_times is not None and observed_tools:
                try:
                    event = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    event = {}
                kind = event.get("type", event.get("event"))
                call_id = event.get("toolCallId")
                if (event.get("toolName") in observed_tools and isinstance(call_id, str)
                        and kind in ("tool_execution_start", "tool_execution_end")):
                    field = "start" if kind == "tool_execution_start" else "end"
                    tool_call_times.setdefault(call_id, {})[field] = time.monotonic() * 1000
            output.write(line)
            output.flush()


def _terminate_process(process: subprocess.Popen, grace_seconds: float) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait()


def run_process(
    command: list[str], cwd: Path, env: dict[str, str], event_path: Path, stderr_path: Path,
    deadline_seconds: float, terminate_grace_seconds: float = 3.0,
    observed_tools: tuple[str, ...] = (),
) -> dict[str, Any]:
    event_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    process = subprocess.Popen(
        command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=(os.name == "posix"),
    )
    tool_call_times: dict[str, dict[str, float]] = {}
    stdout_thread = threading.Thread(
        target=_pump, args=(process.stdout, event_path, tool_call_times, observed_tools), daemon=True,
    )
    stderr_thread = threading.Thread(target=_pump, args=(process.stderr, stderr_path), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    timed_out = interrupted = False
    try:
        process.wait(timeout=deadline_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process(process, terminate_grace_seconds)
    except KeyboardInterrupt:
        interrupted = True
        _terminate_process(process, terminate_grace_seconds)
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    process.stdout.close()
    process.stderr.close()
    return {
        "exit_code": process.returncode, "timed_out": timed_out, "interrupted": interrupted,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "termination_reason": "interrupted" if interrupted else "deadline" if timed_out else "process_exit",
        "tool_call_times": tool_call_times,
    }


def read_events(path: Path) -> tuple[list[dict[str, Any]], int]:
    events, malformed = [], 0
    if not path.is_file():
        return events, 0
    for line in path.read_text(errors="replace").splitlines():
        if line == "[mcporter] stderr from headroom":
            continue
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


def _turn_number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


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
    turn_ids: set[str] = set()
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
            message_id = message.get("id")
            turn_ids.add(str(message_id) if isinstance(message_id, (str, int)) else f"event:{index}")
            usage = _usage_record(message.get("usage"))
            if usage:
                usage_by_id[str(message.get("id", index))] = usage
    final = assistant_messages[-1] if assistant_messages else {}
    stop = final.get("stopReason", final.get("stop_reason"))
    answer = _text(final.get("content"))
    usage = _sum_usage(list(usage_by_id.values()))
    return {
        "settled": settled, "agent_end": agent_end, "final_stop_reason": stop,
        "answer": answer, "usage": usage, "turns": len(turn_ids) if turn_ids else None,
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
    elif isinstance(value, str):
        text = value.lstrip()
        if not text.startswith(("{", "[")):
            marker = text.find("Return:")
            if marker < 0:
                return
            text = text[marker + len("Return:"):].lstrip()
        try:
            parsed, _ = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError:
            return
        yield from _walk(parsed)


def _time_value(record: dict[str, Any], *keys: str) -> int | float | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, (int, float)):
            return value
    return None


def _completed_tool_calls(events: list[dict[str, Any]], name: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    starts: dict[str, dict[str, Any]] = {}
    ends: dict[str, dict[str, Any]] = {}
    for event in events:
        kind = event.get("type", event.get("event"))
        if event.get("toolName") != name:
            continue
        call_id = event.get("toolCallId")
        if not isinstance(call_id, str):
            continue
        if kind == "tool_execution_start":
            starts[call_id] = event
        elif kind == "tool_execution_end":
            ends[call_id] = event
    return [(start, ends[call_id]) for call_id, start in starts.items() if call_id in ends]


def _timestamp_ms(value: Any) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except (ValueError, OverflowError):
        return None


def _trusted_session_file(value: Any, session_dir: Path) -> Path | None:
    if not isinstance(value, str):
        return None
    try:
        root = session_dir.resolve()
        path = Path(value).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return path if path.is_file() and path.is_relative_to(root) else None


def _session_result(path: Path) -> dict[str, Any] | None:
    started_at = ended_at = None
    model = thinking = None
    output = ""
    turn_ids: set[str] = set()
    try:
        with path.open(encoding="utf-8") as session:
            for index, line in enumerate(session):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                timestamp = _timestamp_ms(event.get("timestamp"))
                if event.get("type") == "model_change":
                    provider, model_id = event.get("provider"), event.get("modelId")
                    if isinstance(provider, str) and isinstance(model_id, str):
                        model = f"{provider}/{model_id}"
                elif event.get("type") == "thinking_level_change":
                    thinking = event.get("thinkingLevel")
                if event.get("type") == "session" and started_at is None:
                    started_at = timestamp
                message = event.get("message")
                if (event.get("type") != "message" or not isinstance(message, dict)
                        or message.get("role") != "assistant"):
                    continue
                message_id = message.get("id")
                turn_ids.add(str(message_id) if isinstance(message_id, (str, int)) else f"event:{index}")
                if message.get("stopReason") not in ("stop", "end_turn", "endTurn") or timestamp is None:
                    continue
                ended_at = timestamp
                content = message.get("content", [])
                if isinstance(content, str):
                    output = content
                elif isinstance(content, list):
                    output = "".join(
                        part.get("text", "") for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                else:
                    output = ""
    except OSError:
        return None
    if started_at is None or ended_at is None or ended_at <= started_at or not output.strip():
        return None
    return {"startedAt": started_at, "endedAt": ended_at, "output": output, "model": model, "thinking": thinking, "turns": len(turn_ids)}


def _returned_session_files(events: list[dict[str, Any]], session_dir: Path) -> set[Path]:
    files = set()
    for start, end in _completed_tool_calls(events, "subagent"):
        if (not isinstance(start.get("args"), dict)
                or not isinstance(start["args"].get("workflowScript"), str)
                or end.get("isError") is not False):
            continue
        result = end.get("result")
        details = result.get("details", {}) if isinstance(result, dict) else {}
        if not isinstance(details, dict):
            continue
        for record in details.get("results", []):
            if not isinstance(record, dict):
                continue
            path = _trusted_session_file(record.get("sessionFile"), session_dir)
            if path is not None:
                files.add(path)
    return files


def _subagents_child_records(result: Any, session_dir: Path) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    details = result.get("details")
    if not isinstance(details, dict):
        return []
    workflow = details.get("workflow")
    workflow_value = workflow.get("value") if isinstance(workflow, dict) else None
    children = workflow_value.get("children") if isinstance(workflow_value, dict) else None
    result_rows = details.get("results")
    if not isinstance(children, list) or not isinstance(result_rows, list):
        return []
    rows_by_key = {
        row.get("workflowKey"): row for row in result_rows
        if isinstance(row, dict) and isinstance(row.get("workflowKey"), str)
    }
    records = []
    for child in children:
        if not isinstance(child, dict) or child.get("ok") is not True:
            continue
        key = child.get("key")
        row = rows_by_key.get(key)
        if (not isinstance(key, str) or not isinstance(row, dict)
                or row.get("exitCode") not in (None, 0) or row.get("success") is False):
            continue
        acceptance = row.get("acceptance")
        if isinstance(acceptance, dict) and acceptance.get("status") == "rejected":
            continue
        path = _trusted_session_file(row.get("sessionFile"), session_dir)
        session = _session_result(path) if path is not None else None
        if session is None:
            continue
        run_id = child.get("runId")
        identifier = str(run_id if isinstance(run_id, (str, int)) else key)
        item = {
            "id": identifier, "key": key,
            "agent": row.get("agent") or child.get("agent"),
            "runId": run_id, "state": "completed", "status": "completed",
            **session,
        }
        if isinstance(row.get("model"), str):
            item["extensionModel"] = row["model"]
        if isinstance(row.get("thinking"), str):
            item["extensionThinking"] = row["thinking"]
        row_usage = row.get("usage")
        turns = _turn_number(row_usage.get("turns")) if isinstance(row_usage, dict) else None
        turns = turns if turns is not None else _turn_number(session.get("turns"))
        if turns is not None:
            item["turns"] = turns
        usage = _usage_record(row.get("usage"))
        if usage is not None:
            item["usage"] = usage
        records.append(item)
    return records


def extract_children(
    events: list[dict[str, Any]], arm: str, session_dir: Path | None = None,
    tool_call_times: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    if arm not in ("subagents", "tintin-subagents", "fabric"):
        return []
    found: dict[str, dict[str, Any]] = {}
    if arm == "tintin-subagents":
        for start, end in _completed_tool_calls(events, "Agent"):
            args, result = start.get("args"), end.get("result")
            details = result.get("details") if isinstance(result, dict) else None
            key = args.get("name") if isinstance(args, dict) else None
            identifier = details.get("agentId") if isinstance(details, dict) else None
            if (end.get("isError") is not False or not isinstance(key, str)
                    or not isinstance(identifier, str) or details.get("status") != "completed"):
                continue
            item = {
                "id": identifier, "key": key,
                "agent": args.get("subagent_type"), "status": "completed", "state": "completed",
            }
            call_id = start.get("toolCallId")
            observed = tool_call_times.get(call_id, {}) if isinstance(tool_call_times, dict) else {}
            started_at = _time_value(observed, "start") or _timestamp_ms(start.get("timestamp"))
            ended_at = _time_value(observed, "end") or _timestamp_ms(end.get("timestamp"))
            if started_at is not None and ended_at is not None and ended_at > started_at:
                item.update({"startedAt": started_at, "endedAt": ended_at})
            turns = _turn_number(details.get("turnCount"))
            if turns is not None:
                item["turns"] = turns
            duration = _time_value(details, "durationMs")
            if duration is not None:
                item["durationMs"] = duration
            usage = _usage_record(result.get("usage")) if isinstance(result, dict) else None
            if usage is not None:
                item["usage"] = usage
            found[key] = item
        return list(found.values())
    if arm == "subagents":
        source_events = [
            end.get("result") for start, end in _completed_tool_calls(events, "subagent")
            if isinstance(start.get("args"), dict)
            and isinstance(start["args"].get("workflowScript"), str)
            and end.get("isError") is False
        ]
        if session_dir is None:
            return []
        for result in source_events:
            for record in _subagents_child_records(result, session_dir):
                found[record["id"]] = record
        return list(found.values())
    else:
        source_events = [
            end.get("result") for _, end in _completed_tool_calls(events, "fabric_exec")
            if end.get("isError") is False
        ]
    for event in source_events:
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
            result = record.get("result")
            turns = _turn_number(record.get("turns"))
            if turns is None and isinstance(result, dict):
                turns = _turn_number(result.get("turns"))
            if turns is not None:
                item["turns"] = turns
    return list(found.values())


def overlap_summary(children: list[dict[str, Any]]) -> dict[str, Any]:
    intervals = []
    for child in children:
        start = _time_value(child, "startedAt", "started_at", "startTime")
        end = _time_value(child, "endedAt", "ended_at", "endTime")
        if start is not None and end is not None and end > start:
            intervals.append((start, 1))
            intervals.append((end, -1))
    if len(intervals) != 2 * len(children):
        return {"interval_count": len(intervals) // 2 or None, "max_concurrency": None, "evidenced": False}
    active = maximum = 0
    for _, delta in sorted(intervals, key=lambda item: (item[0], item[1])):
        active += delta
        maximum = max(maximum, active)
    return {
        "interval_count": len(intervals) // 2 or None,
        "max_concurrency": maximum if children else None,
        "evidenced": bool(children) and maximum >= 2,
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


def child_launch_evidence(
    arm: str, events: list[dict[str, Any]], case: str, project_dir: Path, deadline_seconds: int,
) -> dict[str, Any]:
    calls = _tool_calls(events)
    expected_timeout = deadline_seconds * 1000
    if arm == "fabric":
        starts = [event for event in events if event.get("type", event.get("event")) == "tool_execution_start" and event.get("toolName") == "fabric_exec"]
        ends = [event for event in events if event.get("type", event.get("event")) == "tool_execution_end" and event.get("toolName") == "fabric_exec"]
        pairs = _completed_tool_calls(events, "fabric_exec")
        args = starts[0].get("args") if len(starts) == 1 else None
        code = args.get("code") if isinstance(args, dict) else None
        expected_code = _fabric_script(_child_tasks(case, project_dir), project_dir, expected_timeout)
        valid = (
            len(starts) == len(ends) == len(pairs) == 1
            and ends[0].get("isError") is False
            and isinstance(code, str)
            and re.sub(r"\s+", "", code) == re.sub(r"\s+", "", expected_code)
            and all(name == "fabric_exec" for name, _ in calls)
        )
        reason = "one successful fabric_exec call completed the expected child tasks" if valid else "Fabric child launch is unverified"
        return {"verified": valid, "tool_calls": [name for name, _ in calls], "reason": reason}
    expected_cwd = re.escape(json.dumps(str(project_dir)))
    expected_tasks = _child_tasks(case, project_dir)
    if arm == "tintin-subagents":
        expected_specs = {spec["name"]: spec for spec in _tintin_agent_specs(expected_tasks)}
        starts = [
            (index, event) for index, event in enumerate(events)
            if event.get("type", event.get("event")) == "tool_execution_start"
            and event.get("toolName") == "Agent"
        ]
        ends = [
            (index, event) for index, event in enumerate(events)
            if event.get("type", event.get("event")) == "tool_execution_end"
            and event.get("toolName") == "Agent"
        ]
        pairs = _completed_tool_calls(events, "Agent")
        starts_by_name = {
            args.get("name"): args for _, event in starts
            if isinstance((args := event.get("args")), dict)
        }
        completed = all(
            end.get("isError") is False
            and isinstance(end.get("result"), dict)
            and isinstance(end["result"].get("details"), dict)
            and end["result"]["details"].get("status") == "completed"
            for _, end in pairs
        )
        same_batch = bool(starts and ends) and max(i for i, _ in starts) < min(i for i, _ in ends)
        valid = (
            len(starts) == len(ends) == len(pairs) == len(expected_specs)
            and len(starts_by_name) == len(expected_specs)
            and all(starts_by_name.get(name) == spec for name, spec in expected_specs.items())
            and completed and same_batch
            and all(name == "Agent" for name, _ in calls)
        )
        reason = "verified foreground Agent calls ran in one parallel tool batch" if valid else "Tintin Agent launch or result collection is unverified"
        return {"verified": valid, "tool_calls": [name for name, _ in calls], "reason": reason}
    if arm == "subagents":
        scripts = [" ".join(_text_values(args)) for name, args in calls if name == "subagent" and isinstance(args, dict) and isinstance(args.get("workflowScript"), str)]
        valid = False
        if len(scripts) == 1:
            script = scripts[0]

            child_count = lambda pattern: len(re.findall(pattern, script)) == len(expected_tasks)
            valid = (
                script.count("runs.all") == 1 and script.count("runs.run") == 0
                and child_count(r'"key"\s*:')
                and all(json.dumps(task["key"]) in script and json.dumps(task["task"]) in script
                        for task in expected_tasks)
                and child_count(r'"extensions"\s*:\s*\[\s*\]')
                and child_count(r'"skills"\s*:\s*\[\s*\]')
                and child_count(r'"async"\s*:\s*false')
                and child_count(r'"acceptance"\s*:\s*\{\s*"level"\s*:\s*"none"')
                and not re.search(r'"(?:model|thinking)"\s*:', script)
                and child_count(r'"tools"\s*:\s*' + re.escape(json.dumps(",".join(CORE_TOOLS))))
                and child_count(r'"cwd"\s*:\s*' + expected_cwd)
                and child_count(r'"timeoutMs"\s*:\s*' + str(expected_timeout))
                and child_count(r'"context"\s*:\s*"fresh"')
                and child_count(r'"worktree"\s*:\s*false')
            )
        workflow_starts = [
            event for event in events
            if event.get("type", event.get("event")) == "tool_execution_start"
            and event.get("toolName") == "subagent"
            and isinstance(event.get("args"), dict)
            and isinstance(event["args"].get("workflowScript"), str)
        ]
        workflow_pairs = [
            (start, end) for start, end in _completed_tool_calls(events, "subagent")
            if isinstance(start.get("args"), dict)
            and isinstance(start["args"].get("workflowScript"), str)
        ]
        workflow_args = workflow_starts[0].get("args") if len(workflow_starts) == 1 else None
        valid = valid and len(workflow_starts) == len(workflow_pairs) == 1
        valid = valid and workflow_pairs[0][1].get("isError") is False
        valid = valid and isinstance(workflow_args, dict) and workflow_args.get("async") is False
        valid = valid and workflow_args.get("cwd") == str(project_dir)
        valid = valid and workflow_args.get("timeoutMs") == expected_timeout
        valid = valid and workflow_args.get("mission") is False
        enable_calls = [args for name, args in calls if name == "subagents_enable"]
        list_calls = [
            args for name, args in calls if name == "subagent"
            and isinstance(args, dict) and not isinstance(args.get("workflowScript"), str)
        ]
        valid = valid and len(enable_calls) == 1
        valid = valid and len(list_calls) == 1 and list_calls[0].get("action") == "list"
        session_dir = project_dir.parent / "session"
        returned_sessions = _returned_session_files(events, session_dir)
        unexpected_calls = []
        for name, args in calls:
            if name in ("subagents_enable", "subagent"):
                continue
            if name == "read" and isinstance(args, dict):
                path = _trusted_session_file(args.get("path"), session_dir)
                if path is not None and path in returned_sessions:
                    continue
            unexpected_calls.append(name)
        valid = valid and not unexpected_calls
        reason = "one successful foreground subagent workflow completed the expected child tasks" if valid else "subagent child launch or result collection is unverified"
        return {"verified": valid, "tool_calls": [name for name, _ in calls], "reason": reason}
    return {"verified": True, "tool_calls": [name for name, _ in calls], "reason": "stock Pi has no delegated children"}


def _grade(case: str, project: Path, answer: str) -> dict[str, Any]:
    if case in ("triage", "control"):
        try:
            parsed = json.loads(answer)
        except json.JSONDecodeError as error:
            return {"passed": False, "errors": [f"final answer is not JSON: {error.msg}"]}
        if case == "triage":
            from checks.triage import verify
        else:
            from checks.control import verify
        return verify(parsed)
    command = [sys.executable, str(ROOT / "checks" / "patch.py")]
    if case == "integration":
        command.append("--integration")
    command.append(str(project))
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
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
    if pi_version and pi_version.splitlines()[-1].strip() != SUBAGENT_RUNTIME_PIN["version"]:
        errors.append(
            f"Pi wrapper version does not match the pinned Pi host runtime "
            f"{SUBAGENT_RUNTIME_PIN['version']}"
        )
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
            if arm in HOST_RUNTIME_ARMS:
                runtime = SUBAGENT_RUNTIME_PIN
                runtime_spec = f"{runtime['name']}@{runtime['version']}"
                if npm_integrity(runtime_spec) != runtime["integrity"]:
                    raise ValueError("registry integrity differs from the pinned Pi host runtime")
                host_runtime = inspect_host_runtime(cache_root, arm)
                host_runtime["registry_integrity_verified"] = True
                if not host_runtime["integrity_verified"]:
                    raise ValueError("local npm lock does not verify the pinned Pi host runtime")
                saved_runtime = saved.get("host_runtime", {})
                if (saved_runtime.get("sha256") != host_runtime["sha256"]
                        or saved_runtime.get("version") != runtime["version"]
                        or saved_runtime.get("integrity") != runtime["integrity"]):
                    raise ValueError("Pi host runtime differs from the recorded pin; rerun install")
                info["host_runtime"] = host_runtime
            packages[arm] = info
        except Exception as error:
            errors.append(f"{arm} package preflight: {error}")
    return {
        "passed": not errors, "errors": errors, "pi_binary": pi_binary,
        "pi_version": pi_version, "provider": PROVIDER, "model": MODEL,
        "thinking": THINKING, "package_info": packages,
        "model_connectivity": "verified by first live cell; no separate billable probe",
    }


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False,
        ) as output:
            temporary_path = Path(output.name)
            output.write(content)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _write_json(path: Path, value: Any) -> None:
    _write_text(path, json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def _write_report_files(run_dir: Path, report: dict[str, Any]) -> None:
    report["markdown_report"] = str(run_dir.resolve() / "report.md")
    _write_text(run_dir / "report.md", render_markdown(report))
    _write_json(run_dir / "report.json", report)


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
    subagents_runtime_root = (
        packages.get("subagents", {}).get("host_runtime", {}).get("root")
        if arm == "subagents" else None
    )
    env = build_environment(
        cell_dir, source_env, pi_binary, subagents_runtime_root=subagents_runtime_root,
    )
    if arm == "tintin-subagents":
        _write_json(Path(env["PI_CODING_AGENT_DIR"]) / "subagents.json", {"reportUsage": True})
    package_paths = {key: value["entry"] for key, value in packages.items()}
    prompt = build_prompt(case, arm, project, deadline_seconds)
    command = build_command(arm, prompt, session_dir, package_paths, pi_binary)
    extension_args = [command[i + 1] for i, arg in enumerate(command[:-1]) if arg == "-e"]
    selected_tools = command[command.index("--tools") + 1].split(",")
    expected_tools = list(CORE_TOOLS + ARM_EXTENSION_TOOLS.get(arm, ()))
    if arm == "stock":
        parent_loadout_verified = (
            selected_tools == expected_tools and "--no-extensions" in command and not extension_args
        )
    else:
        parent_loadout_verified = (
            selected_tools == expected_tools and "--no-extensions" not in command
            and extension_args == [package_paths[arm]]
        )
    (cell_dir / "prompt.txt").write_text(prompt)
    _write_json(cell_dir / "argv.json", command)
    environment_info = {
        "agent_dir": env["PI_CODING_AGENT_DIR"], "path_prefix": str(Path(pi_binary).parent),
        "environment_keys": sorted(key for key in env if key.endswith("_API_KEY") or "proxy" in key.lower()),
        "child_binary": pi_binary,
    }
    process_options = {"observed_tools": ("Agent",)} if arm == "tintin-subagents" else {}
    result = run_process(
        command, project, env, cell_dir / "events.jsonl", cell_dir / "stderr.log",
        deadline_seconds, **process_options,
    )
    events, malformed = read_events(cell_dir / "events.jsonl")
    agent = parse_events(events, result["exit_code"])
    launches = child_launch_evidence(arm, events, case, project, deadline_seconds)
    children = extract_children(
        events, arm=arm, session_dir=session_dir, tool_call_times=result.get("tool_call_times"),
    ) if launches["verified"] else []
    expected_child_count = len(_child_tasks(case, project))
    child_runtime_matches_parent = (
        len(children) == expected_child_count and all(
            child.get("model") == f"{PROVIDER}/{MODEL}" and child.get("thinking") == THINKING
            for child in children
        ) if arm == "subagents" else launches["verified"] if arm == "tintin-subagents" else True
    )
    overlap = overlap_summary(children)
    child_usage_records = [item["usage"] for item in children if isinstance(item.get("usage"), dict)]
    child_usage = _sum_usage(child_usage_records)
    if arm == "tintin-subagents":
        # Tintin drains pooled child spend onto whichever Agent result completes next.
        child_usage["known"] = child_usage["known"] and len(children) == expected_child_count
    elif len(child_usage_records) != len(children):
        child_usage["known"] = False
    parent_turns = _turn_number(agent.get("turns"))
    child_turn_values = [_turn_number(child.get("turns")) for child in children]
    child_turns = (0 if arm == "stock" else
                   sum(child_turn_values) if len(children) == expected_child_count
                   and all(value is not None for value in child_turn_values) else None)
    turns_known = parent_turns is not None and child_turns is not None
    turns = {
        "known": turns_known, "parent": parent_turns, "children": child_turns,
        "total": parent_turns + child_turns if turns_known else None,
    }
    overlap_required = arm != "stock" and expected_child_count > 1
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
    interrupted = result.get("interrupted", False)
    if interrupted:
        grader = {"passed": False, "errors": ["grading skipped because the cell was interrupted"]}
    else:
        try:
            grader = _grade(case, project, agent["answer"])
        except Exception as error:
            grader = {"passed": False, "errors": [f"grader error: {error}"]}
    failures = []
    if interrupted:
        failures.append("cell interrupted")
    if not parent_loadout_verified:
        failures.append("parent extension loadout is unverified")
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
        if len(children) != expected_child_count:
            failures.append(f"expected {expected_child_count} child records, found {len(children)}")
        if overlap_required and not overlap["evidenced"]:
            failures.append("timestamped overlapping child intervals are unverified")
        if not child_runtime_matches_parent:
            failures.append("subagent child model/thinking did not match the parent")
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
        "parent_loadout_verified": parent_loadout_verified,
        "child_loadout": launches, "child_runtime_matches_parent": child_runtime_matches_parent,
        "packages": {key: value for key, value in packages.items() if key == arm},
        "children": children, "overlap": overlap, "overlap_required": overlap_required,
        "usage": usage, "turns": turns,
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


def _cell_passed(cell: dict[str, Any]) -> bool:
    elapsed = cell.get("elapsed_ms")
    return (cell.get("status") == "passed" and cell.get("correct") is True
            and isinstance(elapsed, (int, float)) and elapsed >= 0)


def _usage_field(cell: dict[str, Any], field: str) -> int | float | None:
    usage = cell.get("usage", {})
    direct = usage.get(field)
    if isinstance(direct, (int, float)):
        return direct
    parent, children = usage.get("parent", {}), usage.get("children", {})
    parent_value = parent.get(field) if isinstance(parent, dict) else None
    child_value = 0 if cell.get("arm") == "stock" else (children.get(field) if isinstance(children, dict) else None)
    if isinstance(parent_value, (int, float)) and isinstance(child_value, (int, float)):
        return parent_value + child_value
    return None


def _report_usage(cells: list[dict[str, Any]], planned: int) -> dict[str, Any]:
    known = [cell for cell in cells
             if isinstance(cell.get("usage"), dict) and cell["usage"].get("known")]
    complete = len(cells) == planned and len(known) == planned
    cost_known = [cell["usage"]["cost_usd"] for cell in known
                  if isinstance(cell["usage"].get("cost_usd"), (int, float))]
    cost_complete = len(cells) == planned and len(cost_known) == planned
    result = {
        "complete": complete, "known_cells": len(known),
        "unknown_cells": max(0, planned - len(known)),
        "total_tokens": None, "input_tokens": None, "output_tokens": None,
        "cost_complete": cost_complete, "cost_known_cells": len(cost_known),
        "cost_usd": None,
    }
    if complete:
        for field in ("total_tokens", "input_tokens", "output_tokens"):
            values = [_usage_field(cell, field) for cell in known]
            if all(isinstance(value, (int, float)) for value in values):
                result[field] = sum(values)
    if cost_complete:
        result["cost_usd"] = sum(cost_known)
    for field in ("cache_read_tokens", "cache_write_tokens"):
        values = [_usage_field(cell, field) for cell in cells]
        known_values = [value for value in values if isinstance(value, (int, float))]
        field_complete = len(cells) == planned and len(known_values) == planned
        result[field] = sum(known_values) if field_complete else None
        result[f"{field}_complete"] = field_complete
        result[f"{field}_known_cells"] = len(known_values)
    return result


def _report_turns(cells: list[dict[str, Any]], planned: int) -> dict[str, Any]:
    known = [cell["turns"] for cell in cells
             if isinstance(cell.get("turns"), dict) and cell["turns"].get("known")
             and all(isinstance(cell["turns"].get(key), int)
                     for key in ("parent", "children", "total"))]
    complete = len(cells) == planned and len(known) == planned
    result = {
        "complete": complete, "known_cells": len(known),
        "unknown_cells": max(0, planned - len(known)),
        "parent": None, "children": None, "total": None,
    }
    if complete:
        for field in ("parent", "children", "total"):
            result[field] = sum(item[field] for item in known)
    return result


def build_report(
    cells: list[dict[str, Any]], repetitions: int,
    cases: tuple[str, ...] | list[str] = CASES,
    arms: tuple[str, ...] | list[str] = ARMS,
    repetition_ids: tuple[int, ...] | list[int] | None = None,
) -> dict[str, Any]:
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    repetition_ids = tuple(range(1, repetitions + 1) if repetition_ids is None else repetition_ids)
    if len(repetition_ids) != repetitions:
        raise ValueError("repetition IDs must match planned repetitions")
    cases, arms = tuple(cases), tuple(arms)
    indexed = {(cell.get("case"), cell.get("arm"), cell.get("repetition")): cell for cell in cells}
    case_reports: dict[str, Any] = {}
    failed_cells, missing_cells = [], []
    for case in cases:
        arm_reports: dict[str, Any] = {}
        for arm in arms:
            selected = [cell for cell in cells if cell.get("case") == case and cell.get("arm") == arm]
            good = [cell for cell in selected if _cell_passed(cell)]
            planned = repetitions
            arm_reports[arm] = {
                "planned": planned, "attempted": len(selected), "passed": len(good),
                "failed": len(selected) - len(good),
                "not_run": max(0, planned - len(selected)),
                "success_rate": len(good) / planned,
                "median_elapsed_ms": statistics.median([cell["elapsed_ms"] for cell in good]) if good else None,
                "usage": _report_usage(selected, planned),
                "turns": _report_turns(selected, planned),
            }
            for cell in selected:
                if not _cell_passed(cell):
                    failed_cells.append({
                        "case": case, "arm": arm, "repetition": cell.get("repetition"),
                        "status": cell.get("status", "unknown"),
                        "failures": cell.get("failures", []),
                        "cell": cell.get("artifacts", {}).get("cell"),
                    })
            for repetition in repetition_ids:
                if (case, arm, repetition) not in indexed:
                    missing_cells.append({"case": case, "arm": arm, "repetition": repetition})
        baseline = {
            cell.get("repetition"): cell for cell in cells
            if cell.get("case") == case and cell.get("arm") == "stock" and _cell_passed(cell)
        }
        paired: dict[str, Any] = {}
        for arm in arms:
            if arm == "stock":
                continue
            ratios, base_times, arm_times = [], [], []
            for cell in cells:
                repetition = cell.get("repetition")
                base = baseline.get(repetition)
                elapsed = cell.get("elapsed_ms")
                if (cell.get("case") == case and cell.get("arm") == arm and base
                        and _cell_passed(cell) and isinstance(elapsed, (int, float)) and elapsed > 0):
                    base_times.append(base["elapsed_ms"])
                    arm_times.append(elapsed)
                    ratios.append(base["elapsed_ms"] / elapsed)
            paired[arm] = {
                "pairs": len(ratios),
                "median_speedup": statistics.median(ratios) if ratios else None,
                "median_stock_elapsed_ms": statistics.median(base_times) if base_times else None,
                "median_arm_elapsed_ms": statistics.median(arm_times) if arm_times else None,
            }
        case_reports[case] = {
            "repetitions": repetitions, "arms": arm_reports,
            "paired_speedup": paired,
        }
    summaries = [{
        "case": cell.get("case"), "arm": cell.get("arm"),
        "repetition": cell.get("repetition"), "status": cell.get("status"),
        "correct": cell.get("correct"), "elapsed_ms": cell.get("elapsed_ms"),
        "usage": cell.get("usage"), "turns": cell.get("turns"),
        "overlap": cell.get("overlap"), "overlap_required": cell.get("overlap_required"),
        "failures": cell.get("failures", []),
        "cell": cell.get("artifacts", {}).get("cell"),
    } for cell in cells]
    return {
        "schema_version": 1, "cases": case_reports,
        "planned_cells": repetitions * len(cases) * len(arms),
        "recorded_cells": len(cells), "passed_cells": sum(_cell_passed(cell) for cell in cells),
        "failed_cells": failed_cells, "missing_cells": missing_cells, "cells": summaries,
    }


def render_markdown(report: dict[str, Any]) -> str:
    def text(value: Any) -> str:
        return (html.escape(str(value), quote=False).replace("\\", "\\\\")
                .replace("|", "\\|").replace("\r", " ").replace("\n", " "))

    def elapsed(value: Any) -> str:
        return f"{value:,.1f} ms" if isinstance(value, (int, float)) else "Unknown"

    def tokens(usage: dict[str, Any], planned: int) -> str:
        value = usage.get("total_tokens")
        if usage.get("complete") and isinstance(value, (int, float)):
            return f"{int(value):,}"
        return f"Unknown ({usage.get('known_cells', 0)}/{planned} cells known)"

    def cost(usage: dict[str, Any], planned: int) -> str:
        value = usage.get("cost_usd")
        if usage.get("cost_complete") and isinstance(value, (int, float)):
            return f"${value:,.4f}"
        return f"Unknown ({usage.get('cost_known_cells', 0)}/{planned} cells known)"

    def metric_tokens(usage: dict[str, Any], field: str, planned: int) -> str:
        value = usage.get(field)
        if usage.get(f"{field}_complete") and isinstance(value, (int, float)):
            return f"{int(value):,}"
        known = usage.get(f"{field}_known_cells", 0)
        return f"Unknown ({known}/{planned} cells known)"

    def turn_count(turns: dict[str, Any], planned: int) -> str:
        value = turns.get("total")
        if turns.get("complete") and isinstance(value, int):
            return str(value)
        return f"Unknown ({turns.get('known_cells', 0)}/{planned} cells known)"

    def cell_metric_tokens(cell: dict[str, Any], field: str) -> str:
        value = _usage_field(cell, field)
        return f"{int(value):,}" if isinstance(value, (int, float)) else "Unknown"

    def cell_overlap(case: str, arm: str, overlap: Any) -> str:
        if arm == "stock":
            return "n/a"
        if len(_child_tasks(case, ROOT / "fixtures" / "project")) <= 1:
            return "not required"
        if not isinstance(overlap, dict) or not isinstance(overlap.get("max_concurrency"), (int, float)):
            return "Unknown"
        status = "verified" if overlap.get("evidenced") else "not verified"
        return f"{status}; peak {int(overlap['max_concurrency'])}"

    def arm_overlap(case: str, arm: str, attempted: int) -> str:
        if arm == "stock":
            return "n/a"
        if len(_child_tasks(case, ROOT / "fixtures" / "project")) <= 1:
            return "not required"
        selected = [cell for cell in report.get("cells", [])
                    if cell.get("case") == case and cell.get("arm") == arm]
        if not selected:
            return "not run"
        observed = [cell["overlap"] for cell in selected
                    if isinstance(cell.get("overlap"), dict)
                    and isinstance(cell["overlap"].get("max_concurrency"), (int, float))]
        if not observed:
            return f"Unknown (0/{attempted} observed)"
        verified = sum(overlap.get("evidenced") is True for overlap in observed)
        unknown = attempted - len(observed)
        result = f"{verified}/{attempted} verified; peak {max(int(item['max_concurrency']) for item in observed)}"
        return result + (f"; {unknown} unknown" if unknown else "")

    planned = report.get("planned_cells", 0)
    failed = report.get("failed_cells", [])
    missing = report.get("missing_cells", [])
    provider, model = report.get("provider"), report.get("model")
    model_name = f"{provider}/{model}" if provider and model else model or provider or "unknown"
    deadline = report.get("deadline_seconds")
    lines = [
        "# Pi parallel-work benchmark",
        "",
        f"- Run: `{text(report.get('run_id') or 'unknown')}`",
        f"- Mode/state: {text(report.get('mode') or 'unknown')} / {text(report.get('state') or 'unknown')}",
        f"- Model: `{text(model_name)}` ({text(report.get('thinking') or 'unknown')})",
        f"- Deadline: {text(deadline)} s" if deadline is not None else "- Deadline: unknown",
        f"- Cells: {report.get('recorded_cells', 0)}/{planned} recorded; "
        f"{report.get('passed_cells', 0)} passed; {len(failed)} failed; {len(missing)} not run",
        "",
        "Median time uses passing cells only. Speedup is stock time divided by arm time, "
        "using only passing paired repetitions; values above 1× are faster.",
    ]

    for case, case_report in report.get("cases", {}).items():
        lines.extend(["", f"## {text(case)}", "",
                      "| Arm | Passed / planned | Failed | Not run | Pass rate | Median time | "
                      "Paired speedup vs stock | Tokens | Cache read tokens | Cache write tokens | Turns | Cost (USD) | Child overlap |",
                      "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"])
        paired = case_report.get("paired_speedup", {})
        for arm, metrics in case_report.get("arms", {}).items():
            pairs = paired.get(arm, {})
            speedup = ("baseline" if arm == "stock" else
                       f"{pairs['median_speedup']:.2f}× (n={pairs['pairs']})"
                       if pairs.get("median_speedup") is not None else "n/a (0 pairs)")
            rate = metrics.get("success_rate")
            pass_rate = f"{rate:.1%}" if isinstance(rate, (int, float)) else "Unknown"
            usage = metrics.get("usage", {})
            lines.append(
                f"| {text(arm)} | {metrics.get('passed', 0)}/{metrics.get('planned', 0)} | "
                f"{metrics.get('failed', 0)} | {metrics.get('not_run', 0)} | {pass_rate} | "
                f"{elapsed(metrics.get('median_elapsed_ms'))} | {speedup} | "
                f"{tokens(usage, metrics.get('planned', 0))} | "
                f"{metric_tokens(usage, 'cache_read_tokens', metrics.get('planned', 0))} | "
                f"{metric_tokens(usage, 'cache_write_tokens', metrics.get('planned', 0))} | "
                f"{turn_count(metrics.get('turns', {}), metrics.get('planned', 0))} | "
                f"{cost(usage, metrics.get('planned', 0))} | "
                f"{arm_overlap(case, arm, metrics.get('attempted', 0))} |"
            )

        cells = [cell for cell in report.get("cells", []) if cell.get("case") == case]
        if cells:
            lines.extend(["", "### Attempted cells", "",
                          "| Repetition | Arm | Status | Correct | Time | Tokens | Cache read tokens | Cache write tokens | Turns | Child overlap | Failure |",
                          "|---:|---|---|---|---:|---:|---:|---:|---:|---|---|"])
            for cell in cells:
                usage = cell.get("usage")
                total = (f"{int(usage['total_tokens']):,}"
                         if isinstance(usage, dict) and usage.get("known")
                         and isinstance(usage.get("total_tokens"), (int, float)) else "Unknown")
                cache_read = cell_metric_tokens(cell, "cache_read_tokens")
                cache_write = cell_metric_tokens(cell, "cache_write_tokens")
                turns = cell.get("turns", {})
                turn_total = (str(turns["total"]) if isinstance(turns, dict)
                              and turns.get("known") and isinstance(turns.get("total"), int)
                              else "Unknown")
                correct = ("yes" if cell.get("correct") is True else
                           "no" if cell.get("correct") is False else "Unknown")
                failures = "; ".join(str(item) for item in cell.get("failures", [])) or "—"
                lines.append(
                    f"| {text(cell.get('repetition', 'unknown'))} | {text(cell.get('arm', 'unknown'))} | "
                    f"{text(cell.get('status') or 'unknown')} | {correct} | "
                    f"{elapsed(cell.get('elapsed_ms'))} | {total} | {cache_read} | {cache_write} | {turn_total} | "
                    f"{cell_overlap(case, cell.get('arm', ''), cell.get('overlap'))} | {text(failures)} |"
                )
        else:
            lines.extend(["", "No cells recorded for this case."])

        absent = [cell for cell in missing if cell.get("case") == case]
        if absent:
            lines.extend(["", "### Not run", "",
                          "| Repetition | Arm |", "|---:|---|"])
            lines.extend(f"| {text(cell.get('repetition', 'unknown'))} | {text(cell.get('arm', 'unknown'))} |"
                         for cell in absent)

    if not report.get("cases"):
        lines.extend(["", "No case metrics are available."])
    return "\n".join(lines) + "\n"


def _load_cells(run_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in sorted(run_dir.glob("cells/*/cell.json"))]


def _create_run(
    cache_root: Path, mode: str, cases: list[str], arms: list[str], repetitions: int,
    deadline_seconds: int, check: dict[str, Any], repetition_ids: list[int] | None = None,
) -> tuple[Path, dict[str, Any]]:
    run_dir = _new_run_dir(cache_root)
    manifest = {
        "schema_version": 1, "run_id": run_dir.name, "mode": mode,
        "state": "running", "cases": cases, "arms": arms,
        "repetitions": repetitions,
        "repetition_ids": repetition_ids if repetition_ids is not None else list(range(1, repetitions + 1)),
        "deadline_seconds": deadline_seconds,
        "provider": PROVIDER, "model": MODEL, "thinking": THINKING,
        "child_cap": CHILD_CAP, "core_tools": list(CORE_TOOLS),
        "preflight": check, "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_cells": [],
    }
    _write_json(run_dir / "preflight.json", check)
    _write_json(run_dir / "run.json", manifest)
    return run_dir, manifest


def _save_report(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    report = build_report(
        _load_cells(run_dir), manifest["repetitions"],
        manifest["cases"], manifest["arms"], manifest.get("repetition_ids"),
    )
    report.update({
        "run_id": manifest["run_id"], "run_dir": str(run_dir.resolve()),
        "mode": manifest["mode"], "state": manifest["state"],
        "preflight_passed": manifest["preflight"].get("passed", False),
        "provider": manifest.get("provider"), "model": manifest.get("model"),
        "thinking": manifest.get("thinking"),
        "deadline_seconds": manifest.get("deadline_seconds"),
    })
    _write_report_files(run_dir, report)
    _write_json(run_dir / "run.json", manifest)
    return report


def _failed_cell(run_dir: Path, case: str, arm: str, repetition: int, error: Exception) -> dict[str, Any]:
    cell_dir = private_dir(run_dir / "cells" / f"{case}-{arm}-r{repetition:02d}")
    record = {
        "case": case, "arm": arm, "repetition": repetition,
        "status": "failed", "correct": False,
        "failures": [f"runner error: {error}"],
        "termination_reason": "runner_error", "artifacts": {"cell": str(cell_dir)},
    }
    _write_json(cell_dir / "cell.json", record)
    return record


def _attempt_cell(
    run_dir: Path, case: str, arm: str, repetition: int, deadline_seconds: int,
    packages: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    try:
        return run_cell(run_dir, case, arm, repetition, deadline_seconds, packages)
    except Exception as error:
        return _failed_cell(run_dir, case, arm, repetition, error)


def _record_attempt(manifest: dict[str, Any], record: dict[str, Any], run_dir: Path) -> None:
    manifest["completed_cells"].append({
        "case": record["case"], "arm": record["arm"],
        "repetition": record["repetition"], "status": record["status"],
    })
    _write_json(run_dir / "run.json", manifest)


def _rotated_arms(offset: int) -> list[str]:
    index = offset % len(ARMS)
    return list(ARMS[index:] + ARMS[:index])


def matrix_schedule(cases: list[str], repetitions: int) -> list[tuple[str, str, int]]:
    if repetitions < 3:
        raise ValueError("matrix requires at least 3 repetitions")
    if not cases:
        raise ValueError("at least one case is required")
    schedule = [(cases[0], arm, 1) for arm in _rotated_arms(0)]
    for repetition in range(1, repetitions + 1):
        for case_index, case in enumerate(cases):
            if repetition == 1 and case_index == 0:
                continue
            schedule.extend((case, arm, repetition)
                            for arm in _rotated_arms(repetition - 1 + case_index))
    return schedule


def _run_cells(
    run_dir: Path, manifest: dict[str, Any], check: dict[str, Any],
    cells: list[tuple[str, str, int]], deadline_seconds: int,
) -> list[dict[str, Any]]:
    records = []
    for case, arm, repetition in cells:
        record = _attempt_cell(run_dir, case, arm, repetition, deadline_seconds, check["package_info"])
        records.append(record)
        interrupted = record.get("termination_reason") == "interrupted"
        if interrupted:
            manifest["state"] = "interrupted"
        _record_attempt(manifest, record, run_dir)
        if interrupted:
            break
    return records


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, default=default_cache_root())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install", help="install and verify the exact pinned extension packages")
    commands.add_parser("preflight", help="check wrapper, package pins, and isolation prerequisites")
    run = commands.add_parser("run", help="run one fresh benchmark cell")
    run.add_argument("--case", choices=CASES, required=True)
    run.add_argument("--arm", choices=ARMS, required=True)
    run.add_argument("--repetition", type=int, default=1)
    run.add_argument("--deadline", type=int, default=DEFAULT_DEADLINE)
    smoke = commands.add_parser("smoke", help="run one triage cell per arm")
    smoke.add_argument("--deadline", type=int, default=DEFAULT_DEADLINE)
    matrix = commands.add_parser(
        "matrix", help="run paired repetitions for each case",
        description=f"Run paired repetitions across arms: {', '.join(ARMS)}.",
    )
    matrix.add_argument("--repetitions", type=int, default=3, help="repetitions per case and arm (minimum 3)")
    matrix.add_argument("--deadline", type=int, default=DEFAULT_DEADLINE)
    matrix.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    report = commands.add_parser("report", help="summarize saved cell.json files")
    report.add_argument("run_dir", type=Path)
    report.add_argument("--repetitions", type=int)
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
        if args.command == "report":
            run_dir = args.run_dir.expanduser().resolve()
            manifest_path = run_dir / "run.json"
            try:
                manifest = json.loads(manifest_path.read_text())
            except FileNotFoundError:
                manifest = {}
            cells = _load_cells(run_dir)
            cases = manifest.get("cases") or sorted({cell["case"] for cell in cells}) or list(CASES)
            arms = manifest.get("arms") or list(ARMS)
            repetitions = (args.repetitions if args.repetitions is not None
                           else manifest.get("repetitions") or max(
                               (cell.get("repetition", 0) for cell in cells), default=1))
            repetition_ids = manifest.get("repetition_ids") if args.repetitions is None else None
            result = build_report(cells, repetitions, cases, arms, repetition_ids)
            result.update({"run_dir": str(run_dir), "run_id": manifest.get("run_id"),
                           "mode": manifest.get("mode"), "state": manifest.get("state"),
                           "provider": manifest.get("provider"), "model": manifest.get("model"),
                           "thinking": manifest.get("thinking"),
                           "deadline_seconds": manifest.get("deadline_seconds")})
            _write_report_files(run_dir, result)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "run":
            if args.deadline < 1 or args.repetition < 1:
                raise ValueError("deadline and repetition must be positive")
            check = preflight(cache_root)
            run_dir, manifest = _create_run(
                cache_root, "single", [args.case], [args.arm], 1, args.deadline, check,
                [args.repetition],
            )
            if not check["passed"]:
                manifest["state"] = "blocked_preflight"
                report = _save_report(run_dir, manifest)
                print(json.dumps(report, indent=2, sort_keys=True))
                return 1
            record = _run_cells(
                run_dir, manifest, check, [(args.case, args.arm, args.repetition)], args.deadline
            )[0]
            if manifest["state"] != "interrupted":
                manifest["state"] = "complete"
            report = _save_report(run_dir, manifest)
            print(json.dumps({"record": record, "report": report}, indent=2, sort_keys=True))
            return 130 if manifest["state"] == "interrupted" else 0 if _cell_passed(record) else 1
        if args.command == "smoke":
            if args.deadline < 1:
                raise ValueError("deadline must be positive")
            check = preflight(cache_root)
            run_dir, manifest = _create_run(
                cache_root, "smoke", ["triage"], list(ARMS), 1, args.deadline, check
            )
            if not check["passed"]:
                manifest["state"] = "blocked_preflight"
            else:
                cells = _run_cells(run_dir, manifest, check,
                                   [("triage", arm, 1) for arm in _rotated_arms(0)], args.deadline)
                if manifest["state"] != "interrupted":
                    manifest["state"] = "complete" if all(_cell_passed(cell) for cell in cells) else "smoke_failed"
            report = _save_report(run_dir, manifest)
            print(json.dumps(report, indent=2, sort_keys=True))
            return 130 if manifest["state"] == "interrupted" else 0 if manifest["state"] == "complete" else 1
        if args.command == "matrix":
            if args.deadline < 1 or args.repetitions < 3:
                raise ValueError("matrix requires a positive deadline and at least 3 repetitions")
            cases = list(dict.fromkeys(args.cases))
            if not cases:
                raise ValueError("at least one case is required")
            check = preflight(cache_root)
            run_dir, manifest = _create_run(
                cache_root, "matrix", cases, list(ARMS), args.repetitions, args.deadline, check
            )
            if not check["passed"]:
                manifest["state"] = "blocked_preflight"
                report = _save_report(run_dir, manifest)
                print(json.dumps(report, indent=2, sort_keys=True))
                return 1
            schedule = matrix_schedule(cases, args.repetitions)
            smoke = _run_cells(run_dir, manifest, check, schedule[:len(ARMS)], args.deadline)
            if manifest["state"] == "interrupted" or not all(_cell_passed(cell) for cell in smoke):
                if manifest["state"] != "interrupted":
                    manifest["state"] = "smoke_failed"
                report = _save_report(run_dir, manifest)
                print(json.dumps(report, indent=2, sort_keys=True))
                return 130 if manifest["state"] == "interrupted" else 1
            for cell in schedule[len(ARMS):]:
                _run_cells(run_dir, manifest, check, [cell], args.deadline)
                if manifest["state"] == "interrupted":
                    break
            if manifest["state"] != "interrupted":
                manifest["state"] = "complete"
            report = _save_report(run_dir, manifest)
            print(json.dumps(report, indent=2, sort_keys=True))
            return 130 if manifest["state"] == "interrupted" else 0 if not report["failed_cells"] and not report["missing_cells"] else 1
    except Exception as error:
        print(json.dumps({"passed": False, "error": str(error)}), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
