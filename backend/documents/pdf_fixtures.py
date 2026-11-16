"""Hand-built PDFs for the extraction tests.

Only used by tests, and kept out of the `test*.py` pattern so discovery does
not mistake it for one. Built by hand, like `tests_ingestion.make_pdf_bytes`,
so the suite needs no authoring library and every fixture is deterministic -
but with what that helper lacks: text placed anywhere on the page, ruled
lines, and embedded raster images, which is what table and layout extraction
have to be tested against.

Coordinates are in points measured from the TOP-left of a 612x792 page, the
way a reader thinks about a page; the builder flips them into PDF's
bottom-left space.
"""

import math
import zlib
from io import BytesIO

PAGE_WIDTH = 612
PAGE_HEIGHT = 792

FONTS = {
    "F1": "Helvetica",
    "F2": "Helvetica-Bold",
    "F3": "Times-Roman",
}


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


# The matrix that turns a page's whole content a quarter turn, for pages whose
# text runs down or up the sheet in the file's own coordinates - a landscape
# table drawn sideways. Content to be turned must be designed within the top
# 450 points of the page (y <= 400, x <= 450) so that it stays on the sheet.
TURNS = {
    "cw": "0 -1 1 0 -300 500",  # text then reads top to bottom
    "ccw": "0 1 -1 0 900 50",  # text then reads bottom to top
}


class Page:
    def __init__(self, turn=None):
        self.turn = turn
        self.ops: list[str] = []
        self.images: list[tuple] = []  # (name, width, height, rgb bytes)

    # -- text ---------------------------------------------------------------

    def text(self, x, y, text, size=10, font="F1"):
        """One run of text whose top-left is (x, y)."""
        baseline = PAGE_HEIGHT - y - size * 0.8
        self.ops.append(
            f"BT /{font} {size} Tf {x} {baseline:.2f} Td ({_escape(text)}) Tj ET"
        )
        return self

    def stamp(self, x, y, text, size=10, angle=90):
        """Text turned `angle` degrees anticlockwise (90: reading up the page,
        like the arXiv stamp down a margin; 180: upside down; 45: a diagonal
        annotation). (x, y) is where
        it starts, measured from the top.
        """
        radians = math.radians(angle)
        cos, sin = round(math.cos(radians), 4), round(math.sin(radians), 4)
        matrix = f"{cos} {sin} {-sin} {cos}"
        self.ops.append(
            f"BT /F1 {size} Tf {matrix} {x} {PAGE_HEIGHT - y:.2f} Tm "
            f"({_escape(text)}) Tj ET"
        )
        return self

    def lines(self, x, y, lines, size=10, pitch=14, font="F1"):
        for index, line in enumerate(lines):
            self.text(x, y + index * pitch, line, size=size, font=font)
        return self

    # -- rules --------------------------------------------------------------

    def rule(self, x1, y1, x2, y2, width=0.8):
        self.ops.append(
            f"{width} w {x1} {PAGE_HEIGHT - y1:.2f} m {x2} {PAGE_HEIGHT - y2:.2f} l S"
        )
        return self

    def fill(self, x, y, width, height, rgb=(1, 1, 1)):
        """A filled rectangle with no outline, top-left at (x, y).

        White by default: what Word paints behind every table cell and every
        paragraph in it when it saves a PDF.
        """
        r, g, b = rgb
        self.ops.append(
            f"q {r} {g} {b} rg {x} {PAGE_HEIGHT - y - height:.2f} "
            f"{width} {height} re f Q"
        )
        return self

    def box(self, x, y, width, height):
        self.rule(x, y, x + width, y)
        self.rule(x, y + height, x + width, y + height)
        self.rule(x, y, x, y + height)
        self.rule(x + width, y, x + width, y + height)
        return self

    def grid(
        self,
        x,
        y,
        column_widths,
        rows,
        *,
        row_height=20,
        size=10,
        merge_right=(),
        right_align=(),
    ):
        """A fully ruled table.

        `rows` is a list of lists of text. `merge_right` is a set of
        (row, column) positions whose cell spans into the next column - the
        vertical rule between them is simply not drawn.
        """
        width = sum(column_widths)
        edges = [x]
        for column_width in column_widths:
            edges.append(edges[-1] + column_width)

        for index in range(len(rows) + 1):
            self.rule(x, y + index * row_height, x + width, y + index * row_height)

        for column in range(len(column_widths) + 1):
            for row in range(len(rows)):
                spans = (row, column - 1) in merge_right
                if 0 < column < len(column_widths) and spans:
                    continue
                self.rule(
                    edges[column],
                    y + row * row_height,
                    edges[column],
                    y + (row + 1) * row_height,
                )

        for row_index, row in enumerate(rows):
            for column, cell in enumerate(row):
                if not cell:
                    continue
                cell_x = edges[column] + 4
                if column in right_align and row_index > 0:
                    cell_x = edges[column + 1] - 4 - 5.6 * len(cell)
                self.text(cell_x, y + row_index * row_height + 5, cell, size=size)
        return self

    # -- images -------------------------------------------------------------

    def image(self, x, y, width, height, picture):
        """Place a PIL image with its top-left at (x, y), `width`x`height` pt."""
        rgb = picture.convert("RGB")
        name = f"Im{len(self.images) + 1}"
        self.images.append((name, rgb.width, rgb.height, rgb.tobytes()))
        self.ops.append(
            f"q {width} 0 0 {height} {x} {PAGE_HEIGHT - y - height:.2f} cm /{name} Do Q"
        )
        return self


def build(pages) -> bytes:
    """Assemble pages into a valid PDF, returned as bytes."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # patched below, once the page tree exists
    tree = add(b"")

    font_numbers = {
        name: add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} >>".encode())
        for name, base in FONTS.items()
    }
    font_dict = " ".join(
        f"/{name} {number} 0 R" for name, number in font_numbers.items()
    )

    page_numbers = []
    for page in pages:
        image_refs = []
        for name, width, height, raw in page.images:
            payload = zlib.compress(raw)
            number = add(
                (
                    f"<< /Type /XObject /Subtype /Image /Width {width} "
                    f"/Height {height} /ColorSpace /DeviceRGB "
                    f"/BitsPerComponent 8 /Filter /FlateDecode "
                    f"/Length {len(payload)} >>\nstream\n"
                ).encode()
                + payload
                + b"\nendstream"
            )
            image_refs.append(f"/{name} {number} 0 R")

        body = "\n".join(page.ops)
        if page.turn:
            body = f"q {TURNS[page.turn]} cm\n{body}\nQ"
        stream = body.encode()
        content = add(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
        xobjects = f"/XObject << {' '.join(image_refs)} >>" if image_refs else ""
        page_numbers.append(
            add(
                (
                    f"<< /Type /Page /Parent {tree} 0 R "
                    f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                    f"/Resources << /Font << {font_dict} >> {xobjects} >> "
                    f"/Contents {content} 0 R >>"
                ).encode()
            )
        )

    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {tree} 0 R >>".encode()
    objects[tree - 1] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


def write(path, pages) -> None:
    with open(path, "wb") as handle:
        handle.write(build(pages))


def to_buffer(pages) -> BytesIO:
    return BytesIO(build(pages))
