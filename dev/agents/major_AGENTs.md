# major_AGENTs.md

> Last updated: 2026-03-14
> Scope: repository-wide orchestration for exporter, NapCat integration, preprocessing, analysis substrate, local RAG, and first-phase LLM analysis.

## Purpose

This file is the top-level coordination guide for all AGENTS and TODO documents in this repository.

Use it to decide:

- which document is authoritative for a given subsystem
- which phase the repository is currently in
- where new work should be documented before implementation
- how to keep CLI, GUI, analyzer, and integration code decoupled

## Current Phase

The exporter and NapCat integration layers are now considered the completed upstream data-acquisition foundation.

The active primary build phase is:

- QQ chat preprocessing
- canonical message ingestion
- local storage and indexing
- hybrid keyword/vector retrieval as internal evidence infrastructure
- target-driven analysis substrate
- pluggable analysis agents
- first-phase abstract LLM analysis over bounded analysis packs
- image-reference and image-vector preparation
- privacy projection and analyzer-facing service boundaries

The current phase does not include:

- reranker inference
- OCR or caption generation
- multimodal image reasoning
- terminal or GUI query adapters as the source of truth
- a full `BenshiAgent` without curated seed data
- a final low-level benshi taxonomy before high-level LLM outputs have been iterated and reviewed

The current phase does include an initial bounded LLM-analysis path:

- select a dense high-signal slice from a candidate event or adaptive window
- cap the slice to a few hundred messages rather than sending the full chat history
- estimate input token cost before the remote call
- record actual `usage` returned by the LLM API after the call
- persist reusable legacy media hash indexes under `state/media_index/` during extraction-side recovery
- a first whole-window LLM report path built around saved `analysis_pack.json` artifacts rather than one-shot raw prompt assembly

The current LLM-analysis direction is now explicitly:

- first abstract
- then refine
- then descend into more concrete specialized agents

This means the immediate goal is not a fine-grained `BenshiAgent`.

The immediate goal is:

- whole-window bounded LLM analysis packs
- open-ended long reports
- manual sample-by-sample review
- later schema extraction from repeated observations

Current first implementation now includes:

- `scripts/run_llm_window_analysis.py`
- saved `analysis_pack.json`
- saved `llm_run_meta.json`
- saved `usage.json`
- saved prompt snapshot text
- replay from a previously saved pack without rebuilding it from SQLite/Qdrant first

For extractor-only share bundles distributed to external testers:

- ship a package-local `README.md` as the primary entry doc
- prefer `start_cli.bat` as the first-run instruction over raw `python app.py`
- validate the bundle by actually launching `start_cli.bat` (or piping `/quit`) before distribution
- ensure bundled `.venv` includes both `Lib/site-packages` and `.venv/Scripts`; partial virtualenv copies are not acceptable
- do not treat a copied Windows `.venv\Scripts\python.exe` as a portable interpreter across machines
- share bundles must prefer a bundled portable Python runtime plus package-local `site-packages`, and only fall back to locally installed Python 3.13 when the bundled runtime is absent
- rewrite any copied NapCat loader files to use bundle-relative imports; absolute `file:///D:/...` loader paths are not distributable

## Current Deliverable Snapshot

As of the current repository state, the following extraction-side deliverables are considered ready enough to share with external testers:

- repo-relative NapCat runtime discovery and bootstrap
- QR login flow from the interactive CLI
- slash-command REPL for:
  - `/login`
  - `/groups`
  - `/friends`
  - `/watch group ...`
  - `/watch friend ...`
  - `/export group ...`
  - `/export friend ...`
- realtime compact watch mode
- historical JSONL and TXT export
- profile-scoped historical export commands:
  - `/export`
  - `/export_onlyText`
  - `/export_TextImage`
  - `/export_TextImageEmoji`
- historical export-side media materialization into sibling `assets/` bundles plus manifests
- mixed-cache media recovery for:
  - NTQQ cache trees under `nt_qq/nt_data`
  - legacy QQ cache trees under `Image/Group2`, `Image/C2C`, `Audio`, `Video`, and `FileRecv`
- public NapCat download-assisted hydration for cloud-only or not-yet-localized media:
  - `get_image`
  - `get_file`
  - `get_record`
