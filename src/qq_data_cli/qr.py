from __future__ import annotations

from io import BytesIO
from pathlib import Path

import qrcode

from qq_data_core.paths import atomic_write_bytes


def build_login_qr_image_path(project_root: Path) -> Path:
    return project_root / "qq_login_qr.png"


def write_qr_png(data: str, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image = qrcode.make(data)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    atomic_write_bytes(out_path, buffer.getvalue())
    return out_path


def render_qr_text(data: str) -> str:
    qr = qrcode.QRCode(border=1)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    lines: list[str] = []
    for row_index in range(0, len(matrix), 2):
        upper = matrix[row_index]
        lower = matrix[row_index + 1] if row_index + 1 < len(matrix) else [False] * len(upper)
        line_chars: list[str] = []
        for upper_dark, lower_dark in zip(upper, lower):
            if upper_dark and lower_dark:
                line_chars.append("█")
            elif upper_dark:
                line_chars.append("▀")
            elif lower_dark:
                line_chars.append("▄")
            else:
                line_chars.append(" ")
        lines.append("".join(line_chars).rstrip())
    return "\n".join(lines)
