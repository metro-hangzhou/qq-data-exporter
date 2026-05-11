# NapCat.source_AGENTs.md

> Last updated: 2026-03-14
> Scope: upstream source architecture map for `NapCatQQ` plus repository-relevant source facts.

## Purpose

This file answers:

- how the upstream source tree is laid out
- which packages own which responsibilities
- where message history, file download, public token, plugin, and WebUI behavior really live

It is the source-level companion to [NapCat.docs_AGENTs.md](NapCat.docs_AGENTs.md).

## Upstream Vs Local

- `Upstream source fact`
  - GitHub repo: `https://github.com/NapNeko/NapCatQQ`
- `Maintainer-side runtime finding`
  - local checkout path: [NapCatQQ](../../NapCatQQ)
- `Maintainer-side runtime finding`
  - local checkout is on `napcat-direct-media-hydration`
  - working tree is dirty
  - local history is shallow/grafted enough that it is not a complete historical archive

Rule:

- use local files for implementation reading
- but treat `origin/main` design intent, not local experimental edits, as upstream truth

## Repo Scale Snapshot

- `Community finding`
  - public GitHub snapshot during this pass showed roughly:
    - `4890 commits`
    - `458 releases`
    - `70 contributors`
    - `554 closed pull requests`

Implication:

- this is a mature and high-churn upstream, not a tiny toy codebase
- reading one or two files is never enough to understand behavior changes

## Package Map

Local `packages/` overview:

- `napcat-core`
  - raw session/core APIs
  - QQ-side message/file services
  - protocol/session listeners
- `napcat-onebot`
  - OneBot action layer
  - message parsing/conversion
  - network adapters
  - plugin manager
- `napcat-webui-backend`
  - WebUI-side HTTP/router/backend glue
- `napcat-webui-frontend`
  - browser UI
- `napcat-framework`
  - runtime/framework shell packaging
- `napcat-plugin-builtin`
  - plugin API reference-by-example
- `napcat-common`
  - shared helpers, cache, path, file-uuid
- `napcat-types`
  - typed exports and schema-like shared definitions
- other supporting packages
  - `napcat-protocol`
  - `napcat-schema`
  - `napcat-shell`
  - `napcat-native`
  - `napcat-rpc`
  - `napcat-image-size`
  - etc.

## Message Path

### Raw History Fetch

- `Upstream source fact`
  - raw message/history fetch begins in [msg.ts](../../NapCatQQ/packages/napcat-core/apis/msg.ts)
  - key method:
    - `getMsgHistory(...)`

### Public OneBot History Actions

- `Upstream source fact`
  - public history actions live in:
    - [GetGroupMsgHistory.ts](../../NapCatQQ/packages/napcat-onebot/action/go-cqhttp/GetGroupMsgHistory.ts)
    - [GetFriendMsgHistory.ts](../../NapCatQQ/packages/napcat-onebot/action/go-cqhttp/GetFriendMsgHistory.ts)
- `Upstream source fact`
  - those actions fetch raw history and then loop messages through `parseMessage(...)`

### Message Parsing

- `Upstream source fact`
  - main parse path lives in [api/msg.ts](../../NapCatQQ/packages/napcat-onebot/api/msg.ts)
  - notable functions:
    - `parseMessage(...)`
    - `parseMessageV2(...)`
    - `parseMessageSegments(...)`
- `Upstream source fact`
  - merged-forward expansion, reply fallback, and media URL acquisition all sit on this hot path

Repo implication:

- our fast-history plugin is justified by source layout, not by guesswork
- the expensive part is not Python normalization; it is upstream per-message parsing on the public action path

## Forward Path

- `Upstream source fact`
  - public forward resolution lives in [GetForwardMsg.ts](../../NapCatQQ/packages/napcat-onebot/action/go-cqhttp/GetForwardMsg.ts)
- `Upstream source fact`
  - `get_forward_msg` itself reconstructs a forward message tree by mixing:
    - fake forward message parsing
    - raw history lookup
    - recursive parse

Repo implication:

- outer forward ids are a real public entry point
- nested forward ids are not guaranteed to behave as independently re-queryable roots just because they appear inside the content tree

## File And Media Path

### downloadMedia Is The Native Core Route

- `Upstream source fact`
  - authoritative media download/hydration lives in [file.ts](../../NapCatQQ/packages/napcat-core/apis/file.ts)
  - key method:
    - `downloadMedia(msgId, chatType, peerUid, elementId, ...)`

This is the core reason the repository now treats message-context hydration as the real media path.

### Public File Actions

