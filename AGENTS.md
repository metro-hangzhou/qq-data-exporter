# AGENTS.md

> Spec baseline: 2026-03-06. This project targets the current NapCatQQ OneBot 11 HTTP/WS interface, not NapCat internals.

## Project Mission

This repository is for a decoupled QQ exporter for group chats and private chats.

Core goals:

- NapCatQQ is only an external gateway.
- Exported data must be stable enough for downstream LLM multimodal analysis.
- Text and image references must both survive normalization.
- The CLI must support both realtime debug watch and historical export.
- The same core export pipeline must be reusable by a future GUI or by the data analyzer directly.

## Source Of Truth

Build against NapCat's public interface only:

- transport: OneBot 11 HTTP server or forward WebSocket
- history APIs: `get_group_msg_history`, `get_friend_msg_history`
- metadata APIs: `get_group_list`, `get_group_member_list`, `get_friend_list`
- message segment taxonomy: `text`, `image`, `record`, `file`, `face`, `mface`, plus reply/at/forward variants

Do not:

- import NapCat internal TypeScript modules
- depend on QQ injection hooks
- reuse `qq-chat-exporter` overlay/bridge logic

## Architecture Rules

1. Prefer forward WebSocket as the main transport because it supports both event push and action calls in one channel.
2. Keep HTTP as a fallback for manual export or diagnosis.
3. JSONL is the primary export format. TXT is a supported secondary rendering format.
4. Message segment order must be preserved. Never flatten all text first and all media later.
5. Do not copy QQ media binaries into the repository state by default. During an explicit export task, materialize actual media files into the export output bundle next to the data file.
6. `content` is the stable inline string for downstream analysis. `segments` is the structured backup.
7. If NapCat exposes extra fields such as `path`, `url`, `md5`, or `summary`, keep them in structured segments when cheap to retain.
8. Keep the exporter independent from later LLM analysis code.
9. Prefer structured array message payloads over CQ-code strings. Do not make CQ parsing the primary normalization path.
10. CLI code must stay thin. Core normalization and export code must not depend on prompt_toolkit, Typer, or terminal UI concepts.
11. `NapCatQQ/` under the repository root may be a local runtime checkout, Git submodule, or reference checkout, but it is intentionally ignored by this exporter repository unless a maintainer explicitly decides to pin it. Runtime code may discover configs and launcher scripts by relative path from the repo root, but message access still must use NapCat public HTTP/WS interfaces.
11a. For Git/GitHub maintenance, treat `NapCatQQ/` as a separately managed upstream-tracking checkout, not ordinary exporter content. The exporter may reference it locally by relative path, but should not flatten it in a way that breaks future upstream `NapCatQQ` merges or obscures the runtime/exporter boundary.
11b. The exporter-owned fast plugin is the only NapCat-tree source intentionally kept in this repo: `NapCat/napcat/plugins/napcat-plugin-qq-data-fast/`. The active NapCat runtime must install or link that plugin into its own plugin directory and be restarted after plugin code changes. See `docs/NapCatRuntime.md`.
12. Prefer message-provided local resource paths such as `sourcePath`, `filePath`, `staticFacePath`, and `dynamicFacePath` when materializing media. Only fall back to QQ cache root discovery when those direct paths are missing or stale.
13. Assume mixed legacy QQ and NTQQ cache layouts may coexist on the same machine. Media lookup must support both:
    - NTQQ-style paths under `nt_qq/nt_data/...`
    - legacy QQ-style paths such as `Image/Group2`, `Image/C2C`, `Audio`, `Video`, and `FileRecv`

## Current Performance Override

For bulk history export, this repository currently has one approved optimization beyond the default public OneBot history action:

- a NapCat runtime plugin under [NapCat/napcat/plugins/napcat-plugin-qq-data-fast](/d:/Coding_Project/IsThisShit/NapCat/napcat/plugins/napcat-plugin-qq-data-fast)

What it does:

- fetches raw history directly inside NapCat
- skips the public OneBot `parseMessage(...)` history path
- returns a slim raw payload for Python-side normalization
- now also exposes a plugin-local bulk recent-tail history route so NapCat can chase anchor-linked history pages inside one plugin request before returning the merged tail slice to Python

Current formal media extraction stance:

- production export now follows a strict NapCat-only route for media recovery
- direct local paths from NapCat remain first
- otherwise exporter asks NapCat, with full message context, to hydrate authoritative local media paths
- when fast-plugin hydration returns a NapCat-recognized public media token, exporter may immediately resolve the asset through public `get_image` / `get_file` / `get_record`
- if NapCat cannot recover the asset, export records `missing_after_napcat`
- legacy local cache scans and MD5-based recovery remain research/benchmark tools only and must not participate in formal CLI export
- current active fidelity work is focused on forwarded and nested-forwarded media; ordinary top-level `image` / `file` / `video` recovery is no longer the main blocker

Important memory note:

- the speedup is not just a Python optimization
- if the plugin is missing or disabled, export will fall back to the slower public path
- after plugin code changes, a real NapCat restart is still required before newly added plugin routes become live on the maintainer/runtime machine
- the authoritative explanation and usage notes now live in the NapCat handbook set rooted at [NapCat_AGENTs.md](/d:/Coding_Project/IsThisShit/NapCat_AGENTs.md)
- for bulk history fetch through the fast plugin, treat `200` as the real per-page ceiling even if higher counts are requested; progress output and adaptive page sizing should reflect the effective page size rather than a theoretical `500`

## Release Branch Sync Caution

Release branches have already hit multiple "half-new half-old" incidents where:

- `main` / `runtime` received a caller-side change
- but the paired implementation or regression test did not ship with it
- and local auto-update then pulled a broken release package

Therefore:

- treat `main` / `runtime` sync as **bundle sync**, not file-by-file opportunistic cherry-pick
- when shipping CLI/repl/login/export/runtime fixes, sync the whole feature family together
- if a release-line incident happens, record it in:
  - [branch_sync_incidents.md](/d:/Coding_Project/IsThisShit/dev/reports/branching/branch_sync_incidents.md)
- branch governance itself now has a dedicated handbook:
  - [dev/agents/GitBranch_AGENTs.md](/d:/Coding_Project/IsThisShit/dev/agents/GitBranch_AGENTs.md)

