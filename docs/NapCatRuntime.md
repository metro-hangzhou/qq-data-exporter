# NapCat Runtime Reference

QQ Data Exporter depends on a running NapCatQQ runtime, but exporter code and NapCat runtime code are separate maintenance surfaces.

## Dependency Boundary

The exporter talks to NapCat only through public runtime interfaces:

- OneBot 11 HTTP actions
- OneBot 11 forward WebSocket actions and events
- public history actions such as `get_group_msg_history` and `get_friend_msg_history`
- public metadata actions such as `get_group_list`, `get_group_member_list`, and `get_friend_list`

The exporter must not import NapCat internal TypeScript modules or depend on QQ injection hooks.

## Repository Layout

Recommended split-repo layout:

```text
qq-data-exporter/
  NapCatQQ/                                # Git submodule / upstream runtime-source reference
  plugins/napcat-plugin-qq-data-fast/      # exporter-owned fast-history plugin source
  src/
  tests/
```

`NapCatQQ/` is tracked as a submodule so GitHub shows it as a runtime reference instead of a flattened copy of the NapCat upstream tree. It may be used by maintainers for source lookup, local runtime builds, and plugin installation targets, but exporter message access must still go through HTTP/WS.

Suggested upstream:

```text
https://github.com/NapNeko/NapCatQQ.git
```

Submodule setup:

```bash
git submodule update --init --recursive
```

## Fast Plugin

This repository keeps only the exporter-owned fast-history plugin source:

```text
plugins/napcat-plugin-qq-data-fast/
```

During runtime setup, install or link this plugin into the active NapCat runtime plugin directory, then restart NapCat. Plugin route changes are not live until NapCat is restarted.

If the plugin is missing or disabled, the exporter must still work through public OneBot history APIs, but bulk history export will be slower.

## Runtime State

Runtime config, logs, caches, generated static assets, and `node_modules` belong to the active NapCat runtime, not to exporter source control. Keep them outside this repo or under ignored local runtime directories.

The exporter may discover a local runtime by conventional relative paths, but those paths are operational conveniences, not source-of-truth APIs:

- `NapCatQQ/` for upstream source/reference lookup
- local runtime paths configured by CLI options or environment-specific config
- optional ignored `NapCat/` compatibility/runtime-output directory

## Maintainer Rule

Do not flatten `NapCatQQ/` into the exporter repository. If the runtime reference needs to move, update the submodule pointer deliberately and keep exporter changes in a separate commit when possible.
