# Pi parallel-work benchmark

Compare stock Pi with `pi-subagents` and `pi-fabric` on the same offline fixture. Each trial gets a fresh copy of the project and an independent grader. The report includes correctness, elapsed time, known token/cost data, child-launch evidence, and failures. A zero exit code alone does not count as a pass.

## Workloads and arms

The fixture has three independent Python modules: `catalog.py`, `billing.py`, and `shipping.py`.

- **triage** asks for one structured finding per module and a cross-module conclusion. `checks/triage.py` grades the answer against `checks/triage.json`.
- **patch** seeds one defect per module. The agent edits only its copied project; `checks/patch.py` checks each fix and the integrated behavior outside that copy.

The arms are `stock`, `subagents`, and `fabric`. Stock Pi may use its normal core-tool behavior, including concurrent tool calls; it is not deliberately slowed down. Plugin prompts request delegation of exactly three independent module tasks.

## Setup

Requires Python 3, npm, `/usr/local/bin/pi`, and an `OPENAI_API_KEY` in the environment. The pinned npm packages are prepared outside the timed trials:

- `pi-subagents@0.73.1`: `sha512-IklOqw67DtvIWQFKxFJpDFXsOGcq8RGYZEbokykD5Kvfs9aa/du1cW2NEJ5xx4GUgN2LVt3vAq5IdzEMo7YwJw==`
- `pi-fabric@0.100.0`: `sha512-tW1DBe8cZYN91yezHHud5R3nLCV8LU2/1rS69f9u0LnsuuDXBVpxbEa06o2sbiJuANMxIJ1PGf+2wGXdGWqAmg==`

```sh
python3 benchmark.py install      # one-time download; npm lifecycle scripts are disabled
python3 benchmark.py preflight    # checks Pi wrapper, credentials, npm integrity, lockfiles and package hashes
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

`preflight` does not install packages or start agents. It checks the registry's pinned tarball integrity and compares the installed package tree to the saved manifest. The API key is checked for presence; its value is not saved in the run artifacts.

On macOS, the default cache is `~/Library/Caches/pi-parallel-benchmark`; elsewhere it follows `XDG_CACHE_HOME` or `~/.cache`. Override it by placing `--cache-root /path` before the subcommand.

## Run

```sh
python3 benchmark.py smoke
python3 benchmark.py run --case triage --arm stock
python3 benchmark.py matrix --repetitions 3 --deadline 900
```

`smoke` runs the three triage arms once. A matrix uses its first three cells as a smoke gate: triage repetition 1 runs in stock, subagents, fabric order. If any fail, it saves the report and stops. Otherwise it runs the remaining cells, rotating arm order across cases and repetitions. The default two cases and three repetitions produce 18 planned cells; matrix repetitions must be at least three. Running `smoke` separately before a matrix costs three additional trials.

Each cell uses provider `openai`, model `gpt-6-sol`, thinking `xhigh`, the core tools `read,grep,find,ls,bash,edit,write`, and a 900-second process deadline by default. Plugin children receive the same project, tools, model, thinking level, and deadline. `--deadline` changes the per-cell deadline in seconds.

Every parent launches through `/usr/local/bin/pi`. Each cell gets a fresh private `PI_CODING_AGENT_DIR` and a filtered environment; the Pi wrapper is first on `PATH` and is set as both `PI_SUBAGENT_PI_BINARY` and `PI_FABRIC_PI_BINARY`. All parents disable skills, prompt templates, themes, context files, and approvals. Stock also passes `--no-extensions`; each plugin arm instead loads exactly one pinned package with `-e`, from its fresh agent directory and extension-free fixture. `pi-subagents` children request `extensions: []` and `skills: []`; Fabric children request `extensions: false`. For `pi-subagents`, the runner verifies the requested child task and loadout and requires three timestamped, overlapping child intervals. The runner has no independent source for Fabric child telemetry, so it ignores child-like data returned by the parent. Fabric cells fail closed, with child overlap and usage unknown, until runner-generated telemetry is available. Direct use of `pi.real` is rejected unless both lowercase proxy variables are present.

## Reports and artifacts

The command prints JSON containing the run ID and run directory. Runs are stored under `<cache-root>/runs/<timestamp>-<id>/` with `run.json`, `preflight.json`, and `report.json`. Rebuild a report with:

```sh
python3 benchmark.py report /path/to/run-directory
```

Every attempted cell writes `cell.json`. A launched cell also retains `events.jsonl`, `stderr.log`, `argv.json`, `prompt.txt`, the Pi session, a writable project copy, and the private agent directory. The full `cell.json` includes the answer, grader result, fixture hash, selected package version/integrity/tree hash, child launch evidence and records, usage, elapsed time, termination reason, and failures. Cache and run directories are created with owner-only permissions. Treat artifacts as private: they contain agent transcripts and project copies.

The report keeps the planned denominator, failed cells, and not-run cells separate. Per-arm medians use only passed cells; paired speedups use only repetitions where both the arm and stock baseline passed. Token and cost totals remain unknown unless every planned cell has usage. Missing evidence is not reported as zero. A cell that fails grading, settling, usage, package/loadout verification, or required child-overlap evidence is not a successful trial. Ctrl-C during Pi execution terminates the process group, saves the interrupted cell and report, and stops the remaining schedule.

The live matrix may use substantial model budget: up to 18 parent trials plus three children for each plugin trial. No failed cell is retried automatically.