Minimum families that must be considered atomic on release sync:

1. CLI / REPL family
   - `src/qq_data_cli/app.py`
   - `src/qq_data_cli/repl.py`
   - `src/qq_data_cli/completion.py`
   - related tests
2. Login / runtime family
   - `src/qq_data_integrations/napcat/login.py`
   - `src/qq_data_integrations/napcat/settings.py`
   - `src/qq_data_integrations/napcat/webui_client.py`
   - `src/qq_data_integrations/napcat/bootstrap.py`
   - related tests
3. Export / downloader family
   - `src/qq_data_integrations/napcat/media_downloader.py`
   - `src/qq_data_core/export_selection.py`
   - `src/qq_data_cli/app.py`
   - related tests
4. Start-script family
   - `start_cli*.bat`
   - `start_napcat_logged.bat`

Do not assume "starts locally" is enough validation for a release sync.
Always run the smallest matching regression set for the feature family being shipped.

## Specialized Planning Docs

Repository work is now split across specialized AGENT documents:

- index: [dev/agents/INDEX.md](/d:/Coding_Project/IsThisShit/dev/agents/INDEX.md)
- [GitBranch_AGENTs.md](/d:/Coding_Project/IsThisShit/dev/agents/GitBranch_AGENTs.md)
  - branch roles, release sync discipline, staging hygiene, and branch-incident response

- [major_AGENTs.md](/d:/Coding_Project/IsThisShit/dev/agents/major_AGENTs.md)
  - repository-wide phase coordination and document routing
- [NapCat_AGENTs.md](/d:/Coding_Project/IsThisShit/NapCat_AGENTs.md)
  - NapCat master index, truth-source rules, and child-handbook routing
- [NapCat.docs_AGENTs.md](/d:/Coding_Project/IsThisShit/dev/agents/NapCat.docs_AGENTs.md)
  - official NapCat docs-site digest and what the docs do or do not specify
- [NapCat.source_AGENTs.md](/d:/Coding_Project/IsThisShit/dev/agents/NapCat.source_AGENTs.md)
  - upstream/local source architecture map, message/file/plugin paths, and token semantics
- [NapCat.community_AGENTs.md](/d:/Coding_Project/IsThisShit/dev/agents/NapCat.community_AGENTs.md)
  - GitHub issues / PR / discussions theme map and recurring operator pain points
- [NapCat.media_AGENTs.md](/d:/Coding_Project/IsThisShit/dev/agents/NapCat.media_AGENTs.md)
  - exporter-facing media semantics, public token route, URL/path behavior, speech, and forwarded media
- [CodeStrict_AGENTs.md](/d:/Coding_Project/IsThisShit/dev/agents/CodeStrict_AGENTs.md)
  - third-party production-hardening review lens, strict failure-mode critique, and harsh-environment design guidance
- [process_AGENTs.md](/d:/Coding_Project/IsThisShit/dev/agents/process_AGENTs.md)
  - preprocessing, canonical ingest, chunk/index policy, and privacy guidance
- [llm_AGENTs.md](/d:/Coding_Project/IsThisShit/dev/agents/llm_AGENTs.md)
  - report-first LLM analysis policy, analysis-pack contracts, and later schema-convergence direction

When working on the new preprocessing subsystem:

- keep CLI, GUI, analyzer, and indexing code decoupled
- do not treat chunk size or chunk presence as a truth-source invariant
- keep OCR/caption/multimodal image reasoning deferred until the later analyzer phase

## Recommended Layout

```text
NapCatQQ/
src/
  qq_data_core/
    interfaces.py
    models.py
    normalize.py
    exporters/
      jsonl.py
      txt.py
    services.py
  qq_data_integrations/
    fixtures.py
    napcat/
      http_client.py
      provider.py
  qq_data_cli/
    app.py
    repl.py
tests/
  fixtures/
  test_normalize.py
  test_txt_export.py
exports/
state/
```

## Canonical Export Schema

Primary output is one JSON object per message, one line per record.

Required fields:

- `chat_type`: `"group"` or `"private"`
- `chat_id`: group ID or private peer QQ ID
- `group_id`: group ID or `null`
- `peer_id`: private peer QQ ID or `null`
- `sender_id`: sender QQ ID
- `message_id`: NapCat/OneBot message ID when available
- `message_seq`: history pagination sequence when available
- `timestamp_ms`: Unix timestamp in milliseconds
- `timestamp_iso`: ISO-8601 string with timezone
- `content`: normalized inline content string
- `text_content`: text-only content with non-text payload removed
- `image_file_names`: ordered image file names extracted from the message
- `uploaded_file_names`: ordered uploaded file names extracted from the message
- `emoji_tokens`: ordered emoji/sticker tokens extracted from the message
- `segments`: ordered normalized segments

Recommended optional fields:

- `chat_name`
- `sender_name`
- `sender_card`
- `reply_to`
- `extra`
- `segments[*].path`
- `segments[*].extra.static_path`
- `segments[*].extra.dynamic_path`

Optional heavy fields:

- `raw_event`
- `raw_message`

Only include heavy raw payloads behind an explicit `--include-raw` style option.

TXT output rules:

- follow the sample `tests/fixtures/testChatRecord/*.txt` layout style
- include chat header summary plus per-message blocks
- include `群ID` or `好友ID` in the header
- include `发送者ID` in each message block
- render resources in a separate `资源:` block when present

Example:

```json
{
  "chat_type": "group",
  "chat_id": "123456789",
  "group_id": "123456789",
  "peer_id": null,
  "sender_id": "987654321",
  "message_id": "753190845221",
  "message_seq": "1048576",
  "timestamp_ms": 1736563423000,
  "timestamp_iso": "2025-01-11T10:43:43+08:00",
  "content": "今天的图来了 [image:57f267d1c1302fca.JPG] [sticker:summary=狗头,emoji_id=12345,package_id=678]",
  "text_content": "今天的图来了",
  "image_file_names": ["57f267d1c1302fca.JPG"],
  "uploaded_file_names": [],
  "emoji_tokens": [
    "[sticker:summary=狗头,emoji_id=12345,package_id=678]"
  ],
  "segments": [
    { "type": "text", "text": "今天的图来了" },
    { "type": "image", "file_name": "57f267d1c1302fca.JPG" },
    {
      "type": "sticker",
      "summary": "狗头",
      "emoji_id": "12345",
      "emoji_package_id": 678
    }
  ]
}
```

