"""DOCX: what the paragraph and table accessors skip, and the walk that keeps it.

Each fixture is a real .docx built in the test. python-docx makes the
ordinary parts; the structures it cannot make - content controls, tracked
changes, text boxes, merged cells, a numbering recipe, footnotes - are
injected as raw XML, so each test shows exactly the markup it is about.
"""

import io
import os
import tempfile
import zipfile
from unittest import mock

import docx
from django.test import SimpleTestCase
from docx.oxml import parse_xml
from PIL import Image, ImageDraw, ImageFont

from documents.processing.extractors import docx as docx_extractor
from documents.processing.extractors.listmarks import format_number
from documents.processing.extractors.ocr import OcrPage

NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"'
)


def para(text, props=""):
    return f'<w:p {NS}><w:pPr>{props}</w:pPr><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def cell(text, props=""):
    return (
        f"<w:tc><w:tcPr>{props}</w:tcPr><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"
    )


def table(rows_xml):
    return f"<w:tbl {NS}><w:tblPr/>{rows_xml}</w:tbl>"


def row(*cells):
    return f"<w:tr>{''.join(cells)}</w:tr>"


class Build:
    """A document under construction."""

    def __init__(self):
        self.document = docx.Document()

    def add(self, xml):
        body = self.document.element.body
        element = parse_xml(xml)
        # Before the section properties, which must stay last.
        body.insert(len(body) - 1, element)
        return self

    def numbering(self, abstract_xml, num_id):
        numbering = self.document.part.numbering_part.element
        numbering.insert(0, parse_xml(abstract_xml))
        numbering.append(
            parse_xml(
                f'<w:num {NS} w:numId="{num_id}">'
                f'<w:abstractNumId w:val="{num_id}"/></w:num>'
            )
        )
        return self

    def bytes(self, extra_parts=None):
        buffer = io.BytesIO()
        self.document.save(buffer)
        if not extra_parts:
            return buffer.getvalue()

        out = io.BytesIO()
        with (
            zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as source,
            zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target,
        ):
            for item in source.infolist():
                target.writestr(item, source.read(item.filename))
            for name, content in extra_parts.items():
                target.writestr(name, content)
        return out.getvalue()


def extract(build, extra_parts=None, **options):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "fixture.docx")
        with open(path, "wb") as handle:
            handle.write(build.bytes(extra_parts))
        return docx_extractor.extract_page_results(path, **options)


def text_of(build, **kwargs):
    pages, _ = extract(build, **kwargs)
    return "\n\f\n".join(page.text for page in pages)


