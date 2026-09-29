import contextlib
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import benchmark

from checks.patch import verify as verify_patch
from checks.triage import verify as verify_triage
from benchmark import (
    PINS,
    _attempt_cell,
    _run_cells,
    _write_json,
    build_command,
    build_report,
    matrix_schedule,
    child_launch_evidence,
    extract_children,
    inspect_package,
    build_environment,
    main,
    run_cell,
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
                if arm == 'stock':
                    self.assertIn('--no-extensions', command)
                else:
                    self.assertNotIn('--no-extensions', command)
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
            project_dir = Path('/cell/project')
            deadline = 900
            subagent_prompt = build_prompt('triage', 'subagents', project_dir, deadline)
            subagent_script = json.loads(
                subagent_prompt.partition('workflowScript = ')[2].splitlines()[0]
            )
            subagent_event = [{
                'type': 'tool_execution_start', 'toolName': 'subagent',
                'args': {'workflowScript': subagent_script},
            }]
            self.assertTrue(child_launch_evidence(
                'subagents', subagent_event, 'triage', project_dir, deadline)['verified'])
            self.assertFalse(child_launch_evidence(
                'subagents', subagent_event * 2, 'triage', project_dir, deadline)['verified'])
            bad_subagent_script = subagent_script.replace(
                '"extensions":[]', '"extensions":["unrelated"]', 1)
            self.assertFalse(child_launch_evidence('subagents', [{
                'type': 'tool_execution_start', 'toolName': 'subagent',
                'args': {'workflowScript': bad_subagent_script},
            }], 'triage', project_dir, deadline)['verified'])
            self.assertFalse(child_launch_evidence(
                'subagents', subagent_event, 'patch', project_dir, deadline)['verified'])
            fabric_prompt = build_prompt('patch', 'fabric', project_dir, deadline)
            fabric_script = json.loads(
                fabric_prompt.partition('fabric_exec code = ')[2].splitlines()[0]
            )
            fabric_event = [{
                'type': 'tool_execution_start', 'toolName': 'fabric_exec',
                'args': {'code': fabric_script},
            }]
            self.assertFalse(child_launch_evidence(
                'fabric', fabric_event, 'patch', project_dir, deadline)['verified'])
            self.assertFalse(child_launch_evidence(
                'fabric', fabric_event * 2, 'patch', project_dir, deadline)['verified'])
            bad_fabric_script = fabric_script.replace('extensions: false', 'extensions: true', 1)
            self.assertFalse(child_launch_evidence('fabric', [{
                'type': 'tool_execution_start', 'toolName': 'fabric_exec',
                'args': {'code': bad_fabric_script},
            }], 'patch', project_dir, deadline)['verified'])
            self.assertFalse(child_launch_evidence(
                'fabric', fabric_event, 'triage', project_dir, deadline)['verified'])
            raw_children = {'children': [
                {'key': 'a', 'startedAt': 100, 'endedAt': 300,
                 'result': {'runnerSessionId': 's1', 'usage': {'input': 1, 'output': 2}}},
                {'key': 'b', 'startedAt': 200, 'endedAt': 400,
                 'result': {'runnerSessionId': 's2', 'usage': {'input': 3, 'output': 4}}},
                {'key': 'c', 'startedAt': 400, 'endedAt': 500,
                 'result': {'runnerSessionId': 's3', 'usage': {'input': 5, 'output': 6}}},
            ]}
            encoded = json.dumps(raw_children)
            fabric_result = {
                'type': 'tool_execution_end', 'toolName': 'fabric_exec',
                'result': {'text': encoded},
            }
            self.assertEqual(extract_children([fabric_result], arm='fabric'), [])
            self.assertEqual(len(extract_children([fabric_result], arm='subagents')), 3)

    def test_fabric_comments_and_returned_records_do_not_prove_child_execution(self):
        project_dir = Path('/cell/project')
        deadline = 900
        prompt = build_prompt('patch', 'fabric', project_dir, deadline)
        script = json.loads(prompt.partition('fabric_exec code = ')[2].splitlines()[0])
        fabricated = {'children': [
            {'key': key, 'startedAt': 100, 'endedAt': 300,
             'result': {'runnerSessionId': key, 'usage': {'input': 10, 'output': 2}}}
            for key in ('a', 'b', 'c')
        ]}
        events = [
            {'type': 'tool_execution_start', 'toolName': 'fabric_exec',
             'args': {'code': f'/* {script} */'}},
            {'type': 'tool_execution_end', 'toolName': 'fabric_exec',
             'result': {'text': json.dumps(fabricated)}},
        ]

        self.assertFalse(child_launch_evidence(
            'fabric', events, 'patch', project_dir, deadline)['verified'])
        children = extract_children(events, arm='fabric')
        self.assertEqual(children, [])
        self.assertFalse(overlap_summary(children)['evidenced'])

    def test_interrupted_process_group_uses_term_then_kill(self):
        class FakeProcess:
            pid = 1234
            returncode = None
            stdout = io.BytesIO()
            stderr = io.BytesIO()

            def __init__(self):
                self.wait_calls = 0

            def wait(self, timeout=None):
                self.wait_calls += 1
                if self.wait_calls == 1:
                    raise KeyboardInterrupt
                if self.wait_calls == 2:
                    raise benchmark.subprocess.TimeoutExpired('fake-pi', timeout)
                self.returncode = -signal.SIGKILL
                return self.returncode

        process = FakeProcess()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('benchmark.subprocess.Popen', return_value=process), \
                    patch('benchmark.os.killpg') as killpg:
                try:
                    result = run_process(['fake-pi'], root, {}, root / 'events.jsonl',
                                         root / 'stderr.log', deadline_seconds=30,
                                         terminate_grace_seconds=0.01)
                except KeyboardInterrupt:
                    result = None

        self.assertIsNotNone(result, 'run_process propagated KeyboardInterrupt')
        self.assertTrue(result['interrupted'])
        self.assertEqual(result['termination_reason'], 'interrupted')
        self.assertEqual(killpg.call_args_list, [
            ((process.pid, signal.SIGTERM),),
            ((process.pid, signal.SIGKILL),),
        ])

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
        self.assertIsNone(missing['interval_count'])
        self.assertIsNone(missing['max_concurrency'])


class ReportingChecks(unittest.TestCase):
    def _save_cell(self, run_dir, case, arm, repetition, status='passed',
                   termination_reason='process_exit'):
        cell_dir = run_dir / 'cells' / f'{case}-{arm}-r{repetition:02d}'
        record = {
            'case': case, 'arm': arm, 'repetition': repetition,
            'status': status, 'correct': status == 'passed', 'elapsed_ms': 100,
            'answer': 'partial or final answer',
            'usage': {
                'known': True, 'input_tokens': 8, 'output_tokens': 4,
                'total_tokens': 12, 'cost_usd': 0.01,
            },
            'failures': [] if status == 'passed' else ['synthetic failure'],
            'termination_reason': termination_reason,
            'artifacts': {
                'cell': str(cell_dir), 'events': str(cell_dir / 'events.jsonl'),
                'stderr': str(cell_dir / 'stderr.log'),
            },
        }
        cell_dir.mkdir(parents=True, exist_ok=True)
        _write_json(cell_dir / 'cell.json', record)
        return record

    def test_duplicate_parent_message_usage_is_counted_once(self):
        message = {
            'id': 'm1', 'role': 'assistant', 'stopReason': 'stop',
            'usage': {'input': 10, 'output': 2},
            'content': [{'type': 'text', 'text': 'ok'}],
        }
        later = {
            'id': 'm2', 'role': 'assistant', 'stopReason': 'stop',
            'usage': {'input': 5, 'output': 1},
            'content': [{'type': 'text', 'text': 'done'}],
        }
        events = [
            {'type': 'message_end', 'message': message},
            {'type': 'message_end', 'message': message},
            {'type': 'message_end', 'message': later},
            {'type': 'agent_settled'},
        ]
        usage = parse_events(events, exit_code=0)['usage']
        self.assertEqual(usage['input_tokens'], 15)
        self.assertEqual(usage['output_tokens'], 3)
        self.assertEqual(usage['total_tokens'], 18)
        self.assertFalse(parse_events([{'type': 'agent_settled'}], 0)['usage']['known'])

    def test_duplicate_child_result_is_counted_once(self):
        event = {'children': [{
            'key': 'worker-a', 'startedAt': 100, 'endedAt': 300,
            'result': {'usage': {'input': 10, 'output': 2}},
        }]}
        children = extract_children([event, event], arm='subagents')
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['usage']['total_tokens'], 12)
        self.assertEqual(extract_children([event], arm='stock'), [])

    def test_report_keeps_failures_and_pairs_only_correct_cells(self):
        def cell(arm, repetition, status, elapsed):
            return {
                'case': 'triage', 'arm': arm, 'repetition': repetition,
                'status': status, 'correct': status == 'passed', 'elapsed_ms': elapsed,
                'usage': {
                    'known': True, 'input_tokens': 10, 'output_tokens': 2,
                    'total_tokens': 12, 'cost_usd': 0.01,
                },
            }

        result = build_report([
            cell('stock', 1, 'passed', 1000),
            cell('stock', 2, 'failed', 800),
            cell('subagents', 1, 'passed', 500),
            cell('subagents', 2, 'passed', 450),
        ], repetitions=2, cases=['triage'])
        triage = result['cases']['triage']
        stock = triage['arms']['stock']
        self.assertEqual(stock['planned'], 2)
        self.assertEqual(stock['attempted'], 2)
        self.assertEqual(stock['passed'], 1)
        self.assertEqual(stock['failed'], 1)
        self.assertEqual(stock['median_elapsed_ms'], 1000)
        self.assertEqual(triage['arms']['subagents']['median_elapsed_ms'], 475)
        self.assertEqual(triage['arms']['fabric']['not_run'], 2)
        self.assertEqual(triage['paired_speedup']['subagents']['pairs'], 1)
        self.assertEqual(triage['paired_speedup']['subagents']['median_speedup'], 2)
        self.assertEqual(len(result['failed_cells']), 1)

    def test_missing_usage_is_unknown_not_zero(self):
        result = build_report([{
            'case': 'triage', 'arm': 'stock', 'repetition': 1,
            'status': 'passed', 'correct': True, 'elapsed_ms': 10,
            'usage': {'known': False, 'total_tokens': None, 'cost_usd': None},
        }], repetitions=1, cases=['triage'], arms=['stock'])
        usage = result['cases']['triage']['arms']['stock']['usage']
        self.assertFalse(usage['complete'])
        self.assertIsNone(usage['total_tokens'])
        self.assertIsNone(usage['cost_usd'])

    def test_single_run_repetition_three_plans_only_that_cell_and_keeps_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp)

            def attempt(run_dir, case, arm, repetition, *_):
                return self._save_cell(run_dir, case, arm, repetition)

            output = io.StringIO()
            with patch('benchmark.preflight', return_value={
                    'passed': True, 'package_info': {}}), \
                    patch('benchmark._attempt_cell', side_effect=attempt), \
                    contextlib.redirect_stdout(output):
                status = main([
                    '--cache-root', str(cache_root), 'run', '--case', 'triage',
                    '--arm', 'stock', '--repetition', '3',
                ])

            self.assertEqual(status, 0)
            result = json.loads(output.getvalue())
            report = result['report']
            stock = report['cases']['triage']['arms']['stock']
            self.assertEqual(result['record']['repetition'], 3)
            self.assertEqual(stock['planned'], 1)
            self.assertEqual(stock['attempted'], 1)
            self.assertEqual(stock['not_run'], 0)
            self.assertEqual(report['missing_cells'], [])
            self.assertEqual(stock['usage']['total_tokens'], 12)
            manifest = json.loads((Path(report['run_dir']) / 'run.json').read_text())
            self.assertEqual(manifest['repetitions'], 1)
            self.assertEqual(manifest['repetition_ids'], [3])

    def test_write_json_replaces_symlink_without_modifying_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'target.json'
            output = root / 'report.json'
            target.write_text('untouched')
            output.symlink_to(target)

            _write_json(output, {'safe': True})

            self.assertEqual(target.read_text(), 'untouched')
            self.assertFalse(output.is_symlink())
            self.assertEqual(json.loads(output.read_text()), {'safe': True})

    def test_grader_error_keeps_completed_cell_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_pi = root / 'pi'
            fake_pi.write_text('#!/bin/sh\nexit 0\n')
            fake_pi.chmod(0o700)
            run_dir = root / 'run'
            run_dir.mkdir()
            events = [
                {'type': 'message_end', 'message': {
                    'id': 'answer', 'role': 'assistant', 'stopReason': 'stop',
                    'usage': {'input': 8, 'output': 4},
                    'content': [{'type': 'text', 'text': 'completed answer'}],
                }},
                {'type': 'agent_settled'},
            ]

            def finished_run(_command, _cwd, _env, event_path, stderr_path, *_args):
                event_path.write_text('\n'.join(json.dumps(event) for event in events) + '\n')
                stderr_path.write_text('captured stderr')
                return {
                    'exit_code': 0, 'timed_out': False, 'interrupted': False,
                    'elapsed_ms': 321, 'termination_reason': 'process_exit',
                }

            with patch('benchmark.run_process', side_effect=finished_run), \
                    patch('benchmark._grade', side_effect=TimeoutError('grader timed out')):
                record = run_cell(
                    run_dir, 'triage', 'stock', 1, 30, {}, pi_binary=str(fake_pi),
                    source_env={'PATH': os.defpath, 'HOME': str(root)},
                )

            saved = json.loads((run_dir / 'cells' / 'triage-stock-r01' / 'cell.json').read_text())
            self.assertEqual(record['answer'], 'completed answer')
            self.assertEqual(record['elapsed_ms'], 321)
            self.assertIn('grader timed out', ' '.join(record['grader']['errors']))
            self.assertEqual(saved['answer'], 'completed answer')
            self.assertEqual(saved['elapsed_ms'], 321)
            self.assertTrue(Path(saved['artifacts']['events']).is_file())
            self.assertTrue(Path(saved['artifacts']['stderr']).is_file())

    def test_runner_error_is_saved_and_recorded_as_an_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            manifest = {'completed_cells': []}
            with patch('benchmark.run_cell', side_effect=RuntimeError('runner exploded')):
                records = _run_cells(
                    run_dir, manifest, {'package_info': {}},
                    [('triage', 'stock', 1)], 30,
                )

            saved = json.loads((run_dir / 'cells' / 'triage-stock-r01' / 'cell.json').read_text())
            run_manifest = json.loads((run_dir / 'run.json').read_text())
            self.assertEqual(records[0]['termination_reason'], 'runner_error')
            self.assertEqual(
                (saved['case'], saved['arm'], saved['repetition'], saved['status'], saved['termination_reason']),
                ('triage', 'stock', 1, 'failed', 'runner_error'),
            )
            self.assertIn('runner exploded', saved['failures'][0])
            self.assertEqual(run_manifest['completed_cells'][0]['status'], 'failed')

    def test_matrix_stops_at_failed_smoke_gate_with_full_missing_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempts = []
            output = io.StringIO()

            def attempt(run_dir, case, arm, repetition, *_):
                attempts.append((case, arm, repetition))
                status = 'failed' if len(attempts) == 2 else 'passed'
                return self._save_cell(run_dir, case, arm, repetition, status)

            with patch('benchmark.preflight', return_value={
                    'passed': True, 'package_info': {}}), \
                    patch('benchmark._attempt_cell', side_effect=attempt), \
                    contextlib.redirect_stdout(output):
                status = main(['--cache-root', tmp, 'matrix', '--repetitions', '3'])

            report = json.loads(output.getvalue())
            manifest = json.loads((Path(report['run_dir']) / 'run.json').read_text())
            self.assertEqual(status, 1)
            self.assertEqual(len(attempts), 3)
            self.assertEqual(manifest['state'], 'smoke_failed')
            self.assertEqual(report['planned_cells'], 18)
            self.assertEqual(len(report['missing_cells']), 15)

    def test_matrix_interrupt_saves_cell_and_report_without_later_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            attempts = []
            output = io.StringIO()

            def attempt(run_dir, case, arm, repetition, *_):
                attempts.append((case, arm, repetition))
                if len(attempts) == 2:
                    return self._save_cell(
                        run_dir, case, arm, repetition, 'failed', 'interrupted')
                return self._save_cell(run_dir, case, arm, repetition)

            with patch('benchmark.preflight', return_value={
                    'passed': True, 'package_info': {}}), \
                    patch('benchmark._attempt_cell', side_effect=attempt), \
                    contextlib.redirect_stdout(output):
                status = main(['--cache-root', tmp, 'matrix', '--repetitions', '3'])

            report = json.loads(output.getvalue())
            run_dir = Path(report['run_dir'])
            manifest = json.loads((run_dir / 'run.json').read_text())
            interrupted = json.loads(
                (run_dir / 'cells' / 'triage-subagents-r01' / 'cell.json').read_text())
            self.assertEqual(status, 130)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(manifest['state'], 'interrupted')
            self.assertEqual(report['state'], 'interrupted')
            self.assertEqual(len(report['missing_cells']), 16)
            self.assertEqual(interrupted['answer'], 'partial or final answer')
            self.assertEqual(interrupted['elapsed_ms'], 100)
            self.assertEqual(interrupted['termination_reason'], 'interrupted')
            self.assertIn('events', interrupted['artifacts'])

    def test_known_tokens_remain_available_when_cost_is_missing(self):
        report = build_report([{
            'case': 'triage', 'arm': 'stock', 'repetition': 1,
            'status': 'passed', 'correct': True, 'elapsed_ms': 10,
            'usage': {
                'known': True, 'input_tokens': 8, 'output_tokens': 4,
                'total_tokens': 12, 'cost_usd': None,
            },
        }], repetitions=1, cases=['triage'], arms=['stock'])
        usage = report['cases']['triage']['arms']['stock']['usage']
        self.assertTrue(usage['complete'])
        self.assertEqual(usage['total_tokens'], 12)
        self.assertFalse(usage['cost_complete'])
        self.assertIsNone(usage['cost_usd'])

    def test_report_sums_parent_and_child_token_breakdown(self):
        result = build_report([{
            'case': 'triage', 'arm': 'subagents', 'repetition': 1,
            'status': 'passed', 'correct': True, 'elapsed_ms': 10,
            'usage': {
                'known': True, 'total_tokens': 18, 'cost_usd': 0.02,
                'parent': {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12},
                'children': {'input_tokens': 5, 'output_tokens': 1, 'total_tokens': 6},
            },
        }], repetitions=1, cases=['triage'], arms=['subagents'])
        usage = result['cases']['triage']['arms']['subagents']['usage']
        self.assertEqual(usage['input_tokens'], 15)
        self.assertEqual(usage['output_tokens'], 3)
        self.assertEqual(usage['total_tokens'], 18)
        self.assertEqual(usage['cost_usd'], 0.02)

    def test_three_repetition_matrix_is_complete_and_rotates_arms(self):
        schedule = matrix_schedule(['triage', 'patch'], 3)
        self.assertEqual(len(schedule), 18)
        self.assertEqual(len(set(schedule)), 18)
        self.assertEqual(schedule[:3], [
            ('triage', 'stock', 1),
            ('triage', 'subagents', 1),
            ('triage', 'fabric', 1),
        ])
        self.assertEqual(schedule[3:6], [
            ('patch', 'subagents', 1),
            ('patch', 'fabric', 1),
            ('patch', 'stock', 1),
        ])
        self.assertEqual(schedule[6:9], [
            ('triage', 'subagents', 2),
            ('triage', 'fabric', 2),
            ('triage', 'stock', 2),
        ])
        with self.assertRaises(ValueError):
            matrix_schedule(['triage'], 2)


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