## Segment Normalization Rules

Formal export rules:

- `text`
  - append text directly to `content`
  - append text directly to `text_content`
- `at`
  - render as `@name` if available, otherwise `@qq`
  - treat as text-like content
- `image`
  - inline token: `[image:<file_name>]`
  - push `<file_name>` into `image_file_names`
  - keep `path`, `url`, `md5`, `width`, `height`, `summary` in the segment when available
- `record`
  - inline token: `[speech audio]`
  - do not export speech transcription in V1
- `file`
  - inline token: `[uploaded_file_name:<file_name>]`
  - push `<file_name>` into `uploaded_file_names`
- `onlinefile`
  - normalize the same way as `file`
- `face`
  - inline token: `[emoji:id=<id>]`
  - push the same token into `emoji_tokens`
  - keep `resultId` and `chainCount` if present
- `mface`
  - inline token: `[sticker:summary=<summary>,emoji_id=<emoji_id>,package_id=<emoji_package_id>]`
  - push the same token into `emoji_tokens`
  - keep `key` if present
  - this is the "表情包 / marketface" class, distinct from lightweight built-in `face` emoji
- `reply`
  - keep reply metadata in `reply_to`
  - do not force reply tokens into `text_content`
  - if needed for lossless replay, add a `reply` segment to `segments`
- `video`
  - inline token: `[video:<file_name-or-url>]`
- `forward` / merged forward
  - inline token: `[forward message]`
  - preserve forwarded preview metadata and preview text extracted from `multiForwardMsgElement.xmlContent` when available
  - when a forward bundle can be resolved through NapCat `get_forward_msg`, preserve recursive forwarded message detail in structured segment data instead of only keeping the preview summary
  - if the forwarded bundle itself contains nested forwarded bundles, expand them recursively until no deeper forwarded records remain
  - treat resolved forwarded detail text as analyzable text-like content in `text_content`, not just the short preview line
- `system`
  - normalize gray-tip and similar system/event payloads into readable inline text
  - preserve original gray-tip payload in `segments[*].extra` when cheap to retain
- `share`
  - normalize ark/share cards into `[share:<title>]` plus compact title/desc/tag text
  - preserve raw share metadata such as `url`, `desc`, and `tag` in `segments[*].extra`
- unsupported types
  - inline token: `[unsupported:<type>]`
  - keep the minimal raw fields necessary for later recovery
  - current known high-value formerly-unsupported families such as forwarded chat bundles, gray-tip system events, and ark/share cards should be promoted to first-class segment types before falling back here

Debug watch rules:

- text: print directly
- image: print `[image]`
- `face` or `mface`: print `[meme or emoji]`
- record: print `[speech audio]`
- file or onlinefile: print `[uploaded file]`
- `forward`: print `[forward message]`
- `share`: print `[share:<title>]`
- `system`: print the normalized readable system text

The debug watch intentionally hides filenames to stay readable. The export path keeps the richer tokens.

TXT rendering rules:

- `image` renders as `[图片: <file_name>]`
- `file` renders as `[文件: <file_name>]`
- `record` renders as `[语音]`
- `face` renders as `[表情<id>]`
- `mface` renders as the summary or sticker name when available
- `reply` prepends `[回复 : <preview>]`
- `forward` renders as `[转发聊天记录: <preview>]`
- `share` renders as `[分享: <title>] <desc>`
- `system` renders as the normalized readable system text

## Exported Media Materialization

Historical export now has two output layers:

- the primary data file:
  - `*.jsonl`
  - or `*.txt`
- a sibling media bundle:
  - `<stem>_assets/`
- a sibling manifest:
  - `<stem>.manifest.json`

Materialization rules:

- image segments copy the underlying cached image file into `assets/images/`
- speech segments copy the underlying local audio file into `assets/audio/`
- file and onlinefile segments copy the underlying file into `assets/files/`
- video segments copy the underlying local video file into `assets/videos/`
- sticker segments copy:
  - static sticker resources into `assets/stickers/static/`
  - dynamic sticker resources into `assets/stickers/dynamic/`

Resolver order:

1. use the direct absolute path already present in the message segment
2. for fast-history-derived `image` / `file` / `video` / `speech` / `sticker` segments with preserved raw message context (`message_id_raw`, `element_id`, `peer_uid`, `chat_type_raw`), use the fast plugin `/hydrate-media` route to force a local hydration attempt inside NapCat
3. if that hydration response also includes a NapCat-recognized `public_file_token`, use:
   - `get_image` for `image` and image-like sticker payloads
   - `get_file` for `file` and `video`
   - `get_record` for `speech`
4. for media that only survives inside resolved `forward` / `nested-forward` public content trees and exposes no stable local path or public token, direct-download the NapCat-provided media `url` if it is still live
5. if still missing, record a `missing_after_napcat` asset entry in the manifest

Research-only routes:

- local QQ cache root discovery
- legacy `MD5` content-hash recovery

These are allowed only for benchmark and research tooling, not for formal CLI export.

Fast-history public action note:

- raw fast-history resource ids such as `fileUuid` are not the same thing as NapCat public `get_image` / `get_file` tokens
- the fast plugin may, however, generate a proper `public_file_token` from full message context and return it alongside hydration metadata
- once `/hydrate-media` has been attempted for a fast-history asset, do not fall back to public `get_image` / `get_file` with the raw id; only call public actions with a plugin-issued `public_file_token`
- doing so creates noisy NapCat-side `file not found` errors and can slow export on old assets without improving recovery

Legacy mixed-cache note:

