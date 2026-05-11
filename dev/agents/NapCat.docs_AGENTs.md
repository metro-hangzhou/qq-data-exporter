# NapCat.docs_AGENTs.md

> Last updated: 2026-03-14
> Scope: first-pass full-site reading of the official NapCat docs at `https://napneko.github.io/`.

## Purpose

This file records what the official NapCat documentation site explicitly says.

It is not the place for:

- local exporter benchmark conclusions
- Python-side workaround history
- speculative interpretations disguised as official behavior

Use [NapCat.source_AGENTs.md](NapCat.source_AGENTs.md) for code truth and [NapCat.media_AGENTs.md](NapCat.media_AGENTs.md) for exporter-facing media strategy.

## Official Site Map

First-pass audited areas in this snapshot:

- quick start / introduction
  - `什么是 NapCatQQ`
  - `快速开始`
  - `启动方式`
  - `接入框架`
  - `社区资源`
- configuration
  - `基础配置`
  - `高级配置`
  - `安全`
- OneBot / protocol surface
  - `网络通讯`
  - `协议概述`
  - `请求接口`
  - `上报事件`
  - `消息类型`
  - `消息元素定义`
  - `处理文件`
  - `资源和消息 ID 的设计`
- plugin system
  - `插件机制原理`
  - `项目结构`
  - `配置与 WebUI`
  - `进阶功能`
  - `热重载开发`
  - `发布插件`
  - `API 文档`

Representative canonical pages used in this pass:

- `https://napneko.github.io/guide/what-is-napcat`
- `https://napneko.github.io/guide/boot/Shell`
- `https://napneko.github.io/guide/boot/Framework`
- `https://napneko.github.io/config/basic`
- `https://napneko.github.io/config/advanced`
- `https://napneko.github.io/config/security`
- `https://napneko.github.io/develop/api`
- `https://napneko.github.io/develop/msg`
- `https://napneko.github.io/develop/file`
- `https://napneko.github.io/develop/plugin/guide`
- `https://napneko.github.io/develop/plugin/api/doc`

## Official Doc Facts

### Identity And Positioning

- `Official doc fact`
  - NapCat is described as a modern protocol-end framework built on NTQQ.
- `Official doc fact`
  - The upstream README and docs place strong emphasis on:
    - release-first onboarding
    - WebUI-assisted usage
    - framework integration rather than one-off exporter use

What this corrects:

- NapCat is not documented as a thin file-dumper or exporter SDK.
- Its official self-image is a protocol/framework platform with WebUI, plugin, and adapter surfaces.

### Startup And Access

- `Official doc fact`
  - The docs split startup into:
    - Shell mode
    - framework access / third-party bot-framework integration
- `Official doc fact`
  - `接入框架` explicitly documents token usage and mentions `403` as the typical symptom when auth is wrong.
- `Official doc fact`
  - The docs emphasize copying the OneBot / framework connection parameters from WebUI rather than guessing them.

What this means here:

- our repository should keep treating WebUI config/token state as a first-class diagnosis surface
- `403` and token mismatches are official, expected failure modes, not custom exporter bugs

### Basic Configuration

- `Official doc fact`
  - `基础配置` covers multiple network modes such as HTTP server, WebSocket variants, and related toggles.
- `Official doc fact`
  - Token auth is part of the documented baseline setup.
- `Official doc fact`
  - HTTP-related features are not presented as exporter-only features; they are part of normal OneBot deployment shape.

Repo interpretation:

- our current preference for forward WebSocket plus HTTP fallback remains aligned with the official product surface

### Advanced Configuration

- `Official doc fact`
  - `高级配置` documents advanced knobs such as `PacketBackend`, `ffmpeg`, `musicSignUrl`, and `enableLocalFile2Url`.
- `Official doc fact`
  - Advanced config is where NapCat documents optional local-file to URL exposure, extra packet backend behaviors, and media-related helpers.

What this corrects:

- file and media behavior is not controlled only by message parsing; some behavior is config-sensitive
- if exporter results differ across machines, advanced config is an official place to inspect before blaming parser logic

### Security

- `Official doc fact`
  - The docs explicitly call out security concerns around defaults and deployment exposure.
- `Official doc fact`
  - The security guidance exists as its own section, not only as scattered config hints.

Repo implication:

- local unauthenticated plugin routes remain acceptable only because this repository keeps them on localhost and for controlled maintainer usage
- never document no-auth plugin routes as internet-safe

### Request Interfaces

