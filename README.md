# The Workshop — your local AI development studio

The Workshop is a local web application for managing AI coding projects. One Ollama model can take turns as your assistant, manager, programmer, designer, debugger, QA specialist, and documentation writer. Your phone connects to the control panel over the same Wi-Fi. Internet access is not required after installing Python, Git, Ollama, and your chosen models.

This repository contains the actual server, persistent worker, and interface. There is no cloud service, CDN, JavaScript build step, or Python package installation.

Previously named Cabin. Existing data directories, project repositories, and saved settings remain compatible; the internal `cabin` Python package and `cabin.db` filename are retained so upgrading does not require migrating your data.

## Requirements

- Python **3.11 or newer**, including its standard library.
- Git available on PATH.
- Ollama running locally with at least one model already downloaded.
- The build tools and dependencies required by the projects you want the agents to work on.

On the GTX 1080 Ti machine, start with one quantised coding model, one worker, and a 4K–8K context. Each role has a separate conversation, while the model weights stay loaded in Ollama. Model requests are serialised. The interface remains responsive during inference; chat waits for the current model request to finish.

## Start on Windows

1. Copy this entire folder to the other PC, for example `C:\The Workshop`.
2. Install Python and Git if needed. Download the model in Ollama while internet is available.
3. Open PowerShell in the copied folder and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

The launcher generates and prints a password on first use and stores only a salted password hash. Save that password and follow the address printed by the server. To choose a new one later, run `python run.py --reset-password`. For phone access use:

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Lan
```

Allow Python through Windows Firewall on **private networks** if Windows asks. Use the PC's local IPv4 address on your phone, e.g. `http://192.168.1.50:8080`. Both devices must be on the same network; guest Wi-Fi client isolation can prevent access. A router reservation keeps that address stable. The PC must remain awake.

On Linux/macOS:

```sh
python3 run.py --host 0.0.0.0 --port 8080
```

By default, data lives in `data/` beside the application. `python run.py --help` lists the available options. Keep Ollama bound to localhost; only The Workshop needs LAN access.

## First project

1. Open **Settings**, test the Ollama connection, refresh installed models, and select a default model.
2. Create a project with a precise brief, including intended behaviour, constraints, and acceptance criteria.
3. Ask the manager to generate a roadmap, or add tasks yourself. Review the generated roadmap before starting it.
4. Configure automated checks in the project settings as JSON arrays of arguments, for example:

```json
[["python", "-m", "unittest", "discover", "-s", "tests"], ["python", "-m", "compileall", "-q", "."]]
```

5. Start the project. Watch tasks, changes, run records, and activity. Pause stops new actions after the current model request or check returns.
6. Review the result and download a project ZIP or inspect the project folder directly.

Checks run in the task workspace, without shell expansion. Executables must be on PATH or specified by absolute path. A successful exit code is evidence for that check, not proof that the entire brief is satisfied.

You can also begin in **Assistant**: explain your idea and ask it to create a project, update its brief, add tasks, plan, or start and pause work. Actions go through the same validation as panel controls. Acceptance of candidates and configuration of executable commands remain user controls.

## Company team room

### Team identities and skills

Open **Team & skills** to see all nine identities, including the Operations Analyst, their actual instructions, and workflow-specific prompts. The inspector uses the same registry as inference; briefs, files, memory and observations are added per assignment. Sessions hold resumable context; Execution history retains the complete new requests and results.

You can customise an identity, restore its default, and add, edit, disable, or assign reusable instruction skills. The built-in playbook covers roadmaps, focused implementation, debugging, acceptance review, interface inspection, documentation, and team handoffs. Edits are blocked while projects or model operations are active; pause and wait first. New prompts use the changes; recorded past prompts do not change.

Evaluation and run records include a fingerprint of the team instructions so results produced under different playbooks remain separate comparisons.

Ollama supplies model inference, not this playbook. Skills are concise workflows supplied by The Workshop. They do not retrain a model, execute scripts automatically, install dependencies, or grant tool permissions. The separate tool inventory lists what the harness can execute and its conditions. A custom skill that requests a missing tool does not create that capability. Custom workflows are stored in the data directory with other settings and included in full backups.

Each project has a **Team room**, shared by you and the specialists. Messages record sender, recipient, purpose, optional task, reply reference, and Git revision. Purposes include requests, findings, blockers, handoffs, decisions, and status reports.

