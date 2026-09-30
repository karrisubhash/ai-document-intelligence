"""Render the synthetic "scanned notice" used by the browser suite.

A PNG with no text layer, so the only way its words reach the index is
OCR. Synthetic content, written for the test - not a real notice.

    python frontend/e2e/fixtures/make_scan.py   # needs Pillow >= 10.1
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

LINES = [
    "INSTITUTE OF TECHNOLOGY",
    "CIRCULAR NO. 42",
    "",
    "The central workshop will remain closed",
    "on the second Saturday of every month",
    "for scheduled maintenance of the lathes.",
]


def main():
    font = ImageFont.load_default(size=40)
    image = Image.new("L", (1400, 520), color=255)
    draw = ImageDraw.Draw(image)
    y = 40
    for line in LINES:
        draw.text((60, y), line, fill=0, font=font)
        y += 72
    out = Path(__file__).with_name("scanned-circular.png")
    image.save(out, optimize=True)
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
