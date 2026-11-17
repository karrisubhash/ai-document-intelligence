"""Where the figures are on a PDF page: pictures, and diagrams drawn as shapes.

A figure in a PDF is one of two things. A PICTURE is an embedded image - a
screenshot, a scanned chart, a photograph. A DRAWING is made of the page's own
vector shapes - a chart exported from Excel, a flowchart drawn in Word, a
pie chart from a plotting library - and has no image anywhere: to the text
extractor it is a scatter of labels with no order, and to the image-based
figure detector it is nothing at all.

Both are found here so that a vision model can be shown them (see
figures/vision.py). Finding a drawing is a judgement, and a wrong one costs a
model call and puts a description of nothing into the page, so the rules ask
for the marks of a drawing that tables and decoration do not have:

* curves (a pie slice, a line series, a rounded box, an arrowhead), at least
  two of them;
* a diagonal line (an arrow, a connector, an axis tick at an angle);
* bars: three or more filled rectangles standing on one baseline (or
  starting from one edge) at different lengths.

A Word export paints a filled rectangle behind every shaded table cell; those
tile in rows and columns of equal heights and are none of the above. Shapes
inside a ruled table, a frame around the whole page and anything small are
left out, and a region full of running text is text, not a figure.
"""

from dataclasses import dataclass

# A figure smaller than this on either side (in points; 72 to the inch) is an
# icon, a bullet or a logo.
MIN_SIDE = 50.0
# ...or smaller than this share of the page.
MIN_PAGE_SHARE = 0.02
# Pixels on the shorter side of the embedded image itself.
MIN_SOURCE_PIXELS = 48
# A picture this big with this many text-layer words over it is a scanned page
# carrying its own OCR layer, or a page background - text, which is read.
BACKGROUND_SHARE = 0.6
BACKGROUND_WORDS = 20

# Shapes closer than this (points) belong to the same drawing.
JOIN_GAP = 8.0
# Past this many shapes a page is a map or a CAD sheet; clustering it would
# be slow, and it is treated as one drawing if it qualifies at all.
MAX_SHAPES = 600
MIN_SHAPES = 4
# A drawing must be at least this big...
MIN_DRAWING_SIDE = 60.0
MIN_DRAWING_SHARE = 0.03
# ...and not a frame around the page.
FRAME_SHARE = 0.85
# A region with more words than this inside it is running text with a border
# or a shaded box, not a figure.
MAX_WORDS_INSIDE = 150

MIN_CURVES = 2
BAR_MIN = 3
BAR_EDGE_TOLERANCE = 1.5
BAR_SPREAD = 1.15

# A drawing's labels - its title, its axis numbers and names, its legend - are
# TEXT on the page, not shapes, so the shapes alone are a box that leaves them
# out; shown that, a model sees bars with no names and invents some. Lines of
# text that sit within this distance (points) of the shapes are part of the
# figure, unless most of the line lies outside it - a paragraph beside a chart
# is not its label.
LABEL_MARGIN = 42.0
LABEL_LINE_INSIDE = 0.8
LINE_TOLERANCE = 3.0

# Never more than this many figures read from one page.
MAX_PER_PAGE = 6
# Padding around a figure when it is rendered, so an edge label is not cut.
PAD = 6.0


@dataclass(frozen=True)
class Region:
    x0: float
    top: float
    x1: float
    bottom: float
    origin: str  # "picture" | "drawing"

    @property
    def box(self) -> tuple:
        return (self.x0, self.top, self.x1, self.bottom)

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.bottom - self.top


def _box(obj) -> tuple:
    return (float(obj["x0"]), float(obj["top"]), float(obj["x1"]), float(obj["bottom"]))