- NTQQ and legacy QQ cache trees may coexist on the same machine
- legacy `Image/Group2` and related trees may use obfuscated file names that do not match exporter UUID-like names
- legacy `Image/Group2` also uses high-fan-out obfuscated two-level subdirectories rather than time-based directory names
- for those trees, direct file-name search is not authoritative; content-hash recovery is the preferred fallback
- repository state may persist reusable legacy media hash indexes under `state/media_index/` to avoid recomputing unchanged file hashes on every export
- legacy QQ cache hashing must not default to sweeping every file under huge cache trees; narrow the candidate set by export time window and path/month hints first
- QQ media roots must not assume caches live directly under `C:\QQ` or `D:\Tencent Files`; real machines can use nested custom roots such as `D:\QQHOT\Tencent Files\...`, so automatic discovery should include one-level nested `QQ` / `Tencent Files` / `QQ Files` directories under common drive roots
- NTQQ month directory names such as `Pic/2025-09/` are more reliable scope hints than directory creation time because folders may be recreated by later cache refreshes
- legacy `Image/Group2` file and directory timestamps are a usable prefilter for narrow export windows and should be applied before MD5 hashing
- legacy `Image/Group2` timestamps are not authoritative enough for a hard exclusion rule because an older cached file can still be reused by a later message; use time-window filtering only as the first-pass accelerator, then do a second-pass targeted MD5 recovery for still-missing assets
- when an NTQQ `Ori/...` source path is stale, resolver fallback should also probe sibling `Thumb/...` variants such as `<stem>_0.jpg` and `<stem>_720.jpg` before declaring the asset missing
- for old assets that already failed fast plugin `/hydrate-media` several times within the same month bucket, stop repeating remote hydration attempts for that month in the current process and rely on local cache recovery plus manifest `missing` records instead
- `Emoji/emoji-recv/...` image paths may actually materialize through `Pic/<month>/Thumb` or `Pic/<month>/OriTemp`; NTQQ fallback should probe those cross-tree Pic directories as well
- assets with blank `sourcePath` but known `file_name` still require root-level fallback search, especially for legacy `FileRecv` materialization
- blank `sourcePath` fallback must stay directory-targeted by asset type; never recurse the entire QQ root just to locate a single `file` or `video` name
- NapCat runtime behavior note: blank `sourcePath` / `filePath` is not, by itself, evidence that a message was recalled or that media is permanently unavailable; in local runtime code the path fields are populated from `downloadMedia(...)`, and empty strings mean no resolved local file path was returned at that moment
- exporter normalization must preserve NapCat-public download hints such as `file_id`, `message_id_raw`, and `element_id` inside segment `extra` when cheap to retain, so export-time media materialization can recover cloud-only attachments without enabling full raw payload export
- fast-history-derived raw `fileUuid` values are not interchangeable with public OneBot `get_image` / `get_file` identifiers; feeding raw `fileUuid` into those public actions will often produce NapCat-side `file not found`
- public OneBot `image` segment field `data.file` may only be a file name in resolved content trees, especially inside forwarded bundles; do not assume it is a valid public `file_id`
- for fast-history exports, context-based hydration through the repository fast plugin is the authoritative proactive media hydration path; public `get_image` / `get_file` should only be called when the plugin has emitted a NapCat-recognized `public_file_token`
- a running NapCat process can expose the older fast-history `/history` route while still missing the newer `/hydrate-media` route; in that mixed state, export code should log a single "restart NapCat to refresh plugin routes" warning and stop re-probing `/hydrate-media` for every asset
- after a real NapCat restart on the maintainer machine, `/hydrate-media` was confirmed live and a cautious `limit=20` export recovered recent `2026-03` image assets that had previously missed; treat "route 404 before restart, route works after restart" as a validated deployment pattern
- the same context-based hydration path now also applies to `sticker` / `marketface` assets once exporter normalization preserves raw message context on the sticker segment
- `sticker` / `marketface` recovery should not rely solely on empty local `Emoji/marketface/<package_id>/...` directories; when local sticker files are absent, exporter normalization should preserve the public remote GIF URL and the exporter should download the native GIF payload directly
- raw-data rule: do not derive presentation-only sticker variants during export. Preserve the native media payload as-is and leave any static-preview rendering to downstream processing
- stale `source_path` mismatches across month directories are a confirmed real-world pattern; the same asset may be recorded under `emoji-recv/2026-01/...` while context hydration returns a valid local path under `emoji-recv/2026-02/...`
- therefore, exporter correctness depends on successfully consuming context-based `/hydrate-media` results, not on trusting the original month bucket in `source_path`
- aggressive "old month" hydration skip logic can create false misses for still-recoverable assets (for example `2026-01` images exported in mid-2026-03); remote hydration backoff must only apply to truly old buckets, not merely "older than a few weeks"
- relative media URLs such as `/download?...` must be resolved against the active NapCat HTTP base URL before remote fallback download is attempted
- after tightening the old-bucket threshold and resolving relative media URLs, a live `data_count=300 asJSONL` export on group `922065597` recovered `assets copied=93 reused=20 missing=0` over the window `2026-01-12 .. 2026-03-12`; treat this as the current post-regression fidelity baseline
- repeated references to the same underlying asset inside one export run must reuse a per-run resolution cache before re-entering local scans, legacy MD5 lookup, or NapCat hydration
- formal export now also keeps a shared per-run outcome cache keyed by stable asset identity rather than full message context alone; this lets repeated old image misses or repeated recovered assets skip re-entering NapCat hydration/public-token work when the same underlying asset reappears under different `message_id` / `element_id`
- shared miss caching is intentionally limited to older assets (`>=30d`) so recent lazily hydrated media still gets a chance to recover later in the same run
- maintainer live timing on `2026-03-14` for the current residual `10` old-image misses showed the effect of this hardening clearly:
  - first pass through `resolve_for_export`: `~775ms`
  - second pass in the same process: `~0.23ms`
  treat this as evidence that repeated old misses no longer re-enter expensive NapCat work within one export run
- NapCat/QQ media access model note:
  - QQ/NapCat do not appear to use exporter-side MD5 matching as a primary lookup strategy
  - the authoritative internal recovery path is message-context hydration through `downloadMedia(msgId, chatType, peerUid, elementId, ...)`
  - NapCat public `get_image` / `get_file` / `get_record` work only after a valid public media token has been created for that exact message context
  - exporter-side MD5 recovery exists only as a research fallback for stale legacy local caches whose file names are obfuscated (`Image/Group2`, etc.)
  - therefore, when improving fidelity or speed, prefer stronger context-based hydration coverage and plugin-issued public tokens before expanding MD5-based scanning
