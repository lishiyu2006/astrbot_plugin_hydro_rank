"""排行榜的文本 / 图片渲染。

- :func:`render_text` 纯文本输出（任何平台都能发，作为兜底方案）
- :func:`render_image` 用 Pillow 画一张表格图片（QQ 等场景观感更好）
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from hydro_client import DETAIL_KEYS, KEY_LABELS, RankPage, RankRow

__all__ = ["render_text", "render_image", "cleanup_cache"]

_DETAIL_SET = set(DETAIL_KEYS)

# 候选字体：优先中文黑体，覆盖 Windows / Linux / macOS 常见路径
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)


# --------------------------------------------------------------------------- #
# 文本
# --------------------------------------------------------------------------- #


def _detail_pairs(row: RankRow, keys: list[str]) -> str:
    parts = []
    for key in keys:
        value = row.value(key)
        if value == "-":
            continue
        parts.append(f"{KEY_LABELS.get(key, key)} {value}")
    return " · ".join(parts)


def _select_keys(page: RankPage, rows: list[RankRow], *, include_empty: bool = False) -> list[str]:
    keys = []
    for key in page.columns:
        if key not in _DETAIL_SET:
            continue
        if include_empty or any(r.value(key) != "-" for r in rows):
            keys.append(key)
    return keys


def _one_line(text: str) -> str:
    """把任意文本压成单行（Hydro 的简介里常带换行与 Markdown 片段）。"""
    return re.sub(r"\s+", " ", (text or "")).strip()


def _truncate(text: str, limit: int) -> str:
    text = _one_line(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_text(
    page: RankPage,
    *,
    rows: list[RankRow] | None = None,
    show_bio: bool = False,
    highlight: set[str] | None = None,
) -> str:
    """把排行榜渲染成纯文本。"""
    rows = list(rows if rows is not None else page.rows)
    if not rows:
        return "没有可展示的排名数据。"

    keys = _select_keys(page, rows)
    hl = {h.lower() for h in (highlight or set())}

    head = [
        f"🏆 Hydro 排行榜 · {page.alias}",
        f"🌐 {page.base_url}",
    ]
    meta = f"第 {page.page}/{page.total_pages} 页"
    if page.total_users:
        meta += f" · 共 {page.total_users} 人"
    if rows:
        meta += f" · 当前 {rows[0].rank if rows[0].rank else page.start_rank}"
        meta += f"-{rows[-1].rank if rows[-1].rank else page.end_rank} 名"
    head.append(meta)

    lines = ["  ".join(head[:1]), head[1], head[2], "─" * 26]

    for row in rows:
        mark = "★ " if row.username.lower() in hl else ""
        rank = f"{row.rank:>4}" if row.rank is not None else "   -"
        name = _truncate(row.username, 18)
        level = f" [{row.level}]" if row.level else ""
        lines.append(f"{rank}. {mark}{name}{level}")
        detail = _detail_pairs(row, keys)
        if detail:
            lines.append(f"      {detail}")
        if show_bio and row.bio:
            lines.append(f"      {_truncate(row.bio, 40)}")

    lines.append("─" * 26)
    if page.total_pages > page.page:
        lines.append(f"👉 下一页：发送 “/hydro_rank page {page.page + 1}”")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 图片
# --------------------------------------------------------------------------- #


def _load_font(size: int, font_path: str = ""):
    from PIL import ImageFont

    candidates = []
    if font_path and Path(font_path).exists():
        candidates.append(font_path)
    candidates.extend(_FONT_CANDIDATES)
    for path in candidates:
        if not path or not Path(path).exists():
            continue
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001 - 换下一个候选字体
            continue
    return None


def _ellipsize(draw, text: str, font, max_width: int) -> str:
    text = _one_line(text)
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    while text and draw.textlength(text + ellipsis, font=font) > max_width:
        text = text[:-1]
    return text + ellipsis


def render_image(
    page: RankPage,
    *,
    rows: list[RankRow] | None = None,
    show_bio: bool = False,
    highlight: set[str] | None = None,
    font_path: str = "",
    out_dir: str | os.PathLike | None = None,
) -> str | None:
    """把排行榜画成 PNG，返回文件路径；失败时返回 ``None``（调用方回退到文本）。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    rows = list(rows if rows is not None else page.rows)
    if not rows:
        return None

    title_font = _load_font(30, font_path)
    head_font = _load_font(21, font_path)
    cell_font = _load_font(21, font_path)
    small_font = _load_font(18, font_path)
    if title_font is None or head_font is None or cell_font is None:
        return None  # 没有可用字体，交给文本模式

    keys = _select_keys(page, rows)
    bio_keys = ["bio"] if (show_bio and any(r.bio for r in rows)) else []
    columns = ["rank", "username", *keys, *bio_keys]

    # 画布宽度：先量一遍内容再定列宽
    probe = Image.new("RGB", (10, 10))
    pdraw = ImageDraw.Draw(probe)

    widths: dict[str, int] = {"rank": 90, "username": 240, "bio": 320}
    for key in keys:
        widths[key] = max(110, int(pdraw.textlength(KEY_LABELS.get(key, key), font=head_font)) + 44)
    for row in rows:
        widths["username"] = max(
            widths["username"],
            int(pdraw.textlength(_one_line(f"{row.username} {row.level}"), font=cell_font)) + 40,
        )
        if "bio" in widths and row.bio:
            widths["bio"] = max(
                widths["bio"],
                min(420, int(pdraw.textlength(_one_line(row.bio), font=cell_font)) + 40),
            )
    widths["username"] = min(widths["username"], 380)
    for key in ("bio",):
        if key in widths:
            widths[key] = min(widths[key], 420)

    pad = 26
    row_h = 46
    head_h = 52
    title_h = 96
    footer_h = 44

    table_w = sum(widths[c] for c in columns)
    width = table_w + pad * 2
    height = title_h + head_h + row_h * len(rows) + footer_h + pad

    img = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    C_TEXT = "#1F2328"
    C_MUTED = "#6E7781"
    C_LINE = "#E3E8EF"
    C_HEAD_BG = "#EEF2F7"
    C_ALT = "#F9FAFB"
    C_HL_BG = "#FFF7E0"
    C_HL_TEXT = "#8A5A00"
    C_ACCENT = "#0B62D0"

    # 顶部标题
    draw.rectangle([0, 0, width, title_h - 12], fill="#F4F7FB")
    draw.rectangle([0, title_h - 14, width, title_h - 12], fill=C_ACCENT)
    draw.text((pad, 26), f"Hydro 排行榜 · {page.alias}", font=title_font, fill=C_TEXT, anchor="lm")
    meta = f"第 {page.page}/{page.total_pages} 页"
    if page.total_users:
        meta += f" · 共 {page.total_users} 人"
    draw.text((width - pad, 26), meta, font=small_font, fill=C_MUTED, anchor="rm")
    draw.text((pad, 62), page.base_url, font=small_font, fill=C_MUTED, anchor="lm")

    # 表头
    y = title_h
    draw.rectangle([pad, y, pad + table_w, y + head_h], fill=C_HEAD_BG)
    x = pad
    for col in columns:
        label = KEY_LABELS.get(col, col)
        anchor = "rm" if col == "rank" else "lm"
        tx = x + widths[col] - 16 if col == "rank" else x + 16
        draw.text((tx, y + head_h / 2), label, font=head_font, fill="#41505F", anchor=anchor)
        x += widths[col]

    # 数据行
    hl = {h.lower() for h in (highlight or set())}
    y += head_h
    for idx, row in enumerate(rows):
        is_hl = row.username.lower() in hl
        if is_hl:
            draw.rectangle([pad, y, pad + table_w, y + row_h], fill=C_HL_BG)
        elif idx % 2:
            draw.rectangle([pad, y, pad + table_w, y + row_h], fill=C_ALT)

        x = pad
        for col in columns:
            if col == "rank":
                text = str(row.rank) if row.rank is not None else "-"
                draw.text(
                    (x + widths[col] - 16, y + row_h / 2),
                    text,
                    font=cell_font,
                    fill=C_HL_TEXT if is_hl else C_MUTED,
                    anchor="rm",
                )
            elif col == "username":
                name = _ellipsize(draw, row.username, cell_font, widths[col] - 32)
                draw.text(
                    (x + 16, y + row_h / 2),
                    name,
                    font=cell_font,
                    fill=C_HL_TEXT if is_hl else C_TEXT,
                    anchor="lm",
                )
                if row.level:
                    name_w = draw.textlength(name, font=cell_font)
                    draw.text(
                        (x + 16 + name_w + 10, y + row_h / 2 + 1),
                        row.level,
                        font=small_font,
                        fill=C_MUTED,
                        anchor="lm",
                    )
            elif col == "bio":
                text = _ellipsize(draw, row.bio, small_font, widths[col] - 32)
                draw.text((x + 16, y + row_h / 2), text, font=small_font, fill=C_MUTED, anchor="lm")
            else:
                text = _one_line(row.value(col))
                draw.text(
                    (x + widths[col] / 2, y + row_h / 2),
                    text,
                    font=cell_font,
                    fill=C_TEXT,
                    anchor="mm",
                )
            x += widths[col]

        draw.line([pad, y + row_h, pad + table_w, y + row_h], fill=C_LINE, width=1)
        y += row_h

    # 底部分页提示
    tip = ""
    if page.total_pages > page.page:
        tip = f"下一页：/hydro_rank page {page.page + 1}"
    draw.text((pad, y + footer_h / 2), tip, font=small_font, fill=C_MUTED, anchor="lm")
    draw.text(
        (width - pad, y + footer_h / 2),
        "Powered by AstrBot",
        font=small_font,
        fill="#A7B0BA",
        anchor="rm",
    )

    target_dir = Path(out_dir) if out_dir else Path(os.getcwd())
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"hydro_rank_{abs(hash((page.base_url, page.page, len(rows)))) % 10**8}_{int(time.time())}.png"
    path = target_dir / filename
    img.save(path, "PNG", optimize=True)
    return str(path)


def cleanup_cache(cache_dir: str | os.PathLike, max_age: int = 3600) -> None:
    """清理过期的排行榜图片，避免磁盘无限增长。"""
    try:
        now = time.time()
        for item in Path(cache_dir).glob("hydro_rank_*.png"):
            if now - item.stat().st_mtime > max_age:
                item.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 - 清理失败不影响主流程
        pass
