from __future__ import annotations

from pathlib import Path

from qq_data_core import ChatExportService, normalize_message
from qq_data_integrations import FixtureSnapshotLoader


def test_normalize_fixture_segments() -> None:
    loader = FixtureSnapshotLoader()
    service = ChatExportService()
    snapshot = loader.load_export(Path("tests/fixtures/private_fixture.json"))
    normalized = service.build_snapshot(snapshot)

    assert normalized.chat_id == "1507833383"
    assert len(normalized.messages) == 6

    image_message = normalized.messages[1]
    assert image_message.content == "[image:demo_image.JPG]"
    assert image_message.image_file_names == ["demo_image.JPG"]

    file_message = normalized.messages[2]
    assert file_message.uploaded_file_names == ["report.png"]
    assert file_message.content == "[uploaded_file_name:report.png]"
    assert file_message.segments[0].extra["file_id"] == "file-uuid"

    reply_message = normalized.messages[3]
    assert reply_message.reply_to is not None
    assert reply_message.reply_to.preview_text == "原消息"
    assert reply_message.emoji_tokens == ["[emoji:id=177]"]
    assert reply_message.content == "[emoji:id=177] 收到"

    sticker_message = normalized.messages[4]
    assert sticker_message.emoji_tokens == [
        "[sticker:summary=[困],emoji_id=821860bafef7473b99ff6b9358035954,package_id=237962]"
    ]

    speech_message = normalized.messages[5]
    assert speech_message.content == "[speech audio]"
    assert speech_message.segments[0].type == "speech"
    assert speech_message.segments[0].extra["file_id"] == "voice-uuid"


def test_normalize_exporter_video_element() -> None:
    message = normalize_message(
        {
            "messageId": "7",
            "messageSeq": "107",
            "timestamp": "2024-03-23T10:00:00.000Z",
            "sender": {"uin": "1585729597", "name": "我"},
            "rawMessage": {
                "msgId": "7",
                "msgSeq": "107",
                "msgTime": "1711188000",
                "senderUin": "1585729597",
                "elements": [
                    {
                        "elementType": 9,
                        "videoElement": {
                            "fileName": "clip.mp4",
                            "filePath": "C:\\QQ\\demo\\Video\\clip.mp4",
                            "md5HexStr": "video-md5",
                        },
                    }
                ],
            },
        },
        chat_type="private",
        chat_id="1507833383",
        chat_name="示例私聊",
    )

    assert message.content == "[video:clip.mp4]"
    assert message.segments[0].type == "video"
    assert message.segments[0].path == "C:\\QQ\\demo\\Video\\clip.mp4"


def test_normalize_exporter_forward_bundle_element() -> None:
    message = normalize_message(
        {
            "messageId": "8",
            "messageSeq": "108",
            "timestamp": "2025-10-03T18:45:16.000+08:00",
            "sender": {"uin": "3375054630", "name": "Remi"},
            "rawMessage": {
                "msgId": "8",
                "msgSeq": "108",
                "msgTime": "1759488316",
                "elements": [
                    {
                        "elementType": 16,
                        "multiForwardMsgElement": {
                            "xmlContent": '<?xml version="1.0" encoding="utf-8"?><msg brief="[聊天记录]" m_fileName="a89c7129-a256-4a03-a7b8-91d95facba70" m_fileSize="0" action="viewMultiMsg" tSum="4" flag="3" serviceID="35" m_resid="demo-res-id"><item layout="1"><title color="#000000" size="34">Remi 蕾米的聊天记录</title><title color="#777777" size="26">Remi 蕾米: [图片]</title><title color="#777777" size="26">Remi 蕾米: 跟老板讨价还价（（）</title><summary color="#808080" size="26">查看4条转发消息</summary></item><source name="聊天记录"></source></msg>',
                            "resId": "demo-res-id",
                            "fileName": "a89c7129-a256-4a03-a7b8-91d95facba70",
                        },
                    }
                ],
            },
        },
        chat_type="group",
        chat_id="922065597",
        chat_name="蕾米二次元萌萌群",
    )

    assert message.segments[0].type == "forward"
    assert message.content.startswith("[forward message]")
    assert "跟老板讨价还价" in message.content
    assert "查看4条转发消息" in message.text_content
    assert message.segments[0].extra["forwarded_count"] == 4


