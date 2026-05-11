from __future__ import annotations

from qq_data_cli.qr import render_qr_text


def test_render_qr_text_outputs_block_chars() -> None:
    rendered = render_qr_text("https://qr.example/login")
    assert "█" in rendered or "▀" in rendered or "▄" in rendered
    assert len(rendered.splitlines()) > 5
