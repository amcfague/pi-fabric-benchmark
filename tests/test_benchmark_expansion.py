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
        self.assertIn("python3 -m unittest test_contracts", build_prompt(
            "debug", "stock", Path("/cell/project")
        ))
        integration = build_prompt("integration", "fabric", Path("/cell/project"))
        self.assertIn("grand_total_cents", integration)
        self.assertIn("same `shipping.py` file", integration.lower())
        delegated = build_prompt("integration", "subagents", Path("/cell/project"))
        workflow = json.loads(delegated.partition("workflowScript = ")[2].splitlines()[0])
        children = json.loads(workflow.partition("const children = ")[2].partition(";\n")[0])
        self.assertEqual([child["key"] for child in children], [
            "catalog", "billing", "shipping-fee", "checkout-total",
        ])

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

            def finish(_command, project, _env, events_path, stderr_path, *_args):
                prompt = build_prompt("control", "fabric", project, 30)
                script = json.loads(prompt.partition("fabric_exec code = ")[2].splitlines()[0])
                child_result = {
                    "key": "billing", "startedAt": 100, "endedAt": 300,
                    "result": {
                        "runnerSessionId": "worker-1", "turns": 2,
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
                events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
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
            self.assertFalse(record["overlap"]["evidenced"])
            self.assertFalse(record["overlap_required"])
            self.assertEqual(record["turns"]["total"], 3)
            self.assertEqual(record["children"][0]["turns"], 2)

    def test_tintin_foreground_agents_pass_with_pooled_usage_and_observed_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_pi = root / "pi"
            fake_pi.write_text("#!/bin/sh\nexit 0\n")
            fake_pi.chmod(0o700)
            run_dir = root / "run"
            answer = json.dumps(TRIAGE)

            def finish(_command, project, _env, events_path, stderr_path, _deadline, **kwargs):
                self.assertEqual(kwargs["observed_tools"], ("Agent",))
                prompt = build_prompt("triage", "tintin-subagents", project, 30)
                specs = json.loads(prompt.partition("Agent arguments = ")[2])
                starts, ends, times = [], [], {}
                for index, spec in enumerate(specs):
                    call_id = f"agent-{index}"
                    starts.append({
                        "type": "tool_execution_start", "toolCallId": call_id,
                        "toolName": "Agent", "args": spec,
                    })
                    times[call_id] = {"start": 1000 + index * 100}
                for index, spec in enumerate(specs):
                    call_id = f"agent-{index}"
                    result = {"content": [{"type": "text", "text": "done"}], "details": {
                        "agentId": f"id-{index}", "status": "completed",
                        "turnCount": index + 2, "durationMs": 3000,
                    }}
                    if index == len(specs) - 1:
                        result["usage"] = {
                            "input": 30, "output": 12, "cacheRead": 5, "cacheWrite": 2,
                            "totalTokens": 49, "cost": {"total": 0.002},
                        }
                    ends.append({
                        "type": "tool_execution_end", "toolCallId": call_id,
                        "toolName": "Agent", "result": result, "isError": False,
                    })
                    times[call_id]["end"] = 5000 - index * 50
                events = starts + ends + [
                    {"type": "message_end", "message": {
                        "id": "parent", "role": "assistant", "stopReason": "stop",
                        "usage": {"input": 10, "output": 5, "cacheRead": 7,
                                  "cacheWrite": 1, "totalTokens": 23},
                        "content": [{"type": "text", "text": answer}],
                    }},
                    {"type": "agent_settled"},
                ]
                events_path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
                stderr_path.write_text("")
                return {
                    "exit_code": 0, "timed_out": False, "interrupted": False,
                    "elapsed_ms": 321, "termination_reason": "process_exit",
                    "tool_call_times": times,
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
            self.assertEqual(record["turns"]["children"], 9)
            self.assertEqual(record["turns"]["total"], 10)
            self.assertTrue(record["usage"]["children"]["known"])
            self.assertEqual(record["usage"]["children"]["cache_read_tokens"], 5)
            self.assertEqual(record["usage"]["children"]["total_tokens"], 49)
            self.assertEqual([child["turns"] for child in record["children"]], [2, 3, 4])
            self.assertTrue(settings["reportUsage"])
            events = [json.loads(line) for line in (Path(record["artifacts"]["events"])).read_text().splitlines()]
            bad_events = json.loads(json.dumps(events))
            bad_events[0]["args"]["isolated"] = False
            self.assertFalse(child_launch_evidence(
                "tintin-subagents", bad_events, "triage", Path(record["artifacts"]["project"]), 30,
            )["verified"])

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
