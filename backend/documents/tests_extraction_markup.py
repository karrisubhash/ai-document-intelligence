"""Web pages, Rich Text, OpenDocument, JSON and XML: the formats beyond Office.

Every fixture is built in the test, the way the Office extractors' are. What is
pinned is what each format puts in a page that a plain-text read would lose or
spoil: a table kept as a table, spans expanded without inventing numbers,
characters in the right encoding, and what must NOT be read at all (scripts,
fonts tables, review comments).
"""

import io
import json
import os
import tempfile
import zipfile

from django.test import SimpleTestCase

from documents.processing.context import FileType
from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import (
    opendocument,
    plain_text,
    rtf,
    structured,
    webpage,
)
from documents.processing.stages import ingestion


def write(data, suffix: str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    handle.write(data)
    handle.close()
    return handle.name


class FileCase(SimpleTestCase):
    def setUp(self):
        self.paths: list = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for path in self.paths:
            if os.path.exists(path):
                os.unlink(path)

    def file(self, data, suffix: str) -> str:
        path = write(data, suffix)
        self.paths.append(path)
        return path


# -- encodings ------------------------------------------------------------------


class EncodingTests(FileCase):
    def read(self, raw: bytes) -> str:
        return plain_text.read_text(self.file(raw, ".txt"))

    def test_utf8_is_read_as_is(self):
        self.assertEqual(self.read("Fee: ₹5,000 – café".encode()), "Fee: ₹5,000 – café")

    def test_a_utf8_mark_is_not_text(self):
        self.assertEqual(self.read(b"\xef\xbb\xbfHello"), "Hello")

    def test_notepads_unicode_utf16_is_read_not_nul_riddled(self):
        raw = "Name\tMarks\nAsha\t92".encode("utf-16")
        self.assertEqual(self.read(raw), "Name\tMarks\nAsha\t92")

    def test_utf16_big_endian_with_a_mark(self):
        raw = b"\xfe\xff" + "Marks 92".encode("utf-16-be")
        self.assertEqual(self.read(raw), "Marks 92")

    def test_utf32_is_read(self):
        self.assertEqual(self.read("Marks 92".encode("utf-32")), "Marks 92")

    def test_windows_ansi_keeps_its_accents_and_the_euro_sign(self):
        raw = "Müller paid €50 for the café".encode("cp1252")
        self.assertEqual(self.read(raw), "Müller paid €50 for the café")

    def test_a_decode_never_fails_on_stray_bytes(self):
        text = self.read(b"ok \x81\x8d\x8f\x90\x9d ok")
        self.assertTrue(text.startswith("ok") and text.endswith("ok"))


# -- web pages ------------------------------------------------------------------

PAGE = """<!doctype html>
<html><head><title>Fee notice 2024</title><style>p{color:red}</style>
<script>var secret = "do not read this";</script></head>
<body>
<nav>Home | About | Contact</nav>
<h1>Fee notice</h1>
<p>Fees for <b>2024-25</b> are payable by <a href="http://x">31 July</a>.<br>Late fee applies.</p>
<div class="cookie-banner">We use cookies</div>
<div style="display:none">hidden text</div>
<ul><li>Tuition</li><li>Hostel<ul><li>Boys</li><li>Girls</li></ul></li></ul>
<ol start="3"><li>Third</li><li>Fourth</li></ol>
<table>
<caption>Annual fees</caption>
<tr><th>Course</th><th>Tuition</th><th>Hostel</th></tr>
<tr><td>CSE</td><td>85000</td><td>40000</td></tr>
<tr><td>ECE</td><td>80000</td><td>40000</td></tr>
</table>
<img src="x.png" alt="Campus map showing block A to D">
<pre>line one
  indented two</pre>
</body></html>"""


class WebPageTests(FileCase):
    def read(self, html: str):
        return webpage.extract_page(self.file(html, ".html"))

    def test_content_is_read_and_furniture_is_not(self):
        page, _ = self.read(PAGE)
        text = page.text
        self.assertIn("Fee notice 2024", text)
        self.assertIn("Fees for 2024-25 are payable by 31 July.", text)
        self.assertIn("Late fee applies.", text)
        for noise in (
            "do not read this",
            "color:red",
            "Home | About",
            "cookies",
            "hidden text",
        ):
            self.assertNotIn(noise, text)

    def test_the_title_is_the_first_line_unless_the_h1_already_says_it(self):
        page, _ = self.read(PAGE)
        self.assertEqual(page.text.split("\n")[0], "Fee notice 2024")
        page, _ = self.read(
            "<html><head><title>Notice</title></head><body><h1>notice</h1><p>x y</p></body></html>"
        )
        self.assertEqual(page.text.lower().count("notice"), 1)

    def test_lists_keep_their_markers_numbers_and_nesting(self):
        text = self.read(PAGE)[0].text
        self.assertIn("- Tuition", text)
        self.assertIn("- Hostel", text)
        self.assertIn("  - Boys", text)
        self.assertIn("3. Third", text)
        self.assertIn("4. Fourth", text)

    def test_a_table_is_a_table_with_its_header_and_a_caption(self):
        page, metadata = self.read(PAGE)
        [table] = page.tables
        self.assertEqual(table.header, ["Course", "Tuition", "Hostel"])
        self.assertEqual(table.rows[1], ["CSE", "85000", "40000"])
        self.assertIn("Annual fees", page.text)
        self.assertIn(
            "ECE | 80000 | 40000",
            page.text[table.character_start : table.character_end],
        )
        self.assertEqual(metadata["tables"], 1)

    def test_a_picture_is_read_as_its_alternative_text(self):
        self.assertIn("Campus map showing block A to D", self.read(PAGE)[0].text)

    def test_preformatted_text_keeps_its_lines_and_indent(self):
        text = self.read(PAGE)[0].text
        self.assertIn("line one\n  indented two", text)

    def test_colspan_leaves_the_other_cells_blank_and_rowspan_fills_a_label_down(self):
        html = (
            "<table>"
            "<tr><th colspan='2'>Fees</th><th>Total</th></tr>"
            "<tr><td rowspan='2'>Hostel</td><td>Boys</td><td>40000</td></tr>"
            "<tr><td>Girls</td><td>42000</td></tr>"
            "<tr><td rowspan='2'>7</td><td>x</td><td>1</td></tr>"
            "<tr><td>y</td><td>2</td></tr>"
            "</table>"
        )
        [table] = self.read(html)[0].tables
        self.assertEqual(table.rows[0], ["Fees", "", "Total"])
        self.assertEqual(table.rows[1], ["Hostel", "Boys", "40000"])
        # A label is filled down; a bare number is not copied into other rows.
        self.assertEqual(table.rows[2], ["Hostel", "Girls", "42000"])
        self.assertEqual(table.rows[4][0], "")

    def test_a_nested_table_is_text_in_its_cell_not_rows_of_the_outer_one(self):
        html = (
            "<table><tr><th>A</th><th>B</th></tr>"
            "<tr><td>one</td><td><table><tr><td>in1</td><td>in2</td></tr></table></td></tr>"
            "<tr><td>two</td><td>three</td></tr></table>"
        )
        [table] = self.read(html)[0].tables
        self.assertEqual(len(table.rows), 3)
        self.assertIn("in1", table.rows[1][1])

    def test_broken_markup_is_read_not_refused(self):
        page, _ = self.read(
            "<p>Unclosed <b>bold <i>both<p>Next paragraph<table><tr><td>cell"
        )
        self.assertIn("Unclosed bold both", page.text)
        self.assertIn("Next paragraph", page.text)

    def test_the_declared_charset_is_honoured(self):
        raw = (
            '<html><head><meta charset="windows-1252"></head><body><p>Müller €5</p></body></html>'
        ).encode("cp1252")
        page, _ = webpage.extract_page(self.file(raw, ".html"))
        self.assertIn("Müller €5", page.text)

    def test_external_entities_are_not_loaded(self):
        page, _ = self.read(
            '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><html><body><p>&e;ok</p></body></html>'
        )
        self.assertNotIn("root:", page.text)

    def test_an_empty_page_is_empty_text(self):
        page, _ = self.read("<html><body></body></html>")
        self.assertEqual(page.text, "")


# -- Rich Text ------------------------------------------------------------------

RTF = (
    r"{\rtf1\ansi\ansicpg1252\deff0"
    r"{\fonttbl{\f0 Times New Roman;}}{\colortbl;\red255\green0\blue0;}"
    r"{\stylesheet{\s1 heading 1;}}"
    r"{\info{\title Secret title}}"
    r"\pard\b Admissions\b0\par "
    r"The fee is Rs 5,000 for caf\'e9 and \u8377? for the rest.\par "
    r"A line\line break and a tab\tab here.\par "
    r"\trowd\cellx3000\cellx6000 Course\cell Fee\cell\row"
    r"\trowd\cellx3000\cellx6000 CSE\cell 85000\cell\row"
    r"\pard After the table\par"
    r"{\*\generator hidden destination;}"
    r"\pard\ls1 First item\par\pard\ls1 Second item\par"
    r"}"
)


class RtfTests(FileCase):
    def read(self, source: str):
        return rtf.extract_page(self.file(source, ".rtf"))

    def test_text_comes_out_and_font_and_info_groups_do_not(self):
        page, _ = self.read(RTF)
        self.assertIn("Admissions", page.text)
        for noise in (
            "Times New Roman",
            "Secret title",
            "heading 1",
            "hidden destination",
        ):
            self.assertNotIn(noise, page.text)

    def test_hex_and_unicode_escapes_are_decoded(self):
        text = self.read(RTF)[0].text
        self.assertIn("café", text)
        self.assertIn("₹", text)  # u8377 is 0x20B9, the rupee sign

    def test_the_fallback_character_after_a_unicode_escape_is_dropped(self):
        text = self.read(RTF)[0].text
        self.assertNotIn("₹?", text)

    def test_line_breaks_and_tabs(self):
        text = self.read(RTF)[0].text
        self.assertIn("A line\nbreak and a tab\there.", text)

    def test_rows_make_a_table(self):
        page, metadata = self.read(RTF)
        [table] = page.tables
        self.assertEqual(table.rows, [["Course", "Fee"], ["CSE", "85000"]])
        self.assertIn("After the table", page.text)
        self.assertEqual(metadata["tables"], 1)

    def test_list_items_are_marked(self):
        text = self.read(RTF)[0].text
        self.assertIn("- First item", text)
        self.assertIn("- Second item", text)

    def test_the_codepage_is_the_files_own(self):
        raw = r"{\rtf1\ansi\ansicpg1251 \'cf\'f0\'e8\'e2\'e5\'f2\par}"
        text = self.read(raw)[0].text
        self.assertEqual(text, "Привет")

    def test_a_file_that_is_not_rtf_is_refused(self):
        with self.assertRaises(StageError) as raised:
            self.read("just some text")
        self.assertEqual(raised.exception.code, ErrorCode.CORRUPT_DOCUMENT)

    def test_a_pathologically_nested_file_is_refused_not_recursed_into(self):
        with self.assertRaises(StageError):
            self.read("{\\rtf1" + "{" * 5000 + "x" + "}" * 5000 + "}")


# -- OpenDocument ---------------------------------------------------------------

NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink"'
)