class StructureTests(SimpleTestCase):
    def test_a_table_is_stored_as_rows_of_cells_in_document_order(self):
        build = Build().add(para("Fee schedule"))
        build.add(
            table(
                row(cell("Course"), cell("Fee"))
                + row(cell("CS101"), cell("5000"))
                + row(cell("CS102"), cell(""))
            )
        )
        build.add(para("Payable in advance."))

        (page,), metadata = extract(build)

        self.assertEqual(
            page.text,
            "Fee schedule\n\nCourse | Fee\nCS101 | 5000\nCS102 |\n\nPayable in advance.",
        )
        self.assertEqual(
            page.tables[0].rows, [["Course", "Fee"], ["CS101", "5000"], ["CS102", ""]]
        )
        self.assertEqual(page.tables[0].source, "python-docx:table")
        self.assertEqual(metadata["tables"], 1)

    def test_content_controls_are_read_in_the_body_and_inline(self):
        build = Build()
        build.add(
            f"<w:sdt {NS}><w:sdtPr/><w:sdtContent>"
            f"<w:p><w:r><w:t>Name: Asha Rao</w:t></w:r></w:p>"
            f"</w:sdtContent></w:sdt>"
        )
        build.add(
            f'<w:p {NS}><w:r><w:t xml:space="preserve">Roll no: </w:t></w:r>'
            f"<w:sdt><w:sdtPr/><w:sdtContent><w:r><w:t>21CS045</w:t></w:r>"
            f"</w:sdtContent></w:sdt></w:p>"
        )

        self.assertEqual(text_of(build), "Name: Asha Rao\nRoll no: 21CS045")

    def test_tracked_insertions_are_kept_and_deletions_are_not(self):
        build = Build().add(
            f'<w:p {NS}><w:r><w:t xml:space="preserve">Keep </w:t></w:r>'
            f'<w:ins><w:r><w:t xml:space="preserve">inserted </w:t></w:r></w:ins>'
            f"<w:del><w:r><w:delText>deleted </w:delText></w:r></w:del>"
            f"<w:r><w:t>end</w:t></w:r></w:p>"
        )

        self.assertEqual(text_of(build), "Keep inserted end")

    def test_a_text_box_is_read_once_not_once_per_compatibility_branch(self):
        """mc:AlternateContent holds the same box twice - Choice for newer
        Word, Fallback for older - so reading both doubles every notice."""
        box = "<w:txbxContent><w:p><w:r><w:t>NOTICE TO STUDENTS</w:t></w:r></w:p></w:txbxContent>"
        build = Build().add(
            f"<w:p {NS}><w:r><mc:AlternateContent>"
            f'<mc:Choice Requires="wps"><w:drawing><a:graphic><a:graphicData>'
            f"<wps:wsp><wps:txbx>{box}</wps:txbx></wps:wsp>"
            f"</a:graphicData></a:graphic></w:drawing></mc:Choice>"
            f"<mc:Fallback><w:pict><v:shape><v:textbox>{box}</v:textbox></v:shape></w:pict></mc:Fallback>"
            f"</mc:AlternateContent></w:r></w:p>"
        )

        pages, metadata = extract(build)

        self.assertEqual(pages[0].text, "NOTICE TO STUDENTS")
        self.assertEqual(metadata["text_boxes"], 1)

    def test_horizontally_merged_cells_hold_their_text_once(self):
        build = Build().add(
            table(
                row(
                    cell("Fees", '<w:gridSpan w:val="2"/>'),
                    cell("Notes"),
                )
                + row(cell("Tuition"), cell("85000"), cell("Annual"))
                + row(cell("Exam"), cell("2500"), cell("Per term"))
            )
        )

        (page,), _ = extract(build)

        self.assertEqual(
            page.tables[0].rows,
            [
                ["Fees", "", "Notes"],
                ["Tuition", "85000", "Annual"],
                ["Exam", "2500", "Per term"],
            ],
        )
        self.assertEqual(page.text.count("Fees"), 1)

    def test_vertically_merged_cells_continue_as_blanks(self):
        build = Build().add(
            table(
                row(cell("Group"), cell("Item"))
                + row(cell("Hostel", '<w:vMerge w:val="restart"/>'), cell("Rent"))
                + row(cell("", "<w:vMerge/>"), cell("Mess"))
            )
        )

        (page,), _ = extract(build)

        self.assertEqual(
            page.tables[0].rows,
            [["Group", "Item"], ["Hostel", "Rent"], ["", "Mess"]],
        )

    def test_a_nested_table_is_inlined_into_its_cell(self):
        inner = table(row(cell("A"), cell("1")) + row(cell("B"), cell("2")))
        outer_cell = (
            f"<w:tc><w:tcPr/><w:p><w:r><w:t>Breakdown</w:t></w:r></w:p>{inner}</w:tc>"
        )
        build = Build().add(
            table(row(cell("Head"), cell("Detail")) + row(cell("Fees"), outer_cell))
        )

        (page,), _ = extract(build)

        self.assertEqual(page.tables[0].rows[1], ["Fees", "Breakdown A / 1; B / 2"])

    def test_a_one_cell_table_is_a_boxed_paragraph_not_a_table(self):
        box = (
            "<w:tc><w:tcPr/>"
            "<w:p><w:r><w:t>First line of the notice</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Second line of the notice</w:t></w:r></w:p></w:tc>"
        )
        build = Build().add(table(row(box)))

        (page,), _ = extract(build)

        self.assertEqual(page.tables, [])
        self.assertEqual(
            page.text, "First line of the notice\nSecond line of the notice"
        )

    def test_math_hyperlinks_and_tabs_are_text(self):
        build = Build()
        build.add(
            f"<w:p {NS}><m:oMathPara><m:oMath><m:r><m:t>E=mc2</m:t></m:r></m:oMath></m:oMathPara></w:p>"
        )
        build.add(
            f'<w:p {NS}><w:hyperlink r:id="rId99"><w:r><w:t>college website</w:t></w:r></w:hyperlink></w:p>'
        )
        build.add(f"<w:p {NS}><w:r><w:t>Name:</w:t><w:tab/><w:t>Asha</w:t></w:r></w:p>")

        self.assertEqual(text_of(build), "E=mc2\ncollege website\nName:\tAsha")


