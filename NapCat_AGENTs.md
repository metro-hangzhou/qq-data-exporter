# NapCat_AGENTs.md

> Last updated: 2026-03-27
> Scope: NapCat documentation routing, truth-source rules, runtime-surface classification, and repository-level integration decisions.

## Purpose

This file is the NapCat master index for this repository.

Use it to decide:

- which NapCat sub-handbook to read first
- which statements come from official docs vs upstream source vs community vs maintainer runtime evidence
- which local runtime files are upstream-tracking shell, mutable state, plugin surface, or generated artifacts
- which repository decisions are already fixed for the exporter / analyzer stack

Do not keep expanding this file into a giant mixed notebook.

Put detailed material into the dedicated child handbooks and runtime-surface reports below.

## Snapshot And Truth Sources

This handbook set is a `2026-03-27` snapshot of five evidence layers:

1. `Official docs`
   - `https://napneko.github.io/`
2. `Upstream source`
   - GitHub `NapNeko/NapCatQQ`
   - treat `origin/main` as upstream code truth
3. `Community evidence`
   - published GitHub issues
   - published PR metadata / visible discussion
   - published GitHub discussions
4. `Maintainer-side runtime findings`
   - live exporter runs
   - local plugin/runtime debug
   - benchmark traces
5. `Repository runtime surface facts`
   - the checked-in `NapCat/` vendored runtime shell
   - repo-owned launcher hooks
   - repo-owned plugin integration

Repository rule:

- never blur these sources together
- every high-value NapCat claim should be labeled as one of:
  - `Official doc fact`
  - `Upstream source fact`
  - `Community finding`
  - `Maintainer-side runtime finding`
  - `Repository runtime surface fact`
  - `Unresolved`
  - `Docs do not specify`

## Local Paths

Relevant local paths:

- vendored runtime shell: [NapCat](/d:/Coding_Project/IsThisShit/NapCat)
- runtime root: [NapCat/napcat](/d:/Coding_Project/IsThisShit/NapCat/napcat)
- upstream/source checkout: [NapCatQQ](/d:/Coding_Project/IsThisShit/NapCatQQ)
- runtime surface reports: [dev/reports/napcat/INDEX.md](/d:/Coding_Project/IsThisShit/dev/reports/napcat/INDEX.md)
- runtime operator handbook: [dev/handbooks/runtime/NapCatRuntime.md](/d:/Coding_Project/IsThisShit/dev/handbooks/runtime/NapCatRuntime.md)

Git maintenance rule:

- treat `NapCatQQ/` as a separately managed upstream-tracking checkout
- treat `NapCat/` as a vendored runtime shell plus mutable local runtime state
- do not flatten either tree into ordinary parent-repo content just for convenience

## Current Repository Decisions

These decisions remain active:

- formal integration target is NapCat public HTTP / WS, not private injection hooks
- forward WebSocket remains the preferred transport; HTTP remains fallback and diagnosis path
- bulk history export may use the local fast plugin because it still stays inside the local NapCat runtime and returns data through explicit plugin routes
- formal media extraction is strict NapCat-only:
  - direct local path first
  - then plugin context hydration
  - then plugin-issued public token plus public `get_image` / `get_file` / `get_record`
  - otherwise `missing_after_napcat`
- legacy local cache scan and MD5 recovery remain benchmark/research tools only
- current last-mile fidelity focus is still `forward` / `nested-forward` media, not ordinary top-level image recovery

## Runtime Surface Rules

For repository maintenance, classify the local `NapCat/` tree as:

- `vendored runtime shell`
  - top-level binaries, wrappers, launcher entrypoints, packaged runtime files
- `mutable runtime state`
  - `NapCat/napcat/cache/`
  - `NapCat/napcat/config/`
  - `NapCat/napcat/logs/`
- `plugin surface`
  - `NapCat/napcat/plugins/`
  - `NapCat/napcat/config/plugins/`
- `generated runtime artifacts`
  - `NapCat/napcat/node_modules/`
  - `NapCat/napcat/static/`
  - native packed runtime blobs
- `repo-side launcher bridge`
  - [start_napcat_logged.bat](/d:/Coding_Project/IsThisShit/start_napcat_logged.bat)
  - [restart_napcat_service.ps1](/d:/Coding_Project/IsThisShit/restart_napcat_service.ps1)
  - [state/napcat_logs](/d:/Coding_Project/IsThisShit/state/napcat_logs)

Implications:

- do not treat `cache/` or `logs/` as canonical documentation
- do not treat `node_modules/` or `static/assets/` churn as a repo architecture signal
- do treat plugin code and launcher hooks as repository-relevant integration surface
- after plugin code changes, a real NapCat restart is still required before new routes go live

## Child Handbooks

Read these in this order when working on NapCat-heavy tasks:

1. [NapCat.docs_AGENTs.md](/d:/Coding_Project/IsThisShit/dev/agents/NapCat.docs_AGENTs.md)
   - official docs site map
   - section-by-section summary
   - what official docs clearly state
   - what official docs do not specify
2. [NapCat.source_AGENTs.md](/d:/Coding_Project/IsThisShit/dev/agents/NapCat.source_AGENTs.md)
   - upstream package map
   - message, file, plugin, WebUI, and action-router architecture
   - token/LRU/file handling implementation facts
3. [NapCat.community_AGENTs.md](/d:/Coding_Project/IsThisShit/dev/agents/NapCat.community_AGENTs.md)
   - recurring operator pain points
   - discussion / issue / PR theme map
4. [NapCat.media_AGENTs.md](/d:/Coding_Project/IsThisShit/dev/agents/NapCat.media_AGENTs.md)
   - exporter-facing media semantics
   - `url/path/file/file_id/public token`
   - `speech`
   - `forward/nested-forward media`
   - current benchmark and runtime findings

## Runtime Surface Reports

When the task is about the local vendored runtime rather than protocol semantics, read:

1. [dev/reports/napcat/runtime_surface.md](/d:/Coding_Project/IsThisShit/dev/reports/napcat/runtime_surface.md)
   - top-level NapCat runtime surface map
   - what each subtree means
   - what belongs to upstream shell vs local mutable state
2. [dev/reports/napcat/runtime_state_and_plugins.md](/d:/Coding_Project/IsThisShit/dev/reports/napcat/runtime_state_and_plugins.md)
   - config/logs/cache/plugin classification
   - repo-owned plugin hooks
   - launcher / state bridge points

## Important Memory Notes

- Do not start new NapCat debugging from memory alone when the relevant child handbook exists.
- Do not treat the local `NapCat/` runtime tree as if every file inside it has equal architectural meaning.
- The repo now has a canonical runtime-surface map precisely to prevent wasting time on `node_modules/`, static build noise, or mutable local cache state when the real issue is launcher/plugin/config wiring.
