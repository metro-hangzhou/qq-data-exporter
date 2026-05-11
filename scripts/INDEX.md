# Scripts Index

Canonical script surface overview.

## Active current scripts

### Workbench / validation

- [validate_subagent_framework.py](validate_subagent_framework.py)

### Analyzer / local corpus

- [run_benshi_live_llm_smoke.py](run_benshi_live_llm_smoke.py)
- [run_local_corpus_analysis_smoke.py](run_local_corpus_analysis_smoke.py)
- [build_benshi_cross_group_review.py](build_benshi_cross_group_review.py)
- [build_benshi_review_packets.py](build_benshi_review_packets.py)
- [parse_benshi_review_packets.py](parse_benshi_review_packets.py)
- [run_review_editor_server.py](run_review_editor_server.py)
- [init_judgment_policy_state.py](init_judgment_policy_state.py)
- [build_judgment_policy_slice.py](build_judgment_policy_slice.py)
- [ingest_judgment_policy_patch.py](ingest_judgment_policy_patch.py)
- [promote_judgment_policy_state.py](promote_judgment_policy_state.py)
- [update_benshi_posterior.py](update_benshi_posterior.py)

### Exporter evidence / probes

- [export_evidence_manifests.py](export_evidence_manifests.py)
- [probe_asset_routes.py](probe_asset_routes.py)
- [profile_logic_test_buckets.py](profile_logic_test_buckets.py)

## Historical / archived script surface

Older tracked scripts that are currently absent from the active worktree are preserved in:

- [scripts surface archive](/d:/Coding_Project/IsThisShit/dev/archive/system_refactor_20260327/scripts_surface_20260327/README.md)

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
