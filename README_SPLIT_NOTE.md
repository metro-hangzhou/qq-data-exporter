# Split Note

This local repository is a first-pass QQ data exporter split from `IsThisShit`.

Included areas:

- `src/qq_data_core`
- `src/qq_data_integrations`
- `src/qq_data_cli`
- `src/qq_data_process`
- exporter/CLI/NapCat/process tests
- `plugins/napcat-plugin-qq-data-fast`
- `NapCatQQ` as a Git submodule/runtime source reference

Intentionally excluded:

- `src/qq_data_analysis`
- ORCH / Benshi analysis code
- `apps/review-editor`
- generated NapCat runtime caches, config, logs, exports, dist, node_modules, and temporary session files

Before publishing broader release packages, review dependency scope in `pyproject.toml`: current project metadata still reflects the source repository and may include analysis/preprocess-heavy dependencies that should move into optional extras.
