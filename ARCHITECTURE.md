# Architecture

The Workshop deliberately separates orchestration from inference. No agent framework or hosted service is required.

## Components

- `cabin/store.py`: SQLite persistence, schema migrations, restart recovery, project lookup and event history. Connections are short-lived, transactions commit on success, and WAL permits concurrent readers.
- `cabin/engine.py`: Ollama transport, role definitions, structured action protocol, sequential worker, file tools, checks, project locks, and Git checkpoints.
- `cabin/server.py`: authenticated HTTP API, session/CSRF handling, validation, project operations, asynchronous planning/chat, repository browsing, and downloads.
- `cabin/evaluation.py`: versioned benchmark fixtures, isolated evaluation workspaces, external acceptance tests, fixed run settings, and leaderboard aggregation.
- `cabin/company.py`: team-message contracts and dependency graph validation.
- `cabin/workspaces.py`: Git task worktrees, accepted integration, reopen/archive, and relocation repair.
- `cabin/metrics.py`: optional RAM/GPU sampling labelled with host/device scope.
- `cabin/operations.py`: consistent backups, safe source-ZIP imports, and host inspection.
- `cabin/visual.py`: optional rendering through an installed Chromium browser.
- `cabin/browser.py`: optional Playwright driver on a dedicated thread with isolated task contexts, semantic controls, actual assertions and workspace-only static origins.
- `cabin/scheduler.py`: foreground reservations and cooperative safe-boundary yielding.
- `cabin/history.py`: complete append-only evidence with large hash-verified payload storage.
- `cabin/auditor.py`: idle deterministic analysis and optional read-only model findings.
- `cabin/editing.py`: unique-match patches with optional stale-file checks.
- `cabin/static/`: dependency-free web interface. API data is rendered as text rather than executable HTML.
- `run.py`: process lifecycle and command-line configuration.

## Agent contract

Agents receive a role instruction, project brief, task acceptance criteria, previous-attempt evidence and a file inventory. Every model response must match an action schema. The harness validates and executes the action, then returns the observation as data. The model never directly changes queue state or marks itself verified. The harness runs configured checks before recording a successful checkpoint.

The structured protocol is independent of the web UI. A future provider adapter can implement the `generate(model, messages, schema, context)` contract without replacing project management, file tools, or Git history. Role instructions are centralised in `ROLES`.

## Concurrency and state

One worker services projects at task-stage boundaries. Runnable tasks satisfy explicit dependencies and are ordered by priority. Autonomous stages are implementation/debugger, QA, reviewer, and integration. The manager triages failures within retry budgets. Each task has an isolated worktree; accepted main files advance only through controlled fast-forward integration. Reopening integrated work archives the previous candidate and starts from current main.

A foreground-aware inference gate serialises model calls. Assistant turns reserve priority across their tools and model requests; background execution yields at safe boundaries. Per-project locks protect mutations and snapshots; interactive mutation APIs use nonblocking locks to avoid deadlocking a yielded worker. Only one server instance owns a data directory. In-flight model requests are not forcibly preempted.

SQLite owns workflow state, group messages, memory, sessions, dependencies, and run evidence; Git owns files. They are not a distributed transaction. Recovery preserves task stages and validates candidates before continuing. Projects pause after restart unless automatic resume was explicitly enabled. A crash between commit and database update can still leave a recoverable commit without a completed run record.

The company room is a structured event/message log. Targeted requests receive bounded read-only consultations; broadcasts provide context. This is not an unbounded multi-agent conversation. The assistant has its own bounded tool protocol for creating/managing projects through validated application methods. Human candidate acceptance and executable-command configuration are not delegated to conversational text.

## Verification boundary

`verified` means configured process checks passed for the recorded revision. `review` means no automated verification was available. Neither means an independent expert approved the work. The same model in separate roles can share systematic errors. Reliable acceptance checks and human inspection remain valuable.

The file tools confine paths to one workspace, but process checks execute ordinary code under the server account. This application does not claim OS-level isolation. Infrastructure that needs hostile-code execution should put the worker and checks inside an explicit VM/container boundary, retaining this API and persistence layer.

## Extension points

Add providers at the model boundary, benchmarks in the versioned suite, and tools through validated actions. Git integration intentionally rejects stale main revisions rather than resolving conflicts automatically. Parallel workers, live backend-service management, comprehensive benchmarking and OS process sandboxing remain separate extensions. The Operations Analyst recommends changes through evidence-linked reports; it does not mutate the system it audits.