class NumberingTests(SimpleTestCase):
    MULTILEVEL = (
        f'<w:abstractNum {NS} w:abstractNumId="90">'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
        '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2."/></w:lvl>'
        '<w:lvl w:ilvl="2"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="(%3)"/></w:lvl>'
        "</w:abstractNum>"
    )

    def numbered(self, text, level, num_id=90):
        return para(
            text,
            f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/></w:numPr>',
        )

    def test_clause_numbers_are_generated_and_restart_under_a_new_parent(self):
        build = Build().numbering(self.MULTILEVEL, 90)
        for text, level in [
            ("Admission", 0),
            ("Eligibility", 1),
            ("Seats", 1),
            ("Fees", 0),
            ("Tuition", 1),
            ("Instalments", 2),
            ("Late fee", 2),
        ]:
            build.add(self.numbered(text, level))

        self.assertEqual(
            text_of(build).split("\n"),
            [
                "1. Admission",
                "  1.1. Eligibility",
                "  1.2. Seats",
                "2. Fees",
                "  2.1. Tuition",
                "    (a) Instalments",
                "    (b) Late fee",
            ],
        )

    def test_an_empty_numbered_paragraph_still_takes_its_number(self):
        build = Build().numbering(self.MULTILEVEL, 90)
        build.add(self.numbered("First", 0))
        build.add(self.numbered("", 0))
        build.add(self.numbered("Third", 0))

        self.assertEqual(text_of(build).split("\n"), ["1. First", "3. Third"])

    def test_the_built_in_list_styles_number_and_bullet(self):
        build = Build()
        build.document.add_paragraph("Alpha", style="List Number")
        build.document.add_paragraph("Beta", style="List Number")
        build.document.add_paragraph("Gamma", style="List Bullet")

        lines = text_of(build).split("\n")

        self.assertEqual(lines[0], "1. Alpha")
        self.assertEqual(lines[1], "2. Beta")
        self.assertEqual(lines[2], "• Gamma")

    def test_roman_and_letter_formats(self):
        self.assertEqual(format_number(4, "lowerRoman"), "iv")
        self.assertEqual(format_number(14, "upperRoman"), "XIV")
        self.assertEqual(format_number(27, "lowerLetter"), "aa")
        self.assertEqual(format_number(3, "upperLetter"), "C")
        self.assertEqual(format_number(7, "decimalZero"), "07")

    def test_numbering_inside_a_table_cell_is_kept(self):
        numbered = (
            '<w:tc><w:tcPr/><w:p><w:pPr><w:numPr><w:ilvl w:val="0"/>'
            '<w:numId w:val="90"/></w:numPr></w:pPr><w:r><w:t>Library card</w:t></w:r></w:p></w:tc>'
        )
        build = Build().numbering(self.MULTILEVEL, 90)
        build.add(table(row(cell("Step"), cell("Do")) + row(cell("One"), numbered)))

        (page,), _ = extract(build)

        self.assertEqual(page.tables[0].rows[1], ["One", "1. Library card"])


class PageTests(SimpleTestCase):
    def test_a_break_in_the_middle_of_a_paragraph_splits_the_page_there(self):
        build = Build().add(
            f'<w:p {NS}><w:r><w:t>Before</w:t><w:br w:type="page"/><w:t>After</w:t></w:r></w:p>'
        )

        pages, metadata = extract(build)

        self.assertEqual([p.text for p in pages], ["Before", "After"])
        self.assertEqual(metadata["pagination"], "explicit_breaks")

    def test_page_break_before_and_section_breaks_start_pages(self):
        build = Build()
        build.add(para("One"))
        build.add(para("Two", "<w:pageBreakBefore/>"))
        build.add(para("Three", '<w:sectPr><w:type w:val="nextPage"/></w:sectPr>'))
        build.add(para("Four"))
        build.add(para("Five", '<w:sectPr><w:type w:val="continuous"/></w:sectPr>'))
        build.add(para("Six"))

        pages, _ = extract(build)

        self.assertEqual(
            [p.text for p in pages], ["One", "Two\nThree", "Four\nFive\nSix"]
        )

    def test_repeated_and_trailing_breaks_do_not_make_empty_pages(self):
        brk = f'<w:p {NS}><w:r><w:br w:type="page"/></w:r></w:p>'
        build = Build().add(brk).add(para("Body")).add(brk).add(brk)

        pages, metadata = extract(build)

        self.assertEqual([p.text for p in pages], ["Body"])
        self.assertEqual(metadata["pagination"], "none")

    def test_tables_stay_with_the_page_they_are_on(self):
        build = Build()
        build.add(table(row(cell("a"), cell("1")) + row(cell("b"), cell("2"))))
        build.add(f'<w:p {NS}><w:r><w:br w:type="page"/></w:r></w:p>')
        build.add(table(row(cell("c"), cell("3")) + row(cell("d"), cell("4"))))

        pages, _ = extract(build)

        self.assertEqual([len(p.tables) for p in pages], [1, 1])
        self.assertEqual(pages[1].tables[0].rows, [["c", "3"], ["d", "4"]])