- `Official doc fact`
  - The official site treats `请求接口` as the main action surface and documents compatibility at the API level.
- `Official doc fact`
  - file-related public actions such as `get_image`, `get_file`, and `get_record` are official public interfaces, not unofficial side channels.
- `Official doc fact`
  - the request docs also document stream-oriented capabilities, which means NapCat officially exposes more than one synchronous request style

What this corrects:

- when we use public `get_image/get_file/get_record`, we are using documented product surface
- but the docs still do not guarantee that any arbitrary raw internal id is valid input for those actions

### Event Model

- `Official doc fact`
  - The docs expose `上报事件` and related protocol/event pages as separate public contract material.
- `Official doc fact`
  - the event docs explicitly point users to source code for the most accurate view in some areas

What this means:

- official docs themselves acknowledge that docs alone are not always sufficient for exact event-field truth
- this justifies the repository rule of pairing docs with upstream source reading instead of stopping at prose pages

### Message Types

- `Official doc fact`
  - The official message docs enumerate media-bearing and structured types such as:
    - `image`
    - `record`
    - `video`
    - `file`
    - `mface`
    - `forward`
    - `node`
- `Official doc fact`
  - `forward` is documented structurally as a message whose `data.content` contains received message nodes.
- `Official doc fact`
  - message-type docs also preserve fields like `file_id`, `url`, and `path` where the protocol exposes them.

What this corrected for this repo:

- nested `forward` trees are officially part of the public message shape
- media nodes inside those trees are therefore not “hidden private internals” at the content-tree layer
- the real hard part is not tree parsing but turning inner media nodes into stable downloadable assets

### File Handling

- `Official doc fact`
  - `处理文件` explicitly documents “通常方法获取直链和文件下载”.
- `Official doc fact`
  - the docs describe direct use of message-provided `url` where available.
- `Official doc fact`
  - when message URLs expire, the docs direct users toward public file interfaces such as `get_image`, `get_file`, and `get_record`.
- `Official doc fact`
  - the file page also documents resource/message-id design and cache/LRU-style limitations rather than implying infinite permanence.

What this corrected for this repo:

- direct URL download is absolutely part of the official file-handling story
- this is exactly why maintainer-side live tests later found some nested-forward image URLs usable even when path/token were absent
- but official docs do not say every URL is durable forever; expired URLs remain an official possibility

### Plugin System

- `Official doc fact`
  - the plugin docs describe plugins as a first-class supported extension surface, not a hack layer
- `Official doc fact`
  - `API 文档` exposes:
    - `ctx.actions.call`
    - `ctx.router`
    - authenticated and unauthenticated route registration
    - plugin config UI hooks
    - page/static/memory-file registration
- `Official doc fact`
  - plugin docs also document hot reload, project structure, publishing, and WebUI/config integration

What this corrected for this repo:

- many exporter-side NapCat experiments should be attempted as plugin work before considering core surgery
- our fast plugin path is aligned with the official extension model, not fighting it

## Docs Do Not Specify Clearly

These are the biggest official-doc gaps that still require source or runtime verification:

- `Docs do not specify`
  - exact transform from raw QQ/NapCat internal media ids such as `fileUuid` into public `file_id` / public token
- `Docs do not specify`
  - exact TTL / cache invalidation behavior for public media tokens beyond the high-level resource/message-id design
- `Docs do not specify`
  - reliable nested-forward media hydration semantics once only the expanded content tree remains
- `Docs do not specify`
  - whether `record` public token flow should differ from image/file token flow in practice
- `Docs do not specify`
  - benchmark or latency expectations between:
    - direct URL
    - context hydration
    - public token plus public `get_*`

## High-Value Repo Implications

- do not re-debug NapCat file/media behavior from memory when `处理文件` already documents URL-first and public `get_*` recovery
- do not treat plugin development as an exotic last resort; it is part of official NapCat design
- do not assume docs alone settle field-level truth for events and resources; official docs themselves point back to source in some areas
- do not infer that every public file action accepts raw internal ids; the docs describe the actions, not a guarantee that any arbitrary internal id is valid input

## Coverage Ledger

First-pass coverage completed in this snapshot:

- [x] introduction / quick-start family
- [x] startup and framework access
- [x] basic and advanced config
- [x] security page
- [x] request interface overview
- [x] event/message/file pages
- [x] plugin docs family

Still worth revisiting later in deeper passes:

- [ ] protocol difference pages field by field
- [ ] every plugin subpage with example-level extraction into reusable repo patterns
- [ ] every event subtype page with exporter-relevant annotations