def odf(mimetype: str, body: str, extra: dict | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "content.xml",
            f'<?xml version="1.0"?><office:document-content {NS}>'
            f"<office:body>{body}</office:body></office:document-content>",
        )
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


ODT_MIME = "application/vnd.oasis.opendocument.text"
ODS_MIME = "application/vnd.oasis.opendocument.spreadsheet"
ODP_MIME = "application/vnd.oasis.opendocument.presentation"

ODT_BODY = """<office:text>
<text:h text:outline-level="1">Exam rules</text:h>
<text:p>Bring your <text:span>hall ticket</text:span>.<text:s text:c="3"/>No phones<text:tab/>allowed.<text:line-break/>Second line.</text:p>
<text:p>Fee<text:note><text:note-body><text:p>FOOTNOTE BODY</text:p></text:note-body></text:note> is due.</text:p>
<text:list><text:list-item><text:p>Pens</text:p></text:list-item>
<text:list-item><text:p>Pencils</text:p><text:list><text:list-item><text:p>HB</text:p></text:list-item></text:list></text:list-item></text:list>
<table:table table:name="Fees">
<table:table-row><table:table-cell office:value-type="string"><text:p>Course</text:p></table:table-cell><table:table-cell office:value-type="string"><text:p>Fee</text:p></table:table-cell></table:table-row>
<table:table-row><table:table-cell><text:p>CSE</text:p></table:table-cell><table:table-cell office:value-type="float" office:value="85000"><text:p>85,000</text:p></table:table-cell></table:table-row>
<table:table-row><table:table-cell><text:p>ECE</text:p></table:table-cell><table:table-cell office:value-type="float" office:value="80000"/></table:table-row>
</table:table>
<text:p><draw:frame><draw:text-box><text:p>A text box note</text:p></draw:text-box></draw:frame></text:p>
<text:p><draw:frame><svg:title>Floor plan</svg:title><draw:image xlink:href="Pictures/none.png"/></draw:frame></text:p>
<text:table-of-content><text:index-body><text:p>CONTENTS 1 2 3</text:p></text:index-body></text:table-of-content>
</office:text>"""


