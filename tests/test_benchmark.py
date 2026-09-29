import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from checks.patch import verify as verify_patch
from checks.triage import verify as verify_triage
from benchmark import (
    PINS,
    build_command,
    child_launch_evidence,
    extract_children,
    inspect_package,
    build_environment,
    build_prompt,
    overlap_summary,
    parse_events,
    run_process,
    validate_pi_binary,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "project"
TRIAGE = json.loads((ROOT / 'checks' / 'triage.json').read_text())


class LauncherChecks(unittest.TestCase):
    ENTRIES = {
        'subagents': Path('/cache/pi-subagents/index.js'),
        'fabric': Path('/cache/pi-fabric/dist/index.js'),
    }

    def test_parent_argv_is_isolated_and_loads_only_the_selected_package(self):
        for arm in ('stock', 'subagents', 'fabric'):
            with self.subTest(arm=arm):
                command = build_command(
                    arm, 'task', '/cell/session', self.ENTRIES,
                    pi_binary='/usr/local/bin/pi',
                )
                self.assertIn('--no-extensions', command)
                for flag in ('--no-skills', '--no-prompt-templates', '--no-themes',
                             '--no-context-files', '--no-approve'):
                    self.assertIn(flag, command)
                self.assertEqual(command[command.index('--provider') + 1], 'openai')
                self.assertEqual(command[command.index('--model') + 1], 'gpt-6-sol')
                self.assertEqual(command[command.index('--thinking') + 1], 'xhigh')
                self.assertEqual(command[command.index('--session-dir') + 1], '/cell/session')
                extensions = [command[i + 1] for i, arg in enumerate(command[:-1])
                              if arg in ('-e', '--extension')]
                if arm == 'stock':
                    self.assertEqual(extensions, [])
                else:
                    self.assertEqual(extensions, [str(self.ENTRIES[arm])])
                    self.assertTrue(Path(extensions[0]).is_absolute())

    def test_cell_environment_is_private_and_routes_child_pi_to_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = {
                'PATH': '/usr/bin:/bin', 'HOME': '/home/operator',
                'OPENAI_API_KEY': 'test-secret', 'ANTHROPIC_API_KEY': 'must-not-leak',
                'http_proxy': 'http://proxy', 'https_proxy': 'https://proxy',
                'PI_CODING_AGENT_DIR': '/home/operator/.pi',
            }
            env = build_environment(Path(tmp) / 'cell', base, '/usr/local/bin/pi')
            self.assertEqual(env['PATH'].split(os.pathsep)[0], '/usr/local/bin')
            self.assertEqual(env['OPENAI_API_KEY'], 'test-secret')
            self.assertNotIn('ANTHROPIC_API_KEY', env)
            self.assertEqual(env['http_proxy'], 'http://proxy')
            self.assertEqual(env['https_proxy'], 'https://proxy')
            self.assertNotEqual(env['PI_CODING_AGENT_DIR'], '/home/operator/.pi')
            self.assertEqual(env['PI_SUBAGENT_PI_BINARY'], '/usr/local/bin/pi')
            self.assertEqual(env['PI_FABRIC_PI_BINARY'], '/usr/local/bin/pi')

    def test_pi_real_requires_both_lowercase_proxy_variables(self):
        with self.assertRaises(ValueError):
            validate_pi_binary('/usr/local/bin/pi.real', {'http_proxy': 'http://proxy'})
        with self.assertRaises(ValueError):
            validate_pi_binary('/usr/local/bin/pi.real', {
                'HTTP_PROXY': 'http://proxy', 'HTTPS_PROXY': 'https://proxy'
            })
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / 'pi.real'
            binary.write_text('#!/bin/sh\nexit 0\n')
            binary.chmod(0o700)
            validate_pi_binary(str(binary), {
                'http_proxy': 'http://proxy', 'https_proxy': 'https://proxy'
            })
        validate_pi_binary('/usr/local/bin/pi', {})

    def test_plugin_prompts_require_extension_free_children_and_parallel_fanout(self):
        subagents = build_prompt('triage', 'subagents', Path('/cell/project'))
        self.assertIn('runs.all', subagents)
        self.assertIn('extensions: []', subagents)
        self.assertIn('skills: []', subagents)
        self.assertIn('child cap is 3', subagents)
        fabric = build_prompt('patch', 'fabric', Path('/cell/project'))
        self.assertIn('agents.run', fabric)
        self.assertIn('Promise.all', fabric)
        self.assertIn('extensions: false', fabric)
        self.assertIn('child cap is 3', fabric)

    def test_success_requires_settled_agent_and_nonerror_final_stop(self):
        good = [
            {'type': 'message_end', 'message': {
                'id': 'm1', 'role': 'assistant', 'stopReason': 'stop',
                'usage': {'input': 10, 'output': 2},
                'content': [{'type': 'text', 'text': 'ok'}],
            }},
            {'type': 'agent_end'}, {'type': 'agent_settled'},
        ]
        result = parse_events(good, exit_code=0)
        self.assertTrue(result['success'])
        self.assertTrue(result['settled'])
        bad = [dict(good[0], message=dict(good[0]['message'], stopReason='error')),
               *good[1:]]
        self.assertFalse(parse_events(bad, exit_code=0)['success'])
        self.assertFalse(parse_events(good[:-1], exit_code=0)['success'])

    def test_fake_pi_streams_jsonl_and_stderr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args_file = root / 'argv.json'
            fake = root / 'fake-pi'
            fake.write_text('\n'.join([
                '#!' + sys.executable,
                'import json, os, sys',
                'from pathlib import Path',
                'Path(os.environ["ARGS_FILE"]).write_text(json.dumps(sys.argv[1:]))',
                "print(json.dumps({'type':'agent_settled'}), flush=True)",
                "print('fake stderr', file=sys.stderr, flush=True)",
            ]) + '\n')
            fake.chmod(0o700)
            env = {**os.environ, 'ARGS_FILE': str(args_file)}
            command = build_command('stock', 'task', root / 'session', self.ENTRIES,
                                    pi_binary=str(fake))
            result = run_process(command, root, env, root / 'events.jsonl',
                                 root / 'stderr.log', deadline_seconds=5)
            self.assertFalse(result['timed_out'])
            self.assertEqual(result['exit_code'], 0)
            self.assertIn('--no-extensions', json.loads(args_file.read_text()))
            self.assertIn('fake stderr', (root / 'stderr.log').read_text())
            self.assertEqual(json.loads((root / 'events.jsonl').read_text())['type'],
                             'agent_settled')

    @unittest.skipUnless(os.name == 'posix', 'process groups require POSIX')
    def test_deadline_terminates_the_entire_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stopped_file = root / 'child.stopped'
            child_code = '\n'.join([
                'import signal,time',
                'from pathlib import Path',
                'def stop(*_):',
                f'    Path({str(stopped_file)!r}).write_text("stopped")',
                '    raise SystemExit(0)',
                'signal.signal(signal.SIGTERM, stop)',
                'while True: time.sleep(0.05)',
            ])
            fake = root / 'fake-pi'
            fake.write_text('\n'.join([
                '#!' + sys.executable,
                'import subprocess, sys, time',
                f'subprocess.Popen([sys.executable, "-c", {child_code!r}])',
                'time.sleep(30)',
            ]) + '\n')
            fake.chmod(0o700)
            result = run_process([str(fake)], root, os.environ.copy(),
                                 root / 'events.jsonl', root / 'stderr.log',
                                 deadline_seconds=0.5, terminate_grace_seconds=0.2)
            self.assertTrue(result['timed_out'])
            deadline = time.monotonic() + 2
            while not stopped_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(stopped_file.exists(),
                            'child survived process-group termination')

    def test_package_and_child_records_are_pinned_and_timestamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            prefix = cache / 'packages' / 'subagents'
            package = prefix / 'node_modules' / 'pi-subagents'
            package.mkdir(parents=True)
            (package / 'package.json').write_text(json.dumps({
                'name': 'pi-subagents', 'version': PINS['subagents']['version'],
                'pi': {'extensions': ['./index.js']},
            }))
            (package / 'index.js').write_text('// pinned fake entry')
            (prefix / 'package-lock.json').write_text(json.dumps({
                'packages': {'node_modules/pi-subagents': {
                    'integrity': PINS['subagents']['integrity'],
                }},
            }))
            info = inspect_package('subagents', cache)
            self.assertTrue(info['integrity_verified'])
            self.assertTrue(Path(info['entry']).is_absolute())
            self.assertEqual(info['version'], '0.73.1')
            script = 'const children = [{"key":"a","extensions":[],"skills":[],"model":"openai/gpt-6-sol:xhigh"},{"key":"b","extensions":[],"skills":[],"model":"openai/gpt-6-sol:xhigh"},{"key":"c","extensions":[],"skills":[],"model":"openai/gpt-6-sol:xhigh"}]; await runs.all(children);'
            self.assertTrue(child_launch_evidence('subagents', [{
                'type': 'tool_execution_start', 'toolName': 'subagent',
                'args': {'workflowScript': script},
            }])['verified'])
            fabric_script = 'const specs = [{"key":"a"},{"key":"b"},{"key":"c"}]; await Promise.all(specs.map(async s => agents.run({model:"openai/gpt-6-sol",extensions:false})));'
            self.assertTrue(child_launch_evidence('fabric', [{
                'type': 'tool_execution_start', 'toolName': 'fabric_exec',
                'args': {'code': fabric_script},
            }])['verified'])
            raw_children = {'children': [
                {'key': 'a', 'startedAt': 100, 'endedAt': 300,
                 'result': {'runnerSessionId': 's1', 'usage': {'input': 1, 'output': 2}}},
                {'key': 'b', 'startedAt': 200, 'endedAt': 400,
                 'result': {'runnerSessionId': 's2', 'usage': {'input': 3, 'output': 4}}},
                {'key': 'c', 'startedAt': 400, 'endedAt': 500,
                 'result': {'runnerSessionId': 's3', 'usage': {'input': 5, 'output': 6}}},
            ]}
            encoded = json.dumps(raw_children)
            records = extract_children([{
                'type': 'tool_execution_end', 'toolName': 'fabric_exec',
                'result': {'text': encoded},
            }])
            self.assertEqual(len(records), 3)
            self.assertTrue(all(record['usage']['known'] for record in records))
            self.assertEqual(overlap_summary(records)['max_concurrency'], 2)

    def test_overlap_requires_real_timestamped_intervals(self):
        overlap = overlap_summary([
            {'runId': 'a', 'startedAt': 10, 'endedAt': 30},
            {'runId': 'b', 'startedAt': 20, 'endedAt': 40},
            {'runId': 'c', 'startedAt': 40, 'endedAt': 50},
        ])
        self.assertTrue(overlap['evidenced'])
        self.assertEqual(overlap['max_concurrency'], 2)
        missing = overlap_summary([{'runId': 'a'}, {'runId': 'b'}])
        self.assertFalse(missing['evidenced'])
        self.assertEqual(missing['max_concurrency'], 0)


class TriageChecks(unittest.TestCase):
    def test_complete_answer_passes(self):
        self.assertTrue(verify_triage(TRIAGE)["passed"])

    def test_missing_finding_or_combined_conclusion_fails(self):
        answer = json.loads(json.dumps(TRIAGE))
        del answer["findings"]["catalog"]
        self.assertFalse(verify_triage(answer)["passed"])

        answer = json.loads(json.dumps(TRIAGE))
        del answer["conclusion"]
        self.assertFalse(verify_triage(answer)["passed"])

    def test_non_object_answer_fails_cleanly(self):
        self.assertFalse(verify_triage(None)["passed"])


class PatchChecks(unittest.TestCase):
    def copy_fixture(self, parent):
        target = Path(parent) / "project"
        shutil.copytree(FIXTURE, target)
        return target

    def patch(self, project, modules):
        fixes = {
            "catalog.py": (
                "return stock + reserved", "return max(0, stock - reserved)"
            ),
            "billing.py": (
                "return unit_price_cents + quantity",
                "return unit_price_cents * quantity",
            ),
            "shipping.py": (
                "if subtotal_cents > free_threshold_cents:",
                "if subtotal_cents >= free_threshold_cents:",
            ),
        }
        for name in modules:
            path = project / name
            source = path.read_text()
            old, new = fixes[name]
            self.assertIn(old, source)
            path.write_text(source.replace(old, new))

    def test_seed_and_two_fixes_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = self.copy_fixture(tmp)
            self.assertFalse(verify_patch(seed)["passed"])
            self.patch(seed, ("catalog.py", "billing.py"))
            self.assertFalse(verify_patch(seed)["passed"])

    def test_all_module_fixes_and_integration_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self.copy_fixture(tmp)
            self.patch(project, ("catalog.py", "billing.py", "shipping.py"))
            self.assertTrue(verify_patch(project)["passed"])

    def test_verifier_and_answer_key_stay_outside_writable_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self.copy_fixture(tmp)
            self.assertFalse((project / "checks").exists())
            self.assertFalse((project / "patch.py").exists())
            self.assertFalse((project / "triage.json").exists())


if __name__ == "__main__":
    unittest.main()
