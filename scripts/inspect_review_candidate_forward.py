from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from qq_data_analysis.review_service import load_review_candidate  # noqa: E402


def _safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def inspect_candidate(run_id: str, candidate_id: str) -> int:
    detail = load_review_candidate(ROOT, run_id, candidate_id)
    for message in detail.get("transcriptMessages") or []:
        for segment in _safe_list(message.get("segments")):
            if not isinstance(segment, dict) or str(segment.get("type") or "").strip() != "forward":
                continue
            forward_messages = _safe_list(_safe_dict(segment.get("extra")).get("forward_messages"))
            if not forward_messages:
                continue
            print("forward_found=true")
            print(f"run_id={run_id}")
            print(f"candidate_id={candidate_id}")
            print(f"message_uid={message.get('messageUid') or ''}")
            print(f"message_sender_name={message.get('senderName') or ''}")
            print(f"message_sender_id={message.get('senderId') or ''}")
            print(f"forward_message_count={len(forward_messages)}")
            for index, child in enumerate(forward_messages[:24], start=1):
                if not isinstance(child, dict):
                    continue
                print(
                    json.dumps(
                        {
                            "index": index,
                            "sender_name": child.get("sender_name"),
                            "sender_id": child.get("sender_id"),
                            "raw_sender_id": child.get("raw_sender_id"),
                            "raw_sender_name": child.get("raw_sender_name"),
                            "avatar_url": child.get("avatar_url"),
                            "text_preview": str(
                                child.get("text_content") or child.get("content") or ""
                            )[:120],
                        },
                        ensure_ascii=False,
                    )
                )
            return 0
    print(
        f"[inspect_review_candidate_forward] no forward messages found in {run_id}/{candidate_id}",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect forward sender identity after review_service candidate loading."
    )
    parser.add_argument("run_id")
    parser.add_argument("candidate_id")
    args = parser.parse_args()
    return inspect_candidate(args.run_id, args.candidate_id)


if __name__ == "__main__":
    raise SystemExit(main())