- `Upstream source fact`
  - public actions live in:
    - [GetImage.ts](../../NapCatQQ/packages/napcat-onebot/action/file/GetImage.ts)
    - [GetFile.ts](../../NapCatQQ/packages/napcat-onebot/action/file/GetFile.ts)
    - [GetRecord.ts](../../NapCatQQ/packages/napcat-onebot/action/file/GetRecord.ts)
- `Upstream source fact`
  - `GetImage` is just a specialization of `GetFileBase`
- `Upstream source fact`
  - `GetFileBase` first tries to decode a NapCat-managed file token through `FileNapCatOneBotUUID.decode(...)`
- `Upstream source fact`
  - if that succeeds, it re-enters `downloadMedia(...)`, then reads the refreshed message/element state
- `Upstream source fact`
  - if token decode fails, it may fall back to model-id mode or name search mode

What this resolves:

- public `get_image/get_file` are not magic arbitrary-id lookups
- they are token-driven adapters back into the context-aware media path

### Speech Conversion

- `Upstream source fact`
  - [GetRecord.ts](../../NapCatQQ/packages/napcat-onebot/action/file/GetRecord.ts) extends `GetFileBase`
- `Upstream source fact`
  - `out_format` triggers FFmpeg conversion on top of the resolved file

Repo implication:

- raw speech export should omit `out_format`
- otherwise exporter may accidentally invoke transcode behavior instead of plain retrieval

## Public Token Design

- `Upstream source fact`
  - token logic lives in [file-uuid.ts](../../NapCatQQ/packages/napcat-common/src/file-uuid.ts)
- `Upstream source fact`
  - `FileNapCatOneBotUUID` uses an in-process time-based cache keyed by generated UUIDs
- `Upstream source fact`
  - the cache has bounded capacity and TTL semantics
  - default wrapper TTL is `86400000`
- `Upstream source fact`
  - `encode(...)` stores:
    - `peer`
    - `msgId`
    - `elementId`
    - optional `fileUUID`
    - optional custom payload
- `Upstream source fact`
  - `encodeModelId(...)` is a separate model-id/file-id variant

What this corrects:

- public file tokens are not raw `fileUuid`
- they are cache-backed contextual handles minted by NapCat

## Plugin System

### Where It Lives

- `Upstream source fact`
  - plugin contracts live in [types.ts](../../NapCatQQ/packages/napcat-onebot/network/plugin/types.ts)
- `Upstream source fact`
  - plugin lifecycle and loading live in [plugin-manger.ts](../../NapCatQQ/packages/napcat-onebot/network/plugin-manger.ts)

### What The Plugin Context Gives You

- `Upstream source fact`
  - plugin context exposes:
    - `core`
    - `oneBot`
    - `actions`
    - `router`
    - `logger`
    - config/data paths
    - `getPluginExports(...)`
- `Upstream source fact`
  - route families are split into:
    - authenticated `/api/Plugin/ext/{pluginId}/...`
    - unauthenticated `/plugin/{pluginId}/api/...`

### Builtin Plugin As Canonical Example

- `Upstream source fact`
  - [napcat-plugin-builtin/index.ts](../../NapCatQQ/packages/napcat-plugin-builtin/index.ts) demonstrates:
    - config UI
    - static files
    - no-auth and auth API routes
    - plugin-to-plugin export lookup
    - page registration

Repo implication:

- our fast plugin is aligned with upstream plugin architecture
- if a NapCat extension can be done via:
  - `ctx.actions`
  - `ctx.router`
  - context hydration
  then that should be preferred before touching core

## WebUI And Framework

- `Upstream source fact`
  - `napcat-webui-backend` is a thin backend package with its own router and WebUI config
- `Upstream source fact`
  - `napcat-framework` packages the runtime/framework shell layer rather than business logic itself

Repo implication:

- WebUI behavior and framework packaging are distinct concerns from message/file semantics
- exporter bugs should not be blamed on framework packaging unless startup/config evidence points there

## Source Facts That Matter Most For This Repository

- `Upstream source fact`
  - `downloadMedia(...)` is the real authoritative media path
- `Upstream source fact`
  - public `get_image/get_file/get_record` expect NapCat-managed contextual handles
- `Upstream source fact`
  - `GetRecord` can transcode; it is not retrieval-only
- `Upstream source fact`
  - merged-forward parsing is part of public message parsing, not a separate exporter feature
- `Upstream source fact`
  - plugins are powerful enough to expose custom local routes and reuse action/core context directly

## Unresolved Or Caution Points

- `Unresolved`
  - exact best-practice token generation for every media family from plugin-side code still needs per-family runtime confirmation
- `Unresolved`
  - source alone does not tell us which nested-forward media nodes are guaranteed to keep enough context for later hydration
- `Maintainer-side runtime finding`
  - the local checkout is dirty and therefore unsuitable as a clean upstream-diff source without manual caution
