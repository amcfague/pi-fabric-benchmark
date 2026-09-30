# Pi parallel-work benchmark

Compare stock Pi with `pi-subagents`, `@tintinweb/pi-subagents`, and `pi-fabric` on the same offline fixture. Each trial gets a fresh copy of the project and an independent grader. The report includes correctness, elapsed time, input/output/cache token counts, turns, cost, child-launch evidence, and failures. A zero exit code alone does not count as a pass.

## Workloads and workflows

The fixture has three Python modules: `catalog.py`, `billing.py`, and `shipping.py`. Each arm gets a fresh project copy and the same task facts. The benchmark controls child launch order in the default scripted mode; `--orchestration model-directed` separately measures which calls a parent model chooses.

### Workloads

| Case | What it tests |
|---|---|
| triage | Find issues in three modules and combine the findings. |
| patch | Fix one balanced defect per module and pass an integrated behavior check. |
| control | Handle one read-only billing task; measure the cost of delegating a single task. |
| debug | Use three failing contract tests to find and fix root causes. |
| integration | Fix three modules with one shipping child owning both shipping functions; verify the combined change. |
| integration-contention | Opt-in stress case: two children edit different functions in `shipping.py`; require edit evidence and final correctness. |

### Workflows

| Arm | What it tests |
|---|---|
| `stock` | Pi core tools without extensions, as a baseline. Normal concurrent core-tool calls remain allowed. |
| `subagents` | A foreground `pi-subagents` workflow that launches child sessions. |
| `tintin-subagents` | Launches background `Agent` children, then collects them with `get_subagent_result`. |
| `fabric` | A `fabric_exec` script that launches parallel child runs. |

Plugin arms use one child for control, three for triage/patch/debug/main integration, and four only for the separate contention case. Stock works directly with its core tools in both comparisons.

## Setup

Requires Python 3, npm, and `/usr/local/bin/pi`. The Pi wrapper handles model authentication; no `OPENAI_API_KEY` is needed. The pinned npm packages are prepared outside the timed trials:

- `pi-subagents@0.73.1`: `sha512-IklOqw67DtvIWQFKxFJpDFXsOGcq8RGYZEbokykD5Kvfs9aa/du1cW2NEJ5xx4GUgN2LVt3vAq5IdzEMo7YwJw==`
- `@tintinweb/pi-subagents@0.19.0`: `sha512-DZsU33Urfb9dhEsJmsmpx0dayIHMYUbxng6AA7B6+bIgspNcTckcSnQRsbrv+QCS7jVKYJTzHIT/j1SU2B/nVQ==`
- `@earendil-works/pi-coding-agent@0.87.1` (host runtime for both subagents packages): `sha512-m8ArJUtVcQMSe1lLE/Ei7vX/JV7O39sWmWBsXV2NOU70F0qCp8GubA24pT3LnwTmM6LL2xV80/h6sQg85n69ew==`
- `pi-fabric@0.100.0`: `sha512-tW1DBe8cZYN91yezHHud5R3nLCV8LU2/1rS69f9u0LnsuuDXBVpxbEa06o2sbiJuANMxIJ1PGf+2wGXdGWqAmg==`

