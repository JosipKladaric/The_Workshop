# Twenty next improvements for The Workshop

Proposed future changes, roughly prioritised for dependable unattended work.
These are not claims about features already implemented.

| # | Change | Practical outcome |
|---|---|---|
| 1 | OS-isolated execution | Run project code in a dedicated VM/container with explicit file and network access. |
| 2 | Hard process resource limits | Enforce RAM, CPU and process-tree limits, including orphaned child processes. |
| 3 | Exact model fingerprints | Record weight digest, quantisation, runner and driver versions for reproducible comparisons. |
| 4 | Token-aware context allocation | Reserve space for requirements and results; compact context before model-side truncation. |
| 5 | Repository maps and retrieval | Retrieve relevant symbols and modules instead of repeatedly sending broad file inventories. |
| 6 | Managed application previews | Start approved backend/frontend services, wait for readiness, test and cleanly stop them. |
| 7 | Saved UI test scenarios | Replay successful browser workflows as independent regression tests. |
| 8 | Automated accessibility checks | Identify missing labels, keyboard problems and contrast violations with actual evidence. |
| 9 | Responsive visual comparisons | Compare approved desktop/phone screenshots against later revisions with explicit baseline approval. |
| 10 | Conflict-aware integration | Rebase stale task branches, handle conflicts, and repeat QA before merging. |
| 11 | Per-project offline environments | Separate Python/Node environments with verified local dependency caches and readiness checks. |
| 12 | Protected acceptance suites | Execute external acceptance tests through an isolated verification boundary. |
| 13 | Test-strength evaluation | Use mutation testing or seeded defects to detect weak test suites. |
| 14 | Representative project benchmarks | Add real bug fixes, UI workflows and multi-step projects with repeated trials. |
| 15 | Role-to-model routing | Measure whether specialist models improve throughput after loading and switching overhead. |
| 16 | Secure local credentials | Keep secrets outside prompts and repositories; grant narrow project-specific capabilities. |
| 17 | Encrypted LAN access | Add understandable HTTPS setup and individually revocable device sessions. |
| 18 | Backup restore rehearsal | Verify checksums, SQLite, Git and worktrees before activating a restored copy. |
| 19 | History storage lifecycle | Compress/archive immutable evidence while preserving search and report references. |
| 20 | Energy-aware scheduling | Support temperature/power limits, quiet hours and measured useful work per unit of energy. |
