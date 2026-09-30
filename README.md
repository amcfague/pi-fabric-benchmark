# Pi parallel-work benchmark

Compare stock Pi with `pi-subagents`, `@tintinweb/pi-subagents`, and `pi-fabric` on the same offline fixture. Each trial gets a fresh copy of the project and an independent grader. The report includes correctness, elapsed time, input/output/cache token counts, turns, cost, child-launch evidence, and failures. A zero exit code alone does not count as a pass.

## Workloads and workflows

The fixture has three Python modules: `catalog.py`, `billing.py`, and `shipping.py`. Each arm runs every case on a fresh project copy. The task stays the same; only the delegation instructions change.

### Workloads

| Case | What it tests |
|---|---|
| triage | Find issues in three modules and combine the findings. |
| patch | Fix one balanced defect per module and pass an integrated behavior check. |
| control | Handle one read-only billing task; measure the cost of delegating a single task. |
| debug | Use three failing contract tests to find and fix root causes. |
| integration | Reconcile uneven child edits to the same file and verify the combined change. |

### Workflows

| Arm | What it tests |
|---|---|
| `stock` | Pi core tools without extensions, as a baseline. Normal concurrent core-tool calls remain allowed. |
| `subagents` | A foreground `pi-subagents` workflow that launches child sessions. |
| `tintin-subagents` | Batched foreground `Agent` calls from `@tintinweb/pi-subagents`. |
| `fabric` | A `fabric_exec` script that launches parallel child runs. |

Plugin arms use one child for control, three for triage/patch/debug, and four for integration.

## Setup

Requires Python 3, npm, and `/usr/local/bin/pi`. The Pi wrapper handles model authentication; no `OPENAI_API_KEY` is needed. The pinned npm packages are prepared outside the timed trials:

- `pi-subagents@0.73.1`: `sha512-IklOqw67DtvIWQFKxFJpDFXsOGcq8RGYZEbokykD5Kvfs9aa/du1cW2NEJ5xx4GUgN2LVt3vAq5IdzEMo7YwJw==`
- `@tintinweb/pi-subagents@0.19.0`: `sha512-DZsU33Urfb9dhEsJmsmpx0dayIHMYUbxng6AA7B6+bIgspNcTckcSnQRsbrv+QCS7jVKYJTzHIT/j1SU2B/nVQ==`
- `@earendil-works/pi-coding-agent@0.87.1` (host runtime for both subagents packages): `sha512-m8ArJUtVcQMSe1lLE/Ei7vX/JV7O39sWmWBsXV2NOU70F0qCp8GubA24pT3LnwTmM6LL2xV80/h6sQg85n69ew==`
- `pi-fabric@0.100.0`: `sha512-tW1DBe8cZYN91yezHHud5R3nLCV8LU2/1rS69f9u0LnsuuDXBVpxbEa06o2sbiJuANMxIJ1PGf+2wGXdGWqAmg==`