class OpenDocumentTextTests(FileCase):
    def read(self, body=ODT_BODY, extra=None):
        path = self.file(odf(ODT_MIME, body, extra), ".odt")
        return opendocument.extract_text(path)

    def test_it_is_recognised_by_its_mimetype_entry(self):
        for mime, kind in ((ODT_MIME, "ODT"), (ODS_MIME, "ODS"), (ODP_MIME, "ODP")):
            path = self.file(odf(mime, "<office:text/>"), ".bin")
            self.assertEqual(opendocument.kind_of(path), kind)
        plain_zip = io.BytesIO()
        with zipfile.ZipFile(plain_zip, "w") as archive:
            archive.writestr("a.txt", "x")
        self.assertIsNone(opendocument.kind_of(self.file(plain_zip.getvalue(), ".zip")))

    def test_ingestion_names_the_type_from_the_archive(self):
        path = self.file(odf(ODT_MIME, ODT_BODY), ".odt")
        self.assertEqual(ingestion.detect_file_type(path), FileType.ODT)
        path = self.file(odf(ODS_MIME, "<office:spreadsheet/>"), ".ods")
        self.assertEqual(ingestion.detect_file_type(path), FileType.ODS)
        path = self.file(odf(ODP_MIME, "<office:presentation/>"), ".odp")
        self.assertEqual(ingestion.detect_file_type(path), FileType.ODP)

    def test_text_in_order_with_spaces_tabs_and_breaks(self):
        pages, metadata = self.read()
        text = pages[0].text
        self.assertTrue(text.startswith("Exam rules"))
        self.assertIn("Bring your hall ticket. No phones allowed.\nSecond line.", text)
        self.assertEqual(metadata["pagination"], "none")

    def test_footnote_bodies_and_the_table_of_contents_are_not_the_text(self):
        text = self.read()[0][0].text
        self.assertNotIn("FOOTNOTE BODY", text)
        self.assertNotIn("CONTENTS 1 2 3", text)
        self.assertIn("Fee is due.", text)

    def test_nested_lists_keep_their_depth(self):
        text = self.read()[0][0].text
        self.assertIn("- Pens", text)
        self.assertIn("- Pencils", text)
        self.assertIn("  - HB", text)

    def test_a_table_is_a_table_and_a_stored_value_stands_in_for_a_blank_display(self):
        pages, _ = self.read()
        [table] = pages[0].tables
        self.assertEqual(table.rows[0], ["Course", "Fee"])
        self.assertEqual(table.rows[1], ["CSE", "85,000"])
        self.assertEqual(table.rows[2], ["ECE", "80000"])

    def test_a_text_box_and_a_pictures_alternative_text_are_read(self):
        text = self.read()[0][0].text
        self.assertIn("A text box note", text)
        self.assertIn("Floor plan", text)

    def test_a_chart_is_the_table_of_numbers_stored_beside_it(self):
        chart = (
            f"<office:document-content {NS}><office:body><office:chart>"
            '<text:p>Admissions by year</text:p><table:table table:name="local-table">'
            '<table:table-row><table:table-cell/><table:table-cell office:value-type="string"><text:p>Students</text:p></table:table-cell></table:table-row>'
            '<table:table-row><table:table-cell office:value-type="string"><text:p>2023</text:p></table:table-cell><table:table-cell office:value-type="float" office:value="610"><text:p>610</text:p></table:table-cell></table:table-row>'
            '<table:table-row><table:table-cell office:value-type="string"><text:p>2024</text:p></table:table-cell><table:table-cell office:value-type="float" office:value="580"><text:p>580</text:p></table:table-cell></table:table-row>'
            "</table:table></office:chart></office:body></office:document-content>"
        )
        body = (
            "<office:text><text:p>Before</text:p><text:p><draw:frame>"
            '<draw:object xlink:href="./Object 1"/></draw:frame></text:p></office:text>'
        )
        pages, metadata = self.read(body, {"Object 1/content.xml": chart})
        text = pages[0].text
        self.assertIn("Admissions by year", text)
        self.assertIn("2023 | 610", text)
        self.assertEqual(metadata["charts"], 1)

    def test_a_file_with_no_content_is_corrupt(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("mimetype", ODT_MIME)
        with self.assertRaises(StageError) as raised:
            opendocument.extract_text(self.file(buffer.getvalue(), ".odt"))
        self.assertEqual(raised.exception.code, ErrorCode.CORRUPT_DOCUMENT)

    def test_a_compression_bomb_is_refused_before_parsing(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", ODT_MIME, compress_type=zipfile.ZIP_STORED)
            archive.writestr("content.xml", "<x>" + "a" * 50_000_000 + "</x>")
        with self.assertRaises(StageError):
            opendocument.extract_text(self.file(buffer.getvalue(), ".odt"))


ODS_BODY = """<office:spreadsheet>
<table:table table:name="Fees">
<table:table-row><table:table-cell office:value-type="string"><text:p>Course</text:p></table:table-cell><table:table-cell office:value-type="string"><text:p>Fee</text:p></table:table-cell><table:table-cell office:value-type="string"><text:p>Share</text:p></table:table-cell></table:table-row>
<table:table-row><table:table-cell office:value-type="string"><text:p>CSE</text:p></table:table-cell><table:table-cell office:value-type="float" office:value="85000"><text:p>85,000</text:p></table:table-cell><table:table-cell office:value-type="percentage" office:value="0.55"/></table:table-row>
<table:table-row table:number-rows-repeated="3"><table:table-cell office:value-type="string"><text:p>ECE</text:p></table:table-cell><table:table-cell table:number-columns-repeated="2" office:value-type="float" office:value="1"/></table:table-row>
<table:table-row table:number-rows-repeated="1048000"><table:table-cell table:number-columns-repeated="1024"/></table:table-row>
</table:table>
<table:table table:name="Hidden" table:display="false"><table:table-row><table:table-cell office:value-type="string"><text:p>secret</text:p></table:table-cell></table:table-row></table:table>
<table:table table:name="Rooms">
<table:table-row><table:table-cell office:value-type="string"><text:p>Room</text:p></table:table-cell><table:table-cell office:value-type="string"><text:p>Seats</text:p></table:table-cell></table:table-row>
<table:table-row><table:table-cell office:value-type="string"><text:p>A1</text:p></table:table-cell><table:table-cell office:value-type="float" office:value="60"><text:p>60</text:p></table:table-cell></table:table-row>
</table:table>
</office:spreadsheet>"""


class OpenDocumentSheetTests(FileCase):
    def read(self):
        return opendocument.extract_sheets(self.file(odf(ODS_MIME, ODS_BODY), ".ods"))

    def test_a_page_per_visible_sheet_named_as_the_sheet(self):
        pages, metadata = self.read()
        self.assertEqual(len(pages), 2)
        self.assertTrue(pages[0].text.startswith("Sheet: Fees"))
        self.assertTrue(pages[1].text.startswith("Sheet: Rooms"))
        self.assertEqual(metadata["sheets"], 2)
        self.assertEqual(metadata["sheets_hidden"], 1)
        self.assertNotIn("secret", pages[0].text + pages[1].text)

    def test_values_as_displayed_or_as_stored_and_repeats_expanded_within_limits(self):
        pages, _ = self.read()
        [table] = pages[0].tables
        self.assertEqual(table.header, ["Course", "Fee", "Share"])
        self.assertEqual(table.rows[1], ["CSE", "85,000", "55%"])
        self.assertEqual(table.rows[2], ["ECE", "1", "1"])
        self.assertEqual(len(table.rows), 5)  # header, CSE, ECE x3

    def test_a_million_empty_rows_do_not_become_a_million_rows(self):
        pages, metadata = self.read()
        self.assertLess(metadata["rows"], 20)


ODP_BODY = """<office:presentation>
<draw:page draw:name="s1">
<draw:frame presentation:class="subtitle"><draw:text-box><text:p>Subtitle line</text:p></draw:text-box></draw:frame>
<draw:frame presentation:class="title"><draw:text-box><text:p>Orientation day</text:p></draw:text-box></draw:frame>
<presentation:notes><draw:frame><draw:text-box><text:p>Remember the mic</text:p></draw:text-box></draw:frame></presentation:notes>
</draw:page>
<draw:page draw:name="s2">
<draw:frame presentation:class="title"><draw:text-box><text:p>Schedule</text:p></draw:text-box></draw:frame>
<draw:frame><table:table><table:table-row><table:table-cell><text:p>Time</text:p></table:table-cell><table:table-cell><text:p>Event</text:p></table:table-cell></table:table-row><table:table-row><table:table-cell><text:p>9:00</text:p></table:table-cell><table:table-cell><text:p>Welcome</text:p></table:table-cell></table:table-row></table:table></draw:frame>
</draw:page>
</office:presentation>"""


class OpenDocumentSlideTests(FileCase):
    def read(self):
        return opendocument.extract_slides(self.file(odf(ODP_MIME, ODP_BODY), ".odp"))

    def test_a_page_per_slide_with_the_title_first_and_the_notes_last(self):
        pages, metadata = self.read()
        self.assertEqual(metadata["slides"], 2)
        first = pages[0].text
        self.assertLess(first.index("Orientation day"), first.index("Subtitle line"))
        self.assertTrue(first.rstrip().endswith("Notes: Remember the mic"))
        self.assertEqual(pages[0].metadata["slide"], 1)

    def test_a_slide_table_is_a_table(self):
        pages, _ = self.read()
        [table] = pages[1].tables
        self.assertEqual(table.rows, [["Time", "Event"], ["9:00", "Welcome"]])

    def test_the_slides_are_counted_for_validation(self):
        path = self.file(odf(ODP_MIME, ODP_BODY), ".odp")
        self.assertEqual(opendocument.slide_count(path), 2)


# -- JSON and XML ---------------------------------------------------------------


class JsonTests(FileCase):
    def read(self, value, suffix=".json", **options):
        data = value if isinstance(value, str) else json.dumps(value)
        return structured.extract_json(self.file(data, suffix), **options)

    def test_a_list_of_records_is_a_table_with_its_column_names(self):
        pages, _ = self.read(
            [
                {"course": "CSE", "fee": 85000, "seats": 60},
                {"course": "ECE", "fee": 80000, "seats": 40},
            ]
        )
        [table] = pages[0].tables
        self.assertEqual(table.header, ["course", "fee", "seats"])
        self.assertEqual(table.rows[2], ["ECE", "80000", "40"])

    def test_records_under_a_single_key_are_unwrapped_and_the_key_names_the_sheet(self):
        pages, _ = self.read(
            {"students": [{"name": "Asha", "marks": 92}, {"name": "Ravi", "marks": 88}]}
        )
        self.assertTrue(pages[0].text.startswith("Sheet: students"))
        self.assertEqual(pages[0].tables[0].rows[1], ["Asha", "92"])

    def test_nested_data_is_path_value_lines(self):
        pages, _ = self.read(
            {
                "fees": {"cse": {"tuition": 85000, "hostel": 40000}},
                "year": "2024-25",
                "ok": True,
            }
        )
        text = pages[0].text
        self.assertIn("fees.cse.tuition: 85000", text)
        self.assertIn("fees.cse.hostel: 40000", text)
        self.assertIn("year: 2024-25", text)
        self.assertIn("ok: true", text)

    def test_a_list_of_plain_values_is_one_line(self):
        pages, _ = self.read(
            {"subjects": ["Maths", "Physics"], "empty": [], "none": None}
        )
        self.assertIn("subjects: Maths, Physics", pages[0].text)
        self.assertNotIn("none", pages[0].text)

    def test_records_with_different_fields_are_not_forced_into_a_table(self):
        pages, _ = self.read([{"a": 1, "b": 2}, {"x": 9, "y": 8}])
        self.assertEqual(pages[0].tables, [])
        self.assertIn("[1].a: 1", pages[0].text)
        self.assertIn("[2].x: 9", pages[0].text)

    def test_json_lines_are_records(self):
        pages, metadata = self.read(
            '{"k": "a", "v": 1}\n{"k": "b", "v": 2}\n', ".jsonl", lines=True
        )
        self.assertEqual(pages[0].tables[0].rows[2], ["b", "2"])
        self.assertEqual(metadata["format"], "jsonl")

    def test_a_record_with_a_nested_value_keeps_it_in_its_cell(self):
        pages, _ = self.read(
            [
                {"name": "A", "contact": {"phone": "123", "city": "X"}},
                {"name": "B", "contact": {"phone": "456", "city": "Y"}},
            ]
        )
        self.assertIn("phone: 123", pages[0].tables[0].rows[1][1])

    def test_invalid_json_is_corrupt_not_text(self):
        with self.assertRaises(StageError) as raised:
            self.read("{not json")
        self.assertEqual(raised.exception.code, ErrorCode.CORRUPT_DOCUMENT)

    def test_a_json_file_in_another_encoding_is_read(self):
        data = json.dumps({"name": "Müller"}, ensure_ascii=False).encode("utf-16")
        pages, _ = structured.extract_json(self.file(data, ".json"))
        self.assertIn("name: Müller", pages[0].text)

    def test_deeply_nested_data_is_bounded(self):
        value: object = "leaf"
        for _ in range(structured.MAX_DEPTH + 30):
            value = {"k": value}
        pages, _ = structured.extract_json(self.file(json.dumps(value), ".json"))
        self.assertTrue(pages[0].text)


XML = """<?xml version="1.0"?>
<catalog>
  <book id="b1"><title>Algebra</title><price>450</price></book>
  <book id="b2"><title>Calculus</title><price>520</price></book>
  <book id="b3"><title>Optics</title><price>380</price></book>
</catalog>"""


class XmlTests(FileCase):
    def read(self, source):
        return structured.extract_xml(self.file(source, ".xml"))

    def test_repeated_elements_are_a_table_with_attributes_as_columns(self):
        pages, metadata = self.read(XML)
        [table] = pages[0].tables
        self.assertEqual(table.header, ["@id", "title", "price"])
        self.assertEqual(table.rows[2], ["b2", "Calculus", "520"])
        self.assertEqual(metadata["root"], "catalog")

    def test_a_single_record_is_path_value_lines(self):
        pages, _ = self.read(
            "<notice><date>2024-07-31</date><fees><cse>85000</cse></fees></notice>"
        )
        self.assertIn("notice.date: 2024-07-31", pages[0].text)
        self.assertIn("notice.fees.cse: 85000", pages[0].text)

    def test_entities_are_not_expanded(self):
        pages, _ = self.read(
            '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x><a>&e;</a><b>ok</b></x>'
        )
        self.assertNotIn("root:", pages[0].text)

    def test_invalid_xml_is_corrupt(self):
        with self.assertRaises(StageError):
            self.read("<a><b></a>")


# -- detection ------------------------------------------------------------------


class DetectionTests(FileCase):
    def detect(self, data, suffix):
        return ingestion.detect_file_type(self.file(data, suffix))

    def test_rtf_is_known_by_its_signature_whatever_it_is_called(self):
        self.assertEqual(self.detect(RTF, ".txt"), FileType.RTF)
        self.assertEqual(self.detect(RTF, ".doc"), FileType.RTF)

    def test_text_formats_are_known_by_extension_when_they_are_text(self):
        for suffix, expected in (
            (".html", FileType.HTML),
            (".htm", FileType.HTML),
            (".json", FileType.JSON),
            (".jsonl", FileType.JSON),
            (".xml", FileType.XML),
            (".md", FileType.TXT),
            (".log", FileType.TXT),
            (".yaml", FileType.TXT),
        ):
            self.assertEqual(self.detect("hello world", suffix), expected, suffix)

    def test_a_binary_renamed_to_a_text_format_is_not_believed(self):
        blob = bytes(range(256)) * 4
        for suffix in (".html", ".json", ".xml", ".csv"):
            self.assertIsNone(self.detect(blob, suffix), suffix)

    def test_every_new_extension_is_accepted_for_upload(self):
        from documents.uploads import SUPPORTED_EXTENSIONS

        for extension in (
            ".rtf",
            ".odt",
            ".ods",
            ".odp",
            ".html",
            ".htm",
            ".json",
            ".xml",
            ".md",
            ".log",
        ):
            self.assertIn(extension, SUPPORTED_EXTENSIONS)