Address a request to a specialist for one focused reply. Broadcasts provide context without waking every role. Agents can request bounded consultations and attach findings to their task; they do not recursively start unlimited conversations. The implementation, QA, reviewer, debugger, and manager stages publish actual handoffs here. Blockers and requests for you also appear in the inbox.

**Knowledge** holds durable architecture notes, decisions, and conventions. Specialists receive relevant messages, task dependencies, and shared memory rather than every conversation. Saved role sessions can be inspected from the project.

## How unattended work behaves

- Projects and their task queues are stored in SQLite. Each project has a genuine local Git repository.
- New projects enable autonomous orchestration. Existing projects retain their prior mode until you enable it in settings.
- The manager produces acceptance criteria, priorities, and dependencies. Omitted dependencies default to an ordered chain in autonomous roadmaps. Tasks wait for accepted prerequisites; independent work can proceed around a blocker.
- Each task gets an isolated Git branch/worktree. Implementation is followed by fresh QA and reviewer sessions, then final checks and controlled fast-forward integration. Dirty candidates or a changed main project stop integration for inspection.
- Failed implementation receives bounded manager triage and debugger guidance. Exhausted attempts become blockers. The manager routes follow-ups and guides retries; it does not invent an unlimited replacement roadmap or waive checks.
- Agents have file tools, configured checks, a numbered allowlist of build commands, issue creation, shared memory, bounded consultations, and optional screenshot capture. They do not have an unrestricted shell tool.
- Step/attempt limits bound each task. Project token and runtime budgets pause work. Runtime is recorded work time rather than a strict wall-clock deadline; in-flight model responses can exceed a token budget by that response.
- `verified` means checks passed for the recorded revision. `done` with stage `integrated` means the candidate was merged. Without checks, human acceptance is required even when model QA and review approve.
- Pause is cooperative: a model request can take up to five minutes and each check up to two minutes. A pending model action is discarded after pause. Already-written files remain available.
- Restart preserves task stages, sessions, and worktrees. Projects remain paused unless you enabled **automatic resume**. Workspace integrity is checked before continuing; worktree references are repaired after restoring a complete backup to a new location.
- Calls are serialised. A queued assistant turn reserves foreground priority. The current bounded action finishes, the worker yields at a safe boundary, and your assistant responds before the next worker request. Work resumes afterward without mixing conversation contexts. There is no mid-response GPU preemption. Project mutations can return a busy conflict while a worker owns its workspace; live status reads remain available.

### Precise file edits

The `patch` tool replaces exactly one matching substring, optionally checking the file's SHA-256. Ambiguous matches and stale checksums are rejected without modifying the file. This supplements full-file writes and reduces generated text for small fixes.

### Complete history and Operations Analyst

New model requests/responses and tool results are appended to Execution history with role, task, timing, errors and instruction fingerprints. They are independent of the shorter resumable conversation window. Database triggers reject updates and deletes. Large image-bearing payloads are stored completely as hash-verified blobs in the data directory. Earlier activity cannot be reconstructed retroactively. Histories may contain private project content and grow over time; full backups include them.

The Operations Analyst runs only when projects and foreground operations are idle. It flags repeated actions, repeated failures, alternating edits, missing capabilities and excessive effort. Optional model analysis produces evidence-linked recommendations; deterministic checks also work without a configured model. Findings appear under Operations analyst and in the inbox. The analyst does not edit code, alter prompts or accept work. You can acknowledge/dismiss a finding or explicitly create an improvement issue.

Idle settings control auditing, model analysis and interval. Normal queued projects do not start by themselves. A project can receive one explicit idle-maintenance approval for its existing queued tasks, configured checks and remaining budget; approval is consumed when started. Finished projects remain stable. Completion records show the accepted revision, task outcomes and configured checks, and create an inbox notification. These records do not claim the entire brief has received independent human approval.

### Imports, previews, and screenshots

Source ZIP imports validate paths and create a fresh repository; original `.git` metadata is not imported. Preview displays static HTML in an opaque browser sandbox; it does not automatically start backend applications.

Screenshot capture uses an **already-installed Edge, Chrome, or Chromium**. Set `WORKSHOP_BROWSER` to its executable if detection fails. No browser is downloaded. Captures render trusted project HTML and execute its JavaScript: they are not OS isolation or a guarantee of offline network behaviour. Vision-capable Ollama models can receive captures; text-only models explicitly defer visual assessment to you. Missing capabilities produce an error rather than a claimed inspection.

Interactive UI tests use the optional browser toolset. Before going offline, install it into the Python environment that runs The Workshop:

