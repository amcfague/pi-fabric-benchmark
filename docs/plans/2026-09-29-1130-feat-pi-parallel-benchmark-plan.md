---
title: Pi parallel-work benchmark - Plan
type: feat
date: 2026-09-29
artifact_contract: ce-unified-plan/v1
product_contract_source: ce-plan-bootstrap
execution: code
---

# Pi parallel-work benchmark - Plan

## Goal Capsule

**Objective:** Operators can compare how stock Pi, `npm:pi-subagents`, and `npm:pi-fabric` complete the same work, with independently checked results and enough evidence to explain differences in time and cost.

**Means:** A small offline fixture and a standard-library runner launch isolated, pinned Pi sessions and retain their results (KTD1, KTD2, KTD4).

**Stop conditions:** Do not count a cell as valid when its loaded extensions, model route, final result, or verifier outcome cannot be established. Do not infer a speedup before collecting paired runs.

---

## Product Contract

### Summary

Build a repeatable three-arm benchmark for parallelizable Pi work. Stock Pi is the baseline; the other arms use only their named npm extension.

### Problem Frame

A parallel-work comparison is misleading if existing extensions load silently, children use a different model route, or a quick but wrong result counts as success. This repository has no benchmark implementation yet. A sibling `pi-fabric` benchmark demonstrates isolated cells and independent verification, but this benchmark must run on its own.

### Requirements

**Isolation and connectivity**

- R1. Stock Pi must have no extensions in its parent process or descendants, with no exception list.
- R2. Each plugin arm must explicitly load only its pinned `npm:pi-subagents` or `npm:pi-fabric` package. Package-owned child runtime modules belong to that package; any unrelated extension makes the cell invalid.
- R3. Parent and child Pi processes must use `/usr/local/bin/pi`. A deliberate `/usr/local/bin/pi_cli/pi.real` route is valid only when both `http_proxy` and `https_proxy` are set appropriately.

**Comparable work**

- R4. Every arm must receive the same task facts, fresh fixture snapshot, provider/model, thinking level, deadline, child cap, and permitted core tools. Stock Pi may use its normal core-tool concurrency; it must not be artificially serialized.
- R5. Cover read-only independent investigations with a combined answer and disjoint-file code fixes with an integration check. Grade both with checks outside the writable trial directory.

**Results**

- R6. Keep each cell's raw events, final outcome, verification result, elapsed time, usage, failures, and evidence of child overlap. Compare correct paired runs against the stock Pi baseline, and show failures in the denominator.

### Scope Boundaries

This work builds a local benchmark, not a general agent orchestration service. It does not modify Pi, install monitoring extensions, require a web grading service, assert that a plugin is faster, or run a paid matrix as part of implementation planning.

---

## Planning Contract

### Key Technical Decisions

- KTD1. **Use one Python standard-library runner.** Keep fixture setup, process control, JSONL capture, and reporting in `benchmark.py`; avoid a second framework or new dependencies. Follow the sibling benchmark's isolated-cell pattern without a runtime dependency on that checkout.
- KTD2. **Preinstall exact published npm package versions outside timed cells.** Load each package by its installed absolute package path. Give each cell a fresh `PI_CODING_AGENT_DIR` with only needed credentials and minimal settings. Use `--no-extensions` for every arm. The baseline has no `-e`; each plugin arm has exactly one explicit `-e` for its package. Pi still loads explicitly selected extensions when discovery is disabled. Also pass `--no-skills --no-prompt-templates --no-themes --no-context-files --no-approve`. Set `PI_SUBAGENT_PI_BINARY` and `PI_FABRIC_PI_BINARY` to `/usr/local/bin/pi`. Reject an unverified package version or loaded-extension set.
- KTD3. **Keep the work identical; change coordination only.** Use one case body with short arm-specific instructions. Stock Pi works directly. The subagents arm waits for one bounded `runs.all` workflow and sets `extensions: []` for every child, disabling ambient extensions while allowing the package-owned child runtime. The Fabric arm waits for concurrent `agents.run()` Pi children with `extensions: false` on leaves. A dispatch receipt is not a completed result.
- KTD4. **Grade independently and report missing evidence honestly.** Use deterministic verifiers. Measure subprocess wall time with a monotonic clock, retain JSONL and child artifacts, and establish overlap from timestamped child lifecycle records. Do not double-count child usage also represented in parent tool results; report missing usage or overlap as unknown, not zero.

### High-Level Technical Design

A cell takes a case and arm, copies the fixture, prepares isolated credentials, launches Pi, consumes JSONL through settlement, gathers child artifacts, runs the external verifier, and writes one result. The matrix runner rotates arm order across paired repetitions and reports medians only for correctly completed cells while retaining every failed cell. Package installation and authentication checks happen before timing; Pi startup remains in the measured interval.

The executable may have package-owned child helpers. Verify parent and child extension provenance through startup declarations and plugin launch records before interpreting the trial. If the installed version does not expose enough evidence, the result remains invalid until the isolation check can be made reliable without adding another extension.