class HeaderFooterNoteTests(SimpleTestCase):
    def test_headers_and_footers_appear_once_at_the_page_edges(self):
        build = Build().add(para("Body one"))
        build.add(f'<w:p {NS}><w:r><w:br w:type="page"/></w:r></w:p>')
        build.add(para("Body two"))
        section = build.document.sections[0]
        section.header.paragraphs[0].text = "ACME COLLEGE"
        section.footer.paragraphs[0].text = "Confidential"

        pages, metadata = extract(build)

        self.assertEqual(pages[0].text, "ACME COLLEGE\nBody one")
        self.assertEqual(pages[1].text, "Body two\nConfidential")
        self.assertEqual(metadata["headers_footers"], 2)

    def test_a_document_opening_with_a_page_break_has_no_header_only_page(self):
        build = Build().add(f'<w:p {NS}><w:r><w:br w:type="page"/></w:r></w:p>')
        build.add(para("Real content"))
        build.document.sections[0].header.paragraphs[0].text = "HEADER"

        pages, _ = extract(build)

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].text, "HEADER\nReal content")

    def test_footnotes_get_a_marker_in_the_text_and_a_line_at_the_end(self):
        build = Build().add(
            f"<w:p {NS}><w:r><w:t>Enrolment rose</w:t></w:r>"
            f'<w:r><w:footnoteReference w:id="2"/></w:r></w:p>'
        )
        notes = (
            f"<w:footnotes {NS}>"
            '<w:footnote w:type="separator" w:id="0"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
            '<w:footnote w:id="2"><w:p><w:r><w:footnoteRef/></w:r>'
            '<w:r><w:t xml:space="preserve"> Source: annual report</w:t></w:r></w:p></w:footnote>'
            "</w:footnotes>"
        )

        pages, metadata = extract(build, {"word/footnotes.xml": notes})

        self.assertEqual(
            pages[0].text, "Enrolment rose[fn1]\n[fn1] Source: annual report"
        )
        self.assertEqual(metadata["notes"], 1)


def picture_png(text="INVOICE 12345", size=(800, 220)):
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text(
        (30, 70), text, fill="black", font=ImageFont.load_default(size=56)
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class PictureTests(SimpleTestCase):
    def with_picture(self, payload=None):
        build = Build().add(para("Scanned form follows"))
        build.document.add_picture(io.BytesIO(payload or picture_png()))
        return build

    def reader(self, *, words=6, confidence=90.0, text="Registrar of examinations"):
        return mock.Mock(
            return_value=OcrPage(text=text, words=words, confidence=confidence)
        )

    def test_a_document_picture_is_read_and_placed_after_its_paragraph(self):
        reader = self.reader()

        pages, metadata = extract(self.with_picture(), image_ocr=reader, max_images=3)

        self.assertEqual(
            pages[0].text, "Scanned form follows\nRegistrar of examinations"
        )
        self.assertEqual(metadata["pictures_ocred"], 1)

    def test_real_ocr_reads_text_out_of_an_embedded_picture(self):
        from documents.processing.extractors import ocr

        pages, _ = extract(self.with_picture(), image_ocr=ocr.read_page, max_images=3)

        self.assertIn("INVOICE 12345", pages[0].text)

    def test_the_picture_budget_is_a_hard_limit(self):
        reader = self.reader()

        extract(self.with_picture(), image_ocr=reader, max_images=0)

        reader.assert_not_called()

    def test_a_logo_sized_picture_is_not_read(self):
        reader = self.reader()
        tiny = io.BytesIO()
        Image.new("RGB", (60, 40), "white").save(tiny, format="PNG")

        extract(self.with_picture(tiny.getvalue()), image_ocr=reader, max_images=3)

        reader.assert_not_called()

    def test_a_picture_that_reads_as_noise_adds_nothing(self):
        for reading in (self.reader(words=1), self.reader(confidence=20.0)):
            with self.subTest(words=reading.return_value.words):
                pages, metadata = extract(
                    self.with_picture(), image_ocr=reading, max_images=3
                )

                self.assertEqual(pages[0].text, "Scanned form follows")
                self.assertEqual(metadata["pictures_ocred"], 0)

    def test_the_same_picture_twice_is_read_once(self):
        reader = self.reader()
        build = self.with_picture()
        build.document.add_picture(io.BytesIO(picture_png()))

        extract(build, image_ocr=reader, max_images=5)

        self.assertEqual(reader.call_count, 1)