```powershell
python -m pip install -r requirements-browser.txt
```

It uses an existing Chromium browser executable; no `playwright install` browser download is required. Each task gets a dedicated headless browser with a workspace-only loopback server. External requests, downloads, popups and WebSockets are blocked. The agent can open a relative HTML page, inspect text/controls, click by accessible role/name or label, fill fields, select options, scroll, capture screenshots, inspect errors and assert actual text. Text-only models can test semantics; visual judgement needs vision support. The current interactive tool serves static projects and does not launch arbitrary backend services. Missing dependencies produce a capability error. The browser is separate from your personal browser, but it is not OS-level isolation.

### Automatic startup

After the first launch and saving your password, opt in on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-startup.ps1
```

This registers a task for your current user **at sign-in**, without a visible window, and retries process failures up to three times. It is not a pre-login service. Logs go to `data/server.log`; a large previous log is rotated at startup. Automatic project resume is a separate per-project setting. Remove startup with the same script plus `-Remove`.

## Evaluation and leaderboard

Suite version 2 contains six Python tasks: debugging, implementation, state management, file persistence, multi-file repairs, and indexed-lookup optimisation. Choose cases, models, and repetitions. Each gets identical starter code, brief, fixed context, budgets, and external acceptance tests. Evaluation uses bounded single-worker mode so orchestration differences do not silently change model comparisons.

The leaderboard separates configurations/case selections, reports functional results by category, and compares successful completion time. Pending runs are not failures. Optional sampled RAM/GPU peaks describe host/device totals including other programs, not precise per-model allocation. Missing metrics remain unavailable. This is a smoke suite, not comprehensive proof of coding quality; the LRU checks do not prove asymptotic complexity. Acceptance tests execute submitted code with host permissions. Keep model weights and runner versions fixed: a model name is not an immutable fingerprint.

## Data, history, and backups

```text
data/
  cabin.db           Project records, tasks, settings, conversations, run evidence
  projects/<id>/     Project files and their .git history
  worktrees/<id>/    Isolated task candidates and retained history
  evaluations/<id>/  Acceptance checks outside agent file access
  screenshots/<id>/ Optional visual captures
```

Git checkpoints are local and do not push anywhere. Restoring a historical revision creates a new checkpoint rather than deleting history. Test evidence belongs to its recorded revision; after further edits, rerun checks before relying on it.

For a browser backup, pause all projects and wait for operations to finish, then use **Health → Backup**. It copies SQLite consistently and includes repositories, worktrees, and captures (512 MB browser limit). Backups contain private conversations and a password hash; keep them private.

With the server stopped, use `python run.py --backup C:\Backups\workshop.zip`, or copy the entire data directory including hidden files. Restore into an **empty data directory**, then launch with `--data-dir` pointing there. A project download is not a complete backup. Preserve your data when upgrading application code.

For automatic backups, set an interval and retention count in **Settings**. Scheduling is off by default. While running, The Workshop checks every 30 seconds and creates due backups only when projects and assistant operations are idle. Busy periods defer the backup. Archives are saved in `data/backups/`; retention applies only to the scheduler's timestamped files. Failures appear in the inbox and Health. These copies are on the same disk, so transfer important backups to separate storage. Machine sleep settings remain a host configuration task.

## Trust and network boundaries

The Workshop is for a trusted private LAN. Login uses a hashed password, session cookies, and request-origin/CSRF protections. Plain HTTP does not encrypt Wi-Fi traffic. Do not expose its port to the internet, configure port forwarding, or run it on an untrusted network. Use a TLS reverse proxy if you need encrypted browser traffic.

**Project execution is not sandboxed.** Your configured checks execute project code, which the model can modify, with your user account's privileges. No-shell invocation and project-path validation do not change that. Run The Workshop under a dedicated non-administrator account and keep valuable secrets out of that account. Use a disposable VM if executing untrusted projects. The model can modify tests inside its project; use acceptance scripts outside the project for stronger verification.

## Development and checks

```sh
python -m unittest discover -s tests -v
```

Tests use temporary repositories and simulated model responses; no Ollama download is required. For an actual model trial, use a disposable project with configured checks, then test pause, resume, review, download, and a server restart before leaving the machine unattended.

See [ARCHITECTURE.md](ARCHITECTURE.md) for component boundaries and extension points.

See [IMPROVEMENTS.md](IMPROVEMENTS.md) for twenty prioritised proposals for the next iteration.
