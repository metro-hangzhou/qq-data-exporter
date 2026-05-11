# QQ Data Exporter

Standalone QQ group/private chat exporter built around NapCatQQ public HTTP/WS interfaces.

## Scope

This repository owns the exporter pipeline only:

- normalize QQ/NapCat messages into stable JSONL/TXT records
- preserve ordered text/media segments
- materialize export asset bundles when requested
- provide CLI export, watch, login, and diagnostics flows
- provide optional preprocessing/import adapters for downstream analysis

It intentionally does not include the later ORCH/Benshi analysis system or review-editor UI.

## NapCat Runtime

The exporter depends on a running NapCatQQ runtime, but the runtime is not vendored here. Keep NapCat as a separate upstream checkout, local runtime install, or Git submodule/reference checkout.

Read the runtime boundary and plugin setup notes in [docs/NapCatRuntime.md](docs/NapCatRuntime.md).

## Fast Plugin

The exporter-owned fast plugin lives at:

```text
NapCat/napcat/plugins/napcat-plugin-qq-data-fast/
```

Copy or link this plugin into the active NapCat runtime plugin directory when using accelerated bulk history export. Restart NapCat after plugin changes.

## Development

Core package groups:

- `src/qq_data_core`
- `src/qq_data_integrations`
- `src/qq_data_cli`
- `src/qq_data_process`

Basic test entry:

```text
python -m pytest
```
