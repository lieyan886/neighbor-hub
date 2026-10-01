"""分享卡片渲染：把一条内容画成一张适合微信群的竖图。

尺寸默认 900x1200，微信群预览不会被压得太小。中文字体自动从
Windows 字体目录里找，找不到就退化到 PIL 默认字体（会很难看但不崩）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core import config
from core.models import CardTemplate, Item, KIND_GROUPBUY, kind_label
from core.utils import format_price, humanize

_FONT_CANDIDATES = (
    ("msyh.ttc", "微软雅黑"),
    ("msyhbd.ttc", "微软雅黑粗"),
    ("simhei.ttf", "黑体"),
    ("msjh.ttc", "微软正黑"),
    ("simsun.ttc", "宋体"),
    ("deng.ttf", "等线"),
)
_FONT_DIR = Path("C:/Windows/Fonts")
_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """按优先级找一个支持中文的字体。"""
    key = ("bold" if bold else "regular", size)
    if key in _cache:
        return _cache[key]

    names = _FONT_CANDIDATES
    if bold:
        names = tuple(n for n in _FONT_CANDIDATES if n[0] in ("msyhbd.ttc", "simhei.ttf")) + names
    for fname, _label in names:
        p = _FONT_DIR / fname
        if p.exists():
            try:
                font = ImageFont.truetype(str(p), size)
                _cache[key] = font
                return font
            except Exception:
                continue
    fallback = ImageFont.load_default()
    _cache[key] = fallback  # type: ignore[assignment]
    return fallback


# --- 绘制小工具 -------------------------------------------------------------


def wrapped_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """中英混排换行：优先按空格/标点切，超长则逐字断。"""
    if not text:
        return []
    lines: list[str] = []
    cur = ""
    for ch in text:
        trial = cur + ch
        if draw.textlength(trial, font=font) <= max_width:
            cur = trial
            continue
        if not cur:
            lines.append(ch)
            continue
        lines.append(cur)
        cur = ch
    if cur:
        lines.append(cur)
    return lines


def hex_to_rgb(value: str, default: tuple[int, int, int] = (0, 0, 0)) -> tuple[int, int, int]:
    v = (value or "").strip().lstrip("#")
    if len(v) == 6:
        try:
            return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        except ValueError:
            return default
    if len(v) == 3:
        try:
            return tuple(int(c * 2, 16) for c in v)  # type: ignore[return-value]
        except ValueError:
            return default
    return default


def round_rect(draw, box, radius: int, fill=None, outline=None, width: int = 1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def cover_image(path: str, size: tuple[int, int]) -> Image.Image | None:
    """按封面区比例裁剪原图（居中裁剪，不留白边）。"""
    if not path or not Path(path).exists():
        return None
    try:
        img = Image.open(path).convert("RGB")
    except Exception:
        return None
    tw, th = size
    sw, sh = img.size
    scale = max(tw / sw, th / sh)
    new = (max(1, int(sw * scale)), max(1, int(sh * scale)))
    img = img.resize(new, Image.LANCZOS)
    left = (img.width - tw) // 2
    top = (img.height - th) // 2
    return img.crop((left, top, left + tw, top + th))


def make_qr(data: str, size: int = 220) -> Image.Image | None:
    """生成二维码图片；qrcode 不可用时返回 None。"""
    if not data:
        return None
    try:
        import qrcode
    except ImportError:
        return None
    try:
        qr = qrcode.QRCode(box_size=8, border=2)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        return img.resize((size, size), Image.NEAREST)
    except Exception:
        return None


# --- 主渲染流程 -------------------------------------------------------------


@dataclass
class CardContext:
    """渲染时需要的外挂信息。"""

    community_name: str = "我们小区"
    operator_name: str = "团长"
    contact_info: str = ""
    join_url: str = ""          # 二维码内容，通常是报名链接
    done_qty: float = 0.0       # 已完成份数（拼单/报名进度）
    headcount: int = 0


def render_card(
    item: Item,
    template: CardTemplate,
    ctx: CardContext | None = None,
    output_dir: str | Path | None = None,
) -> str:
    """渲染单张卡片，返回落盘路径。"""
    ctx = ctx or CardContext()
    W, H = template.width, template.height
    bg = hex_to_rgb(template.background, (250, 247, 240))
    accent = hex_to_rgb(template.accent, (216, 90, 48))
    title_c = hex_to_rgb(template.title_color, (44, 44, 42))
    body_c = hex_to_rgb(template.body_color, (95, 94, 90))

    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    pad = 56
    y = pad

    # 顶部：小区名 + 类型标签
    head_font = load_font(30, bold=True)
    tag_font = load_font(24)
    draw.text((pad, y), f"#{ctx.community_name}#", font=head_font, fill=accent)

    label = kind_label(item.kind)
    lw = draw.textlength(label, font=tag_font)
    chip_w = lw + 40
    round_rect(draw, (W - pad - chip_w, y - 6, W - pad, y + 40), 20, fill=accent)
    draw.text((W - pad - chip_w + 20, y - 2), label, font=tag_font, fill=(255, 255, 255))
    y += 66

    # 封面图
    cover_h = 420
    cover = cover_image(item.cover, (W - pad * 2, cover_h))
    if cover is not None:
        img.paste(cover, (pad, y))
        draw = ImageDraw.Draw(img)
    else:
        round_rect(draw, (pad, y, W - pad, y + cover_h), 24, fill=tuple(
            min(255, c + 8) for c in bg
        ))
        ph = load_font(34)
        hint = _placeholder_hint(item)
        tw = draw.textlength(hint, font=ph)
        draw.text(((W - tw) / 2, y + cover_h / 2 - 20), hint, font=ph,
                  fill=tuple(180 for _ in range(3)))
    y += cover_h + 36

    # 标题
    title_font = load_font(46, bold=True)
    lines = wrapped_text(draw, item.title, title_font, W - pad * 2)[:2]
    for ln in lines:
        draw.text((pad, y), ln, font=title_font, fill=title_c)
        y += 58
    y += 10

    # 摘要
    if item.summary:
        body_font = load_font(28)
        body_lines = wrapped_text(draw, item.summary, body_font, W - pad * 2)[:3]
        for ln in body_lines:
            draw.text((pad, y), ln, font=body_font, fill=body_c)
            y += 42
        y += 12

    # 价格 / 进度
    if template.show_price and item.price is not None:
        _draw_price(draw, pad, y, item, accent, body_c)
        y += 96

    if template.show_deadline and (item.deadline or item.event_at):
        y = _draw_deadline(draw, pad, y, item, body_c, W)

    # 拼单进度条
    if item.kind == KIND_GROUPBUY and item.quota > 0:
        y = _draw_progress(draw, pad, y, item, ctx, accent, body_c, W)

    # 标签
    if template.show_tags and item.tag_list:
        y = _draw_tags(draw, pad, y, item, body_c, accent, W)

    # 底部：二维码 + 联系方式
    y = _draw_footer(draw, img, item, template, ctx, body_c, W, H, pad)

    out_dir = Path(output_dir or config.OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = "".join(c for c in item.title[:18] if c not in '\\/:*?"<>|') or "card"
    stamp = datetime.now().strftime("%m%d%H%M%S")
    dest = out_dir / f"{item.id or 0}_{slug}_{stamp}.png"
    img.save(dest, "PNG", optimize=True)
    return str(dest)


def _placeholder_hint(item: Item) -> str:
    return {"event": "活动配图待补", "groupbuy": "商品图待补"}.get(item.kind, "优惠截图待补")


def _draw_price(draw, x, y, item: Item, accent, body_c) -> None:
    num_font = load_font(64, bold=True)
    unit_font = load_font(28)
    text = format_price(item.price)
    draw.text((x, y + 14), "¥", font=unit_font, fill=accent)
    draw.text((x + 32, y), text, font=num_font, fill=accent)
    nx = x + 32 + draw.textlength(text, font=num_font) + 12
    if item.unit:
        draw.text((nx, y + 40), f"/{item.unit}", font=unit_font, fill=body_c)
        nx += draw.textlength(f"/{item.unit}", font=unit_font) + 16
    if item.origin_price and item.origin_price > (item.price or 0):
        old = f"¥{format_price(item.origin_price)}"
        small = load_font(26)
        draw.text((nx, y + 44), old, font=small, fill=(160, 158, 150))
        ow = draw.textlength(old, font=small)
        draw.line((nx, y + 58, nx + ow, y + 58), fill=(160, 158, 150), width=2)


def _draw_deadline(draw, x, y, item: Item, body_c, W) -> int:
    font = load_font(28)
    prefix = "活动时间 " if item.kind == "event" and item.event_at else "截止 "
    target = item.event_at if item.kind == "event" and item.event_at else item.deadline
    text = prefix + humanize(target)
    chip_w = 40 + draw.textlength(text, font=font)
    round_rect(draw, (x, y, x + chip_w, y + 56), 28, fill=(232, 229, 222))
    draw.text((x + 20, y + 12), text, font=font, fill=body_c)
    return y + 76


def _draw_progress(draw, x, y, item: Item, ctx: CardContext, accent, body_c, W) -> int:
    font = load_font(28)
    done = ctx.done_qty
    target = float(item.quota or 0)
    ratio = min(1.0, done / target) if target else 0.0
    bar_w = W - x * 2
    round_rect(draw, (x, y + 34, x + bar_w, y + 58), 12, fill=(224, 222, 215))
    if ratio > 0:
        round_rect(draw, (x, y + 34, x + int(bar_w * ratio), y + 58), 12, fill=accent)
    text = f"已拼 {format_price(done)} / {format_price(target)} {item.unit or '份'}"
    draw.text((x, y), text, font=font, fill=body_c)
    if ctx.headcount:
        tail = f"{ctx.headcount} 人参与"
        draw.text((x + bar_w - draw.textlength(tail, font=font), y), tail,
                  font=font, fill=body_c)
    return y + 84


def _draw_tags(draw, x, y, item: Item, body_c, accent, W) -> int:
    font = load_font(24)
    gap = 14
    tint = tuple(min(255, int(c * 0.16 + 246 * 0.84)) for c in accent)
    for tag in item.tag_list[:5]:
        tw = draw.textlength(tag, font=font) + 32
        if x + tw > W - 56:
            x = 56
            y += 50
        round_rect(draw, (x, y, x + tw, y + 40), 20, fill=tint)
        draw.text((x + 16, y + 6), tag, font=font, fill=accent)
        x += tw + gap
    return y + 58


def _draw_footer(draw, img, item: Item, template: CardTemplate,
                 ctx: CardContext, body_c, W, H, pad) -> int:
    line_y = H - 150
    draw.line((pad, line_y - 20, W - pad, line_y - 20), fill=(214, 212, 205), width=2)

    font = load_font(26)
    left_x = pad
    text = template.footer or f"联系{ctx.operator_name}报名"
    if ctx.contact_info:
        text += f"  {ctx.contact_info}"
    draw.text((left_x, line_y + 10), text, font=font, fill=body_c)

    if template.show_qr:
        qr = make_qr(ctx.join_url or item.url or item.title, size=140)
        if qr is not None:
            img.paste(qr, (W - pad - 140, line_y - 6))
    return line_y


def render_batch(
    items: list[Item],
    template: CardTemplate,
    contexts: dict[int, CardContext] | None = None,
    output_dir: str | Path | None = None,
    progress_cb=None,
) -> list[str]:
    """批量渲染，返回路径列表；单张失败不影响其余。"""
    contexts = contexts or {}
    paths: list[str] = []
    for idx, it in enumerate(items, 1):
        try:
            paths.append(render_card(it, template, contexts.get(it.id or -1), output_dir))
        except Exception:
            continue
        if progress_cb:
            progress_cb(idx, len(items))
    return paths
