"""生成应用图标 assets/icon.ico（圆角方块 + 「邻」字）。

跑一次即可，产物已提交到仓库；换配色时改下面三个常量再跑。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "icon.ico"

BG = (216, 90, 48, 255)      # 卡片强调色
FG = (255, 255, 255, 255)
GLYPH = "邻"
FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)


def main() -> int:
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=58, fill=BG)

    font = None
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            font = ImageFont.truetype(path, 150)
            break
    if font is None:
        raise SystemExit("没找到中文字体，无法绘制图标")

    # 视觉居中：Windows 字体的 mm 锚点会偏低，手动上提一点
    draw.text((size / 2, size / 2 - 8), GLYPH, font=font, fill=FG, anchor="mm")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, format="ICO",
             sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(f"图标已生成：{OUT}（{OUT.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