- local benchmark note:
  - the repository includes [scripts/benchmark_media_resolution.py](/d:/Coding_Project/IsThisShit/scripts/benchmark_media_resolution.py) to measure resolver hit rates and timing
  - benchmark modes now explicitly include:
    - `napcat_context_only`
    - `napcat_public_token`
    - `legacy_md5_research_only`
  - use this benchmark to compare true NapCat-context recovery against the legacy MD5 route; do not assume MD5 remains acceptable for production just because it is fast after warmup
  - live benchmark on `2026-03-13` against [group_922065597_20260313_011547.jsonl](/d:/Coding_Project/IsThisShit/exports/group_922065597_20260313_011547.jsonl) with `120` unique `image` assets and a logged-in NapCat session showed:
    - `napcat_context_only`: `74/120` hits, average `~364.5ms`, `p95 ~1222.8ms`
    - `napcat_public_token`: `74/120` hits, average `~24.9ms`, `p95 ~41.3ms`
    - `legacy_md5_research_only`: `68/120` hits, average `~43.0ms`
  - current interpretation: when a valid plugin-issued public token is available, public `get_image` materially outperforms direct context hydration while preserving the same hit rate on this sample
  - post-restart live benchmark on `2026-03-14` refined the remaining gaps:
    - fresh speech probe on [friend_1507833383_20260314_speech_probe.jsonl](/d:/Coding_Project/IsThisShit/exports/friend_1507833383_20260314_speech_probe.jsonl):
      - initial result before the latest fix:
        - `direct_local_precheck`: `1/1`
        - `napcat_context_only`: `1/1`, average `~19.5ms`
        - `napcat_public_token`: `0/1`
      - after switching the speech public-token branch to `get_record(..., out_format='mp3')`:
        - `napcat_context_only`: `1/1`, average `~7.959ms`
        - `napcat_public_token`: `1/1`, average `~12.917ms`
      - interpretation: recent speech hydration works, and `public token -> get_record` is now also confirmed on this runtime when `mp3` output is requested
    - older group speech sample on [group_922065597_20260307_163116.jsonl](/d:/Coding_Project/IsThisShit/exports/group_922065597_20260307_163116.jsonl) remains `0/8` on both NapCat routes even after the fix, and all tested misses fall in `91-180d` or `>180d` buckets; treat that remaining gap as age-biased availability rather than a recent-token regression
    - post-restart nested-forward image probe on [friend_1507833383_20260313_012225.jsonl](/d:/Coding_Project/IsThisShit/exports/friend_1507833383_20260313_012225.jsonl):
      - `napcat_context_only`: `2/8`
      - `napcat_public_token`: `2/8`
      - `legacy_md5_research_only`: `2/8`
      - interpretation: restart plus token fixes did not improve nested forwarded media recovery; the remaining blocker is still forwarded-bundle context, not top-level image hydration
  - additional `2026-03-13` live small-sample checks now show:
    - `file` on [group_922065597_20260313_011547.jsonl](/d:/Coding_Project/IsThisShit/exports/group_922065597_20260313_011547.jsonl): `napcat_context_only 2/2`, `napcat_public_token 2/2`, legacy not applicable
    - `video` on [group_922065597_20260310_002020.jsonl](/d:/Coding_Project/IsThisShit/exports/group_922065597_20260310_002020.jsonl): both NapCat routes `1/2` on the tested old sample, legacy not applicable
    - `speech` on [group_922065597_20260307_163116.jsonl](/d:/Coding_Project/IsThisShit/exports/group_922065597_20260307_163116.jsonl): both NapCat routes `0/8`
    - nested `forward` images on [friend_1507833383_20260313_012225.jsonl](/d:/Coding_Project/IsThisShit/exports/friend_1507833383_20260313_012225.jsonl): both NapCat routes `2/8`
- `sticker` route has now been narrowed by live verification on `2026-03-14`:
  - fast plugin hydration for `marketface` now returns immediate local-path hints plus deterministic remote GIF URL without entering `downloadMedia(...)`
  - maintainer-side sticker benchmark on [media_resolution_group922_sticker_20260314_postrestart_fix.json](/d:/Coding_Project/IsThisShit/state/benchmarks/media_resolution_group922_sticker_20260314_postrestart_fix.json) now shows:
    - `direct_local_precheck`: `10/18`
    - `napcat_context_only`: `10/18`, avg `~5.4ms`
    - `napcat_public_token`: `0/18`, avg `~4.6ms`
  - direct public-token `get_image` on a live recent sticker token still hangs on the maintainer runtime, so formal export must not treat sticker public tokens as a production route
  - the current formal sticker route is therefore:
    - direct local `staticFacePath` / `dynamicFacePath`
    - otherwise preserved QQ raw300 GIF `remote_url`
    - never MD5/local-cache fallback in formal export
  - a fresh live `speech` probe on [friend_1507833383_20260314_speech_probe.jsonl](/d:/Coding_Project/IsThisShit/exports/friend_1507833383_20260314_speech_probe.jsonl) now shows `napcat_context_only 1/1` in `~12.0ms`, while `napcat_public_token` still returns `0/1`; current interpretation is that recent speech recovery itself works, but the public-token `get_record` branch is still not wired correctly for speech
  - NapCat live logs on `2026-03-14` further narrowed the remaining speech/forward issues:
    - plugin `/hydrate-forward-media` can still be called with malformed forward-parent hints that omit `element_id`; exporter must skip the route entirely in that case instead of producing repeated `element_id is required` route noise
    - for raw speech export, passing `out_format=amr` into public `get_record` can trigger NapCat FFmpeg conversion attempts (`*.amr -> *.amr.amr`) and fail with `Encoder not found`
    - current maintainer-side runtime instead accepts `get_record(..., out_format='mp3')`; formal exporter now uses that only for the speech public-token branch, while direct local-path/context hydration still preserves the original payload when available
    - speech public tokens should follow NapCat core's own pattern and be generated from full message context plus `fileName` as the custom token payload, not from raw `pttElement.fileUuid`
  - live debug on `2026-03-14` against `get_friend_msg_history(user_id=1507833383, count=5, parse_mult_msg=true)` confirmed that NapCat public history already returns recursively expanded `forward.data.content` trees for this friend sample, including a nested `forward` inside another `forward`
  - on the same sample, public `get_forward_msg` succeeds for outer forward ids (`7616346896018481986`, `7616347227538700777`) but fails for nested inner ids such as `7616347227538700784` and `7616347227538700788` with `消息已过期或者为内层消息，无法获取转发消息`
  - plugin `/hydrate-forward-media` on outer-context payload `message_id_raw=7616346896018481986`, `element_id=7616346896018481985`, `peer_uid=u_PILHFfCbozu1GXYD_BVW7g`, `chat_type_raw=1` still hung until client read-timeout (`>120s`), so current nested-forward media misses are not explainable as simple tree-expansion failure
  - `forward nested media` means media segments that live inside forwarded chat bundles, especially when one forwarded message itself contains another forwarded bundle with its own image/file/share content
  - current unresolved `forward nested media` misses are therefore not "ordinary top-level image" failures; they are failures to hydrate media that only exists inside recursively expanded forwarded records
  - maintainer-side live probing on friend `1507833383` now shows a split:
    - top-level image URLs can already be dead even when local path/token exist
    - but the tested `forward` / `nested-forward` image URLs were live and directly downloadable while no public token/path was exposed
  - current formal interpretation is therefore:
    - do not globally prefer media `url`
    - but for forwarded-bundle media with no stable path/token, a still-live NapCat-provided `url` is an acceptable strict-NapCat recovery path before declaring `missing_after_napcat`
  - after wiring that rule directly into formal exporter resolution for forward-parent cases, a live `export-history private 1507833383 --limit 5` run on `2026-03-14` dropped from `missing=6` to `missing=0` and completed in about `5.7s`; the same `8` assets now resolve as `copied=5 reused=3 missing=0`, with forwarded images using resolver `napcat_forward_remote_url`
