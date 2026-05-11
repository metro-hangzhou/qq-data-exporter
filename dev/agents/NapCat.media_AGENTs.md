# NapCat.media_AGENTs.md

> Last updated: 2026-03-14
> Scope: NapCat media semantics for this repository's exporter, including public actions, public tokens, URL/path behavior, speech, and forwarded media.

## Purpose

Use this file when the task involves:

- `get_image`
- `get_file`
- `get_record`
- `file_id` / public media token
- direct media URL vs local path vs plugin hydration
- `forward` / `nested-forward` media
- maintainer-side media benchmarks

## Repository-Level Route Decision

Formal exporter policy is now strict NapCat-only:

1. trust NapCat-provided direct local path first
2. otherwise ask the fast plugin to hydrate with full message context
3. if the plugin emits a NapCat-recognized public token, immediately call the matching public action:
   - `get_image`
   - `get_file`
   - `get_record`
4. if NapCat still cannot recover the asset, emit `missing_after_napcat`

Do not use local cache scan or MD5 recovery in formal CLI export.

Those remain benchmark/research tools only.

## Official Doc Facts That Matter

- `Official doc fact`
  - file handling docs explicitly allow direct use of message-provided `url`
- `Official doc fact`
  - when URL is expired or insufficient, NapCat documents public recovery actions such as:
    - `get_image`
    - `get_file`
    - `get_record`
- `Official doc fact`
  - message docs expose media-bearing nodes such as:
    - `image`
    - `record`
    - `video`
    - `file`
    - `mface`
    - `forward`
- `Official doc fact`
  - `forward` content trees are public protocol structures, not private internals

What official docs do not settle:

- how raw internal `fileUuid` relates to public `file_id`
- whether every nested-forward media node will retain enough context for later token minting

## Upstream Source Facts

- `Upstream source fact`
  - `downloadMedia(msgId, chatType, peerUid, elementId, ...)` is the native media recovery path
- `Upstream source fact`
  - `GetImage` is just a specialization of `GetFileBase`
- `Upstream source fact`
  - `GetFileBase` first tries to decode a NapCat-managed contextual token through `FileNapCatOneBotUUID.decode(...)`
- `Upstream source fact`
  - `GetRecord` adds optional conversion on top of `GetFileBase`
- `Upstream source fact`
  - public token generation is cache-backed and contextual, not a simple reversible transform of raw file name or raw `fileUuid`

## Public Token Rule

This is the key rule that replaced earlier guesswork:

- raw fast-history `fileUuid` is **not** a valid substitute for NapCat public token
- public `get_image/get_file/get_record` should only be called with a plugin-issued `public_file_token`

Why:

- public actions expect NapCat-managed contextual handles
- feeding raw internal ids into public actions only creates noisy `file not found` behavior and wasted time

## Top-Level Media Reality

For ordinary top-level message media, the strongest pattern now is:

- `path/sourcePath` when available
- otherwise context hydration
- otherwise plugin-issued public token plus public action

Maintainer-side live benchmark on ordinary image assets:

- sample: [group_922065597_20260313_011547.jsonl](../../exports/group_922065597_20260313_011547.jsonl)
- `120` unique images
- results:
  - `napcat_context_only`: `74/120`, average `~364.5ms`
  - `napcat_public_token`: `74/120`, average `~24.9ms`
  - `legacy_md5_research_only`: `68/120`, average `~43.0ms`

Current conclusion:

- for ordinary top-level image assets, `public token -> get_image` is the fastest verified NapCat-native route once the plugin can mint the token

## URL / Path / File Semantics

Do not conflate these fields:

- `path` / `sourcePath` / `filePath`
  - local resolved file path
- `url`
  - remote/download hint
  - can be valid right now, but not guaranteed durable forever
- `file`
  - usually file name or file identifier field in the message layer
  - not automatically a NapCat public token
- `file_id` / plugin-issued `public_file_token`
  - public-action input candidate

`Maintainer-side runtime finding`

