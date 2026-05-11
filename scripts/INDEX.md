# Scripts Index

Canonical script surface overview.

## Active current scripts

### NapCat media / forward diagnostics

- [benchmark_media_resolution.py](benchmark_media_resolution.py)
- [inspect_forward_payload.py](inspect_forward_payload.py)
- [inspect_review_candidate_forward.py](inspect_review_candidate_forward.py)
- [probe_asset_routes.py](probe_asset_routes.py)
- [probe_napcat_forward_live.ps1](probe_napcat_forward_live.ps1)

### Export retest helpers

- [targeted_missing_retest.py](../targeted_missing_retest.py)
- [run_targeted_missing_retest.bat](../run_targeted_missing_retest.bat)

This split repo intentionally keeps only exporter/runtime diagnostic scripts.
Review-editor, ORCH, and later analyzer scripts remain in the original
development workspace and are not part of this exporter release surface.

## Historical / archived script surface

Older tracked scripts that are currently absent from this split repo remain in
the original development workspace archive. They are intentionally not copied
into the exporter repo.

These include historical benchmark, build, preview, and one-off helper scripts such as:

- `run_preprocess.py`
- `run_analysis.py`
- `run_llm_analysis.py`
- `run_llm_window_analysis.py`
- `run_rag_query.py`
- `benchmark_media_resolution.py`

## Surface rule

This index is now the canonical entry for the `scripts/` surface.

The directory still contains historical churn and local worktree noise, but this file defines:

- what is active
- what is historical
- where to look first