- export progress callbacks for `materialize_assets` should be time-throttled and step-throttled; refreshing the terminal on every asset can become a visible bottleneck during large exports
- the currently covered proactive-hydration media families are:
  - `image`
  - `file`
  - `video`
  - `speech`
  - `sticker` / `marketface`
- export completion followup must be decided from the accepted completion token, not just from the final full command line
- accepting `data_count=` or `asTXT` / `asJSONL` must terminate completion chaining immediately
- `Left` / `Right` cursor movement in export inputs must cancel any lingering completion menu before moving the cursor
- when `data_count` is provided without an explicit time interval, export semantics are "from `@final_content` backward across as many history pages as needed" rather than "single latest page only"
- watch-mode export summary should stay compact enough for a single status/help line; detailed per-type counts remain in the manifest and root-CLI multiline summary
- if watch-mode header/footer text exceeds terminal width, prefer multi-line wrapping over truncation; export result feedback must not silently drop information from display
- watch-mode export summary should explicitly report:
  - export time range for the selected slice
  - per-content exported counts for every normalized segment type
  - per-asset materialization in `actual/expected miss err` form
- live watch export progress should separate:
  - `page_window=...` for the currently scanned history page
  - final `time_range=...` in the completed export summary
  - post-scan `materialize_assets` progress so long media recovery phases are visible instead of appearing stalled
- very recent NTQQ image messages can still report an `Ori/...` source path even when the original file has not been hydrated to disk yet; if some images from the same minute or same multi-image message resolve while others do not, treat that first as a "not yet locally materialized" case before assuming resolver failure
- a real `data_count=300` mixed export smoke on the maintainer machine has now reached `miss=0` for `image`, `file`, `sticker.static`, and `sticker.dynamic` after resolver fixes plus QQ-side hydration of lazy local assets; treat that as the current extraction-side fidelity baseline for routine regression checks
- after the `2026-03-14` sticker-route fix, a fresh mixed export on `group_922065597` changed from:
  - [group_922065597_20260314_160344.manifest.json](/d:/Coding_Project/IsThisShit/exports/group_922065597_20260314_160344.manifest.json): `copied=36 reused=11 missing=25`, including `8` sticker misses
  to:
  - [group_922065597_20260314_163723.manifest.json](/d:/Coding_Project/IsThisShit/exports/group_922065597_20260314_163723.manifest.json): `copied=44 reused=13 missing=15`
  - all `sticker` misses were eliminated; the remaining `15` misses are all `image`
- one more `2026-03-14` maintainer-side fix then narrowed ordinary top-level `image` handling further:
- when plugin hydration plus public `get_image` succeed but return only a remote `multimedia.nt.qq.com.cn` URL rather than a local `file`, formal export now downloads that public-token URL into the remote-media cache and records resolver `napcat_public_token_get_image_remote_url`
- for stale top-level NTQQ `Pic/<month>/Ori/...` image paths, formal export now also probes only the immediate sibling `OriTemp/` and `Thumb/` directories for the same stem before declaring `missing_after_napcat`; this is a narrow local-neighbor fallback, not a general cache scan
- maintainer live verification on `2026-03-14` against a `group 922065597 data_count=2000 page_size=500` export further refined old-image miss handling:
  - representative `2025-09` misses often do **not** expose an expired-like remote URL in `/hydrate-media`
  - instead, `/hydrate-media` returns a stale local `Pic/<month>/Ori/...` path plus a valid `public_file_token`
  - only the second hop `public token -> get_image` reveals a cloud URL such as `https://gchat.qpic.cn/...`, and that cloud URL can already be dead
  - formal exporter now classifies that pattern as `qq_expired_after_napcat` for sufficiently old buckets instead of leaving it as generic `missing_after_napcat`
  - current maintainer probe result:
    - [debug_probe_group_922065597_20260314_195933_pagesize500_full.json](/d:/Coding_Project/IsThisShit/state/export_perf/debug_probe_group_922065597_20260314_195933_pagesize500_full.json)
    - `229` total missing assets
    - `134` classified as `qq_expired_after_napcat`
    - `95` remaining as generic `missing_after_napcat`
  - practical interpretation:
    - `2025-09` / `2025-10` old `ntqq_pic` misses are now mostly confirmed QQ-expired rather than unresolved exporter bugs
    - remaining generic misses are concentrated in newer residual buckets such as `2026-01 emoji-recv`