def test_normalize_onebot_forward_content_recurses_nested_nodes() -> None:
    message = normalize_message(
        {
            "message_id": "88",
            "time": 1710000000,
            "user_id": 1,
            "sender": {"nickname": "外层发送者"},
            "rawMessage": {
                "msgId": "raw-msg-id",
                "peerUid": "u_group_peer",
                "chatType": 2,
                "elements": [
                    {
                        "elementType": 16,
                        "elementId": "outer-forward-element",
                        "multiForwardMsgElement": {
                            "resId": "forward-root",
                            "fileName": "forward-root-file",
                        },
                    }
                ],
            },
            "message": [
                {
                    "type": "forward",
                    "data": {
                        "id": "forward-root",
                        "title": "聊天记录",
                        "content": [
                            {
                                "type": "node",
                                "data": {
                                    "user_id": "111",
                                    "nickname": "甲",
                                    "message": [
                                        {"type": "text", "data": {"text": "外层开始"}},
                                        {
                                            "type": "node",
                                            "data": {
                                                "message": [
                                                    {
                                                        "type": "node",
                                                        "data": {
                                                            "user_id": "222",
                                                            "nickname": "乙",
                                                            "message": [
                                                                {
                                                                    "type": "text",
                                                                    "data": {
                                                                        "text": "内层内容"
                                                                    },
                                                                },
                                                                {
                                                                    "type": "image",
                                                                    "data": {
                                                                        "name": "nested.jpg"
                                                                    },
                                                                },
                                                            ],
                                                        },
                                                    }
                                                ]
                                            },
                                        },
                                    ],
                                },
                            }
                        ],
                    },
                }
            ],
        },
        chat_type="group",
        chat_id="922065597",
        chat_name="蕾米二次元萌萌群",
    )

    forward_segment = message.segments[0]
    assert forward_segment.type == "forward"
    assert forward_segment.extra["forward_depth"] >= 2
    assert "内层内容" in message.content
    assert "[image:nested.jpg]" in message.content
    assert forward_segment.extra["forward_messages"][0]["sender_name"] == "甲"
    nested_segment = forward_segment.extra["forward_messages"][0]["segments"][1]
    assert nested_segment["type"] == "forward"
    assert nested_segment["extra"]["message_id_raw"] == "raw-msg-id"
    assert nested_segment["extra"]["element_id"] == "outer-forward-element"
    assert nested_segment["extra"]["peer_uid"] == "u_group_peer"
    assert nested_segment["extra"]["chat_type_raw"] == 2
    nested_image = nested_segment["extra"]["forward_messages"][0]["segments"][1]
    assert nested_image["extra"]["message_id_raw"] == "raw-msg-id"
    assert nested_image["extra"]["element_id"] == "outer-forward-element"
    assert nested_image["extra"]["peer_uid"] == "u_group_peer"
    assert nested_image["extra"]["chat_type_raw"] == 2
    assert nested_segment["extra"]["forward_messages"][0]["sender_name"] == "乙"


def test_normalize_message_tolerates_string_raw_message_with_dict_fallback() -> None:
    message = normalize_message(
        {
            "message_id": "700",
            "message_seq": "700",
            "time": 1710000700,
            "user_id": 1507833383,
            "sender": "legacy-string-sender",
            "rawMessage": "legacy-client-string",
            "raw_message": {
                "msgId": "700",
                "msgSeq": "700",
                "peerUid": "u_special_friend",
                "chatType": 1,
                "elements": [
                    {
                        "elementType": 16,
                        "elementId": "forward-element-700",
                        "multiForwardMsgElement": {
                            "resId": "forward-700",
                            "fileName": "forward-file",
                        },
                    }
                ],
                "senderUin": "1507833383",
            },
            "message": [
                {
                    "type": "forward",
                    "data": {
                        "id": "forward-700",
                        "content": [
                            {
                                "message": [
                                    {
                                        "type": "text",
                                        "data": {"text": "特殊好友正文"},
                                    }
                                ]
                            }
                        ],
                    },
                }
            ],
        },
        chat_type="private",
        chat_id="1507833383",
        chat_name="ㅤㅤㅤㅤㅤㅤㅤㅤ",
    )

    assert message.sender_id == "1507833383"
    assert message.message_id == "700"
    assert "特殊好友正文" in message.content
    assert message.segments[0].extra["message_id_raw"] == "700"
    assert message.segments[0].extra["peer_uid"] == "u_special_friend"