```sh
python3 benchmark.py install      # one-time download; npm lifecycle scripts are disabled
python3 benchmark.py preflight --arm subagents --arm tintin-subagents  # required offline native proof
python3 benchmark.py preflight    # all pinned arms; currently reports Fabric blocked
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

`preflight` does not install packages or call a paid model. It checks the Pi wrapper, package pins and tree hashes, then dispatches one local-provider child through each selected supported extension. The native join must show the child ID, settings, session, output and synthetic usage; probe tokens are not included in benchmark cells. A single-arm run checks only its package. Authentication stays with the Pi wrapper and is exercised by the first live cell.

The pinned `pi-fabric@0.100.0` cannot pass this offline proof: the synthetic child settles but misses Fabric's five-second exit grace. Earlier paid main cases passed; this block does not claim paid Fabric work is impossible. The full four-arm scripted matrix stops before paid cells. Stock, `subagents` and `tintin-subagents` remain available as single-arm runs; a model-directed run is not a substitute for a blocked scripted arm.

On macOS, the default cache is `~/Library/Caches/pi-parallel-benchmark`; elsewhere it follows `XDG_CACHE_HOME` or `~/.cache`. Override it by placing `--cache-root /path` before the subcommand.

## Run

```sh
python3 benchmark.py smoke
python3 benchmark.py run --case triage --arm stock
python3 benchmark.py run --case triage --arm tintin-subagents --orchestration model-directed
python3 benchmark.py run --case integration-contention --arm tintin-subagents
python3 benchmark.py matrix --arms stock subagents tintin-subagents --repetitions 3 --deadline 900
```

`run`, `smoke`, and `matrix` default to scripted execution. `smoke` attempts the four triage arms once and currently blocks on Fabric. A scripted matrix uses its first repetition across all five main cases and selected arms as a gate; it stops on the first failed cell before paying for later repetitions. A model-directed matrix records ordering failures and continues. The selected three-arm matrix plans 45 cells and lists Fabric as excluded and capability-blocked, never as a failed trial. The default four-arm matrix plans 60 cells but currently stops before paid cells. A separate smoke run costs four additional trials when its arms pass preflight.

Each cell targets `openai/gpt-6-sol` at `xhigh` with a 900-second deadline. Stock and plugin children get `read,grep,find,ls,bash,edit,write`. Scripted plugin cells load their pinned extension plus the local `scripted-provider.ts` driver. The driver emits fixed tool calls through Pi's normal RPC tool path without a model choosing their order: one foreground `runs.all` workflow, all Tintin background launches before result collection, or one Fabric `Promise.all` program. A separate tool-free Pi session on the target model synthesizes the child results. The measured time includes dispatch, children, and synthesis; fixture setup and external grading stay outside it. Model-directed mode keeps the original parent-selected tool calls as a separate measurement.

Every Pi process uses `/usr/local/bin/pi`, a private `PI_CODING_AGENT_DIR`, and a filtered environment. The wrapper is first on `PATH` and supplies both child binary routes. Skills, prompt templates, themes, context files, and approvals are disabled. `pi-subagents` uses its pinned host runtime; scripted cells pin the worker model and thinking in private settings because the zero-token dispatch provider is not the child model. Tintin children receive explicit model/thinking settings and report pooled usage; Fabric returns package-owned child timestamps and usage. Child extension/skill isolation remains in force. Multi-child cells require native overlap and known usage; the one-child control does not require overlap. Provider-side prompt-cache state is not isolated across cells, so later cells may see a warm cache.

## Reports and artifacts

Each run prints JSON with its run ID, run directory, and `markdown_report` path. Runs are stored under `<cache-root>/runs/<timestamp>-<id>/` with `run.json`, `preflight.json`, `report.json`, and `report.md`. Rebuild new reports with:

```sh
python3 benchmark.py report /path/to/run-directory
```

Every attempted cell writes `cell.json` plus raw events, stderr, arguments, prompt, session, project copy, and private agent directory. Scripted cells also retain their fixed call plan, driver events, and separate synthesis events. Debug cells retain initial failing-test output. The cell records child evidence, model settings, usage, diagnostics, elapsed time, grading, and failures. Cache and run directories are owner-only; treat transcripts and project copies as private.

Reports keep scripted, model-directed, stress, and historical case versions separate. Rebuilding a historical mode-less report prints a newly labeled summary without rewriting its saved files. `report.md` lists attempted and missing cells, capability blocks, pass rates, passing-only median times, paired sample counts, tokens/cache, turns, cost completeness, and native overlap. Stress has no paired stock speedup and needs native evidence of both same-file edit attempts; missing evidence stays unknown. Failed children, deadlines, missing usage, and unrecognized non-JSON events remain failures. Exact known relay diagnostics are counted rather than rejected. No failed cell is retried automatically.

The selected matrix may spend on 45 parent trials and up to three children per plugin trial. A four-arm default matrix would spend on 60 after Fabric passes preflight. The separate same-file stress case uses four children per plugin arm.