- for `marketface` / sticker payloads on the maintainer runtime, a plugin-issued public token is not currently a usable production recovery route:
  - recent live probe after the `2026-03-14` plugin update showed `/hydrate-media` returning:
    - direct local sticker path when present
    - deterministic raw300 GIF `remote_url`
    - `public_action = get_image`
    - `public_file_token = <prefix-emojiId.gif>`
  - but a direct `get_image(file=<that token>)` call still hung until HTTP timeout
- practical rule for this repository:
  - keep emitting the token for research visibility
  - but formal export must skip sticker public-token recovery and use:
    - direct local `staticFacePath` / `dynamicFacePath`
    - otherwise the preserved raw300 GIF `remote_url`

## Maintainer-Side Direct URL Probe On Friend `1507833383`

Recent live probe on the last `5` friend messages established a crucial pattern:

- top-level image:
  - local path exists
  - public token exists
  - direct URL returned `404`
- top-level fresh speech:
  - local path exists
  - public token exists
  - direct URL returned `200`
- outer-forward and nested-forward images:
  - no public token was exposed in the public content tree
  - no authoritative local path was exposed there either
  - but direct URL returned `200` for all tested images

What this means:

- top-level media still prefers `path/token`
- forwarded media cannot currently assume that same route
- direct URL is not universally reliable
- but for current nested-forward image samples it is the only immediately verified public success path

## Fast-History Page Ceiling

- `Upstream source fact`
  - the vendored fast-history plugin currently caps `/history` page size at `200`
- `Maintainer-side runtime finding`
  - exporter progress previously reported `page_size=500` while the plugin still only returned about `200` messages, which made tail-scan speed look worse and delayed adaptive downshifts
- current repository rule:
  - bulk tail/full scans should clamp requested fast-history page size to `200`
  - progress output should report the effective page size, not the caller's aspirational count

## Forward And Nested-Forward Media

### What Is Already Confirmed

- `Maintainer-side runtime finding`
  - `get_friend_msg_history(..., parse_mult_msg=true)` on friend `1507833383` already exposes the nested `forward.data.content` tree
- `Maintainer-side runtime finding`
  - `get_forward_msg` succeeds for the outer forward ids on this sample
- `Maintainer-side runtime finding`
  - inner nested forward ids fail with:
    - `消息已过期或者为内层消息，无法获取转发消息`

Interpretation:

- the parse tree itself is not missing
- the remaining problem is media hydration inside that tree

### Current Blocker

Nested-forward media nodes often expose only:

- `file`
- `url`
- `file_size`

but not a stable public token or authoritative local path.

That means:

- structure is visible
- media node exists
- but it is not yet stably transformed into a download-ready NapCat handle

### Benchmark Snapshot

Nested-forward image probe on:

- [friend_1507833383_20260313_012225.jsonl](../../exports/friend_1507833383_20260313_012225.jsonl)

Results:

- `napcat_context_only`: `2/8`
- `napcat_public_token`: `2/8`
- `legacy_md5_research_only`: `2/8`

Current meaning:

- restart and token fixes did not improve nested-forward media
- the active blocker is forwarded-bundle media context, not tree expansion

### Current Practical Route

For forwarded media, the current likely formal path to test next is:

- path if present
- otherwise plugin hydration if bundle context is sufficient
- otherwise direct URL when the public content tree already exposes a live one
- otherwise `missing_after_napcat`

This is still narrower than saying “all media should prefer URL”.

It remains a forward/nested-forward-specific rule only.

`Maintainer-side runtime finding`

- this route is now wired into formal exporter behavior for forwarded media only:
  - if plugin-returned forwarded assets expose neither a stable local path nor a public token
  - but still expose a live NapCat media `url`
  - formal export may direct-download that URL into the exporter cache and mark resolver `napcat_forward_remote_url`