- root CLI note on `2026-03-14`:
  - the standalone `app.py export-history ... --limit N` command must not rely on a single history page when `N > 200`
  - the vendored fast-history plugin clamps one page to `MAX_PAGE_SIZE = 200`, so large CLI exports must use the same cross-page tail path as root REPL/watch
  - after wiring CLI `--limit > 200` to `fetch_snapshot_tail(..., data_count=limit, page_size=min(limit, 500))`, a live rerun of `export-history group 922065597 --limit 2000 --format txt` on `2026-03-14` produced:
    - [group_922065597_20260314_215230.manifest.json](/d:/Coding_Project/IsThisShit/exports/group_922065597_20260314_215230.manifest.json)
    - `record_count = 2000`
    - `copied = 264`
    - `reused = 128`
    - `missing = 172`
    - `missing_breakdown = {qq_expired_after_napcat: 169, missing_after_napcat: 3}`
  - a follow-up live rerun after classifying stale forwarded residual images produced:
    - [group_922065597_20260314_220705.manifest.json](/d:/Coding_Project/IsThisShit/exports/group_922065597_20260314_220705.manifest.json)
    - `record_count = 2000`
    - `copied = 264`
    - `reused = 128`
    - `missing = 172`
    - all `172/172` missing assets now resolve to `qq_expired_after_napcat`
  - a later root-REPL perf trace on [root_export_group_922065597_20260314_223522.jsonl](/d:/Coding_Project/IsThisShit/state/export_perf/root_export_group_922065597_20260314_223522.jsonl) showed that a perceived "stuck at 399/564" pause was actually a `60.1251s` stall on step `401`, file `3BE10FA97950F66D11876F8E815A763C.gif`
  - the stall source was not local cache search breadth; it was an old blank-source forwarded image still being sent through plugin `/hydrate-forward-media` and waiting for the fast-history client's default `60s` timeout
  - after moving stale blank-source forwarded-image expiry classification ahead of that route, a new trace on [root_export_group_922065597_20260314_224143.jsonl](/d:/Coding_Project/IsThisShit/state/export_perf/root_export_group_922065597_20260314_224143.jsonl) showed:
    - total export elapsed dropping from `86.75s` to `27.266s`
    - `slowest_materialize_step_s` dropping from `60.1251s` to `1.6836s`
    - the former `401` step shrinking to `0.1405s`
  - treat the latter as the current large-tail CLI baseline after the paging regression fix and stale-forward expiry classification
- a live `limit=300` mixed export on `2026-03-14` confirmed that `3D056A0F987123794BA2FA2C84A1E742.jpg` was recovered from `C:\QQ\3956020260\nt_qq\nt_data\Pic\2026-02\Thumb\3d056a0f987123794ba2fa2c84a1e742_720.jpg`, reducing the mixed-export miss count from `14` to `10`
- final maintainer-side image benchmark on [group_922065597_20260314_170925.jsonl](/d:/Coding_Project/IsThisShit/exports/group_922065597_20260314_170925.jsonl) with `60` unique image assets and live NapCat on `2026-03-14` showed:
  - `direct_local_precheck`: `45/60`
  - `napcat_context_only`: `45/60`, average `~10.8ms`
  - `napcat_public_token`: `45/60`, average `~35.2ms`
  - `legacy_md5_research_only`: `2/60`, average `~81.4ms`, max `~1992ms`
  current interpretation: for the current exporter baseline, public NapCat routes plus narrow local-neighbor fallback clearly outperform legacy MD5 as a production strategy
- current branch decision:
  - keep the vendored NapCat working branch for now because formal export still depends on the fast plugin routes and plugin-issued metadata
  - do not keep investing in a separate "NapCat core direct-download" line unless a future unresolved blocker proves the plugin/public-route design insufficient
  - a fresh rerun on [group_922065597_20260314_164529.manifest.json](/d:/Coding_Project/IsThisShit/exports/group_922065597_20260314_164529.manifest.json) reduced the remaining mixed-export misses from `15` to `14`
  - a direct live probe on representative miss `95EA10C2DD53F66B9F480DC1980FD340.jpg` showed:
    - plugin `/hydrate-media` returned a valid contextual public token
    - `get_image(token)` succeeded but returned `file=""` plus a remote `multimedia.nt.qq.com.cn` URL
    - that remote URL responded `404` and did not materialize the old local cache path
  - current interpretation: after this fix, at least part of the residual `2026-01/2026-02` miss set reflects assets NapCat can still describe contextually but cannot actually retrieve under the current QQ/NapCat state
- a large remote full-history sample (`group_763328502_20260310_195324.*`) showed that extraction misses are strongly age- and cache-family-dependent rather than random:
  - images newer than `<=7d` had `missing=0`
  - `8-30d` images were mixed but still partially recoverable
  - `31-90d` and older images dominated the missing set
  - `ntqq_pic` originals were dramatically more failure-prone than `emoji-recv`
  - `emoji-recv` contributed most `reused` hits because repeated meme images reappeared often
  use this to distinguish "recent hydration failure" from "old original cache erosion" when debugging remote exports
- a follow-up remote run on the newer extractor (`group_763328502_20260310_204453.*`) improved copied/reused counts but did not materially change the overall pattern:
  - `image missing` still concentrated in `2025-12` to `2026-01` and other older windows rather than recent days
  - `<=7d` remained effectively healthy
  - most misses were still plain single-image messages rather than special segment families
  - when validating remote exports, ask operators to manually open a few representative missing images in QQ and report whether QQ shows them normally, marks them expired, or reveals a concrete local path
- a renewed maintainer-side `data_count=2000` export on `group_922065597` reached only `5` image misses out of `518` expected images; all `5` misses were embedded inside two older forwarded-chat bundles (`2025-12-14` and `2026-01-10`) rather than ordinary top-level image messages
- treat "residual misses limited to images nested inside old forwarded records" as a distinct remaining fidelity class from ordinary recent-image extraction; fixing it likely requires deeper forwarded-image hydration rather than more root-cache path scanning

The manifest must record at least:

- source data file name
- export chat ID and name snapshot
- record count
- copied / reused / missing / error asset counts
- per-asset mapping from source path to exported relative path

Suggested QQ media root override:

- environment variable: `QQ_MEDIA_ROOTS`
- multiple roots may be separated with `;`

## CLI Contract

The CLI should run as an interactive REPL.

Top-level rules:

