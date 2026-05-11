from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


DEFAULT_EXPORT = (
    Path(__file__).resolve().parents[1]
    / "dev"
    / "testdata"
    / "local"
    / "amd_guanren_group_712742342"
    / "export.jsonl"
)

ASSET_TYPES = {"image", "video", "file", "speech", "sticker"}


def _safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _match_message(payload: dict[str, Any], message_id: str | None) -> bool:
    if not message_id:
        return True
    return str(payload.get("message_id") or "").strip() == message_id.strip()


def _iter_forward_segments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        segment
        for segment in _safe_list(payload.get("segments"))
        if isinstance(segment, dict) and str(segment.get("type") or "").strip() == "forward"
    ]


def _find_first_asset_with_path(nodes: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for segment in _safe_list(node.get("segments")):
            if not isinstance(segment, dict):
                continue
            segment_type = str(segment.get("type") or "").strip()
            path_value = str(segment.get("path") or "").strip()
            if segment_type in ASSET_TYPES and path_value:
                return node, segment
            extra = _safe_dict(segment.get("extra"))
            nested = extra.get("forward_messages") or extra.get("children")
            nested_result = _find_first_asset_with_path(_safe_list(nested))
            if nested_result is not None:
                return nested_result
    return None


def inspect_export(export_path: Path, message_id: str | None) -> int:
    if not export_path.exists():
        print(f"[inspect_forward_payload] export not found: {export_path}", file=sys.stderr)
        return 2

    with export_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                print(
                    f"[inspect_forward_payload] skip invalid json at line {line_number}: {exc}",
                    file=sys.stderr,
                )
                continue
            if not isinstance(payload, dict) or not _match_message(payload, message_id):
                continue

            for segment in _iter_forward_segments(payload):
                extra = _safe_dict(segment.get("extra"))
                nodes = _safe_list(extra.get("forward_messages") or extra.get("children"))
                found = _find_first_asset_with_path(nodes)
                if found is None:
                    continue
                node, asset = found
                print("forward_message_found=true")
                print(f"export_path={export_path}")
                print(f"line_number={line_number}")
                print(f"message_id={payload.get('message_id') or ''}")
                print(f"message_seq={payload.get('message_seq') or ''}")
                print(f"outer_sender_id={payload.get('sender_id') or ''}")
                print(f"outer_sender_name={payload.get('sender_name') or ''}")
                print(f"child_sender_id={node.get('sender_id') or ''}")
                print(f"child_sender_name={node.get('sender_name') or ''}")
                print(f"child_raw_sender_id={node.get('raw_sender_id') or ''}")
                print(f"child_raw_sender_name={node.get('raw_sender_name') or ''}")
                print(f"child_avatar_url={node.get('avatar_url') or ''}")
                print(f"asset_type={asset.get('type') or ''}")
                print(f"asset_file_name={asset.get('file_name') or ''}")
                print(f"asset_path={asset.get('path') or ''}")
                print(
                    f"asset_message_id_raw={_safe_dict(asset.get('extra')).get('message_id_raw') or ''}"
                )
                return 0

    print(
        f"[inspect_forward_payload] no forward asset path found in {export_path}"
        + (f" for message_id={message_id}" if message_id else ""),
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect one forward message from exporter JSONL and print sender/avatar/asset path fields."
    )
    parser.add_argument("export_path", nargs="?", default=str(DEFAULT_EXPORT))
    parser.add_argument("--message-id", dest="message_id")
    args = parser.parse_args()
    return inspect_export(Path(args.export_path).resolve(), args.message_id)


if __name__ == "__main__":
    raise SystemExit(main())