- `Maintainer-side runtime finding`
  - a fresh live `export-history private 1507833383 --limit 5` run on `2026-03-14` verified the rule end-to-end:
    - before the fix, the same slice produced `missing=6` and spent minutes stuck behind forwarded-media hydration timeouts
    - after the fix, the same slice completed in about `5.7s` with `copied=5 reused=3 missing=0`
    - all `6` previously missing forwarded/nested-forwarded images resolved through `napcat_forward_remote_url`

Related normalization correction:

- `Maintainer-side runtime finding`
  - public OneBot `image.data.file` inside resolved forward trees is often only the file name
  - exporter normalization must not silently promote that field into `file_id`

## Speech

### Source-Level Constraint

- `Upstream source fact`
  - `GetRecord` will transcode if `out_format` is passed

### Maintainer Runtime Constraint

- `Maintainer-side runtime finding`
  - passing `out_format=amr` in exporter hydration triggered FFmpeg attempts like:
    - `*.amr -> *.amr.amr`
  - NapCat then failed with:
    - `Encoder not found`
- `Maintainer-side runtime finding`
  - the same runtime accepts `get_record` when `out_format='mp3'`
  - live validation on `2026-03-14` succeeded with both:
    - `file='2dbb50fbe48500b541db718684b8c26e.amr', out_format='mp3'`
    - `file_id='2dbb50fbe48500b541db718684b8c26e.amr', out_format='mp3'`
  - the returned payload points at a local `*.amr.mp3` file and includes base64 content

Rule:

- if direct local path or context hydration already returns the original speech file, preserve that raw payload
- but when formal exporter must use the speech public-token route on this runtime, call `get_record(..., out_format='mp3')`
- do not request `amr` conversion through `get_record`

### Current Benchmark State

- old group sample:
  - [media_resolution_group922_speech_20260314_public_token_fix.json](../../state/benchmarks/media_resolution_group922_speech_20260314_public_token_fix.json)
  - `napcat_context_only`: `0/8`
  - `napcat_public_token`: `0/8`
  - all tested misses were in `91-180d` or `>180d` age buckets
  - fresh recent friend probe:
  - `napcat_context_only`: `1/1`

## Sticker / MarketFace

### Upstream Source Facts

- `Upstream source fact`
  - upstream `marketFaceElement` conversion encodes a contextual `FileNapCatOneBotUUID` and exposes the sticker as an `image`-like public media node, not as a separate `get_file` family
- `Upstream source fact`
  - upstream public file retrieval code path (`GetFileBase`) does explicitly recognize `marketFaceElement`

### Maintainer Runtime Findings

- `Maintainer-side runtime finding`
  - plugin-side `marketface` hydration should not call `downloadMedia(...)`; on the maintainer runtime that path can stall for tens of seconds on stickers
- `Maintainer-side runtime finding`
  - after aligning the fast plugin with upstream semantics, sticker hydration now returns immediately with:
    - direct local `staticFacePath` / `dynamicFacePath` when present
    - deterministic QQ raw300 GIF `remote_url`
    - research-only `public_action/public_file_token`
- `Maintainer-side runtime finding`
  - live benchmark on [media_resolution_group922_sticker_20260314_postrestart_fix.json](../../state/benchmarks/media_resolution_group922_sticker_20260314_postrestart_fix.json):
    - `direct_local_precheck`: `10/18`
    - `napcat_context_only`: `10/18`, avg `~5.385ms`
    - `napcat_public_token`: `0/18`, avg `~4.559ms`
    - no more minute-long hangs once formal exporter stopped trying sticker public tokens

### Current Repository Rule

- formal sticker export order:
  - direct local `staticFacePath` / `dynamicFacePath`
  - otherwise preserved raw300 GIF `remote_url`
  - do not use sticker public token in formal export
- benchmark interpretation:
  - `napcat_context_only` on sticker now measures "can plugin-surfaced local sticker paths be used directly"
- `napcat_public_token` on sticker is currently expected to stay unresolved on the maintainer runtime

## Top-Level Image Remote URL From Public Token

`Maintainer-side runtime finding`