- export fidelity rules now explicitly include:
  - inline `data_count=` completion coverage
  - single-digit explicit date literals such as `2026-3-9_00-00-00` must parse the same as zero-padded forms
  - batch export selectors under `group_asBatch=` / `friend_asBatch=` with comma-separated fuzzy/pinyin-resolved targets
  - completion-menu priority over date rolling in export inputs
  - token-aware completion followup for terminal export tokens such as `data_count=` and `asJSONL`
  - cursor-movement cleanup for stale completion menus before `Left` / `Right` navigation
  - batch-target completion must continue across `,` separators and then hand off to time-expression completion after the batch token is finished
  - `data_count` without an explicit interval now means a true multi-page latest-tail export, not a single-page recent snapshot
  - watch-mode export feedback now uses a compact one-line top result plus multiline bottom detail while root CLI keeps multiline detail
  - root CLI export now mirrors watch-mode progress phases instead of staying mostly silent during long exports
  - watch-mode terminal feedback must wrap across multiple lines instead of truncating export result or summary information when width is insufficient
  - thumbnail-path upgrade to original media when possible during asset materialization
  - file-magic-based extension correction when QQ cache suffixes are misleading
- fast-history fallback chain:
  - preferred NapCat fast plugin path when available
  - fallback public OneBot history path otherwise
- repo-local Windows share-bundle builder for friends who only need data extraction
- validated extraction-side fidelity baseline:
  - a real `data_count=300` mixed sample now materializes `image`, `file`, `sticker.static`, and `sticker.dynamic` with `miss=0` after resolver fixes and QQ-side lazy-cache hydration

This means the "QQ friend/group extraction" side is now a separable upstream deliverable from the later preprocessing and analysis stack.

## Authority Order

Use documents in this order when they overlap:

1. `AGENTS.md`
2. `major_AGENTs.md`
3. subsystem-specific AGENTs:
   - `NapCat_AGENTs.md`
   - NapCat child handbooks routed from `NapCat_AGENTs.md`:
     - `NapCat.docs_AGENTs.md`
     - `NapCat.source_AGENTs.md`
     - `NapCat.community_AGENTs.md`
     - `NapCat.media_AGENTs.md`
   - `CodeStrict_AGENTs.md`
   - `process_AGENTs.md`
4. TODO documents:
   - `TODOs.md`
   - `TODOs.napcat-research.md`
   - `TODOs.production-review.md`
   - `TODOs.export-performance.md`
   - `TODOs.preprocess.md`
   - `TODOs.rag.md`
   - `TODOs.analysis-agents.md`
   - `TODOs.llm-analysis.md`

Rule of thumb:

- `AGENTS.md` defines repository-wide engineering rules and stable contracts
- `major_AGENTs.md` defines phase focus, cross-subsystem boundaries, and doc routing
- subsystem AGENTs define implementation policy inside one subsystem
- TODO files track work breakdown, sequencing, and open gaps

## Documentation Discipline

At every critical turning point, newly confirmed implementation fact, or important machine-specific finding:

- record it proactively in the most relevant `AGENTS.md` document
- update the matching `TODOs*.md` file when the finding changes work sequencing, closes an uncertainty, or changes a default
- do this without waiting for a separate user reminder

Examples of facts that must be recorded immediately:

- a confirmed QQ or NapCat cache/layout rule
- a validated performance bottleneck and its approved workaround
- a model/provider/network rule that changes default execution behavior
- a hardware constraint that affects batching, storage, or runtime defaults
- an exporter-side scoping rule that materially changes how much local data must be scanned or hashed
- a CLI contract expansion such as new export profiles, count-based truncation, or summary reporting that downstream testers will rely on
- a regression root cause and its repair, such as:
  - stale `source_path` month drift requiring context-based re-hydration
  - relative `/download?...` media URLs needing NapCat base-url resolution
  - over-aggressive old-month hydration skipping causing false `missing` results
