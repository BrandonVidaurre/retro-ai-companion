#!/usr/bin/env python3
"""Build screens.png — the labelled grid of every screen, for the README.

    python companion.py --shots out/
    python tools/contact_sheet.py out/ screens.png

Kept out of companion.py deliberately: this needs Pillow, and the Pi should
only ever install pygame.
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

SHOTS = [
    ("01_home.png", "Home"),
    ("02_search_keyboard.png", "D-pad keyboard"),
    ("03_keyboard_typed.png", "Query entry"),
    ("04_systems.png", "Systems"),
    ("05_games.png", "Game list"),
    ("06_game_entries.png", "Entries"),
    ("07_entry_text.png", "Entry detail"),
    ("08_performance.png", "Zero 2 W perf"),
    ("09_perf_detail.png", "Perf detail"),
    ("10_search_results.png", "Search results"),
    ("11_answer_offline.png", "Offline answer"),
    ("12_about.png", "About"),
]

THUMB = (400, 300)          # 640x480 at 62.5%, aspect preserved
COLS, GAP, PAD, LABEL = 4, 14, 20, 22
BG, BORDER, TEXT = (13, 17, 23), (48, 54, 61), (139, 148, 158)

FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/Library/Fonts/Menlo.ttc",
    "C:/Windows/Fonts/consola.ttf",
]


def _font(size=14):
    for path in FONTS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def build(shot_dir: str, out_path: str) -> None:
    tw, th = THUMB
    rows = -(-len(SHOTS) // COLS)
    width = PAD * 2 + COLS * tw + (COLS - 1) * GAP
    height = PAD * 2 + rows * (th + LABEL) + (rows - 1) * GAP

    sheet = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(sheet)
    font = _font()

    for i, (name, label) in enumerate(SHOTS):
        row, col = divmod(i, COLS)
        x = PAD + col * (tw + GAP)
        y = PAD + row * (th + LABEL + GAP)
        shot = Image.open(os.path.join(shot_dir, name)).convert("RGB")
        sheet.paste(shot.resize(THUMB, Image.LANCZOS), (x, y))
        draw.rectangle([x, y, x + tw - 1, y + th - 1], outline=BORDER)
        draw.text((x + 1, y + th + 5), label, font=font, fill=TEXT)

    sheet.save(out_path, optimize=True)
    print(f"wrote {out_path} ({sheet.width}x{sheet.height})")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "out",
          sys.argv[2] if len(sys.argv) > 2 else "screens.png")