- ordinary top-level image recovery still has a split outcome on older assets:
  - plugin `/hydrate-media` may return:
    - stale/nonexistent local `file`
    - valid contextual `public_file_token`
  - public `get_image(token)` may then succeed but return:
    - `file=""`
    - `url=https://multimedia.nt.qq.com.cn/download?...`
    - `file_size`
    - `file_name`
- this is not the same as sticker remote GIF fallback:
  - the token route is still required to mint the fresh multimedia URL
  - only after that should formal export direct-download the returned remote URL

Repository rule:

- for non-sticker `image` public-token payloads:
  - if `get_image(token)` returns a real local `file`, use it
  - if it returns only a remote `multimedia.nt.qq.com.cn` URL, formal export may download that URL into remote-media cache and record resolver `napcat_public_token_get_image_remote_url`
- for stale top-level NTQQ `Pic/<month>/Ori/...` image paths:
  - formal export may also probe only the immediate sibling `OriTemp/` and `Thumb/` directories for the same stem
  - prefer non-empty `OriTemp` over `Thumb`
  - within `Thumb`, prefer larger variants such as `_720` over `_0`
  - record resolver `stale_source_neighbor`
  - treat this as a narrow local-neighbor fallback, not as legacy cache scanning
- exporter performance hardening now also includes a shared per-run outcome cache keyed by stable asset identity:
  - repeated occurrences of the same old image asset should not re-enter `/hydrate-media`, public token resolution, or dead remote URL fetches just because `message_id` changed
  - shared miss caching only applies to older assets (`>=30d`) to avoid suppressing late hydration of recent media during the same export run
  - maintainer live timing on `2026-03-14` for the current residual `10` old-image misses:
    - first pass: `~775ms`
    - second pass in the same process: `~0.23ms`
    this confirms the cache is actually eliminating repeated old-miss work rather than only improving theory

Current limit:

- a representative remaining miss (`95EA10C2DD53F66B9F480DC1980FD340.jpg`) still produced a `404` on that returned remote URL and did not materialize the original local cache path
- so this route reduces some residual misses but does not guarantee recovery of all older `2026-01/2026-02` assets
- maintainer live validation on `2026-03-14` confirmed that `3D056A0F987123794BA2FA2C84A1E742.jpg` was exported from sibling `C:\\QQ\\3956020260\\nt_qq\\nt_data\\Pic\\2026-02\\Thumb\\3d056a0f987123794ba2fa2c84a1e742_720.jpg`, reducing the mixed-export residual miss set from `14` to `10`
- final maintainer-side image benchmark on `2026-03-14` against [group_922065597_20260314_170925.jsonl](../../exports/group_922065597_20260314_170925.jsonl) showed:
  - `napcat_context_only`: `45/60`, average `~10.8ms`
  - `napcat_public_token`: `45/60`, average `~35.2ms`
  - `legacy_md5_research_only`: `2/60`, average `~81.4ms`, max `~1992ms`
  interpretation: current formal export should stay on plugin/public-route media recovery plus narrow local-neighbor fallback; legacy MD5 remains research-only
  - `napcat_public_token`: `1/1`

### Expired-In-QQ Classification

`Maintainer-side runtime finding`

- live debug on `2026-03-14` against the large `group 922065597 data_count=2000 page_size=500` export showed that many old-image misses do not expose the "expired" signal in `/hydrate-media` itself
- the actual runtime shape for representative `2025-09` misses is:
  - `/hydrate-media` returns only a stale local `Pic/<month>/Ori/...` path plus a valid `public_file_token`
  - `public token -> get_image` then returns only a cloud URL such as `https://gchat.qpic.cn/...`
  - direct download of that cloud URL fails
- formal exporter now treats that second-hop failure as `qq_expired_after_napcat` for sufficiently old buckets rather than keeping it as generic `missing_after_napcat`
- current maintainer live probe after wiring this rule:
  - [debug_probe_group_922065597_20260314_195933_pagesize500_full.json](../../state/export_perf/debug_probe_group_922065597_20260314_195933_pagesize500_full.json)
  - `229` total missing assets
  - `134` now classified as `qq_expired_after_napcat`
  - `95` remain generic `missing_after_napcat`
