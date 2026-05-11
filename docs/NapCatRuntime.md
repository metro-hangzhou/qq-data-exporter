# NapCat Runtime Reference

QQ Data Exporter depends on a running NapCatQQ runtime, but the runtime itself is not vendored into this repository.

## Dependency Boundary

The exporter talks to NapCat only through public runtime interfaces:

- OneBot 11 HTTP actions
- OneBot 11 forward WebSocket actions and events
- public history actions such as `get_group_msg_history` and `get_friend_msg_history`
- public metadata actions such as `get_group_list`, `get_group_member_list`, and `get_friend_list`

The exporter must not import NapCat internal TypeScript modules or depend on QQ injection hooks.

## Recommended Local Layout

For local development, keep a NapCat runtime checkout next to this exporter repo or under the exporter root as an ignored reference checkout:

```text
qq-data-exporter/
  NapCatQQ/              # optional local upstream/runtime checkout, gitignored
  NapCat/napcat/plugins/napcat-plugin-qq-data-fast/
  src/
  tests/
```

`NapCatQQ/` is intentionally ignored by this repository. If a team wants a pinned runtime reference, add it as a Git submodule or document the exact upstream commit used by the release package.

Suggested upstream:

```text
https://github.com/NapNeko/NapCatQQ.git
```

## Fast Plugin

This repository keeps only the exporter-owned fast-history plugin source:

```text
NapCat/napcat/plugins/napcat-plugin-qq-data-fast/
```

During runtime setup, install or copy that plugin into the active NapCat runtime plugin directory, then restart NapCat. Plugin route changes are not live until NapCat is restarted.

If the plugin is missing or disabled, the exporter must still work through public OneBot history APIs, but bulk history export will be slower.

## Runtime Discovery

The exporter may discover a local runtime by conventional relative paths, but message access still goes through HTTP/WS:

- `NapCatQQ/`
- `NapCat/napcat/`
- paths configured through CLI options or environment-specific config

Local runtime config, logs, caches, and generated static files must stay outside version control.