- an export-fidelity finding such as "thumbnail path is not authoritative" or "completion priority is wrong under watch mode"
- a confirmed extractor diagnostic such as "the latest N messages can still span multiple months" or "missing recent images are actually stale `Ori` paths with recoverable `Thumb` siblings"
- a confirmed exporter-correctness rule such as "NapCat history pages may arrive newest-first or otherwise unstable; page-level sorting is required before interval, bounds, and tail decisions"
- a confirmed extractor semantic such as "blank NapCat `sourcePath/filePath` means no resolved local path right now, not automatically recalled or expired media"
- a confirmed extractor recovery rule such as "cloud-only files with blank local paths should first try NapCat public `get_file` / `get_record` download before falling back to QQ cache trees"
- a confirmed extraction interface rule such as "fast-history raw `fileUuid` is not a drop-in identifier for NapCat public `get_image` / `get_file`; fast-history asset hydration must prefer context-based plugin hydration"
- a confirmed extraction architecture rule such as "QQ/NapCat primarily recover media through message-context hydration (`downloadMedia`) rather than exporter-side hash lookup; MD5 matching is a fallback for stale legacy caches, not the main retrieval path"
- a confirmed extraction architecture rule such as "plugin-issued NapCat public media tokens are the preferred bridge into public `get_image/get_file/get_record`; raw `fileUuid` is not a valid substitute"
- a confirmed extraction deployment rule such as "a long-running NapCat process may still serve older fast plugin routes after repository updates; route-level capability changes like `/hydrate-media` require a runtime restart to become effective"
- a confirmed extraction performance rule such as "large old-tail exports can bottleneck on repeated legacy loose-MD5 recovery and repeated old-month remote hydration misses; prefer per-bucket caching and per-bucket failure short-circuiting instead of redoing the same work per asset"
- a confirmed extraction performance rule such as "repeated references to the same asset must reuse one per-export resolution result instead of rescanning or rehydrating on every occurrence"
- a confirmed extraction UX/performance rule such as "root/watch export progress must be time-throttled during asset materialization; do not redraw the terminal once per asset for large exports"
- a confirmed extraction validation result such as "after a real NapCat restart, a cautious `limit=20` live export recovered recent `2026-03` image assets via the refreshed route set, while remaining misses were narrowed to local `marketface` sticker files"
- a confirmed extraction research decision such as "formal export now compares three routes - context hydration, plugin-issued public token, and legacy MD5 benchmark-only fallback - but only the first two are allowed in production"
- a confirmed benchmark result such as "plugin-issued public token plus `get_image` matched context-hydration hit rate on a live sample while cutting average latency by an order of magnitude"
- a confirmed extraction pattern such as "a large remote full-history sample may show near-perfect `<=7d` recent image recovery while `31-90d+` old `ntqq_pic` originals still dominate the miss set; treat those as old-cache fidelity issues rather than current-hydration issues"
- a confirmed extraction capability expansion such as "sticker / marketface assets use the same context-based hydration pipeline as image/file/video/speech once raw message context is preserved during normalization"
- a confirmed extraction remote-fallback rule such as "marketface sticker files may be absent locally even when the message is valid; preserve the QQ public remote GIF URL during normalization and use it to materialize dynamic/static sticker exports"
- a confirmed raw-data rule such as "export must preserve native media payloads and must not generate convenience preview variants that could pollute downstream analysis inputs"
- a confirmed extraction residual-gap pattern such as "when a `data_count=2000` export is down to a handful of misses, and every remaining miss is nested inside old forwarded-chat bundles, the next fix direction is forwarded-bundle asset hydration rather than more generic cache-root scanning"
- Any artifact, report, or doc that surfaces a placeholder/heuristic/sampled feature **must** now include a dedicated "实现状态" block that states: current state (placeholder/heuristic/sample/unimplemented), missing dependencies/evidence, and downstream consequences. This block must not be replaced by vague adjectives or left implicit. The absence of such a block constitutes a blocker for release-style phrasing.
- a confirmed extraction performance pitfall such as "blank-path file fallback recursively scanned the whole QQ root and stalled watch-mode export"
- a confirmed extraction fidelity pitfall such as "legacy `Image/Group2` file timestamps can predate the referencing message by weeks, so time-window filtering must not be the only MD5 recovery pass"
- a confirmed extraction cache-layout rule such as "`Emoji/emoji-recv` records may actually resolve through `Pic/<month>/Thumb` after NTQQ rewrites cache placement"
- a confirmed extraction deployment/layout rule such as "remote testers may keep QQ caches under nested custom roots like `D:\\QQHOT\\Tencent Files\\...`; root discovery must include one-level nested `QQ` / `Tencent Files` / `QQ Files` directories under common drive letters"
- a confirmed extraction hydration rule such as "recent NTQQ image messages may advertise an `Ori/...` path before the original file has actually been downloaded to disk; partial misses inside the same minute are often cache-hydration lag, not resolver breakage"
- a confirmed extraction recovery rule such as "image segments with blank/stale local paths but preserved NapCat-public `file_id` should proactively try `get_image` before deeper local-cache fallback"
- a confirmed NapCat login semantic such as "WebUI may report `当前账号已登录，无法重复登录` with `isLogin=false`; treat this as effectively logged in for bootstrap/login flow compatibility"
- a confirmed remote-support rule such as "portable extractor bundles must always emit session logs under `state/logs/`; remote crash reports are not actionable without the latest CLI log and export manifest"