def test_normalize_onebot_forward_preserves_parent_context() -> None:
    message = normalize_message(
        {
            "message_id": "188",
            "time": 1710001234,
            "user_id": 1,
            "sender": {"nickname": "外层发送者"},
            "rawMessage": {
                "msgId": "raw-forward-msg",
                "peerUid": "u_123456",
                "chatType": 2,
                "elements": [
                    {
                        "elementType": 16,
                        "elementId": "forward-element-77",
                        "multiForwardMsgElement": {
                            "resId": "forward-res",
                            "fileName": "forward-file",
                        },
                    }
                ],
            },
            "message": [
                {
                    "type": "forward",
                    "data": {
                        "id": "forward-root",
                        "title": "聊天记录",
                        "content": [
                            {
                                "type": "node",
                                "data": {
                                    "user_id": "111",
                                    "nickname": "甲",
                                    "message": [
                                        {
                                            "type": "image",
                                            "data": {"name": "nested.jpg"},
                                        }
                                    ],
                                },
                            }
                        ],
                    },
                }
            ],
        },
        chat_type="group",
        chat_id="922065597",
        chat_name="蕾米二次元萌萌群",
    )

    forward_segment = message.segments[0]
    assert forward_segment.type == "forward"
    assert forward_segment.extra["message_id_raw"] == "raw-forward-msg"
    assert forward_segment.extra["element_id"] == "forward-element-77"
    assert forward_segment.extra["peer_uid"] == "u_123456"
    assert forward_segment.extra["chat_type_raw"] == 2
    child = forward_segment.extra["forward_messages"][0]
    assert child["sender_id"] == "111"
    assert child["raw_sender_id"] == "111"
    assert child["raw_sender_name"] == "甲"
    assert child["avatar_url"] == "https://q.qlogo.cn/headimg_dl?dst_uin=111&spec=0&img_type=jpg"
    nested_image = child["segments"][0]
    assert "file_id" in nested_image["extra"]
    assert nested_image["extra"]["file_id"] is None


def test_normalize_exporter_gray_tip_element() -> None:
    message = normalize_message(
        {
            "messageId": "9",
            "messageSeq": "109",
            "timestamp": "2025-10-27T20:15:35.000+08:00",
            "sender": {"uin": "0", "name": "0"},
            "rawMessage": {
                "msgId": "9",
                "msgSeq": "109",
                "msgTime": "1761567335",
                "elements": [
                    {
                        "elementType": 8,
                        "grayTipElement": {
                            "subElementType": 17,
                            "jsonGrayTipElement": {
                                "busiId": "1061",
                                "jsonStr": '{"align":"center","items":[{"col":"1","nm":"Alice","type":"qq","uid":"u_alice"},{"txt":"踢了踢","type":"nor"},{"col":"1","nm":"Bob","tp":"0","type":"qq","uid":"u_bob"},{"txt":"的HashMap ","type":"nor"}]}\n',
                                "recentAbstract": "",
                            },
                        },
                    }
                ],
            },
        },
        chat_type="group",
        chat_id="922065597",
        chat_name="蕾米二次元萌萌群",
    )

    assert message.segments[0].type == "system"
    assert message.content == "Alice踢了踢Bob的HashMap"
    assert message.text_content == "Alice踢了踢Bob的HashMap"


def test_normalize_exporter_ark_share_element() -> None:
    message = normalize_message(
        {
            "messageId": "10",
            "messageSeq": "110",
            "timestamp": "2026-02-10T20:00:08.000+08:00",
            "sender": {"uin": "3375054630", "name": "Remi"},
            "rawMessage": {
                "msgId": "10",
                "msgSeq": "110",
                "msgTime": "1770724808",
                "elements": [
                    {
                        "elementType": 10,
                        "arkElement": {
                            "bytesData": '{"meta":{"news":{"title":"你的好友送你一张免单卡","desc":"千问请客，1分钱喝奶茶","tag":"通义","jumpUrl":"https://b.qianwen.com"}},"prompt":"[分享]你的好友送你一张免单卡"}'
                        },
                    }
                ],
            },
        },
        chat_type="group",
        chat_id="922065597",
        chat_name="蕾米二次元萌萌群",
    )

    assert message.segments[0].type == "share"
    assert message.content.startswith("[share:你的好友送你一张免单卡]")
    assert "千问请客" in message.text_content
    assert message.segments[0].extra["url"] == "https://b.qianwen.com"