### Risks and prerequisites

- The repository has no `origin`, so there is no `origin/main` to fetch or rebase onto. Use a dedicated worktree for implementation once the initial commit exists; do not invent a remote.
- OAuth refresh and model service contention can distort timing. Keep cells serial, rotate arm order, and record model/provider errors separately from verifier failures.
- Parent and child usage formats differ by plugin version. Report a missing aggregate rather than estimating an attractive but incorrect cost.
- Subagents may run in-process and Fabric children may spawn Pi processes. Process counts alone do not prove overlap; require child lifecycle evidence.

---

## Implementation Units

### U1. Build parallelizable cases and independent grading

**Goal:** Supply realistic bounded work with objective outcomes.

**Requirements:** R4, R5.

**Dependencies:** None.

**Files:** `fixtures/project/catalog.py`, `fixtures/project/billing.py`, `fixtures/project/shipping.py`, `cases/triage.md`, `cases/patch.md`, `checks/triage.json`, `checks/patch.py`, `tests/test_benchmark.py`.

**Approach:** Seed three independent module defects in a small offline Python project. The read-only case asks for one finding per module and a cross-module conclusion; compare its structured answer with fixed facts. The patch case requires three fixes and an integration check. Copy only the fixture into each writable trial directory; keep the expected answer and patch verifier outside it.

**Test scenarios:**
- A correct complete triage answer passes, while an answer missing one module finding or the combined conclusion fails.
- Correcting two of the three defects fails the patch check; correcting all three and the integration case passes.
- A trial cannot change its grade by editing the fixture or attempting to edit a verifier outside its writable copy.

**Verification:** Both graders distinguish complete from partial work before any paid run.

### U2. Launch each arm with closed extension and network routes

**Goal:** Make trial configuration reproducible and reject contamination.

**Requirements:** R1, R2, R3, R4.

**Dependencies:** U1.

**Files:** `benchmark.py`, `tests/test_benchmark.py`.

**Approach:** Require a provider/model and repetition count, preflight pinned packages and credentials, then run cells serially against fresh fixture copies. Use the wrapper for every Pi process, fresh config and session directories, identical core-tool permissions, one explicit plugin package at most, and bounded child fanout. Allow normal stock Pi core-tool behavior. Kill the process group at the deadline and retain failure artifacts.

**Execution note:** Prove argv and environment isolation with a fake Pi executable before the first live smoke run.

**Test scenarios:**
- The fake executable sees zero `-e` arguments for stock Pi and exactly the chosen npm package for either plugin; child launch records disable ambient extensions and skills.
- Child-binary environment variables point to the wrapper; a direct `pi.real` selection without both proxy variables is refused.
- A missing package, unavailable model credential, extra extension, or deadline produces a failed cell, not a substituted configuration or an automatic retry.

**Verification:** Recorded parent and child loadouts match R1-R3, with no unrelated extension in any cell.

### U3. Record, grade, and compare cells

**Goal:** Produce a comparison someone can audit without trusting an agent's summary.

**Requirements:** R5, R6.

**Dependencies:** U1, U2.

**Files:** `benchmark.py`, `tests/test_benchmark.py`, `README.md`.

**Approach:** Stream JSONL without blocking Pi; retain stderr, version pins, seed hash, session and child records, grader output, elapsed time, and termination reason per cell. Distinguish `agent_settled` from `agent_end`, and check assistant stop reasons because JSON mode can exit zero after a model error. Aggregate non-overlapping usage once. Report correctness, median time against the baseline, token and cost breakdown when complete, and evidenced child overlap. Document preflight and repeatable local commands in the README.

**Test scenarios:**
- An exit-zero stream with an error stop reason fails; a settled, verified result passes.
- A duplicate child-usage record is counted once; missing usage remains unknown.
- Child launches without timestamped overlapping intervals cannot be reported as parallel execution.
- One failed repetition stays in the comparison denominator rather than disappearing from a median-only report.

**Verification:** A reader can match every reported number to a stored cell and its verifier result.

---

## Verification Contract

Run `python3 -m unittest discover -s tests` for the fake-executable launcher checks and deterministic case graders. Then preflight the selected model, exact npm versions, extension loadouts, and wrapper connectivity before paid work. Run one live cell per arm and require a settled result, a passing external verifier, the expected extension set, and recorded overlap for the plugin arms. Finally run at least three paired repetitions of each case in rotating arm order. Report all cells, including timeout, model, verifier, and evidence failures; compare elapsed time only among correctly completed cells.

---

## Definition of Done

The baseline launches with no extensions, each plugin arm loads only its named package, and every child follows the same network and isolation rules. Both cases have independently testable outcomes. A repeatable matrix produces raw artifacts plus a report that separates correctness, time, usage, cost, failures, and proven overlap. Unit tests and the three-arm smoke pass. Remove abandoned fixture or runner experiments before declaring the implementation complete.
