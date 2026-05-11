from __future__ import annotations

import json
from pathlib import Path

from qq_data_process.adapters import (
    ExporterJsonlAdapter,
    QceJsonAdapter,
    TxtTranscriptAdapter,
)


def test_exporter_jsonl_adapter_loads_high_fidelity_fixture() -> None:
    adapter = ExporterJsonlAdapter()
    bundle = adapter.load(Path("tests/fixtures/smoke.jsonl"))

    assert bundle.source_type == "exporter_jsonl"
    assert bundle.fidelity == "high"
    assert bundle.chat_name == "示例私聊"
    assert len(bundle.messages) == 6
    assert bundle.messages[1].assets[0].asset_type == "image"


def test_qce_json_adapter_loads_compat_fixture() -> None:
    adapter = QceJsonAdapter()
    bundle = adapter.load(Path("tests/fixtures/private_fixture.json"))

    assert bundle.source_type == "qce_json"
    assert bundle.fidelity == "compat"
    assert bundle.chat_name == "示例私聊"
    assert len(bundle.messages) == 6
    assert any(message.assets for message in bundle.messages)


def test_exporter_jsonl_adapter_compacts_forward_sender_identity_without_dropping_asset_paths() -> None:
    payload = {
        "chat_type": "group",
        "chat_id": "712742342",
        "group_id": "712742342",
        "chat_name": "AMD传说玩家交流群①",
        "sender_id": "2328731380",
        "sender_name": "2328731380",
        "message_id": "forward-msg-1",
        "message_seq": "2001",
        "timestamp_ms": 1772778362000,
        "timestamp_iso": "2026-03-06T14:26:02+08:00",
        "content": "[forward message]",
        "text_content": "海阔天空: [image:demo.jpg]",
        "image_file_names": [],
        "uploaded_file_names": [],
        "emoji_tokens": [],
        "segments": [
            {
                "type": "forward",
                "token": "[forward message]",
                "summary": "群聊的聊天记录",
                "extra": {
                    "preview_text": "海阔天空: [图片]",
                    "forward_depth": 1,
                    "forward_messages": [
                        {
                            "sender_id": "1094950020",
                            "sender_name": "海阔天空",
                            "raw_sender_id": "3626629292",
                            "raw_sender_name": "海阔天空",
                            "avatar_url": "https://q.qlogo.cn/headimg_dl?dst_uin=3626629292&spec=0&img_type=jpg",
                            "content": "[image:demo.jpg]",
                            "text_content": "[image:demo.jpg]",
                            "segments": [
                                {
                                    "type": "image",
                                    "file_name": "demo.jpg",
                                    "path": "D:\\QQHOT\\Tencent Files\\2141129832\\nt_qq\\nt_data\\Pic\\2026-03\\Ori\\demo.jpg",
                                    "extra": {
                                        "file_id": "demo-token",
                                        "message_id_raw": "nested-forward-msg",
                                    },
                                }
                            ],
                        }
                    ],
                },
            }
        ],
    }
    source_path = Path(".tmp/test_forward_preprocess_adapter/forward.jsonl")
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    adapter = ExporterJsonlAdapter()
    bundle = adapter.load(source_path)

    source_payload = bundle.messages[0].extra["source_payload"]
    forward_segment = source_payload["segments"][0]
    child = forward_segment["extra"]["children"][0]
    assert child["raw_sender_id"] == "3626629292"
    assert child["avatar_url"] == "https://q.qlogo.cn/headimg_dl?dst_uin=3626629292&spec=0&img_type=jpg"
    assert child["segments"][0]["path"].endswith("demo.jpg")
    assert child["segments"][0]["extra"]["file_id"] == "demo-token"


def test_txt_adapter_loads_lossy_fixture() -> None:
    adapter = TxtTranscriptAdapter()
    bundle = adapter.load(Path("tests/fixtures/smoke.txt"))

    assert bundle.source_type == "qq_txt"
    assert bundle.fidelity == "lossy"
    assert bundle.chat_name == "示例私聊"
    assert len(bundle.messages) == 6
    assert bundle.messages[1].assets[0].asset_type == "image"
    assert bundle.messages[1].sender_id_raw == "1585729597"
