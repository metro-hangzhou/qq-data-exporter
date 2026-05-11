# QQ Data Exporter

Standalone QQ group/private chat exporter built around NapCatQQ public HTTP/WS interfaces.

This repository is the exporter slice split out of `IsThisShit`. It keeps QQ data collection, normalization, media materialization, CLI/watch tooling, and preprocessing adapters together, while leaving ORCH/Benshi analysis and review-editor UI outside this repo.

## Primary Entry Points

- [AGENTS.md](AGENTS.md) — exporter architecture rules, canonical schema, media recovery policy, and maintenance constraints.
- [CLI_USAGE.md](CLI_USAGE.md) — CLI-oriented operator notes.
- [docs/NapCatRuntime.md](docs/NapCatRuntime.md) — NapCat runtime/submodule boundary and fast plugin installation model.
- [NapCat_AGENTs.md](NapCat_AGENTs.md) — NapCat-facing source/runtime handbook index.
- [dev/agents/INDEX.md](dev/agents/INDEX.md) — specialized maintainer handbooks.

## Scope

This repository owns the exporter pipeline only:

- normalize QQ/NapCat group and private messages into stable JSONL/TXT records
- preserve ordered text/media segments for downstream multimodal analysis
- materialize export asset bundles and manifests when explicitly requested
- provide CLI export, realtime watch, login, diagnostics, and targeted retest flows
- provide optional preprocessing/import adapters for later analyzers

It intentionally does not include the ORCH/Benshi analysis system or review-editor UI.

## NapCat Runtime

NapCatQQ is treated as an external runtime gateway. The exporter accesses it only through public OneBot 11 HTTP/WS interfaces.

`NapCatQQ/` is tracked as a Git submodule/reference checkout so the exporter repo can carry a concrete runtime source reference without flattening NapCat upstream into exporter code.

Clone with submodules when you need the local runtime source/reference:

```bash
git clone --recurse-submodules https://github.com/metro-hangzhou/qq-data-exporter.git
```

Or initialize it after cloning:

```bash
git submodule update --init --recursive
```

Generated NapCat runtime caches, config, logs, and build output must stay out of exporter Git history.

## Fast History Plugin

The exporter-owned fast plugin source lives at:

```text
plugins/napcat-plugin-qq-data-fast/
```

Install or link that plugin into the active NapCat runtime plugin directory when using accelerated bulk history export. Restart NapCat after plugin code changes; plugin routes are loaded by the running NapCat process, not by Python.

If the plugin is missing or disabled, exporter functionality should still work through public OneBot history APIs, but bulk export will be slower.

## Canonical Surfaces

- `src/qq_data_core/` — schema, normalization, exporters, media bundle logic
- `src/qq_data_integrations/napcat/` — OneBot HTTP/WS clients, NapCat runtime integration, fast-history client
- `src/qq_data_cli/` — thin CLI/REPL/watch shell around core services
- `src/qq_data_process/` — preprocessing/import/retrieval adapters used by later analysis systems
- `plugins/napcat-plugin-qq-data-fast/` — exporter-owned NapCat runtime plugin
- `tests/` — exporter, CLI, NapCat integration, and preprocessing regression tests

## Development

Basic validation:

```bash
python -m pytest
```

Targeted old-missing media retest:

```bat
run_targeted_missing_retest.bat
```

Keep release-line sync bundled by feature family. Do not cherry-pick caller-side changes without paired implementation and regression tests; see [dev/agents/GitBranch_AGENTs.md](dev/agents/GitBranch_AGENTs.md).
