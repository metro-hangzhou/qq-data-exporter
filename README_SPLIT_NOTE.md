# Split Note

This local repository is a first-pass QQ data exporter split from `IsThisShit`.

Included areas:

- `src/qq_data_core`
- `src/qq_data_integrations`
- `src/qq_data_cli`
- `src/qq_data_process`
- exporter/CLI/NapCat/process tests
- `NapCat/napcat/plugins/napcat-plugin-qq-data-fast`

Intentionally excluded:

- `src/qq_data_analysis`
- ORCH / Benshi analysis code
- `apps/review-editor`
- full NapCat runtime checkout
- generated caches, exports, dist, node_modules, and temporary session files

Before publishing to GitHub, review dependency scope in `pyproject.toml`: current project metadata still reflects the source repository and may include analysis/preprocess-heavy dependencies that should move into optional extras.
