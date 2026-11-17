"""Rich Text Format: the text and the tables of a .rtf file.

RTF is a stream of groups, control words and text. Reading it is mostly a
matter of knowing what NOT to read: the font table, the colour table, the
stylesheet, headers and embedded pictures are all groups full of text-like
data that is not the document's. What is read:

* paragraphs (\\par), line breaks and tabs;
* characters written as \\'hh in the document's code page (cp1252 unless the
  file says otherwise) and as \\uN Unicode, with the fallback characters that
  follow a \\uN skipped as the format requires;
* tables: \\trowd begins a row, \\cell ends a cell and \\row ends the row;
  consecutive rows are one table;
* list items, bulleted or numbered, as lines starting "-".

Nothing is executed; \\object and \\pict are skipped whole.
"""

import re
from dataclasses import dataclass, field

from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import compose

SOURCE = "rtf:document"
VERSION = "1"

# Groups whose content is not the document's.
DESTINATIONS = frozenset(
    {
        "fonttbl",
        "colortbl",
        "stylesheet",
        "info",
        "pict",
        "object",
        "objdata",
        "themedata",
        "colorschememapping",
        "datastore",
        "latentstyles",
        "listtable",
        "listoverridetable",
        "rsidtbl",
        "generator",
        "header",
        "headerl",
        "headerr",
        "headerf",
        "footer",
        "footerl",
        "footerr",
        "footerf",
        "fldinst",
        "xmlnstbl",
        "mmathPr",
        "private",
        "bkmkstart",
        "bkmkend",
        "nonshppict",
        "shpinst",
        "wgrffmtfilter",
        "pgdsctbl",
        "revtbl",
        "protusertbl",
        "userprops",
        "falt",
        "panose",
        "pn",
        "pntxta",
        "pntxtb",
        "listtext",
    }
)

MAX_BYTES = 50 * 1024 * 1024
MAX_DEPTH = 200

_WORD = re.compile(rb"\\([a-zA-Z]{1,32})(-?\d{1,10})? ?")
_SYMBOL = re.compile(rb"\\([^a-zA-Z])")


@dataclass
class RtfPage:
    text: str = ""
    tables: list = field(default_factory=list)


class _State:
    __slots__ = ("skip", "unicode_skip", "in_list")

    def __init__(self, skip=False, unicode_skip=1, in_list=False):
        self.skip = skip
        self.unicode_skip = unicode_skip
        self.in_list = in_list

    def copy(self):
        return _State(self.skip, self.unicode_skip, self.in_list)


def _codepage(data: bytes) -> str:
    match = re.search(rb"\\ansicpg(\d+)", data[:2000])
    if match:
        name = f"cp{match.group(1).decode()}"
        try:
            b"a".decode(name)
            return name
        except LookupError:
            pass
    return "cp1252"