- every top-level command must start with `/`
- bare text at top level is invalid and should show a short hint such as `Use /help`
- bare text is allowed only inside follow-up selection prompts opened by a command
- CLI sessions should always write a file log under `state/logs/`
- shared extractor bundles should tell remote operators to send back `state/logs/cli_latest.log` together with any exported manifest when reporting crashes or unexplained exits
- root REPL/watch exports should also persist a perf trace under `state/export_perf/`; current traces now include per-asset `materialize_asset_step` start/done events with `step_elapsed_s`, `status`, `resolver`, and a rolled-up `slowest_materialize_step` summary in `export_complete`

Minimum commands:

- `/help`
- `/groups [keyword]`
- `/friends [keyword]`
- `/watch group <group-name-or-id>`
- `/watch friend <friend-name-or-id>`
- `/export group <group-name-or-id> [--since ...] [--until ...] [--limit ...] [--out ...] [--resume]`
- `/export friend <friend-name-or-id> [--since ...] [--until ...] [--limit ...] [--out ...] [--resume]`
- `/export group_asBatch=<group1,group2,...> [<time-a> <time-b>] [data_count=NN]`
- `/export friend_asBatch=<friend1,friend2,...> [<time-a> <time-b>] [data_count=NN]`
- `/export_onlyText group|friend <target> [<time-a> <time-b>] [data_count=NN]`
- `/export_TextImage group|friend <target> [<time-a> <time-b>] [data_count=NN]`
- `/export_TextImageEmoji group|friend <target> [<time-a> <time-b>] [data_count=NN]`
- `/quit`

Recommended additions:

- `/recent`
- `/config`
- `/status`

## Completion And Selection UX

Use `prompt_toolkit` for interactive input.

Required behavior:

- fuzzy match group names and friend names from cached metadata
- show the top few matches while typing
- `Up` / `Down` moves the highlighted option
- `Tab` completes the current highlighted option
- `Enter` accepts the highlighted option, or executes if the line is already complete
- `Esc` cancels the current selection prompt

When the same display name maps to multiple QQ IDs, the suggestion list must show both name and ID.

## Watch Output Format

Realtime debug output should stay compact and stable:

```text
2026-03-06T20:31:02+08:00 group=123456789 sender=987654321 content=今天好累 [image] [meme or emoji]
2026-03-06T20:31:08+08:00 private=2468101214 sender=2468101214 content=语音给你了 [speech audio]
```

Do not print raw JSON by default in watch mode.

## Export Rules

- default output: one JSONL file per export task
- export profile commands:
  - `/export`
    - keep all normalized segment types
  - `/export_onlyText`
    - keep only text segments
  - `/export_TextImage`
    - keep only text and image segments
  - `/export_TextImageEmoji`
    - keep only text, image, emoji, and sticker segments
- file name pattern:
  - `group_<group_id>_<YYYYMMDD_HHMMSS>.jsonl`
  - `friend_<user_id>_<YYYYMMDD_HHMMSS>.jsonl`
- write a small manifest next to each export with:
  - target ID
  - target name snapshot
  - export time range
  - record count
  - NapCat endpoint used
- persist a resumable state file keyed by target ID and latest exported `message_seq`
- deduplicate by `message_seq` first, `message_id` second
- `data_count=NN` or `--data-count NN` means:
  - if no time interval is provided, export only the latest `NN` messages, counting backward from `@final_content`
  - if a closed interval is provided, scan that interval from the newest matching message backward and export only the latest `NN` messages inside the interval
  - if `data_count` is not provided, keep the previous behavior and export the full resolved interval
- NapCat history pages must be treated as order-unstable:
  - sort every fetched page by `(message time, stable message key)` before using page boundaries
  - apply that rule to:
    - bounds scans
    - closed-interval scans
    - interval-tail scans
    - latest-tail scans
    - full-history scans
  - never assume `page_messages[0]` and `page_messages[-1]` are oldest/newest until the page has been normalized
- after each export, print a compact extraction summary with:
  - total exported message count
  - source message count before profile trimming
  - requested `data_count` when applicable
  - per-segment-type counts
  - expected vs actual vs missing vs error asset counts for all materialized content types
- batch export uses a comma-separated target list inside `group_asBatch=` or `friend_asBatch=`
- root export parsing must tolerate space-split batch target names and keep merging batch-target tokens until a recognized time expression, format alias, or inline `data_count=` token begins
- top-level REPL export should expose the same staged progress visibility as watch-mode export:
  - history scan progress
  - data-file write progress
  - asset materialization progress
  - final multiline export summary
  - during an active root or batch export, staged progress should refresh in-place as a single dynamic status block rather than printing one log line per callback
- completion coverage requirements for export commands:
  - inline `data_count=` must be completable from `/export d...`
  - `Up` / `Down` must prioritize the active completion menu before any date-token rolling behavior
  - accepting `group_asBatch=` / `friend_asBatch=` must reopen same-token completion for the first batch target
  - typing `,` inside a batch target token should reopen completion for the next target fragment
  - batch completion menus must display only the remaining candidate target labels, not the already-typed `group_asBatch=` / `friend_asBatch=` prefix
  - batch completion menus must exclude targets that have already been selected earlier in the same comma-separated token
  - after a finished batch-target token and a following space, time-expression completion should start the same way as single-target export
- media fidelity requirements:
  - do not treat NTQQ `Thumb/` images as authoritative originals when a better `Ori/` or `OriTemp/` candidate exists
  - prefer original media over thumbnails for exported filenames and extension inference, especially for animated content
  - do not trust QQ-provided file extensions blindly; some QQ cached "jpg" files are actually GIF/WebP payloads and must be corrected from file magic during export

## Non-Goals For V1

- no GUI
- no dependency on `qq-chat-exporter`
- no automatic media copy/download pipeline
- no speech-to-text
- no full LLM analysis workflow in this repo
- no write-back actions such as sending messages

## Testing Expectations

- use `tests/fixtures/testChatRecord/` as local fixture material
- add unit tests for normalization of text/image/file/record/face/mface
- add tests for completion behavior and slash-command parsing
- add mocked NapCat integration tests for history pagination and event watch
- validate manually against a live NapCat build that matches the current QQ client

## Reference Notes

This spec is based on:

- NapCat official docs for connection modes and supported APIs
  - https://napcat.napneko.icu/config/basic
  - https://napcat.napneko.icu/develop/
- NapCat public OneBot message type definitions
- local sample chat records in `tests/fixtures/testChatRecord/`
