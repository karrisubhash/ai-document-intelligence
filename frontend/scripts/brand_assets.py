"""Render the PNG brand assets in public/ from the BrandMark and Inter.

    docker run --rm -v <repo>/frontend:/frontend -w /frontend \
        adi-rel-ci-env:b77a9ca python -I scripts/brand_assets.py

Writes public/apple-touch-icon.png (180 px), public/icon-192.png,
public/icon-512.png (the web app manifest's icons) and public/og-image.png
(1200 x 630, the preview a chat or social app shows for a shared link).
public/favicon.svg is drawn by hand; this draws the same mark - the
BrandMark of src/components/icons.tsx on the indigo-to-violet
--brand-gradient - with Pillow, at four times the size and scaled down, so
its edges are smooth.

Needs Pillow >= 10.1 (in the backend image) and `npm ci` first: the text is
set in the Inter the application bundles (@fontsource-variable/inter), read
from node_modules. Commit what it writes.
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).resolve().parent.parent
PUBLIC = HERE / "public"
INTER = (
    HERE
    / "node_modules"
    / "@fontsource-variable"
    / "inter"
    / "files"
    / "inter-latin-wght-normal.woff2"
)

INDIGO = (99, 102, 241)
VIOLET = (139, 92, 246)
WHITE = (255, 255, 255)


def font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    face = ImageFont.truetype(str(INTER), size)
    face.set_variation_by_axes([weight])
    return face


def diagonal(size: tuple[int, int], start, end) -> Image.Image:
    """A top-left to bottom-right gradient, as --brand-gradient's 135deg."""
    width, height = size
    span = width + height - 2 or 1
    mask = Image.new("L", size)
    mask.putdata([(x + y) * 255 // span for y in range(height) for x in range(width)])
    return Image.composite(Image.new("RGB", size, end), Image.new("RGB", size, start), mask)


def draw_mark(canvas: Image.Image, x: int, y: int, size: int, radius: float = 0.25):
    """The BrandMark (a page with a folded corner and a spark) on a
    gradient tile, in the 32-unit coordinates of its SVG."""
    tile = diagonal((size, size), INDIGO, VIOLET)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * radius), fill=255
    )
    canvas.paste(tile, (x, y), mask)

    k = size / 32
    draw = ImageDraw.Draw(canvas)

    def at(points):
        return [(x + px * k, y + py * k) for px, py in points]

    def arc(cx, cy, start, end, steps=8):
        return [
            (cx + 1.5 * math.cos(math.radians(a)), cy + 1.5 * math.sin(math.radians(a)))
            for a in (start + (end - start) * i / steps for i in range(steps + 1))
        ]

    stroke = max(1, round(2 * k))
    # The SVG's outline: straight edges, and 1.5-unit rounded corners
    # everywhere but the folded one.
    page = at(
        [(10, 6.5), (17.5, 6.5), (23, 12)]
        + arc(21.5, 24.5, 0, 90)
        + arc(10, 24.5, 90, 180)
        + arc(10, 8, 180, 270)
    )
    draw.line(page, fill=WHITE, width=stroke, joint="curve")
    draw.line(at([(17.5, 6.5), (17.5, 12), (23, 12)]), fill=WHITE, width=stroke, joint="curve")
    for px, py in page:
        r = stroke / 2
        draw.ellipse((px - r, py - r, px + r, py + r), fill=WHITE)
    spark = [(15.75, 14.5), (16.85, 17.15), (19.5, 18.25), (16.85, 19.35), (15.75, 22), (14.65, 19.35), (12, 18.25), (14.65, 17.15)]
    draw.polygon(at(spark), fill=WHITE)


def icon(size: int, path: Path, *, full_bleed: bool):
    """A square icon. Full bleed for Apple (it rounds the corners itself);
    otherwise the rounded tile on transparency."""
    scale = 4
    big = size * scale
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    if full_bleed:
        canvas.paste(diagonal((big, big), INDIGO, VIOLET), (0, 0))
        # The mark's own tile, drawn the same size, just repaints the
        # gradient; its glyph is what matters.
        draw_mark(canvas, 0, 0, big, radius=0)
    else:
        draw_mark(canvas, 0, 0, big)
    canvas = canvas.resize((size, size), Image.Resampling.LANCZOS)
    if full_bleed:
        canvas = canvas.convert("RGB")
    canvas.save(path, optimize=True)
    print(f"wrote {path.name} ({path.stat().st_size} bytes)")  # noqa: T201


