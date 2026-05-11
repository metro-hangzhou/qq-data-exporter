# 脚本索引

这是 `scripts/` 的 canonical 入口。

## 当前 active scripts

### NapCat 媒体 / 转发诊断

- [benchmark_media_resolution.py](benchmark_media_resolution.py)
- [inspect_forward_payload.py](inspect_forward_payload.py)
- [inspect_review_candidate_forward.py](inspect_review_candidate_forward.py)
- [probe_asset_routes.py](probe_asset_routes.py)
- [probe_napcat_forward_live.ps1](probe_napcat_forward_live.ps1)

### 导出复测工具

- [targeted_missing_retest.py](../targeted_missing_retest.py)
- [run_targeted_missing_retest.bat](../run_targeted_missing_retest.bat)

这个拆分仓库只保留 exporter / runtime 诊断脚本。
review-editor、ORCH 和后续 analyzer 脚本仍属于原开发工作区，不属于
exporter release surface。

## 历史 / 归档脚本面

当前拆分仓库里已经不在 active surface 的旧 tracked scripts，保留在原开发
工作区的 archive 中；它们不会复制进 exporter repo。

## 这份文件的作用

它用来回答：

- 现在该先看哪个脚本
- 哪些脚本仍然是主战场
- 哪些只是历史件/旧 helper