## Validation Notes

- repository-root `pytest` collection is currently noisy because the repo intentionally contains backup test trees and model/cache directories under `state/`
- prefer `pytest tests` for routine validation unless the collection rules are cleaned up later

## Current Document Map

- `AGENTS.md`
  - exporter contract
  - NapCat public-interface rules
  - repository-wide architecture constraints
- `NapCat_AGENTs.md`
  - NapCat master index
  - truth-source rules
  - child-handbook routing
- `NapCat.docs_AGENTs.md`
  - official docs-site digest
  - site-map coverage and official-fact summaries
- `NapCat.source_AGENTs.md`
  - upstream/local source architecture map
  - message/file/plugin/WebUI paths
- `NapCat.community_AGENTs.md`
  - GitHub issues / PR / discussions theme map
  - recurring operator pain points
- `NapCat.media_AGENTs.md`
  - exporter-facing media semantics
  - URL/path/file/token rules
  - current speech / forward-media findings
- `CodeStrict_AGENTs.md`
  - third-party production review lens
  - scale, failure-domain, observability, and harsh-environment critique
- `process_AGENTs.md`
  - preprocessing architecture
  - canonical ingest rules
  - chunk/index/privacy/model placeholders
  - retrieval/generation boundaries
  - analysis-substrate handoff rules
- `llm_AGENTs.md`
  - first-phase abstract LLM analysis
  - analysis-pack contract
  - report-first iteration policy
  - prompt-review-to-schema convergence path
- `TODOs.md`
  - main exporter and transport backlog
- `TODOs.export-performance.md`
  - export performance investigation and NapCat-side acceleration history
- `TODOs.production-review.md`
  - strict production hardening backlog derived from third-party review findings
- `TODOs.napcat-research.md`
  - official-doc coverage
  - upstream-source coverage
  - GitHub community coverage
- `TODOs.preprocess.md`
  - preprocessing-specific milestone plan
- `TODOs.rag.md`
  - hybrid retrieval, context building, and generation backlog
- `TODOs.analysis-agents.md`
  - target-driven analysis substrate
  - pluggable analysis-agent backlog
  - content-composition and future benshi specialization plan
- `TODOs.llm-analysis.md`
  - report-first LLM analysis
  - prompt iteration and sample review
  - later convergence into more structured schemas

## Decoupling Rules

The repository must remain library-first.

Required boundaries:

- `qq_data_cli` may call core/process services, but core/process must not import CLI code
- future GUI may call the same services, but process code must not assume terminal interaction
- analyzer code may call the same services, but preprocessing must not assume a specific report or RAG frontend
- `qq_data_analysis` may consume `qq_data_process`, but `qq_data_process` must not import `qq_data_analysis`
- analysis agents may consume analysis-substrate interfaces, but must not bind directly to SQLite table layouts
- NapCat integrations stay in integration modules; preprocessing must not depend on NapCat runtime internals
- chunking, embeddings, image features, and identity projection must be configurable policy objects, not hard-coded globals
- target/time-scope resolution must be policy-based and replaceable

Never let any of these become truth sources:

- CLI command strings
- prompt-toolkit widgets
- watch-mode screen state
- NapCat plugin-specific payload shapes
- a fixed chunk size or fixed chunk strategy
- one hard-coded analysis taxonomy
- one hard-coded analysis agent

## Dependency Rule

When implementation requires new Python dependencies:

- install from the repository virtual environment
- prefer `.venv\Scripts\uv.exe add ...`
- do not switch to ad hoc global installs

If a package is only needed for later phases:

- keep it out of the default runtime path until the code actually uses it

## External Network Rule

External model and data downloads must use the local proxy:

- proxy URL: `http://127.0.0.1:7897`
- env var override: `QQ_DATA_EXTERNAL_PROXY`

Apply this rule to external sources such as:

- Hugging Face model downloads
- other non-local model or dataset endpoints

Do not apply this proxy rule to local services:

- NapCat local HTTP/WS endpoints
- `127.0.0.1` / `localhost` diagnostics
- local SQLite/Qdrant access

Current exception:

- DeepSeek API access is currently configured to use direct default networking rather than the local proxy
- if this changes later, update `state/config/llm.local.json` and this section together

## Local Hardware Profile

Current confirmed local development machine profile:

- GPU: `NVIDIA GeForce RTX 2070`
- VRAM: `8 GB`
- system memory: `39.9 GB`