def glow(canvas: Image.Image, box, colour, alpha: int, blur: int):
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse(box, fill=(*colour, alpha))
    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))


def og_image(path: Path):
    scale = 2
    width, height = 1200 * scale, 630 * scale

    def s(value):
        return int(value * scale)

    canvas = diagonal((width, height), (11, 11, 30), (30, 27, 75)).convert("RGBA")
    glow(canvas, (s(-160), s(-220), s(560), s(380)), INDIGO, 120, s(90))
    glow(canvas, (s(760), s(260), s(1380), s(820)), VIOLET, 110, s(110))

    # A faint grid, like the landing page's hero.
    grid = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    lines = ImageDraw.Draw(grid)
    for gx in range(0, width, s(44)):
        lines.line((gx, 0, gx, height), fill=(165, 180, 252, 14), width=1)
    for gy in range(0, height, s(44)):
        lines.line((0, gy, width, gy), fill=(165, 180, 252, 14), width=1)
    canvas.alpha_composite(grid)

    draw = ImageDraw.Draw(canvas)
    draw_mark(canvas, s(80), s(78), s(64))
    draw.text((s(162), s(88)), "Document Intelligence", font=font(s(32), 640), fill=WHITE)

    headline = font(s(56), 760)
    draw.text((s(80), s(200)), "Answers you can trace", font=headline, fill=WHITE)
    draw.text((s(80), s(268)), "back to", font=headline, fill=WHITE)
    accent_x = s(80) + draw.textlength("back to ", font=headline)
    draw.text((accent_x, s(268)), "the page.", font=headline, fill=(196, 181, 253))

    sub = font(s(26), 450)
    draw.text((s(80), s(378)), "Cited answers, hybrid search and OCR", font=sub, fill=(199, 210, 254))
    draw.text((s(80), s(414)), "for your institution's documents.", font=sub, fill=(199, 210, 254))

    small = font(s(21), 560)
    draw.text(
        (s(80), s(532)),
        "Self-hosted  ·  Department boundaries  ·  Audit trail",
        font=small,
        fill=(165, 180, 252),
    )

    # The product, in miniature: a question, a cited answer, its source.
    # Drawn on its own layer and composited: ImageDraw REPLACES pixels, so
    # a translucent fill drawn straight onto the canvas comes out opaque.
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    card = (s(790), s(150), s(1128), s(492))
    draw.rounded_rectangle(card, radius=s(22), fill=(255, 255, 255, 18), outline=(255, 255, 255, 46), width=s(1.5))
    draw.rounded_rectangle((s(900), s(176), s(1104), s(216)), radius=s(14), fill=INDIGO)
    label = font(s(15), 600)
    draw.text((s(914), s(186)), "Minimum attendance?", font=label, fill=WHITE)
    draw.rounded_rectangle((s(812), s(234), s(1104), s(352)), radius=s(14), fill=(255, 255, 255, 30))
    for row, length in enumerate((236, 190, 252)):
        top = s(252 + row * 30)
        draw.rounded_rectangle((s(830), top, s(830 + length), top + s(10)), radius=s(5), fill=(255, 255, 255, 150))
    draw.rounded_rectangle((s(1030), s(278), s(1056), s(300)), radius=s(6), fill=(224, 231, 255))
    draw.text((s(1039), s(279)), "1", font=label, fill=(55, 48, 163))
    draw.rounded_rectangle((s(812), s(370), s(1104), s(470)), radius=s(14), fill=(255, 255, 255, 235))
    for row, (length, hit) in enumerate(((246, False), (262, True), (210, True), (160, False))):
        top = s(388 + row * 19)
        colour = (255, 233, 119) if hit else (210, 214, 224)
        draw.rounded_rectangle((s(830), top, s(830 + length), top + s(9)), radius=s(4), fill=colour)
    canvas.alpha_composite(layer)

    canvas = canvas.resize((1200, 630), Image.Resampling.LANCZOS).convert("RGB")
    canvas.save(path, optimize=True)
    print(f"wrote {path.name} ({path.stat().st_size} bytes)")  # noqa: T201


def main():
    icon(180, PUBLIC / "apple-touch-icon.png", full_bleed=True)
    icon(192, PUBLIC / "icon-192.png", full_bleed=False)
    icon(512, PUBLIC / "icon-512.png", full_bleed=False)
    og_image(PUBLIC / "og-image.png")


if __name__ == "__main__":
    main()
