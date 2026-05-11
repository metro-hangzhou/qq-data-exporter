from __future__ import annotations

from pathlib import Path

from qq_data_integrations.local_qq import discover_qq_media_roots


def test_discover_qq_media_roots_finds_nested_drive_targets(monkeypatch, tmp_path) -> None:
    drive_root = tmp_path / "D"
    nested_tencent = drive_root / "QQHOT" / "Tencent Files"
    nested_qq = drive_root / "PortableQQ" / "QQ"
    nested_qq_files = drive_root / "Something" / "QQ Files"
    direct_tencent = drive_root / "Tencent Files"

    nested_tencent.mkdir(parents=True, exist_ok=True)
    nested_qq.mkdir(parents=True, exist_ok=True)
    nested_qq_files.mkdir(parents=True, exist_ok=True)
    direct_tencent.mkdir(parents=True, exist_ok=True)

    original_path = Path

    def fake_path(value: str | Path = ".") -> Path:
        text = str(value).replace("\\", "/")
        if text.startswith("D:/"):
            suffix = text[3:]
            return original_path(drive_root / suffix)
        return original_path(value)

    monkeypatch.setattr("qq_data_integrations.local_qq.Path", fake_path)
    monkeypatch.delenv("QQ_MEDIA_ROOTS", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)

    roots = discover_qq_media_roots()
    root_strings = {str(path.resolve()) for path in roots}

    assert str(direct_tencent.resolve()) in root_strings
    assert str(nested_tencent.resolve()) in root_strings
    assert str(nested_qq.resolve()) in root_strings
    assert str(nested_qq_files.resolve()) in root_strings
