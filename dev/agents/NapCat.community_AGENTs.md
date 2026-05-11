# NapCat.community_AGENTs.md

> Last updated: 2026-03-14
> Scope: GitHub-facing community snapshot for NapCatQQ issues, pull requests, and discussions.

## Purpose

This file is not a full transcript archive.

It is a theme map of what the public GitHub surface shows operators and contributors repeatedly struggling with.

Use it to answer:

- where the community actually spends attention
- which misunderstandings recur
- which official-doc gaps keep resurfacing in support traffic

## Snapshot Scope

This `2026-03-14` pass used:

- public GitHub repo homepage
- public pull-request listing metadata
- public discussions index and category pages
- visible issue pages and search hits for representative topics

Important limitation:

- this is a first-pass thematic scan, not an issue-by-issue or PR-by-PR transcript dump
- future deeper indexing belongs in [TODOs.napcat-research.md](../todos/TODOs.napcat-research.md)

## Repo Scale Snapshot

- `Community finding`
  - public GitHub repo snapshot showed roughly:
    - `7.9k stars`
    - `662 forks`
    - `4890 commits`
    - `458 releases`
    - `70 contributors`
    - `554 closed pull requests`

What this means:

- NapCat is not a niche one-maintainer side project anymore
- the community surface contains enough churn that “I remember how it worked” is not a safe debugging strategy

## Discussion Categories Observed

- `Community finding`
  - public discussions categories visible in the snapshot included:
    - `Announcements`
    - `General`
    - `Ideas`
    - `Q&A`
    - `Show and tell`

Current interpretation:

- operator-facing and maintainer-facing knowledge is concentrated in discussions more than in classic issue tickets
- when researching “how people actually use NapCat” or “what keeps breaking for operators”, discussions are the first public community source to scan

## Recurring Community Themes

### 1. Installation, Startup, And Packaging Drift

Representative signals seen during this pass:

- `Community finding`
  - announcement threads around LiteLoader deprecation and Shell-first guidance
- `Community finding`
  - repeated questions around launch mode, framework access, and environment-specific startup behavior

Repo meaning:

- startup mode confusion is upstream-normal, not a problem unique to this repository
- our documentation should keep saying exactly which local runtime mode is assumed

### 2. WebUI, Token, And Remote Access Confusion

Representative signals:

- `Community finding`
  - discussions such as:
    - Android-side WebUI token usability
    - mobile debugging limitations
    - token/input ergonomics
- `Community finding`
  - framework access docs explicitly warning about token/403 matches what operators keep tripping over

Repo meaning:

- token and access failure should stay first-class in `/doctor` and NapCat troubleshooting docs
- not every “export is broken” report is an exporter parser bug; many are auth or runtime access mistakes

### 3. Media And File Handling Pain

Representative signals:

- `Community finding`
  - discussions/questions around:
    - 正确处理多媒体信息
    - 机器人接收文件并自动缓存
    - 自行调节文件下载
- `Community finding`
  - closed issues and search hits also show repeated concern around media retrieval, upload/download, and URL availability

Repo meaning:

- our current media strategy work is aligned with real upstream operator pain
- file/url/token/path confusion is not just our local misunderstanding; it is a genuine recurring support topic

### 4. Protocol/Adapter Surface And Framework Integration

Representative signals:

- `Community finding`
  - framework-access documentation and community chatter both revolve around:
    - HTTP vs WS access
    - client/server role confusion
    - permission and endpoint mistakes

Repo meaning:

- keeping our transport layer narrow and explicit remains the right call
- “public protocol contract first” is aligned with how the broader ecosystem approaches NapCat

### 5. Platform Compatibility

Representative signals:

- `Community finding`
  - community traffic around Windows variants, Android access, server/VPS usage, and environment-specific behavior continues to recur
- `Community finding`
  - some support questions are environment/setup questions rather than API-contract questions

Repo meaning:

- when external testers report failures, always ask for environment/runtime context before assuming logic regression

### 6. Feature Requests And Expectations Mismatch

Representative signals:

- `Community finding`
  - visible issues/discussions mention topics such as:
    - avatar retrieval
    - richer account/auth flows
    - plugin or adapter ergonomics
    - unsupported or “won’t do” requests

Repo meaning:

- not every “QQ can do this” expectation maps cleanly to NapCat’s documented OneBot surface
- our repository should continue distinguishing:
  - official public capability
  - plugin-extensible capability
  - unsupported or runtime-specific hope

## PR Signal

- `Community finding`
  - public PR metadata showed a large closed-PR volume (`554`)
- `Community finding`
  - visible docs-page histories and repo churn show that docs and behavior are still actively edited

What this means:

- PR churn is high enough that any hand-maintained local knowledge will go stale quickly
- the research handbooks should prefer stable architecture patterns and explicitly dated snapshots over timeless claims

## Community Guidance For This Repository

- discussions are often better than issues for operator pain and real-world usage patterns
- issues are still useful for representative bug themes, but they are not the whole story
- PR volume tells us NapCat behavior evolves quickly even when operator docs lag behind

## What This File Should Drive

This file should inform:

- what needs extra explanation in `NapCat.docs_AGENTs.md`
- what needs source confirmation in `NapCat.source_AGENTs.md`
- what deserves exporter-specific handling in `NapCat.media_AGENTs.md`

Example:

- the community repeatedly runs into media/file ambiguity
- official docs say URL-first plus `get_*`
- source says `get_*` needs a NapCat-managed contextual token
- our exporter handbooks should therefore never jump straight from “field named file exists” to “public action must accept it”

## Coverage Ledger

First-pass community coverage completed:

- [x] repo scale and activity snapshot
- [x] discussions category map
- [x] recurring operator-theme extraction
- [x] representative issue/discussion scan for media, startup, auth, and platform pain

Still pending for deeper future indexing:

- [ ] per-topic PR discussion index
- [ ] deeper issue archive for media/token/forward-specific regressions
- [ ] discussion-link appendix grouped by subject rather than theme summary only
