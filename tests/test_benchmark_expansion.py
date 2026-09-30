import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import benchmark
from benchmark import ARMS, CASES, build_prompt, build_report, child_launch_evidence, matrix_schedule, parse_events, render_markdown
from checks.patch import verify as verify_patch
from checks.triage import verify as verify_triage

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "project"
TRIAGE = json.loads((ROOT / "checks" / "triage.json").read_text())


class ExpandedBenchmarkTests(unittest.TestCase):
    def test_default_matrix_covers_distinct_work_shapes(self):
        self.assertEqual(set(CASES), {"control", "triage", "patch", "debug", "integration"})
        self.assertEqual(ARMS, ("stock", "subagents", "tintin-subagents", "fabric"))
        self.assertEqual(len(matrix_schedule(CASES, 3)), 60)

        control = build_prompt("control", "subagents", Path("/cell/project"))
        self.assertIn("child cap is 1", control)
        self.assertIn("delegate exactly 1 independent task", control.lower())
        debug = build_prompt("debug", "stock", Path("/cell/project"))
        self.assertIn("captured failing contract tests", debug)
        self.assertNotIn("Run `python3", debug)
        integration = build_prompt("integration", "fabric", Path("/cell/project"))
        self.assertIn("grand_total_cents", integration)
        self.assertIn("one shipping owner", integration.lower())
        self.assertNotIn("Delegate three independent tasks", build_prompt(
            "integration", "stock", Path("/cell/project")))
        delegated = build_prompt("integration", "subagents", Path("/cell/project"))
        workflow = json.loads(delegated.partition("workflowScript = ")[2].splitlines()[0])
        children = json.loads(workflow.partition("const children = ")[2].partition(";\n")[0])
        self.assertEqual([child["key"] for child in children], ["catalog", "billing", "shipping"])
        self.assertIn("quote_order", children[2]["task"])
        stress = build_prompt("integration-contention", "fabric", Path("/cell/project"))
        self.assertIn("same `shipping.py` file", stress.lower())
        self.assertEqual(len(benchmark._child_tasks("integration-contention", Path("/cell/project"))), 4)
        stress_code = json.loads(stress.partition("fabric_exec code = ")[2])
        self.assertIn("agents.log", stress_code)

    def test_same_file_stress_is_not_paired_with_stock_or_main_cases(self):
        cell = {"case": "integration-contention", "arm": "fabric", "repetition": 1,
                "orchestration": "scripted", "case_version": benchmark.CASE_VERSION,
                "status": "passed", "correct": True, "elapsed_ms": 100,
                "usage": {"known": False}, "overlap": {"evidenced": True, "max_concurrency": 4}}
        report = build_report([cell], 1, cases=["integration-contention"], arms=["fabric"])
        self.assertEqual(report["case_class"], "stress")
        self.assertEqual(report["passed_cells"], 0)
        attempts = [{"key": key, "tool": "edit", "timestamp": "2026-09-30T00:00:02Z"}
                    for key in ("shipping-fee", "checkout-total")]
        verified = build_report([{**cell, "stress_evidence": {"verified": True, "attempts": attempts}}],
                                1, cases=["integration-contention"], arms=["fabric"])
        self.assertEqual(verified["passed_cells"], 1)
        self.assertIsNone(report["cells"][0]["stress_evidence"])
        self.assertTrue(verified["cells"][0]["stress_evidence"]["verified"])
        self.assertIn("Edit evidence", render_markdown(verified))
        self.assertEqual(report["cases"]["integration-contention"]["paired_speedup"], {})
        self.assertIn("not compared", render_markdown(report))
        with self.assertRaises(ValueError):
            build_report([cell, {**cell, "case": "integration"}], 1,
                         cases=["integration-contention", "integration"], arms=["fabric"])

    def test_debug_precheck_is_captured_before_the_measured_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_pi = root / "pi"
            fake_pi.write_text("#!/bin/sh\nexit 0\n")
            fake_pi.chmod(0o700)
            def finished(command, project, env, events_path, stderr_path, *_args):
                baseline = project / "debug-precheck.txt"
                self.assertTrue(baseline.is_file())
                self.assertIn("FAIL: test_available_units_subtracts_reservations", baseline.read_text())
                self.assertIn("debug-precheck.txt", command[-1])
                self.assertNotIn("Run `python3", command[-1])
                events_path.write_text("\n".join(json.dumps(event) for event in [
                    {"type": "message_end", "message": {"id": "parent", "role": "assistant",
                     "stopReason": "stop", "content": [{"type": "text", "text": "done"}],
                     "usage": {"input": 8, "output": 3}}},
                    {"type": "agent_settled"},
                ]) + "\n")
                stderr_path.write_text("")
                return {"exit_code": 0, "timed_out": False, "interrupted": False,
                        "elapsed_ms": 25, "termination_reason": "process_exit"}

            with patch("benchmark.run_process", side_effect=finished), \
                 patch("benchmark._grade", return_value={"passed": True, "errors": []}):
                record = benchmark.run_cell(root / "run", "debug", "stock", 1, 30, {},
                                            pi_binary=str(fake_pi),
                                            source_env={"PATH": os.defpath, "HOME": str(root)})
            self.assertEqual(record["status"], "passed", record["failures"])
            self.assertEqual(record["elapsed_ms"], 25)
            self.assertTrue(Path(record["artifacts"]["debug_precheck"]).is_file())

    def test_one_child_launch_is_verified_without_parallel_fanout(self):
        project = Path("/cell/project")
        deadline = 30
        prompt = build_prompt("control", "subagents", project, deadline)
        script = json.loads(prompt.partition("workflowScript = ")[2].splitlines()[0])
        result_text = json.dumps({"children": [{"key": "billing", "ok": True, "runId": "child-1"}]})
        events = [
            {"type": "tool_execution_start", "toolCallId": "enable", "toolName": "subagents_enable", "args": {}},
            {"type": "tool_execution_end", "toolCallId": "enable", "toolName": "subagents_enable", "result": {"content": []}, "isError": False},
            {"type": "tool_execution_start", "toolCallId": "list", "toolName": "subagent", "args": {"action": "list", "capabilities": True}},
            {"type": "tool_execution_end", "toolCallId": "list", "toolName": "subagent", "result": {"content": []}, "isError": False},
            {"type": "tool_execution_start", "toolCallId": "workflow", "toolName": "subagent", "args": {
                "workflowScript": script, "cwd": str(project), "async": False,
                "timeoutMs": deadline * 1000, "mission": False,
            }},
            {"type": "tool_execution_end", "toolCallId": "workflow", "toolName": "subagent", "result": {
                "content": [{"type": "text", "text": result_text}], "details": {},
            }, "isError": False},
        ]
        self.assertTrue(child_launch_evidence("subagents", events, "control", project, deadline)["verified"])

        fabric_prompt = build_prompt("control", "fabric", project, deadline)
        fabric_script = json.loads(fabric_prompt.partition("fabric_exec code = ")[2].splitlines()[0])
        fabric_events = [
            {"type": "tool_execution_start", "toolCallId": "fabric", "toolName": "fabric_exec", "args": {"code": fabric_script}},
            {"type": "tool_execution_end", "toolCallId": "fabric", "toolName": "fabric_exec", "result": {"content": []}, "isError": False},
        ]
        self.assertTrue(child_launch_evidence("fabric", fabric_events, "control", project, deadline)["verified"])

    def test_control_cell_passes_without_parallel_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_pi = root / "pi"
            fake_pi.write_text("#!/bin/sh\nexit 0\n")
            fake_pi.chmod(0o700)
            run_dir = root / "run"
            answer = json.dumps({"observed_cents": 1254, "required_cents": 5000})
            child_model = "openai/gpt-6-sol"
            child_status = "completed"
            child_error = None

            def finish(_command, project, _env, events_path, stderr_path, *_args):
                prompt = build_prompt("control", "fabric", project, 30)
                script = json.loads(prompt.partition("fabric_exec code = ")[2].splitlines()[0])
                child_result = {
                    "key": "billing", "startedAt": 100, "endedAt": 300,
                    "result": {
                        "runnerSessionId": "worker-1", "turns": 2, "text": "billing result",
                        "status": child_status, "error": child_error,
                        "model": child_model, "thinking": "xhigh",
                        "usage": {"input": 4, "output": 2, "cacheRead": 1, "cacheWrite": 0},
                    },
                }
                events = [
                    {"type": "tool_execution_start", "toolCallId": "fabric", "toolName": "fabric_exec", "args": {"code": script}},
                    {"type": "tool_execution_end", "toolCallId": "fabric", "toolName": "fabric_exec", "result": {
                        "content": [{"type": "text", "text": json.dumps({"children": [child_result]})}],
                        "details": {},
                    }, "isError": False},
                    {"type": "message_end", "message": {
                        "id": "parent", "role": "assistant", "stopReason": "stop",
                        "usage": {"input": 8, "output": 3, "cacheRead": 2, "cacheWrite": 1},
                        "content": [{"type": "text", "text": answer}],
                    }},
                    {"type": "agent_settled"},
                ]
                events_path.write_text("\n".join(json.dumps(event) for event in events)
                                       + "\n[mcporter] stderr from serena\n")
                stderr_path.write_text("")
                return {
                    "exit_code": 0, "timed_out": False, "interrupted": False,
                    "elapsed_ms": 321, "termination_reason": "process_exit",
                }

            with patch("benchmark.run_process", side_effect=finish):
                record = benchmark.run_cell(
                    run_dir, "control", "fabric", 1, 30,
                    {"fabric": {"entry": str(root / "fabric.js")}},
                    pi_binary=str(fake_pi), source_env={"PATH": os.defpath, "HOME": str(root)},
                )

            self.assertEqual(record["status"], "passed", record["failures"])
            self.assertEqual(record["event_diagnostics"], {"serena": 1})
            self.assertEqual(json.loads((run_dir / "cells" / "control-fabric-r01" / "cell.json").read_text())
                             ["event_diagnostics"], {"serena": 1})
            self.assertFalse(record["overlap"]["evidenced"])
            self.assertFalse(record["overlap_required"])
            self.assertEqual(record["turns"]["total"], 3)
            self.assertEqual(record["children"][0]["turns"], 2)
            self.assertEqual(record["children"][0]["output"], "billing result")
            self.assertEqual(record["children"][0]["model"], "openai/gpt-6-sol")
            self.assertEqual(record["children"][0]["thinking"], "xhigh")
            child_model = "openai/wrong-model"
            with patch("benchmark.run_process", side_effect=finish):
                wrong = benchmark.run_cell(
                    root / "wrong-model", "control", "fabric", 1, 30,
                    {"fabric": {"entry": str(root / "fabric.js")}},
                    pi_binary=str(fake_pi), source_env={"PATH": os.defpath, "HOME": str(root)},
                )
            self.assertFalse(wrong["child_runtime_matches_parent"])
            self.assertFalse(wrong["status"] == "passed")
            self.assertTrue(wrong["grader"]["passed"])
            child_model = "openai/gpt-6-sol"
            child_status = "failed"
            child_error = "child did not exit after settlement"
            with patch("benchmark.run_process", side_effect=finish):
                failed = benchmark.run_cell(
                    root / "failed-child", "control", "fabric", 1, 30,
                    {"fabric": {"entry": str(root / "fabric.js")}},
                    pi_binary=str(fake_pi), source_env={"PATH": os.defpath, "HOME": str(root)},
                )
            self.assertTrue(failed["grader"]["passed"])
            self.assertIn("one or more child runs did not complete", failed["failures"])
            self.assertEqual(failed["children"][0]["error"], child_error)

    def test_scripted_control_cell_uses_fixed_dispatch_then_parent_synthesis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_pi = root / "pi"
            fake_pi.write_text("#!/bin/sh\nexit 0\n")
            fake_pi.chmod(0o700)
            answer = json.dumps({"observed_cents": 1254, "required_cents": 5000})
            child = {"key": "billing", "startedAt": 100, "endedAt": 300,
                     "result": {"id": "worker-1", "status": "completed", "model": "openai/gpt-6-sol",
                                "thinking": "xhigh", "text": "billing result", "turns": 2,
                                "usage": {"input": 4, "output": 2, "cacheRead": 1,
                                          "cacheWrite": 1, "totalTokens": 8}}}
            invalid_line = [False]
            dispatch_override = {}

            def dispatch(command, project, env, events_path, stderr_path, *_args):
                self.assertEqual(command[command.index("--provider") + 1], "benchmark-driver")
                spec = json.loads(Path(env["PI_BENCHMARK_SCRIPTED_SPEC"]).read_text())
                self.assertEqual(spec["arm"], "fabric")
                events = [
                    {"type": "response", "id": "benchmark-launch", "command": "prompt", "success": True},
                    {"type": "tool_execution_start", "toolCallId": "bench-fabric", "toolName": "fabric_exec",
                     "args": spec["stages"][0]["calls"][0]["arguments"]},
                    {"type": "tool_execution_end", "toolCallId": "bench-fabric", "toolName": "fabric_exec",
                     "result": {"content": [{"type": "text", "text": json.dumps({"children": [child]})}],
                                "details": {}}, "isError": False},
                    {"type": "message_end", "message": {"id": "driver", "role": "assistant",
                     "stopReason": "stop", "content": [{"type": "text", "text": "Scripted dispatch complete"}],
                     "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0,
                               "totalTokens": 0}}},
                    {"type": "agent_settled"},
                ]
                events_path.write_text("\n".join(json.dumps(event) for event in events)
                                       + "\n" + ("not-json\n" if invalid_line[0] else ""))
                stderr_path.write_text("")
                return {**{"exit_code": 0, "timed_out": False, "interrupted": False,
                           "elapsed_ms": 100, "termination_reason": "process_exit",
                           "accepted": True, "settled": True}, **dispatch_override}

            def synthesize(command, project, env, events_path, stderr_path, *_args):
                self.assertIn("--no-tools", command)
                self.assertIn("billing result", command[-1])
                events_path.write_text("\n".join(json.dumps(event) for event in [
                    {"type": "message_end", "message": {"id": "parent", "role": "assistant",
                     "stopReason": "stop", "content": [{"type": "text", "text": answer}],
                     "usage": {"input": 8, "output": 3, "cacheRead": 2, "cacheWrite": 1,
                               "totalTokens": 14}}},
                    {"type": "agent_settled"},
                ]) + "\n")
                stderr_path.write_text("")
                return {"exit_code": 0, "timed_out": False, "interrupted": False,
                        "elapsed_ms": 60, "termination_reason": "process_exit"}

            packages = {"fabric": {"entry": str(root / "fabric.js"),
                                   "scripted_runtime": {"entry": str(root / "pi-ai.js")}}}
            with patch("benchmark.scripted_ai_runtime", side_effect=AssertionError("rehashed runtime")), \
                 patch("benchmark.run_rpc_process", side_effect=dispatch), \
                 patch("benchmark.run_process", side_effect=synthesize) as synthesis:
                def cell(name):
                    return benchmark.run_cell(
                        root / name, "control", "fabric", 1, 30, packages,
                        pi_binary=str(fake_pi), source_env={"PATH": os.defpath, "HOME": str(root)},
                        orchestration="scripted", cache_root=root,
                    )

                record = cell("run")
                baseline = dict(child["result"])
                for name, failure in (
                    ("failed-child", "one or more child runs did not complete"),
                    ("wrong-model", "subagent child model/thinking did not match the parent"),
                    ("missing-usage", "child token usage is unknown"),
                    ("malformed", "1 malformed JSONL event(s)"),
                    ("deadline", "deadline exceeded"),
                ):
                    with self.subTest(name=name):
                        child["result"] = dict(baseline)
                        invalid_line[0] = name == "malformed"
                        dispatch_override.clear()
                        if name == "failed-child":
                            child["result"]["status"] = "failed"
                        elif name == "wrong-model":
                            child["result"]["model"] = "openai/wrong-model"
                        elif name == "missing-usage":
                            child["result"].pop("usage")
                        elif name == "deadline":
                            dispatch_override.update(timed_out=True, termination_reason="deadline")
                        before = synthesis.call_count
                        failed = cell(name)
                        self.assertEqual(failed["status"], "failed")
                        self.assertIn(failure, failed["failures"])
                        self.assertEqual(synthesis.call_count, before,
                                         "invalid dispatch must not pay for synthesis")

            self.assertEqual(record["status"], "passed", record["failures"])
            self.assertEqual(record["answer"], answer)
            self.assertEqual(record["elapsed_ms"], 160)
            self.assertEqual(record["orchestration"], "scripted")
            self.assertEqual(record["case_version"], benchmark.CASE_VERSION)
            self.assertEqual(record["turns"]["total"], 3)
            self.assertTrue(record["parent_loadout_verified"])
            self.assertTrue(record["child_runtime_matches_parent"])
            self.assertTrue(Path(record["artifacts"]["synthesis_events"]).is_file())

    def test_scripted_specs_reuse_the_native_extension_workloads(self):
        project = Path("/cell/project")
        subagents = benchmark._scripted_spec("triage", "subagents", project, 30)
        stages = subagents["stages"]
        self.assertEqual([stage["calls"][0]["name"] for stage in stages],
                         ["subagents_enable", "subagent", "subagent"])
        self.assertEqual(stages[1]["calls"][0]["arguments"],
                         {"action": "list", "capabilities": True})
        workflow = stages[2]["calls"][0]["arguments"]
        self.assertIn("runs.all(children)", workflow["workflowScript"])
        self.assertFalse(workflow["async"])
        self.assertEqual(workflow["timeoutMs"], 30000)

        tintin = benchmark._scripted_spec("triage", "tintin-subagents", project, 30)
        self.assertEqual([call["arguments"]["name"] for call in tintin["stages"][0]["calls"]],
                         ["catalog", "billing", "shipping"])
        self.assertTrue(all(call["arguments"]["run_in_background"]
                            for call in tintin["stages"][0]["calls"]))
        self.assertEqual([item["launchId"] for item in tintin["stages"][1]["collect"]],
                         ["bench-launch-catalog", "bench-launch-billing", "bench-launch-shipping"])

        fabric = benchmark._scripted_spec("triage", "fabric", project, 30)
        code = fabric["stages"][0]["calls"][0]["arguments"]["code"]
        self.assertIn("Promise.all", code)
        self.assertIn("extensions: false", code)
        self.assertIn("startedAt: result.startedAt", code)
        self.assertIn("endedAt: result.finishedAt", code)
        self.assertIn("error: result.error", code)
        with self.assertRaises(ValueError):
            benchmark._scripted_spec("triage", "stock", project, 30)

    def test_synthesis_receives_only_complete_keyed_child_outputs(self):
        project = Path("/cell/project")
        prompt = benchmark.build_synthesis_prompt("control", project, [
            {"key": "billing", "output": '{"observed_cents":1254,"required_cents":5000}'},
        ])
        self.assertEqual(json.loads(prompt.rsplit("\n\n", 1)[1])["billing"],
                         '{"observed_cents":1254,"required_cents":5000}')
        self.assertIn("Return only a JSON object", prompt)
        self.assertIn("Do not inspect or modify files", prompt)
        with self.assertRaises(ValueError):
            benchmark.build_synthesis_prompt("control", project, [{"key": "billing"}])
        with self.assertRaises(ValueError):
            benchmark.build_synthesis_prompt("control", project, [{"key": "shipping", "output": "wrong"}])

    def test_scripted_provider_invokes_registered_tool_without_model_choice(self):
        pi_binary = Path(benchmark.PI_BINARY)
        ai_entry = (benchmark.default_cache_root() / "packages" / "tintin-subagents"
                    / "node_modules" / "@earendil-works" / "pi-ai" / "dist" / "index.js")
        if not pi_binary.is_file() or not ai_entry.is_file():
            self.skipTest("Pi wrapper and pinned AI runtime are required for the offline provider probe")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            tool = root / "tool.ts"
            tool.write_text('''export default function (pi: any) {
  pi.registerTool({ name: "fabric_exec", label: "Probe", description: "Offline probe",
    parameters: { type: "object", properties: { code: { type: "string" } }, required: ["code"] },
    async execute() { return { content: [{ type: "text", text: "probe complete" }], details: {} }; }
  });
}
''')
            spec = root / "scripted.json"
            spec.write_text(json.dumps({"version": 1, "arm": "fabric", "stages": [
                {"calls": [{"id": "bench-probe", "name": "fabric_exec",
                            "arguments": {"code": "return 42"}}]},
            ]}))
            env = benchmark.build_environment(root / "cell", dict(os.environ), str(pi_binary))
            env.update({"PI_OFFLINE": "1", "PI_BENCHMARK_AI_ENTRY": str(ai_entry),
                        "PI_BENCHMARK_SCRIPTED_SPEC": str(spec)})
            command = [str(pi_binary), "--mode", "rpc", "--provider", "benchmark-driver",
                       "--model", "scripted", "--thinking", "off", "--session-dir", str(root / "session"),
                       "--tools", "fabric_exec", "--no-extensions", "-e", str(tool),
                       "-e", str(ROOT / "scripted-provider.ts"), "--no-skills", "--no-prompt-templates",
                       "--no-themes", "--no-context-files", "--no-approve"]
            result = benchmark.run_rpc_process(command, project, env, root / "events.jsonl",
                                               root / "stderr.log", 30, "Run fixed probe")
            events, malformed, _ = benchmark.read_events(root / "events.jsonl")
            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(result["accepted"] and result["settled"])
            self.assertEqual(malformed, 0)
            self.assertEqual([event["toolName"] for event in events
                              if event.get("type") == "tool_execution_start"], ["fabric_exec"],
                             [(event.get("type"), event.get("message", {}).get("errorMessage"))
                              for event in events if event.get("type") == "message_end"])

            self.assertEqual([event["isError"] for event in events
                              if event.get("type") == "tool_execution_end"], [False])

    def test_scripted_provider_collects_background_ids_after_all_launches(self):
        pi_binary = Path(benchmark.PI_BINARY)
        ai_entry = (benchmark.default_cache_root() / "packages" / "tintin-subagents"
                    / "node_modules" / "@earendil-works" / "pi-ai" / "dist" / "index.js")
        if not pi_binary.is_file() or not ai_entry.is_file():
            self.skipTest("Pi wrapper and pinned AI runtime are required for the offline provider probe")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            tool = root / "tools.ts"
            tool.write_text('''export default function (pi: any) {
  pi.registerTool({ name: "Agent", label: "Agent", description: "Fake background launch",
    parameters: { type: "object", properties: { name: { type: "string" } }, required: ["name"] },
    async execute(_id: string, params: { name: string }) {
      return { content: [{ type: "text", text: "started" }],
        details: { agentId: `id-${params.name}`, status: "background" } };
    }
  });
  pi.registerTool({ name: "get_subagent_result", label: "Result", description: "Fake join",
    parameters: { type: "object", properties: { agent_id: { type: "string" } }, required: ["agent_id"] },
    async execute(_id: string, params: { agent_id: string }) {
      return { content: [{ type: "text", text: `Agent: ${params.agent_id}` }], details: {} };
    }
  });
}
''')
            spec = root / "scripted.json"
            spec.write_text(json.dumps({"version": 1, "arm": "tintin-subagents", "stages": [
                {"calls": [{"id": f"bench-launch-{key}", "name": "Agent",
                            "arguments": {"name": key}} for key in ("catalog", "billing")]},
                {"collect": [{"id": f"bench-result-{key}", "launchId": f"bench-launch-{key}"}
                             for key in ("catalog", "billing")]},
            ]}))
            env = benchmark.build_environment(root / "cell", dict(os.environ), str(pi_binary))
            env.update({"PI_OFFLINE": "1", "PI_BENCHMARK_AI_ENTRY": str(ai_entry),
                        "PI_BENCHMARK_SCRIPTED_SPEC": str(spec)})
            command = [str(pi_binary), "--mode", "rpc", "--provider", "benchmark-driver",
                       "--model", "scripted", "--thinking", "off", "--session-dir", str(root / "session"),
                       "--tools", "Agent,get_subagent_result", "--no-extensions", "-e", str(tool),
                       "-e", str(ROOT / "scripted-provider.ts"), "--no-skills", "--no-prompt-templates",
                       "--no-themes", "--no-context-files", "--no-approve"]
            result = benchmark.run_rpc_process(command, project, env, root / "events.jsonl",
                                               root / "stderr.log", 30, "Run fixed launches")
            events, malformed, _ = benchmark.read_events(root / "events.jsonl")
            starts = [(i, event) for i, event in enumerate(events)
                      if event.get("type") == "tool_execution_start"]
            launch_ends = [i for i, event in enumerate(events)
                           if event.get("type") == "tool_execution_end" and event.get("toolName") == "Agent"]
            self.assertEqual(result["exit_code"], 0)
            self.assertTrue(result["accepted"] and result["settled"])
            self.assertEqual(malformed, 0)
            self.assertEqual([event["toolName"] for _, event in starts],
                             ["Agent", "Agent", "get_subagent_result", "get_subagent_result"])
            self.assertLess(max(launch_ends), min(i for i, event in starts
                                                   if event["toolName"] == "get_subagent_result"))
            self.assertEqual([event["args"]["agent_id"] for _, event in starts
                              if event["toolName"] == "get_subagent_result"],
                             ["id-catalog", "id-billing"])

    def test_tintin_background_agents_collect_pooled_usage_and_overlapping_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_pi = root / "pi"
            fake_pi.write_text("#!/bin/sh\nexit 0\n")
            fake_pi.chmod(0o700)
            run_dir = root / "run"
            answer = json.dumps(TRIAGE)
            intervals = [(1000, 5000), (1200, 5100), (1400, 5200)]

            def finish(_command, project, _env, events_path, stderr_path, _deadline):
                specs = json.loads(build_prompt(
                    "triage", "tintin-subagents", project, 30,
                ).partition("Agent arguments = ")[2])
                session_root = events_path.parent / "agent" / "sessions" / "fixture"
                session_root.mkdir(parents=True)
                events = []
                identifiers = [f"child-{index:02d}-agent" for index in range(len(specs))]
                outputs = []
                for index, spec in enumerate(specs):
                    identifier = identifiers[index]
                    output = json.dumps({"child": spec["name"]})
                    outputs.append(output)
                    call_id = f"agent-{index}"
                    tool_result = {
                        "content": [{"type": "text", "text": f"Agent ID: {identifier}"}],
                        "details": {"agentId": identifier, "status": "background"},
                    }
                    if index == 0:
                        tool_result["usage"] = {
                            "input": 10, "output": 4, "cacheRead": 2, "cacheWrite": 1,
                            "totalTokens": 18, "cost": {"total": 0.001},
                        }
                    events.extend([
                        {"type": "tool_execution_start", "toolCallId": call_id,
                         "toolName": "Agent", "args": spec},
                        {"type": "tool_execution_end", "toolCallId": call_id,
                         "toolName": "Agent", "result": tool_result, "isError": False},
                        {"type": "entry_appended", "entry": {"type": "custom",
                         "customType": "subagents:record", "data": {
                             "id": identifier, "type": "general-purpose",
                             "description": spec["description"], "status": "completed",
                             "result": output, "startedAt": intervals[index][0],
                             "completedAt": intervals[index][1],
                         }}},
                    ])
                    session_events = [
                        {"type": "session", "timestamp": "2026-09-30T00:00:00.000Z"},
                        {"type": "model_change", "provider": benchmark.PROVIDER,
                         "modelId": benchmark.MODEL},
                        {"type": "thinking_level_change", "thinkingLevel": benchmark.THINKING},
                        {"type": "session_info",
                         "name": f"general-purpose#{identifier[:8]}"},
                    ]
                    for turn in range(index + 2):
                        session_events.append({
                            "type": "message",
                            "timestamp": f"2026-09-30T00:00:{turn + 1:02d}.000Z",
                            "message": {
                                "id": f"{identifier}-turn-{turn}", "role": "assistant",
                                "stopReason": "stop" if turn == index + 1 else "toolUse",
                                "content": [{"type": "text", "text": output}],
                            },
                        })
                    (session_root / f"{index}.jsonl").write_text(
                        "\n".join(json.dumps(event) for event in session_events) + "\n"
                    )

                for index, spec in enumerate(specs):
                    identifier = identifiers[index]
                    result_text = (
                        f"Agent: {identifier}\nType: general-purpose | Status: completed | Tool uses: 1 | Duration: 4s\n"
                        f"Description: {spec['description']}\n\n{outputs[index]}"
                    )
                    result_id = f"result-{index}"
                    tool_result = {"content": [{"type": "text", "text": result_text}]}
                    if index == len(specs) - 1:
                        tool_result["usage"] = {
                            "input": 20, "output": 8, "cacheRead": 3, "cacheWrite": 1,
                            "totalTokens": 31, "cost": {"total": 0.001},
                        }
                    events.extend([
                        {"type": "tool_execution_start", "toolCallId": result_id,
                         "toolName": "get_subagent_result",
                         "args": {"agent_id": identifier, "wait": True}},
                        {"type": "tool_execution_end", "toolCallId": result_id,
                         "toolName": "get_subagent_result", "result": tool_result,
                         "isError": False},
                    ])
                events.extend([
                    {"type": "message_end", "message": {
                        "id": "parent", "role": "assistant", "stopReason": "stop",
                        "usage": {"input": 10, "output": 5, "cacheRead": 7,
                                  "cacheWrite": 1, "totalTokens": 23},
                        "content": [{"type": "text", "text": answer}],
                    }},
                    {"type": "agent_settled"},
                ])
                events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
                stderr_path.write_text("")
                return {
                    "exit_code": 0, "timed_out": False, "interrupted": False,
                    "elapsed_ms": 321, "termination_reason": "process_exit",
                }

            with patch("benchmark.run_process", side_effect=finish):
                record = benchmark.run_cell(
                    run_dir, "triage", "tintin-subagents", 1, 30,
                    {"tintin-subagents": {"entry": str(root / "tintin.js")}},
                    pi_binary=str(fake_pi), source_env={"PATH": os.defpath, "HOME": str(root)},
                )

            settings = json.loads((Path(record["environment"]["agent_dir"]) / "subagents.json").read_text())
            self.assertEqual(record["status"], "passed", record["failures"])
            self.assertTrue(record["overlap"]["evidenced"])
            self.assertEqual(record["overlap"]["max_concurrency"], 3)
            self.assertEqual(record["turns"]["children"], 9)
            self.assertEqual(record["turns"]["total"], 10)
            self.assertTrue(record["usage"]["children"]["known"])
            self.assertEqual(record["usage"]["children"]["cache_read_tokens"], 5)
            self.assertEqual(record["usage"]["children"]["cache_write_tokens"], 2)
            self.assertEqual(record["usage"]["children"]["total_tokens"], 49)
            self.assertEqual(record["usage"]["children"]["cost_usd"], 0.002)
            self.assertEqual([child["turns"] for child in record["children"]], [2, 3, 4])
            self.assertEqual([child["output"] for child in record["children"]],
                             [json.dumps({"child": key}) for key in ("catalog", "billing", "shipping")])
            self.assertTrue(settings["reportUsage"])
            self.assertTrue(settings["rememberAgents"])
            self.assertEqual(settings["maxConcurrent"], benchmark.CHILD_CAP)
            events = [json.loads(line) for line in Path(record["artifacts"]["events"]).read_text().splitlines()]
            bad_events = json.loads(json.dumps(events))
            bad_events[0]["args"]["isolated"] = False
            self.assertFalse(child_launch_evidence(
                "tintin-subagents", bad_events, "triage", Path(record["artifacts"]["project"]), 30,
            )["verified"])

            intervals[:] = [(1000, 2000), (11000, 12000), (21000, 22000)]
            with patch("benchmark.run_process", side_effect=finish):
                disjoint = benchmark.run_cell(
                    root / "run-disjoint", "triage", "tintin-subagents", 1, 30,
                    {"tintin-subagents": {"entry": str(root / "tintin.js")}},
                    pi_binary=str(fake_pi), source_env={"PATH": os.defpath, "HOME": str(root)},
                )
            self.assertEqual(disjoint["status"], "failed")
            self.assertEqual(len(disjoint["children"]), 3)
            self.assertFalse(disjoint["overlap"]["evidenced"])
            self.assertIn("timestamped overlapping child intervals are unverified", disjoint["failures"])

    def test_control_grader_checks_computed_output(self):
        answer = json.dumps({"observed_cents": 1254, "required_cents": 5000})
        result = benchmark._grade("control", FIXTURE, answer)
        self.assertTrue(result["passed"], result)

    def test_report_includes_cached_tokens_and_turns(self):
        report = build_report([{
            "case": "triage", "arm": "stock", "repetition": 1,
            "status": "passed", "correct": True, "elapsed_ms": 10,
            "usage": {
                "known": True, "input_tokens": 5, "output_tokens": 2,
                "total_tokens": 7, "cache_read_tokens": 11,
                "cache_write_tokens": 3, "cost_usd": 0.001,
            },
            "turns": {"known": True, "parent": 2, "children": 0, "total": 2},
        }], repetitions=1, cases=["triage"], arms=["stock"])

        metrics = report["cases"]["triage"]["arms"]["stock"]
        self.assertEqual(metrics["usage"]["cache_read_tokens"], 11)
        self.assertEqual(metrics["usage"]["cache_write_tokens"], 3)
        self.assertEqual(metrics["turns"]["total"], 2)
        markdown = render_markdown(report)
        self.assertIn("Cache read", markdown)
        self.assertIn("Cache write", markdown)
        self.assertIn("Turns", markdown)
        self.assertIn("| stock | 1/1 | 0 | 0 | 100.0% | 10.0 ms | baseline | 7 | 11 | 3 | 2 | $0.0010 | n/a |", markdown)

    def test_absent_cache_and_turn_telemetry_remains_unknown(self):
        report = build_report([{
            "case": "triage", "arm": "stock", "repetition": 1,
            "status": "passed", "correct": True, "elapsed_ms": 10,
            "usage": {"known": True, "input_tokens": 5, "output_tokens": 2,
                      "total_tokens": 7, "cost_usd": 0.001},
        }], repetitions=1, cases=["triage"], arms=["stock"])
        metrics = report["cases"]["triage"]["arms"]["stock"]
        self.assertIsNone(metrics["usage"]["cache_read_tokens"])
        self.assertEqual(metrics["usage"]["cache_read_tokens_known_cells"], 0)
        self.assertIsNone(metrics["usage"]["cache_write_tokens"])
        self.assertFalse(metrics["turns"]["complete"])
        self.assertIsNone(metrics["turns"]["total"])

    def test_parent_turn_count_and_cached_usage_come_from_unique_assistant_messages(self):
        events = [
            {"type": "message_end", "message": {
                "id": "turn-1", "role": "assistant", "stopReason": "toolUse",
                "usage": {"input": 10, "output": 4, "cacheRead": 3, "cacheWrite": 1},
                "content": [{"type": "text", "text": "first"}],
            }},
            {"type": "message_end", "message": {
                "id": "turn-1", "role": "assistant", "stopReason": "toolUse",
                "usage": {"input": 10, "output": 4, "cacheRead": 3, "cacheWrite": 1},
                "content": [{"type": "text", "text": "duplicate"}],
            }},
            {"type": "message_end", "message": {
                "id": "turn-2", "role": "assistant", "stopReason": "stop",
                "usage": {"input": 8, "output": 2, "cacheRead": 5, "cacheWrite": 2},
                "content": [{"type": "text", "text": "answer"}],
            }},
        ]

        result = parse_events(events, exit_code=0)
        self.assertEqual(result["turns"], 2)
        self.assertEqual(result["usage"]["cache_read_tokens"], 8)
        self.assertEqual(result["usage"]["cache_write_tokens"], 3)

    def test_triage_checker_accepts_equivalent_max_argument_order(self):
        answer = json.loads(json.dumps(TRIAGE))
        answer["findings"]["catalog"]["correct_expression"] = "max(stock - reserved, 0)"
        result = verify_triage(answer)
        self.assertTrue(result["passed"], result)

    def test_stress_evidence_requires_both_native_shipping_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            sessions = root / "session"
            project.mkdir()
            sessions.mkdir()
            shipping = project / "shipping.py"
            shipping.write_text("seed")
            children = []
            for key in ("shipping-fee", "checkout-total"):
                path = sessions / f"{key}.jsonl"
                path.write_text("\n".join(json.dumps(entry) for entry in [
                    {"type": "message", "timestamp": "2026-09-30T00:00:01Z", "message": {
                        "role": "assistant", "content": [{"type": "toolCall", "id": key,
                            "name": "edit", "arguments": {"path": str(shipping),
                            "oldText": "seed", "newText": key}}]}},
                    {"type": "message", "timestamp": "2026-09-30T00:00:02Z", "message": {
                        "role": "toolResult", "toolCallId": key, "toolName": "edit", "isError": False}},
                ]) + "\n")
                children.append({"key": key, "sessionFile": str(path),
                                 "startedAt": 100, "endedAt": 300})
            evidence = benchmark.stress_edit_evidence(children, project)
            self.assertTrue(evidence["verified"])
            self.assertEqual({entry["key"] for entry in evidence["attempts"]},
                             {"shipping-fee", "checkout-total"})
            sequential = [{**children[0], "startedAt": 100, "endedAt": 200},
                          {**children[1], "startedAt": 201, "endedAt": 300}]
            others = [{"key": key, "startedAt": 100, "endedAt": 300}
                      for key in ("catalog", "billing")]
            self.assertTrue(benchmark.overlap_summary(others + sequential)["evidenced"])
            self.assertFalse(benchmark.stress_edit_evidence(others + sequential, project)["verified"])
            self.assertFalse(benchmark.stress_edit_evidence(children[:1], project)["verified"])
            missing_session = [{**children[0], "sessionFile": str(root.parent / "outside.jsonl")},
                               children[1]]
            self.assertFalse(benchmark.stress_edit_evidence(missing_session, project)["verified"])
            native_logs = [{"key": key, "startedAt": 100, "endedAt": 300,
                            "stressEdits": [{"tool": "edit", "path": str(shipping),
                                             "timestamp": "2026-09-30T00:00:02Z"}]}
                           for key in ("shipping-fee", "checkout-total")]
            self.assertTrue(benchmark.stress_edit_evidence(native_logs, project)["verified"])
            native_logs[0]["stressEdits"][0]["path"] = str(root / "outside.py")
            self.assertFalse(benchmark.stress_edit_evidence(native_logs, project)["verified"])

    def test_integration_grader_checks_total_after_parallel_module_fixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            shutil.copytree(FIXTURE, project)
            replacements = {
                "catalog.py": ("return stock + reserved", "return max(0, stock - reserved)"),
                "billing.py": (
                    "return unit_price_cents + quantity",
                    "return unit_price_cents * quantity",
                ),
                "shipping.py": (
                    "if subtotal_cents > free_threshold_cents:",
                    "if subtotal_cents >= free_threshold_cents:",
                ),
            }
            for name, (old, new) in replacements.items():
                path = project / name
                source = path.read_text()
                self.assertIn(old, source)
                path.write_text(source.replace(old, new))
            path = project / "shipping.py"
            source = path.read_text()
            old = '"shipping_fee_cents": shipping_fee_cents(subtotal),'
            self.assertIn(old, source)
            path.write_text(source.replace(
                old,
                old + '\n        "grand_total_cents": subtotal + shipping_fee_cents(subtotal),',
            ))

            result = verify_patch(project, integration=True)
            self.assertTrue(result["passed"], result)
            grade = benchmark._grade("integration", project, "")
            self.assertTrue(grade["passed"], grade)


if __name__ == "__main__":
    unittest.main()