```sh
python3 benchmark.py install      # one-time download; npm lifecycle scripts are disabled
python3 benchmark.py preflight    # checks the Pi wrapper, npm integrity, lockfiles and package hashes
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

`preflight` does not install packages or start agents. It checks the Pi wrapper, the registry's pinned tarball integrity, and the installed package tree against the saved manifest. Authentication is delegated to the Pi wrapper and is exercised by the first live cell, with no separate billable probe.

On macOS, the default cache is `~/Library/Caches/pi-parallel-benchmark`; elsewhere it follows `XDG_CACHE_HOME` or `~/.cache`. Override it by placing `--cache-root /path` before the subcommand.

## Run

```sh
python3 benchmark.py smoke
python3 benchmark.py run --case triage --arm stock
python3 benchmark.py matrix --repetitions 3 --deadline 900
```

`smoke` runs the four triage arms once. A matrix uses its first four cells as a smoke gate: triage repetition 1 runs in stock, subagents, tintin-subagents, fabric order. If any fail, it saves the report and stops. Otherwise it runs the remaining cells, rotating arm order across cases and repetitions. The default five cases and three repetitions produce 60 planned cells; matrix repetitions must be at least three. Running `smoke` separately before a matrix costs four additional trials.

Each cell uses provider `openai`, model `gpt-6-sol`, thinking `xhigh`, and a 900-second process deadline by default. Stock receives the core tools `read,grep,find,ls,bash,edit,write`; plugin children receive that set with the same project and deadline. Subagents inherit the parent provider/model; private cell settings match the worker's effective thinking level to the parent's `xhigh`. The subagents parent allowlists `subagents_enable` and `subagent`; the Fabric parent allowlists `fabric_exec`. `--deadline` changes the per-cell deadline in seconds.

Every parent launches through `/usr/local/bin/pi`. Each cell gets a fresh private `PI_CODING_AGENT_DIR` and a filtered environment; the Pi wrapper is first on `PATH` and is set as both `PI_SUBAGENT_PI_BINARY` and `PI_FABRIC_PI_BINARY`. All parents disable skills, prompt templates, themes, context files, and approvals. Stock also passes `--no-extensions`; each plugin parent loads its pinned package with `-e` from the private cache, while the fixture stays extension-free. The subagents installer pins the wrapper-matched `@earendil-works/pi-coding-agent@0.87.1` host runtime and sets `PI_SUBAGENTS_PI_CODING_AGENT_PACKAGE_ROOT` so foreground child sessions use the parent provider registry. `pi-subagents` children request `extensions: []` and `skills: []`; Tintin children use foreground `Agent` calls with `isolated: true` and fresh context, and cell-local `subagents.json` enables its otherwise-off usage reporting; Fabric children request `extensions: false`. The subagents arm runs one foreground workflow with foreground children and no per-run model override; it reads each child answer and actual start/final-response timestamps from a returned `sessionFile` confined to that cell's session directory, and usage from successful child result records. For Fabric, it verifies the exact parallel `fabric_exec` script and reads child telemetry only from that successful tool result. All plugin arms require known token usage. Timestamped child overlap is required for multi-child cases; the one-child control is exempt. Direct use of `pi.real` is rejected unless both lowercase proxy variables are present. Provider-side prompt-cache state is not controlled or isolated per cell: cache-read/write usage is reported when available, and later cells may observe a warm cache even though sessions and project copies are fresh.

## Reports and artifacts

Each run prints JSON with its run ID, run directory, and `markdown_report` path. Runs are stored under `<cache-root>/runs/<timestamp>-<id>/` with `run.json`, `preflight.json`, `report.json`, and `report.md`. Rebuild both reports with:

```sh
python3 benchmark.py report /path/to/run-directory
```

Every attempted cell writes `cell.json`. A launched cell also retains `events.jsonl`, `stderr.log`, `argv.json`, `prompt.txt`, the Pi session, a writable project copy, and the private agent directory. The full `cell.json` includes the answer, grader result, fixture hash, selected package version/integrity/tree hash, child launch evidence and records, usage, elapsed time, termination reason, and failures. Cache and run directories are created with owner-only permissions. Treat artifacts as private: they contain agent transcripts and project copies.

`report.md` summarizes each case and arm with passed/planned counts, failures, not-run cells, pass rate, median elapsed time, paired speedup and sample size, input/output/cache-token totals, total parent-plus-child turns, cost completeness, and child-overlap evidence. It also lists attempted and missing cells. Medians use passed cells only; paired speedups compare stock and an arm only where both passed in the same repetition. Incomplete usage or overlap evidence is labeled unknown, never zero. `report.json` retains the structured data. A cell that fails grading, settling, usage, package/loadout verification, or required child-overlap evidence is not a successful trial. Ctrl-C during Pi execution terminates the process group, saves the interrupted cell and both reports, and stops the remaining schedule.

The live matrix may use substantial model budget: up to 60 parent trials plus four children for each plugin trial (the integration case; other plugin cases use one or three). No failed cell is retried automatically.