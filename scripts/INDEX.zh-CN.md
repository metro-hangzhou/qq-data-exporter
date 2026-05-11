# 脚本索引

这是 `scripts/` 的 canonical 入口。

## 当前 active scripts

### 共轨 / 校验

- [validate_subagent_framework.py](validate_subagent_framework.py)

### analyzer / 本地语料

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

### exporter evidence / probe

- [export_evidence_manifests.py](export_evidence_manifests.py)
- [probe_asset_routes.py](probe_asset_routes.py)
- [profile_logic_test_buckets.py](profile_logic_test_buckets.py)

## 历史 / 归档脚本面

当前 worktree 里已经不在 active surface 的旧 tracked scripts，都已归档到：

- [scripts surface archive](/d:/Coding_Project/IsThisShit/dev/archive/system_refactor_20260327/scripts_surface_20260327/README.md)

## 这份文件的作用

它用来回答：

- 现在该先看哪个脚本
- 哪些脚本仍然是主战场
- 哪些只是历史件/旧 helper
