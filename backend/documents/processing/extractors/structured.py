"""JSON and XML data files, read as the records they hold.

A .json or .xml file is data, and read as the raw text it is mostly braces,
quotes and angle brackets: "fee": 85000 sits far from the "course": "CSE" it
belongs to. What is written instead is what a person would write down:

* a list of records with the same fields (a JSON array of objects, an XML
  element repeated under one parent) is a TABLE, one row per record, so a
  question about one record finds its row with its column names;
* anything else is `path: value` lines - "fees.cse.tuition: 85000" - one per
  leaf, in file order, so a value is never separated from what it is the
  value of.

JSON Lines (.jsonl, .ndjson) is a list of records, one per line. The XML
parser has entity expansion and network access off.
"""

import json
from collections import Counter

from lxml import etree

from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import compose, plain_text, spreadsheet

SOURCE_JSON = "json:records"
SOURCE_XML = "xml:records"
VERSION = "1"

MAX_LEAVES = 100_000
MAX_DEPTH = 40
MAX_COLUMNS = 60
# A list this long, of records this alike, is a table.
MIN_RECORDS = 2
MIN_SHARED = 0.6


def _scalar(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return spreadsheet.cell_text(value)
    return " ".join(str(value).split())


def _flatten(value, path: str, out: list, depth: int = 0) -> None:
    if len(out) >= MAX_LEAVES:
        return
    if depth > MAX_DEPTH:
        out.append((path, _scalar(json.dumps(value, ensure_ascii=False)[:500])))
        return
    if isinstance(value, dict):
        if not value:
            return
        for key, item in value.items():
            _flatten(item, f"{path}.{key}" if path else str(key), out, depth + 1)
    elif isinstance(value, list):
        if all(not isinstance(item, dict | list) for item in value):
            text = ", ".join(_scalar(item) for item in value)
            if text:
                out.append((path, text))
            return
        for index, item in enumerate(value, start=1):
            _flatten(item, f"{path}[{index}]", out, depth + 1)
    else:
        text = _scalar(value)
        if text:
            out.append((path, text))


def _records(items: list):
    """(columns, rows) when `items` are records alike enough to be a table."""
    if len(items) < MIN_RECORDS or not all(isinstance(i, dict) for i in items):
        return None
    counts: Counter = Counter()
    for item in items:
        counts.update(item.keys())
    columns = [key for key, n in counts.most_common() if n >= MIN_SHARED * len(items)]
    if len(columns) < 2 or len(columns) > MAX_COLUMNS:
        return None
    # Columns in the order the first record gives them.
    first = list(items[0].keys())
    columns.sort(key=lambda key: first.index(key) if key in first else len(first))
    rows = []
    for item in items:
        row = []
        for key in columns:
            value = item.get(key)
            if isinstance(value, dict | list):
                flat: list = []
                _flatten(value, "", flat)
                value = "; ".join(f"{p}: {v}" if p else v for p, v in flat)
            row.append(_scalar(value))
        rows.append(row)
    return columns, rows


def _pages_for(value, name: str, source: str) -> list:
    """Pages for a parsed document: a table when it is a list of records."""
    inner = value
    while isinstance(inner, dict) and len(inner) == 1:
        key, deeper = next(iter(inner.items()))
        if not isinstance(deeper, dict | list):
            break
        name = name or key
        inner = deeper

    if isinstance(inner, list):
        table = _records(inner)
        if table is not None:
            columns, rows = table
            return spreadsheet.build_pages(
                [columns, *rows],
                name=name,
                source=source,
                source_version=VERSION,
                metadata={"extractor": "structured"},
                # The records' keys: the columns' names, not a guess.
                header_row=True,
            )

    leaves: list = []
    _flatten(value, "", leaves)
    builder = compose.PageBuilder(source=source, source_version=VERSION)
    lines = [f"{path}: {text}" if path else text for path, text in leaves]
    if lines:
        builder.add_text("\n".join(lines))
    text, tables = builder.build()
    return [
        spreadsheet.SheetPage(
            text=text, tables=tables, metadata={"extractor": "structured"}
        )
    ]


def extract_json(file_path: str, *, lines: bool = False):
    """(pages, metadata) for a JSON or JSON Lines file."""
    try:
        with open(file_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise StageError(
            f"Could not read the file: {exc}", code=ErrorCode.EXTRACTION_FAILED
        ) from exc
    text = plain_text.decode(raw)

    try:
        if lines:
            value = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            value = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise StageError(
            f"The file is not valid JSON: {exc}", code=ErrorCode.CORRUPT_DOCUMENT
        ) from exc

    pages = _pages_for(value, "", SOURCE_JSON)
    return pages, {
        "extractor": "structured",
        "extractor_version": VERSION,
        "format": "jsonl" if lines else "json",
    }


# -- XML ---------------------------------------------------------------------


def _local(element) -> str:
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _xml_value(element):
    """An element as Python data: its text, or a dict of its attributes and
    children (a child that repeats becomes a list)."""
    children = [child for child in element if isinstance(child.tag, str)]
    attributes = {f"@{_local_name(k)}": v for k, v in element.attrib.items()}
    text = " ".join((element.text or "").split())
    if not children and not attributes:
        return text
    result: dict = dict(attributes)
    if text:
        result["#text"] = text
    grouped: dict = {}
    for child in children:
        grouped.setdefault(_local(child), []).append(_xml_value(child))
    for name, values in grouped.items():
        result[name] = values[0] if len(values) == 1 else values
    return result


def _local_name(key: str) -> str:
    return key.rsplit("}", 1)[-1]


def extract_xml(file_path: str):
    """(pages, metadata) for an XML file."""
    try:
        with open(file_path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise StageError(
            f"Could not read the file: {exc}", code=ErrorCode.EXTRACTION_FAILED
        ) from exc

    try:
        parser = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            huge_tree=False,
            recover=False,
        )
        root = etree.fromstring(raw, parser)
    except etree.XMLSyntaxError as exc:
        raise StageError(
            f"The file is not valid XML: {exc}", code=ErrorCode.CORRUPT_DOCUMENT
        ) from exc

    value = {_local(root): _xml_value(root)}
    pages = _pages_for(value, "", SOURCE_XML)
    return pages, {
        "extractor": "structured",
        "extractor_version": VERSION,
        "format": "xml",
        "root": _local(root),
    }
