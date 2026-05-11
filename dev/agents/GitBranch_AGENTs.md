# GitBranch_AGENTs.md

> Scope: repository branch governance, release sync discipline, staging hygiene, and branch-incident response.

## Purpose

This handbook exists because the repository has repeatedly hit failures that looked like runtime bugs but were actually:

- release-line bundle skew
- partial cherry-picks
- launcher/runtime family drift
- pushing only caller-side changes without the paired implementation or tests

Treat this document as the authoritative "how we keep branches sane" reference.

## Branch Roles

### `full-dev`

- primary development branch
- may contain in-progress or broader local-only work
- local commits are expected
- do **not** push `full-dev` to remote unless the user explicitly changes policy

### `main`

- operator-facing release branch
- `start_cli.bat` on `main` is allowed to auto-update from `origin/main`
- every push to `main` must be treated as a potentially user-facing rollout

### `runtime`

- runtime/release companion branch
- keep it functionally aligned with `main` for launcher/runtime behavior
- do not leave `runtime` behind on startup/login/export family changes

## Current Policy

Unless the user explicitly overrides it:

- local live test account is only `3956020260`
- local live test targets are only:
  - group `922065597`
  - private peer `1507833383`
- `full-dev`:
  - local commit only
  - no remote push
- `main` and `runtime`:
  - may be pushed after targeted release validation

## Release Sync Rule

Release sync is **bundle sync**, not opportunistic file sync.

Do not ask:

- "what is the minimum one file I can cherry-pick?"

Ask:

- "what feature family actually moved?"
- "what tests prove the whole family shipped?"

## Atomic Feature Families

These families must be considered atomic during `main` / `runtime` sync.

### 1. CLI / REPL / completion family

- `src/qq_data_cli/app.py`
- `src/qq_data_cli/repl.py`
- `src/qq_data_cli/completion.py`
- relevant tests:
  - `tests/test_repl_login.py`
  - `tests/test_cli_login_completion.py`
  - `tests/test_cli_app_login.py`

### 2. Login / bootstrap / runtime family

- `src/qq_data_integrations/napcat/login.py`
- `src/qq_data_integrations/napcat/settings.py`
- `src/qq_data_integrations/napcat/webui_client.py`
- `src/qq_data_integrations/napcat/bootstrap.py`
- `src/qq_data_integrations/napcat/runtime.py`
- relevant tests:
  - `tests/test_napcat_quick_login.py`
  - `tests/test_napcat_bootstrap.py`
  - `tests/test_napcat_runtime_diagnostics.py`

### 3. Export / downloader / summary family

- `src/qq_data_integrations/napcat/media_downloader.py`
- `src/qq_data_core/export_selection.py`
- `src/qq_data_cli/app.py`
- relevant tests:
  - `tests/test_media_downloader_progress_and_forward_timeout.py`
  - `tests/test_export_selection_summary.py`

### 4. Start-script / launcher family

- `start_cli.bat`
- `start_cli_compat.bat`
- `start_cli_modern_host.bat`
- `start_napcat_logged.bat`
- `restart_napcat_service.ps1`
- relevant tests:
  - `tests/test_start_cli_script.py`
  - `tests/test_restart_napcat_service.py`

### 5. NapCat plugin / launcher family

- `NapCat/napcat/plugins/...`
- `src/qq_data_integrations/napcat/...`
- any launcher helper that starts, restarts, or configures NapCat

If a change spans more than one family, sync all affected families together.

## Staging Hygiene

Never stage from a broad dirty tree with:

- `git add .`
- `git add -A`

Prefer:

- `git add -- <explicit file list>`

Always stage the exact release bundle, not the surrounding noise.

## Files That Must Stay Out Of Release Commits

Unless a task explicitly says otherwise, do not commit:

- `exports/`
- `state/`
- `runtime_site_packages/`
- `python_runtime/` generated caches
- QR images and ad-hoc screenshots
- local ZIP archives
- transient startup captures
- local benchmark scratch outputs

If a file is untracked and you are not sure whether it is product code or local artifact, stop and classify it before staging.

## Worktree Strategy

When pushing release fixes:

- prepare and validate in `full-dev`
- sync the minimal complete bundle into:
  - `.tmp/release_fix_main`
  - `.tmp/release_fix_runtime`
- run release-line tests there
- commit in each release worktree
- push only after both the code and tests line up

This is preferred over trying to reason about a dirty root tree during release pushes.

## Release Checklist

Before pushing `main` or `runtime`, verify:

1. The feature family is fully identified.
2. All paired implementation files are staged.
3. Matching regression tests are staged.
4. The release worktree status only shows intended files.
5. The targeted regression set passes.
6. The push target branch is correct.
7. Any branch incident or new rule is recorded.

## Minimum Validation Matrix

### For login/completion changes

- `tests/test_repl_login.py`
- `tests/test_cli_login_completion.py`
- `tests/test_napcat_quick_login.py`
- `tests/test_napcat_bootstrap.py`
- `tests/test_napcat_runtime_diagnostics.py`

### For start-script / launcher changes

- `tests/test_start_cli_script.py`
- `tests/test_restart_napcat_service.py`
- `tests/test_napcat_runtime_diagnostics.py`

### For export/downloader changes

- `tests/test_media_downloader_progress_and_forward_timeout.py`
- `tests/test_export_selection_summary.py`
- live sanity if the feature touches real exporter hot paths

## Incident Logging Rule

If a release-line issue turns out to be branch skew, record it immediately in:

- [branch_sync_incidents.md](/d:/Coding_Project/IsThisShit/dev/reports/branching/branch_sync_incidents.md)

Do not rely on memory.

## Context Management Rule

When a branch-related incident happens, update all relevant places together:

- this handbook
- [AGENTS.md](/d:/Coding_Project/IsThisShit/AGENTS.md) if the top-level routing changed
- [technical_roadmap.md](/d:/Coding_Project/IsThisShit/dev/reports/repo/technical_roadmap.md)
- [TODOs.release-runtime-stability.md](/d:/Coding_Project/IsThisShit/dev/todos/TODOs.release-runtime-stability.md)
- [branch_sync_incidents.md](/d:/Coding_Project/IsThisShit/dev/reports/branching/branch_sync_incidents.md)

## Operational Heuristic

If a bug appears only on:

- `main`
- `runtime`
- auto-updated local clone

and not on `full-dev`, assume **branch skew first** until disproven.

That single heuristic will save time.