Operational constraint:

- embedding/model inference must not intentionally rely on Windows shared-memory spillover as virtual VRAM
- prefer a stable GPU sweet spot over aggressive batch sizes that push VRAM to the ceiling

Current repository guidance for local heavy embedding models on this machine:

- `Qwen/Qwen3-VL-Embedding-2B` is currently the preferred local embedding model
- measured text-only embedding sweet spot on this machine:
  - device: `cuda`
  - dtype: `float16`
  - batch size: `32` to `128`
  - observed peak allocated VRAM: about `4.0 GB`
- for large-scale preprocessing, skip image embeddings by default and keep image references only
- preprocessing should stream embeddings in small outer chunks and print realtime progress

Current local storage bottleneck note:

- local vector-store writes on a slow HDD can dominate end-to-end preprocess time even when GPU embedding throughput is healthy
- observed symptom pattern:
  - HDD at or near `100%`
  - VRAM usage looks normal
  - GPU compute utilization stays low
- on this machine, large preprocess runs should prefer one of:
  - put `--qdrant-path` on a faster SSD
  - or run with `--skip-vector-index` first and build the vector index later
- the current local default should prefer sequential append and bounded RAM preload over random-write-heavy local vector backends

If future hardware changes, update this section before retuning embedding defaults.

## Preprocessing Defaults To Remember

The current agreed defaults are:

- storage: SQLite + FTS5 + local contiguous vector store
- current default local embedding target: `Qwen/Qwen3-VL-Embedding-2B`
- alternate providers kept available:
  - `jinaai/jina-embeddings-v4`
  - OpenRouter remote embeddings
- LLM reserved config:
  - model: `deepseek-reasoner`
  - base URL: `https://api.deepseek.com`
- API key env var: `DEEPSEEK_API_KEY`
- local secret file for immediate analyzer use:
  - `state/config/llm.local.json`
- initial LLM test path:
  - do not send all `12000` messages
  - first select a dense candidate-event slice of roughly `200-300` messages
  - enforce a prompt-token ceiling before the call
  - print the slice plan to the console before executing
- image semantics now:
  - keep raw references
  - build image vectors
  - defer OCR/caption/multimodal reasoning
- privacy:
  - raw identity layer + alias layer
  - default outward projection uses alias
  - dangerous raw output must be explicit
- analysis:
  - default product shape is target-driven analysis, not user-question-first RAG
  - RAG is an internal evidence component for agents
  - first implementation focuses on base statistics and content composition
  - `BenshiAgent` is a later specialization layer built on top of the same substrate

## Implementation Routing

Before coding new subsystem work:

1. record the subsystem contract in the matching AGENT file
2. break work down in the subsystem TODO file
3. keep interfaces stable before adding UI adapters

For the current phase:

- write policy and contracts into `process_AGENTs.md`
- track build order in `TODOs.preprocess.md`
- track retrieval/generation work in `TODOs.rag.md`
- track analysis-substrate and agent work in `TODOs.analysis-agents.md`
- keep exporter and NapCat code unchanged unless integration boundaries require a new adapter

## Share Bundle Rule

When preparing a distributable extraction bundle for external testers or friends:

- package only the upstream extraction stack
- include:
  - `app.py`
  - `src/qq_data_cli`
  - `src/qq_data_core`
  - `src/qq_data_integrations`
  - bundled `NapCat/`
  - bundled `.venv/`
  - launch helpers and minimal readme files
- exclude by default:
  - preprocessing code
  - analysis-agent code
  - local analysis states
  - LLM config secrets
  - test fixtures not needed for extraction
  - local embedding model weights and caches
  - analysis-only Python packages such as `torch`, `transformers`, `peft`, `qdrant-client`, and related heavy stacks when a trimmed runtime is being prepared

Current builder entrypoint:

- `scripts/build_share_bundle.py`

Expected output shape:

- `dist/<bundle_name>/`
- `dist/<bundle_name>.zip`

The share bundle is intended to let another Windows user start one script and immediately perform QQ friend/group extraction without installing the later analyzer stack.
## Current Architecture Override

- The repository is now on the aggressive NapCat-direct-media route.
- Formal exporter behavior must prefer "NapCat like QQ itself" media recovery, not cache guessing.
- MD5/local cache fallback is benchmark-only and must not re-enter production export paths unless the user explicitly reverses this decision.
- All future findings about direct media hydration vs cache-guessing must be proactively recorded into the relevant AGENTs/TODOs files.