def extract_page(file_path: str) -> tuple:
    try:
        with open(file_path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        raise StageError(
            f"Could not read the file: {exc}", code=ErrorCode.EXTRACTION_FAILED
        ) from exc

    if not data.lstrip().startswith(b"{\\rtf"):
        raise StageError(
            "The file is not RTF: it does not begin with {\\rtf.",
            code=ErrorCode.CORRUPT_DOCUMENT,
        )
    truncated = len(data) > MAX_BYTES
    data = data[:MAX_BYTES]

    codepage = _codepage(data)
    builder = compose.PageBuilder(source=SOURCE, source_version=VERSION)

    paragraph: list = []  # text of the paragraph being read
    cells: list = []  # cells of the row being read
    rows: list = []  # rows of the table being read
    in_row = False
    stack: list = []
    state = _State()
    pending_bytes = bytearray()

    def flush_bytes():
        if pending_bytes:
            paragraph.append(bytes(pending_bytes).decode(codepage, errors="replace"))
            pending_bytes.clear()

    def end_paragraph():
        flush_bytes()
        text = "".join(paragraph).strip()
        paragraph.clear()
        return text

    def emit_paragraph():
        text = end_paragraph()
        if not text:
            return
        if state.in_list and not text.startswith(("-", "*")):
            text = f"- {text}"
        builder.add_text(text)

    def end_table():
        nonlocal rows
        if rows:
            builder.add_table(rows)
        rows = []

    i = 0
    n = len(data)
    while i < n:
        byte = data[i : i + 1]

        if byte == b"{":
            stack.append(state)
            if len(stack) > MAX_DEPTH:
                raise StageError(
                    "The RTF is nested too deeply to be real.",
                    code=ErrorCode.CORRUPT_DOCUMENT,
                )
            state = state.copy()
            i += 1
            continue
        if byte == b"}":
            flush_bytes()
            if stack:
                state = stack.pop()
            i += 1
            continue
        if byte in (b"\r", b"\n"):
            i += 1
            continue

        if byte == b"\\":
            symbol = _SYMBOL.match(data, i)
            if symbol:
                char = symbol.group(1)
                i = symbol.end()
                if char == b"*":
                    # An optional destination: skipped unless we know it.
                    state.skip = True
                elif char == b"'":
                    code = data[i : i + 2]
                    i += 2
                    if not state.skip:
                        try:
                            pending_bytes.append(int(code, 16))
                        except ValueError:
                            pass
                elif char in (b"~",):
                    if not state.skip:
                        flush_bytes()
                        paragraph.append("\u00a0")
                elif char == b"-":
                    pass  # an optional hyphen
                elif char == b"_":
                    if not state.skip:
                        flush_bytes()
                        paragraph.append("-")
                elif char in (b"\\", b"{", b"}"):
                    if not state.skip:
                        flush_bytes()
                        paragraph.append(char.decode())
                continue

            word = _WORD.match(data, i)
            if not word:
                i += 1
                continue
            name = word.group(1).decode()
            argument = word.group(2)
            i = word.end()

            if name in DESTINATIONS:
                state.skip = True
                continue
            if state.skip:
                continue

            if name == "uc":
                state.unicode_skip = int(argument) if argument else 1
            elif name == "u":
                flush_bytes()
                code = int(argument) if argument else 0
                if code < 0:
                    code += 65536
                try:
                    paragraph.append(chr(code))
                except ValueError:
                    pass
                # The fallback characters that follow are not text.
                skip = state.unicode_skip
                while skip > 0 and i < n:
                    if data[i : i + 2] == b"\\'":
                        i += 4
                    elif data[i : i + 1] in (b"\\", b"{", b"}"):
                        break
                    else:
                        i += 1
                    skip -= 1
            elif name in ("par", "sect", "page"):
                if in_row:
                    # A paragraph mark inside a cell is a line break in it.
                    flush_bytes()
                    paragraph.append("\n")
                else:
                    end_table()
                    emit_paragraph()
            elif name in ("line", "lbr"):
                flush_bytes()
                paragraph.append("\n")
            elif name == "tab":
                flush_bytes()
                paragraph.append("\t")
            elif name == "emdash":
                flush_bytes()
                paragraph.append("\u2014")
            elif name == "endash":
                flush_bytes()
                paragraph.append("\u2013")
            elif name == "bullet":
                flush_bytes()
                paragraph.append("\u2022")
            elif name in ("lquote", "rquote"):
                flush_bytes()
                paragraph.append("'")
            elif name in ("ldblquote", "rdblquote"):
                flush_bytes()
                paragraph.append('"')
            elif name == "trowd":
                end_paragraph()
                in_row = True
                cells = []
            elif name == "cell":
                flush_bytes()
                cells.append(" ".join("".join(paragraph).split()))
                paragraph.clear()
            elif name == "row":
                if cells and any(cells):
                    rows.append(cells)
                cells = []
                in_row = False
            elif name == "pntext" or name == "listtext":
                state.skip = True
            elif name == "ls":
                state.in_list = True
            elif name == "pard":
                state.in_list = False
            continue

        # Plain text.
        if state.skip:
            i += 1
            continue
        j = i
        while j < n and data[j : j + 1] not in (b"{", b"}", b"\\", b"\r", b"\n"):
            j += 1
        chunk = data[i:j]
        # Text outside \'hh is in the document's code page, too.
        pending_bytes.extend(chunk)
        i = j

    if not in_row:
        end_table()
    emit_paragraph()
    end_table()

    text, tables = builder.build()
    return RtfPage(text=text, tables=tables), {
        "extractor": "rtf",
        "extractor_version": VERSION,
        "codepage": codepage,
        "truncated": truncated,
        "tables": len(tables),
    }