- bucket split from that same probe:
  - `qq_expired_after_napcat` is dominated by `2025-09` / `2025-10` `ntqq_pic` images
  - the remaining `missing_after_napcat` set is dominated by `2026-01` `emoji-recv` plus some `2025-12` / `2026-02` residuals
- current interpretation:
  - the exporter can now distinguish "QQ itself appears expired" from "still unresolved for other reasons"
  - this should be surfaced in summaries and manifests rather than hidden under one generic missing bucket
- follow-up maintainer verification on `2026-03-14` after two rounds of manual QQ checks against residual `2025-12` / `2026-01` / `2026-02` images:
  - sampled residual images were all `QQ 过期` in the native client as well
  - exporter classification was widened carefully to stale assets (`>=30d`) rather than only `OLD_CONTEXT_BUCKET_MIN_AGE_DAYS`
  - recent `<30d` images still preserve the fresh-public-token retry path
  - after also fixing the standalone CLI paging regression (`--limit > 200` must use cross-page tail fetch because fast plugin `/history` clamps one page to `200`), a live rerun of:
    - `export-history group 922065597 --limit 2000 --format txt`
    produced:
    - [group_922065597_20260314_215230.manifest.json](../../exports/group_922065597_20260314_215230.manifest.json)
    - `record_count = 2000`
    - `missing = 172`
    - `missing_breakdown = {qq_expired_after_napcat: 169, missing_after_napcat: 3}`
  - after adding a cautious stale-forward residual expiry classification, a second live rerun produced:
    - [group_922065597_20260314_220705.manifest.json](../../exports/group_922065597_20260314_220705.manifest.json)
    - `record_count = 2000`
    - `missing = 172`
    - all `172/172` missing assets classify as `qq_expired_after_napcat`
  - current interpretation:
    - the entire remaining old-image tail on this sample is now explainable as QQ-expired rather than unresolved extractor behavior
    - the last generic `missing_after_napcat` forward residuals were old blank-source forwarded images and are now folded into the same expired bucket
  - maintainer perf follow-up on `2026-03-14` further clarified a major large-export stall source:
    - root trace [root_export_group_922065597_20260314_223522.jsonl](../../state/export_perf/root_export_group_922065597_20260314_223522.jsonl) showed a single `materialize_asset_step` for old blank-source forward image `3BE10FA97950F66D11876F8E815A763C.gif` taking `60.1251s`
    - this was not broad local cache search; it was plugin `/hydrate-forward-media` waiting until the fast-history client's default `60s` timeout
    - after moving stale blank-source forward-image expiry classification ahead of that route, rerun trace [root_export_group_922065597_20260314_224143.jsonl](../../state/export_perf/root_export_group_922065597_20260314_224143.jsonl) dropped:
      - total export elapsed from `86.75s` to `27.266s`
      - slowest materialization step from `60.1251s` to `1.6836s`
    - current interpretation: the dominant pathology there was route selection for stale forwarded residuals, not generic filesystem search strategy

Current interpretation:

- recent speech hydration itself works
- `public token -> get_record` also works on this runtime once exporter requests `out_format='mp3'`
- remaining speech risk is now mostly about older/unavailable samples, not the recent-token route

## Current Active Gaps

These are the remaining NapCat-only media gaps worth active engineering time:

- `speech public token -> get_record`
- nested-forward media token/path context
- deciding whether forward/nested-forward media should formally allow direct URL fallback
- benchmark-mode `sticker` parity with formal export
- one more recent-sample verification for `video`

## What To Avoid

- do not reintroduce MD5/local cache recovery into formal export
- do not assume any field named `file` is a valid public action token
- do not assume URL success on one media family automatically generalizes to all others
- do not confuse “tree parsed successfully” with “media recoverable successfully”
