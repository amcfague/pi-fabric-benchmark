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
from types import SimpleNamespace
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
    render_markdown,
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
                expected_tools = {
                    'stock': 'read,grep,find,ls,bash,edit,write',
                    'subagents': 'read,grep,find,ls,bash,edit,write,subagents_enable,subagent',
                    'fabric': 'read,grep,find,ls,bash,edit,write,fabric_exec',
                }
                self.assertEqual(command[command.index('--tools') + 1], expected_tools[arm])
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
            runtime_root = '/cache/subagents/node_modules/@earendil-works/pi-coding-agent'
            env = build_environment(
                Path(tmp) / 'cell', base, '/usr/local/bin/pi', runtime_root,
            )
            self.assertEqual(env['PATH'].split(os.pathsep)[0], '/usr/local/bin')
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertNotIn('ANTHROPIC_API_KEY', env)
            self.assertEqual(env['http_proxy'], 'http://proxy')
            self.assertEqual(env['https_proxy'], 'https://proxy')
            self.assertNotEqual(env['PI_CODING_AGENT_DIR'], '/home/operator/.pi')
            self.assertEqual(env['PI_SUBAGENT_PI_BINARY'], '/usr/local/bin/pi')
            self.assertEqual(env['PI_FABRIC_PI_BINARY'], '/usr/local/bin/pi')
            settings = json.loads((Path(env['PI_CODING_AGENT_DIR']) / 'settings.json').read_text())
            self.assertEqual(
                settings['subagents']['agentOverrides']['worker']['thinking'], 'xhigh',
            )

    def test_preflight_accepts_wrapper_auth_without_openai_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            packages = {
                arm: {'sha256': 'package-hash', 'integrity': PINS[arm]['integrity']}
                for arm in PINS
            }
            host_pin = benchmark.SUBAGENT_RUNTIME_PIN
            packages['subagents']['host_runtime'] = {
                'sha256': 'runtime-hash', 'version': host_pin['version'],
                'integrity': host_pin['integrity'],
            }
            (cache / 'package-manifest.json').write_text(json.dumps({'packages': packages}))
            integrities = {
                f"{pin['name']}@{pin['version']}": pin['integrity']
                for pin in PINS.values()
            }
            integrities[f"{host_pin['name']}@{host_pin['version']}"] = host_pin['integrity']
            cli = [
                SimpleNamespace(returncode=0, stdout='0.87.1\n'),
                SimpleNamespace(
                    returncode=0,
                    stdout='--mode --provider --model --thinking --session-dir --tools '
                           '--no-extensions --no-skills --no-prompt-templates --no-themes '
                           '--no-context-files --no-approve',
                ),
            ]
            with patch('benchmark.validate_pi_binary'), \
                    patch('benchmark.subprocess.run', side_effect=cli), \
                    patch('benchmark.npm_integrity', side_effect=integrities.__getitem__), \
                    patch('benchmark.inspect_package', return_value={
                        'sha256': 'package-hash', 'integrity_verified': True,
                    }), \
                    patch('benchmark.inspect_subagents_runtime', return_value={
                        'sha256': 'runtime-hash', 'version': host_pin['version'],
                        'integrity': host_pin['integrity'], 'integrity_verified': True,
                        'root': '/cache/runtime',
                    }):
                result = benchmark.preflight(cache, '/usr/local/bin/pi', source_env={})

            self.assertTrue(result['passed'], result['errors'])
            self.assertNotIn('credential_present', result)

    def test_subagents_host_runtime_is_pinned_for_foreground_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            prefix = cache / 'packages' / 'subagents'
            runtime = prefix / 'node_modules' / '@earendil-works' / 'pi-coding-agent'
            runtime.mkdir(parents=True)
            pin = benchmark.SUBAGENT_RUNTIME_PIN
            (runtime / 'package.json').write_text(json.dumps({
                'name': pin['name'], 'version': pin['version'],
            }))
            (prefix / 'package-lock.json').write_text(json.dumps({
                'packages': {'node_modules/@earendil-works/pi-coding-agent': {
                    'integrity': pin['integrity'],
                }},
            }))
            info = benchmark.inspect_subagents_runtime(cache)
            self.assertTrue(info['integrity_verified'])
            self.assertEqual(info['version'], '0.87.1')
            env = build_environment(
                cache / 'cell', {}, '/usr/local/bin/pi', info['root'],
            )
            self.assertEqual(env['PI_SUBAGENTS_PI_CODING_AGENT_PACKAGE_ROOT'], info['root'])

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
        self.assertIn('inherits the parent provider and model', subagents)
        self.assertIn('private cell worker settings match the parent thinking level', subagents)
        self.assertIn('sets acceptance to none', subagents)
        self.assertIn('Pass no per-run model or thinking override', subagents)
        self.assertIn('returned sessionFile', subagents)
        self.assertIn('Never read fixture source in the parent', subagents)
        self.assertIn('async=false', subagents)
        self.assertNotIn('bg_wait', subagents)
        self.assertNotIn('output-archives', subagents)
        self.assertIn('before fulfillment', build_prompt('triage', 'stock', Path('/cell/project')))
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

    def test_read_events_ignores_headroom_relay_marker_but_counts_other_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            events_path = Path(tmp) / 'events.jsonl'
            events_path.write_text(
                '{"type":"agent_start"}\n'
                '[mcporter] stderr from headroom\n'
                'not-json\n'
            )
            events, malformed = benchmark.read_events(events_path)
        self.assertEqual(events, [{'type': 'agent_start'}])
        self.assertEqual(malformed, 1)

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
            runtime = prefix / 'node_modules' / '@earendil-works' / 'pi-coding-agent'
            runtime.mkdir(parents=True)
            host_pin = benchmark.SUBAGENT_RUNTIME_PIN
            (runtime / 'package.json').write_text(json.dumps({
                'name': host_pin['name'], 'version': host_pin['version'],
            }))
            (prefix / 'package-lock.json').write_text(json.dumps({
                'packages': {
                    'node_modules/pi-subagents': {
                        'integrity': PINS['subagents']['integrity'],
                    },
                    'node_modules/@earendil-works/pi-coding-agent': {
                        'integrity': host_pin['integrity'],
                    },
                },
            }))
            info = inspect_package('subagents', cache)
            self.assertTrue(info['integrity_verified'])
            self.assertTrue(Path(info['entry']).is_absolute())
            self.assertEqual(info['version'], '0.73.1')
            project_dir = Path('/cell/project')
            deadline = 900
            raw_children = {'children': [
                {'key': 'catalog', 'startedAt': 100, 'endedAt': 300,
                 'result': {'runnerSessionId': 's1', 'usage': {'input': 1, 'output': 2}}},
                {'key': 'billing', 'startedAt': 200, 'endedAt': 400,
                 'result': {'runnerSessionId': 's2', 'usage': {'input': 3, 'output': 4}}},
                {'key': 'shipping', 'startedAt': 400, 'endedAt': 500,
                 'result': {'runnerSessionId': 's3', 'usage': {'input': 5, 'output': 6}}},
            ]}
            encoded = json.dumps(raw_children)
            subagent_prompt = build_prompt('triage', 'subagents', project_dir, deadline)
            subagent_script = json.loads(
                subagent_prompt.partition('workflowScript = ')[2].splitlines()[0]
            )
            self.assertNotIn('\"model\":', subagent_script)
            self.assertEqual(subagent_script.count('\"async\":false'), 3)
            self.assertEqual(subagent_script.count('\"acceptance\":{\"level\":\"none\",\"reason\":\"Read-only benchmark output is checked by the parent grader\"}'), 3)
            subagent_events = [
                {'type': 'tool_execution_start', 'toolCallId': 'enable',
                 'toolName': 'subagents_enable', 'args': {}},
                {'type': 'tool_execution_end', 'toolCallId': 'enable',
                 'toolName': 'subagents_enable', 'result': {'content': []}, 'isError': False},
                {'type': 'tool_execution_start', 'toolCallId': 'agent-list',
                 'toolName': 'subagent', 'args': {'action': 'list', 'capabilities': True}},
                {'type': 'tool_execution_end', 'toolCallId': 'agent-list',
                 'toolName': 'subagent', 'result': {'content': []}, 'isError': False},
                {'type': 'tool_execution_start', 'toolCallId': 'workflow-1',
                 'toolName': 'subagent', 'args': {
                     'workflowScript': subagent_script, 'cwd': str(project_dir),
                     'async': False, 'timeoutMs': deadline * 1000, 'mission': False,
                 }},
                {'type': 'tool_execution_end', 'toolCallId': 'workflow-1',
                 'toolName': 'subagent', 'result': {
                     'content': [{'type': 'text', 'text': encoded + '\nchildren:\n'}],
                     'details': {},
                 }, 'isError': False},
            ]
            workflow_start = next(
                i for i, event in enumerate(subagent_events)
                if event.get('toolCallId') == 'workflow-1' and event.get('type') == 'tool_execution_start'
            )
            self.assertTrue(child_launch_evidence(
                'subagents', subagent_events, 'triage', project_dir, deadline)['verified'])
            source_read = subagent_events + [
                {'type': 'tool_execution_start', 'toolCallId': 'parent-read',
                 'toolName': 'read', 'args': {'path': str(project_dir / 'catalog.py')}},
            ]
            self.assertFalse(child_launch_evidence(
                'subagents', source_read, 'triage', project_dir, deadline)['verified'])
            bad_async_events = list(subagent_events)
            bad_async_events[workflow_start] = {
                **bad_async_events[workflow_start],
                'args': {**bad_async_events[workflow_start]['args'], 'async': True},
            }
            self.assertFalse(child_launch_evidence(
                'subagents', bad_async_events, 'triage', project_dir, deadline)['verified'])
            subagent_children = extract_children(subagent_events, arm='subagents')
            self.assertEqual(subagent_children, [])
            self.assertFalse(overlap_summary(subagent_children)['evidenced'])
            self.assertFalse(child_launch_evidence(
                'subagents', subagent_events[:2], 'triage', project_dir, deadline)['verified'])
            self.assertFalse(child_launch_evidence(
                'subagents', subagent_events + subagent_events[workflow_start:workflow_start + 2], 'triage', project_dir, deadline)['verified'])
            bad_subagent_events = list(subagent_events)
            bad_subagent_script = subagent_script.replace(
                '"extensions":[]', '"extensions":["unrelated"]', 1)
            bad_subagent_events[workflow_start] = {
                **bad_subagent_events[workflow_start],
                'args': {**bad_subagent_events[workflow_start]['args'], 'workflowScript': bad_subagent_script},
            }
            self.assertFalse(child_launch_evidence(
                'subagents', bad_subagent_events, 'triage', project_dir, deadline)['verified'])
            self.assertFalse(child_launch_evidence(
                'subagents', subagent_events, 'patch', project_dir, deadline)['verified'])
            fabric_prompt = build_prompt('patch', 'fabric', project_dir, deadline)
            fabric_script = json.loads(
                fabric_prompt.partition('fabric_exec code = ')[2].splitlines()[0]
            )
            fabric_event = [
                {'type': 'tool_execution_start', 'toolCallId': 'fabric-1',
                 'toolName': 'fabric_exec', 'args': {'code': fabric_script}},
                {'type': 'tool_execution_end', 'toolCallId': 'fabric-1',
                 'toolName': 'fabric_exec',
                 'result': {'content': [{'type': 'text', 'text': encoded + '\nchildren:\n'}], 'details': {}},
                 'isError': False},
            ]
            self.assertTrue(child_launch_evidence(
                'fabric', fabric_event, 'patch', project_dir, deadline)['verified'])
            self.assertFalse(child_launch_evidence(
                'fabric', fabric_event * 2, 'patch', project_dir, deadline)['verified'])
            bad_fabric_script = fabric_script.replace('extensions: false', 'extensions: true', 1)
            bad_fabric_event = list(fabric_event)
            bad_fabric_event[0] = {
                **bad_fabric_event[0], 'args': {'code': bad_fabric_script},
            }
            self.assertFalse(child_launch_evidence(
                'fabric', bad_fabric_event, 'patch', project_dir, deadline)['verified'])
            self.assertFalse(child_launch_evidence(
                'fabric', fabric_event, 'triage', project_dir, deadline)['verified'])
            fabric_children = extract_children(fabric_event, arm='fabric')
            self.assertEqual(len(fabric_children), 3)
            self.assertTrue(overlap_summary(fabric_children)['evidenced'])
            self.assertTrue(all(child['usage']['known'] for child in fabric_children))
            self.assertEqual(extract_children([fabric_event[1]], arm='fabric'), [])
            failed_fabric_event = [fabric_event[0], {**fabric_event[1], 'isError': True}]
            self.assertFalse(child_launch_evidence(
                'fabric', failed_fabric_event, 'patch', project_dir, deadline)['verified'])
            self.assertEqual(extract_children(failed_fabric_event, arm='fabric'), [])

    def test_subagents_extract_output_and_native_intervals_from_returned_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            cell = Path(tmp) / 'cell'
            project_dir = cell / 'project'
            session_dir = cell / 'session'
            session_dir.mkdir(parents=True)
            deadline = 900
            keys = ('catalog', 'billing', 'shipping')
            children, results, session_paths = [], [], []
            for index, key in enumerate(keys):
                started = f'2026-09-29T22:00:{index:02d}.000Z'
                ended = f'2026-09-29T22:00:{index + 8:02d}.000Z'
                session_file = session_dir / f'{key}.jsonl'
                session_file.write_text('\n'.join(json.dumps(event) for event in [
                    {'type': 'session', 'timestamp': started, 'version': 3},
                    {'type': 'model_change', 'timestamp': started,
                     'provider': 'openai', 'modelId': 'gpt-6-sol'},
                    {'type': 'thinking_level_change', 'timestamp': started,
                     'thinkingLevel': 'xhigh'},
                    {'type': 'message', 'timestamp': ended, 'message': {
                        'role': 'assistant', 'stopReason': 'stop',
                        'content': [{'type': 'text', 'text': json.dumps({
                            'module': f'{key}.py', 'finding': 'captured from child session',
                        })}],
                    }},
                ]) + '\n')
                session_paths.append(session_file)
                children.append({'key': key, 'ok': True, 'runId': f'run-{key}', 'agent': 'worker'})
                results.append({
                    'workflowKey': key, 'sessionFile': str(session_file), 'exitCode': 0,
                    'acceptance': {'status': 'none'},
                    'model': 'openai/gpt-6-sol', 'thinking': 'xhigh',
                    'usage': {'input': 10 + index, 'output': 5, 'cacheRead': 0,
                              'cacheWrite': 0, 'cost': 0.001, 'turns': 2},
                })
            workflow_result = {
                'content': [{'type': 'text', 'text': 'Workflow complete.\n\nReturn:\n' +
                            json.dumps({'children': children})}],
                'details': {'workflow': {'value': {'children': children}}, 'results': results},
            }
            prompt = build_prompt('triage', 'subagents', project_dir, deadline)
            script = json.loads(prompt.partition('workflowScript = ')[2].splitlines()[0])
            events = [
                {'type': 'tool_execution_start', 'toolCallId': 'enable',
                 'toolName': 'subagents_enable', 'args': {}},
                {'type': 'tool_execution_end', 'toolCallId': 'enable',
                 'toolName': 'subagents_enable', 'result': {'content': []}, 'isError': False},
                {'type': 'tool_execution_start', 'toolCallId': 'list', 'toolName': 'subagent',
                 'args': {'action': 'list', 'capabilities': True}},
                {'type': 'tool_execution_end', 'toolCallId': 'list', 'toolName': 'subagent',
                 'result': {'content': []}, 'isError': False},
                {'type': 'tool_execution_start', 'toolCallId': 'workflow', 'toolName': 'subagent',
                 'args': {'workflowScript': script, 'cwd': str(project_dir), 'async': False,
                          'timeoutMs': deadline * 1000, 'mission': False}},
                {'type': 'tool_execution_end', 'toolCallId': 'workflow', 'toolName': 'subagent',
                 'result': workflow_result, 'isError': False},
                {'type': 'tool_execution_start', 'toolCallId': 'session-read', 'toolName': 'read',
                 'args': {'path': str(session_paths[0])}},
            ]
            self.assertTrue(child_launch_evidence(
                'subagents', events, 'triage', project_dir, deadline)['verified'])
            records = extract_children(events, 'subagents', session_dir)
            self.assertEqual(len(records), 3)
            self.assertTrue(overlap_summary(records)['evidenced'])
            self.assertTrue(all(record['usage']['known'] for record in records))
            failed_events = list(events)
            failed_events[5] = {**failed_events[5], 'isError': True}
            self.assertEqual(extract_children(failed_events, 'subagents', session_dir), [])
            self.assertTrue(all(record['model'] == 'openai/gpt-6-sol' for record in records))
            self.assertTrue(all(record['thinking'] == 'xhigh' for record in records))
            self.assertEqual(json.loads(records[0]['output'])['module'], 'catalog.py')

            rejected_result = json.loads(json.dumps(workflow_result))
            rejected_result['details']['results'][0]['acceptance'] = {'status': 'rejected'}
            rejected_events = list(events)
            rejected_events[5] = {**rejected_events[5], 'result': rejected_result}
            self.assertEqual(len(extract_children(rejected_events, 'subagents', session_dir)), 2)

            outside = cell / 'outside.jsonl'
            outside.write_text(session_paths[0].read_text())
            bad_result = json.loads(json.dumps(workflow_result))
            bad_result['details']['results'][0]['sessionFile'] = str(outside)
            bad_events = list(events)
            bad_events[5] = {**bad_events[5], 'result': bad_result}
            self.assertEqual(len(extract_children(bad_events, 'subagents', session_dir)), 2)
            self.assertFalse(child_launch_evidence(
                'subagents', bad_events, 'triage', project_dir, deadline)['verified'])

    def test_fabric_comment_wrapped_code_does_not_verify_child_launch(self):
        project_dir = Path('/cell/project')
        deadline = 900
        prompt = build_prompt('patch', 'fabric', project_dir, deadline)
        script = json.loads(prompt.partition('fabric_exec code = ')[2].splitlines()[0])
        events = [
            {'type': 'tool_execution_start', 'toolCallId': 'fabric-comment',
             'toolName': 'fabric_exec', 'args': {'code': f'/* {script} */'}},
            {'type': 'tool_execution_end', 'toolCallId': 'fabric-comment',
             'toolName': 'fabric_exec', 'result': {'content': []}, 'isError': False},
        ]
        self.assertFalse(child_launch_evidence(
            'fabric', events, 'patch', project_dir, deadline)['verified'])

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

    def test_subagents_require_native_session_telemetry(self):
        result = {'children': [{
            'key': 'worker-a', 'startedAt': 100, 'endedAt': 300,
            'result': {'usage': {'input': 10, 'output': 2}},
        }]}
        events = [
            {'type': 'tool_execution_start', 'toolCallId': 'workflow-1',
             'toolName': 'subagent', 'args': {'workflowScript': 'runs.all(children)'}},
            {'type': 'tool_execution_end', 'toolCallId': 'workflow-1',
             'toolName': 'subagent',
             'result': {'content': [{'type': 'text', 'text': json.dumps(result)}]},
             'isError': False},
        ]
        self.assertEqual(extract_children(events, arm='subagents'), [])
        self.assertEqual(extract_children(events, arm='stock'), [])

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

    def test_markdown_report_preserves_metrics_and_unknowns(self):
        def cell(arm, repetition, status, elapsed, usage, overlap=None, failures=None):
            return {
                'case': 'triage', 'arm': arm, 'repetition': repetition,
                'status': status, 'correct': status == 'passed', 'elapsed_ms': elapsed,
                'usage': usage, 'overlap': overlap, 'failures': failures or [],
            }

        known = {
            'known': True, 'input_tokens': 80, 'output_tokens': 20,
            'total_tokens': 100, 'cost_usd': 0.01,
        }
        unknown = {
            'known': False, 'input_tokens': None, 'output_tokens': None,
            'total_tokens': None, 'cost_usd': None,
        }
        result = build_report([
            cell('stock', 1, 'passed', 1000, known),
            cell('stock', 2, 'passed', 900, known),
            cell('subagents', 1, 'passed', 500, known,
                 {'evidenced': True, 'interval_count': 3, 'max_concurrency': 3}),
            cell('subagents', 2, 'failed', 400, unknown,
                 {'evidenced': False, 'interval_count': None, 'max_concurrency': None},
                 ['bad | <script>']),
        ], repetitions=2, cases=['triage'], arms=['stock', 'subagents', 'fabric'])
        markdown = render_markdown(result)

        self.assertIn('4/6 recorded; 3 passed; 1 failed; 2 not run', markdown)
        self.assertIn('1/2 | 1 | 0 | 50.0% | 500.0 ms', markdown)
        self.assertIn('2.00× (n=1)', markdown)
        self.assertIn('Unknown (1/2 cells known)', markdown)
        self.assertIn('1/2 verified; peak 3; 1 unknown', markdown)
        self.assertIn('| 2 | subagents | failed | no | 400.0 ms | Unknown |', markdown)
        self.assertIn(r'bad \| &lt;script&gt;', markdown)
        self.assertNotIn('<script>', markdown)
        self.assertIn('| fabric | 0/2 | 0 | 2 |', markdown)

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
            markdown_path = Path(report['markdown_report'])
            self.assertEqual(markdown_path, Path(report['run_dir']) / 'report.md')
            self.assertIn('100.0 ms', markdown_path.read_text())

            markdown_path.unlink()
            regenerated_output = io.StringIO()
            with contextlib.redirect_stdout(regenerated_output):
                regenerate_status = main([
                    '--cache-root', str(cache_root), 'report', str(Path(report['run_dir'])),
                ])
            self.assertEqual(regenerate_status, 0)
            regenerated = json.loads(regenerated_output.getvalue())
            self.assertTrue(markdown_path.exists())
            self.assertEqual(regenerated['markdown_report'], str(markdown_path))
            self.assertIn('100.0 ms', markdown_path.read_text())

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