def _area(box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _overlap_area(a, b) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _near(a, b, gap: float) -> bool:
    return not (
        a[2] + gap < b[0] or b[2] + gap < a[0] or a[3] + gap < b[1] or b[3] + gap < a[1]
    )


def _union(a, b) -> tuple:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _center_inside(box, outer) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return outer[0] <= cx <= outer[2] and outer[1] <= cy <= outer[3]


def words_inside(words, box) -> int:
    return sum(
        1
        for word in words
        if box[0] <= (word.x0 + word.x1) / 2 <= box[2]
        and box[1] <= (word.top + word.bottom) / 2 <= box[3]
    )


def _pictures(page, words, page_area: float) -> list:
    found = []
    for picture in page.images:
        box = _clip(_box(picture), page)
        width, height = box[2] - box[0], box[3] - box[1]
        if width < MIN_SIDE or height < MIN_SIDE:
            continue
        if width * height < MIN_PAGE_SHARE * page_area:
            continue
        source = picture.get("srcsize") or (MIN_SOURCE_PIXELS, MIN_SOURCE_PIXELS)
        try:
            if min(source) < MIN_SOURCE_PIXELS:
                continue
        except TypeError:
            pass
        if (
            width * height >= BACKGROUND_SHARE * page_area
            and words_inside(words, box) > BACKGROUND_WORDS
        ):
            continue
        found.append(box)
    return found


def _shapes(page, table_boxes, page_area: float) -> list:
    shapes = []
    for kind, items in (
        ("curve", page.curves),
        ("rect", page.rects),
        ("line", page.lines),
    ):
        for obj in items:
            box = _box(obj)
            if _area(box) > FRAME_SHARE * page_area:
                continue
            if any(_center_inside(box, table) for table in table_boxes):
                continue
            if kind == "rect" and not obj.get("fill") and not obj.get("stroke"):
                continue  # drawn in nothing: a clipping path or a hit area
            shapes.append((kind, box, obj))
    return shapes


def _clusters(shapes) -> list:
    """Shapes grouped by proximity: [[shape, ...], ...]."""
    groups: list = []  # [(box, [shapes])]
    for shape in sorted(shapes, key=lambda s: (s[1][1], s[1][0])):
        box = shape[1]
        joined = [g for g in groups if _near(g[0], box, JOIN_GAP)]
        if not joined:
            groups.append((box, [shape]))
            continue
        merged_box, merged = box, [shape]
        for group in joined:
            merged_box = _union(merged_box, group[0])
            merged.extend(group[1])
            groups.remove(group)
        groups.append((merged_box, merged))
    # One pass can leave two groups that only touch through a later shape.
    changed = True
    while changed:
        changed = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                if _near(groups[i][0], groups[j][0], JOIN_GAP):
                    box = _union(groups[i][0], groups[j][0])
                    shapes_ = groups[i][1] + groups[j][1]
                    groups[j] = (box, shapes_)
                    groups.pop(i)
                    changed = True
                    break
            if changed:
                break
    return groups


def _is_diagonal(obj) -> bool:
    return (obj["x1"] - obj["x0"]) > 2.0 and (obj["bottom"] - obj["top"]) > 2.0


def _has_bars(rects) -> bool:
    """Three or more filled bars on one baseline, or from one edge, at
    different lengths - what a bar chart is and a shaded table is not."""
    filled = [
        box
        for _, box, obj in rects
        if obj.get("fill") and box[2] - box[0] >= 2.0 and box[3] - box[1] >= 2.0
    ]
    if len(filled) < BAR_MIN:
        return False

    def grouped(edge, length):
        buckets: dict = {}
        for box in filled:
            key = round(edge(box) / BAR_EDGE_TOLERANCE)
            buckets.setdefault(key, []).append(box)
        for boxes in buckets.values():
            if len(boxes) < BAR_MIN:
                continue
            lengths = sorted(length(b) for b in boxes)
            if lengths[0] <= 0 or lengths[-1] / lengths[0] < BAR_SPREAD:
                continue
            # Bars stand apart; shaded cells tile edge to edge.
            ordered = sorted(boxes, key=lambda b: (b[0], b[1]))
            gaps = [
                max(later[0] - earlier[2], later[1] - earlier[3])
                for earlier, later in zip(ordered, ordered[1:], strict=False)
            ]
            if sum(1 for gap in gaps if gap > 1.0) >= BAR_MIN - 1:
                return True
        return False

    vertical = grouped(lambda b: b[3], lambda b: b[3] - b[1])
    horizontal = grouped(lambda b: b[0], lambda b: b[2] - b[0])
    return vertical or horizontal


def _is_drawing(shapes) -> bool:
    if len(shapes) < MIN_SHAPES:
        return False
    curves = sum(1 for kind, _, _ in shapes if kind == "curve")
    if curves >= MIN_CURVES:
        return True
    if any(kind == "line" and _is_diagonal(obj) for kind, _, obj in shapes):
        return True
    return _has_bars([s for s in shapes if s[0] == "rect"])


def _drawings(page, words, table_boxes, page_area: float) -> list:
    shapes = _shapes(page, table_boxes, page_area)
    if len(shapes) < MIN_SHAPES:
        return []
    if len(shapes) > MAX_SHAPES:
        box = shapes[0][1]
        for _, other, _ in shapes[1:]:
            box = _union(box, other)
        groups = [(box, shapes)]
    else:
        groups = _clusters(shapes)

    found = []
    for box, members in groups:
        box = _clip(box, page)
        width, height = box[2] - box[0], box[3] - box[1]
        if width < MIN_DRAWING_SIDE or height < MIN_DRAWING_SIDE:
            continue
        if width * height < MIN_DRAWING_SHARE * page_area:
            continue
        if width >= FRAME_SHARE * float(page.width) and height >= FRAME_SHARE * float(
            page.height
        ):
            continue
        if words_inside(words, box) > MAX_WORDS_INSIDE:
            continue
        if not _is_drawing(members):
            continue
        found.append(with_labels(box, words, page))
    return found


def _lines_of(words) -> list:
    """Words grouped into lines by their vertical position."""
    lines: list = []
    for word in sorted(words, key=lambda w: ((w.top + w.bottom) / 2, w.x0)):
        middle = (word.top + word.bottom) / 2
        for line in lines:
            if abs(line["middle"] - middle) <= LINE_TOLERANCE:
                line["words"].append(word)
                break
        else:
            lines.append({"middle": middle, "words": [word]})
    return [line["words"] for line in lines]


def with_labels(box, words, page) -> tuple:
    """`box` grown to take in the text that labels it.

    Once, not repeatedly: growing until nothing changes would walk down the
    page one paragraph at a time.
    """
    reach = (
        box[0] - LABEL_MARGIN,
        box[1] - LABEL_MARGIN,
        box[2] + LABEL_MARGIN,
        box[3] + LABEL_MARGIN,
    )
    grown = box
    for line in _lines_of(words):
        inside = [
            w
            for w in line
            if reach[0] <= (w.x0 + w.x1) / 2 <= reach[2]
            and reach[1] <= (w.top + w.bottom) / 2 <= reach[3]
        ]
        if not inside or len(inside) < LABEL_LINE_INSIDE * len(line):
            continue
        for word in inside:
            grown = _union(grown, (word.x0, word.top, word.x1, word.bottom))
    return _clip(grown, page)


def _clip(box, page) -> tuple:
    x0, top, x1, bottom = (float(v) for v in page.bbox)
    return (max(box[0], x0), max(box[1], top), min(box[2], x1), min(box[3], bottom))


def find(page, words, table_boxes=()) -> list:
    """The figures on a page, top to bottom, as `Region`s.

    `words` are the page's text-layer words (geometry.Word) and
    `table_boxes` the ruled tables already read as tables.
    """
    page_area = float(page.width * page.height)
    if page_area <= 0:
        return []

    boxes = [(box, "picture") for box in _pictures(page, words, page_area)]
    boxes += [
        (box, "drawing") for box in _drawings(page, words, table_boxes, page_area)
    ]

    # A picture inside a drawing (a photo in a diagram, a chart with an image
    # background) is one figure.
    merged: list = []
    for box, origin in sorted(boxes, key=lambda item: -_area(item[0])):
        for index, (kept, kept_origin) in enumerate(merged):
            smaller = min(_area(box), _area(kept)) or 1.0
            if _overlap_area(box, kept) >= 0.5 * smaller:
                merged[index] = (_union(kept, box), kept_origin)
                break
        else:
            merged.append((box, origin))

    regions = [Region(box[0], box[1], box[2], box[3], origin) for box, origin in merged]
    regions.sort(key=lambda region: (region.top, region.x0))
    return regions[:MAX_PER_PAGE]


def padded(region: Region, page) -> tuple:
    return _clip(
        (region.x0 - PAD, region.top - PAD, region.x1 + PAD, region.bottom + PAD), page
    )
