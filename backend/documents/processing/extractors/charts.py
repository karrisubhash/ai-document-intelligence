"""The numbers behind a chart in an Office file, read exactly.

A chart in a Word or PowerPoint file is not a picture. Its data - every
category, every series, every value - is stored beside it in a chart part
(`word/charts/chart1.xml`, `ppt/charts/chartN.xml`), as the cache the chart
was last drawn from. Reading that is exact where a vision model would read
bars off an axis, free where a model call is not, and works with no key.

PowerPoint charts are read through python-pptx (extractors/presentation.py).
A Word document has no such library for charts, so the part is parsed here,
and the same reader serves any other file that carries a chart part.
"""

from lxml import etree

C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"

SOURCE = "ooxml:chart"

# A chart with more points than this is a data dump, not something a reader
# asks about by point; it is cut, and says so.
MAX_ROWS = 200
MAX_SERIES = 12

_PLOTS = {
    "barChart": "bar chart",
    "bar3DChart": "bar chart",
    "lineChart": "line chart",
    "line3DChart": "line chart",
    "pieChart": "pie chart",
    "pie3DChart": "pie chart",
    "ofPieChart": "pie chart",
    "doughnutChart": "doughnut chart",
    "areaChart": "area chart",
    "area3DChart": "area chart",
    "scatterChart": "scatter chart",
    "bubbleChart": "bubble chart",
    "radarChart": "radar chart",
    "stockChart": "stock chart",
    "surfaceChart": "surface chart",
}


def _q(name: str) -> str:
    return f"{{{C}}}{name}"


def _text(element) -> str:
    """The text of a title or a name: its runs, joined."""
    if element is None:
        return ""
    runs = [t.text or "" for t in element.iter(f"{{{A}}}t")]
    if runs:
        return "".join(runs).strip()
    values = [v.text or "" for v in element.iter(_q("v"))]
    return "".join(values).strip()


def _points(reference) -> list:
    """[(index, text)] of a numeric or string reference's cache."""
    if reference is None:
        return []
    cache = reference.find(f".//{_q('strCache')}")
    if cache is None:
        cache = reference.find(f".//{_q('numCache')}")
    if cache is None:
        return []
    found = []
    for point in cache.findall(_q("pt")):
        value = point.find(_q("v"))
        try:
            index = int(point.get("idx"))
        except (TypeError, ValueError):
            continue
        found.append((index, (value.text or "").strip() if value is not None else ""))
    return found


def _format_code(reference) -> str:
    code = reference.find(f".//{_q('formatCode')}") if reference is not None else None
    return (code.text or "") if code is not None else ""


def _number(text: str, code: str = "") -> str:
    """A cached value as a reader would write it: no 0.30000000000000004."""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return text
    if "%" in code:
        shown = round(value * 100, 6)
        return f"{shown:g}%"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{round(value, 6):g}"


def read(blob: bytes) -> dict | None:
    """{"title", "kind", "rows"} for a chart part, or None when it holds no
    data this reader understands. rows[0] is the header."""
    try:
        parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            huge_tree=False,
            dtd_validation=False,
        )
        root = etree.fromstring(blob, parser)
    except Exception:
        return None

    chart = root.find(_q("chart"))
    if chart is None:
        return None
    title = _text(chart.find(_q("title")))
    plot_area = chart.find(_q("plotArea"))
    if plot_area is None:
        return None

    kind = ""
    series = []  # [(name, categories{idx: text}, values{idx: text}, code)]
    scatter = False
    for plot in plot_area:
        tag = etree.QName(plot).localname
        if tag not in _PLOTS:
            continue
        kind = kind or _PLOTS[tag]
        scatter = scatter or tag in ("scatterChart", "bubbleChart")
        for item in plot.findall(_q("ser")):
            name = _text(item.find(_q("tx")))
            if scatter:
                x = item.find(_q("xVal"))
                y = item.find(_q("yVal"))
                cats, vals, code = _points(x), _points(y), _format_code(y)
            else:
                cats = _points(item.find(_q("cat")))
                values = item.find(_q("val"))
                vals, code = _points(values), _format_code(values)
            series.append((name, dict(cats), dict(vals), code))

    if not series:
        return None
    series = series[:MAX_SERIES]

    indexes = sorted({i for _, cats, vals, _ in series for i in (*cats, *vals)})
    if not indexes:
        return None
    truncated = len(indexes) > MAX_ROWS
    indexes = indexes[:MAX_ROWS]

    label = "x" if scatter else ""
    header = [label] + [
        name or f"Series {number}" for number, (name, _, _, _) in enumerate(series, 1)
    ]
    rows = [header]
    for index in indexes:
        category = next((cats[index] for _, cats, _, _ in series if index in cats), "")
        row = [category or str(index + 1)]
        for _, _, vals, code in series:
            row.append(_number(vals[index], code) if index in vals else "")
        rows.append(row)

    return {
        "title": title,
        "kind": kind or "chart",
        "rows": rows,
        "truncated": truncated,
    }
