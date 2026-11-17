"""Figures in documents: where they are, and what reaches the page.

PDF drawings are built by hand (pdf_fixtures): a bar chart of filled
rectangles, a pie of curves, a flowchart of boxes and a diagonal arrow, beside
the things that must NOT be taken for figures - a shaded table, a bordered
paragraph, a page frame, a logo-sized mark. The describer is a stub that
records what it was shown and answers with a fixed reading.
"""

import io
import os
import tempfile

from django.test import SimpleTestCase
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from lxml import etree
from PIL import Image, ImageDraw

from documents import pdf_fixtures
from documents.processing.extractors import charts, docx, figure_regions, pdf

PROSE = [
    "The admissions office publishes the figures below each year.",
    "They are drawn from the registrar's records and checked in August.",
]


def write(pages):
    handle = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    handle.write(pdf_fixtures.build(pages))
    handle.close()
    return handle.name


def page_with(draw):
    page = pdf_fixtures.Page()
    page.lines(72, 60, PROSE)
    draw(page)
    return page


def bars(page):
    for index, height in enumerate((80, 120, 160, 140)):
        page.solid(100 + index * 50, 400 - height, 30, height)
    page.rule(90, 400, 320, 400)
    page.rule(90, 400, 90, 230)


def pie(page):
    page.circle(200, 320, 70)
    page.circle(200, 320, 35)
    page.rule(200, 320, 270, 320)
    page.rule(200, 320, 165, 380)


def flowchart(page):
    page.box(150, 200, 120, 36)
    page.box(150, 290, 120, 36)
    page.box(150, 380, 120, 36)
    page.rule(210, 236, 210, 290)
    page.rule(210, 326, 210, 380)
    page.rule(210, 326, 260, 350)  # a diagonal connector


def shaded_table(page):
    # What a Word export draws: a filled rectangle behind every cell, in rows
    # and columns of equal height, touching.
    for row in range(8):
        for column in range(4):
            if (row + column) % 2 == 0:
                page.solid(100 + column * 90, 200 + row * 22, 90, 22, (0.9, 0.9, 0.9))


def bordered_paragraph(page):
    page.box(70, 200, 470, 120)
    page.lines(
        80,
        210,
        [f"A boxed note, line {n}, with ordinary words in it." for n in range(7)],
    )


def frame(page):
    page.box(20, 20, 572, 752)


def logo(page):
    page.solid(500, 40, 30, 30)
    page.circle(515, 55, 12)


def regions(draw, **kwargs):
    path = write([page_with(draw)])
    try:
        import pdfplumber

        with pdfplumber.open(path) as document:
            page = document.pages[0]
            from documents.processing.extractors import geometry

            words = [
                geometry.Word(
                    text=w["text"],
                    x0=w["x0"],
                    x1=w["x1"],
                    top=w["top"],
                    bottom=w["bottom"],
                )
                for w in page.extract_words()
            ]
            return figure_regions.find(page, words, **kwargs)
    finally:
        os.unlink(path)


class FigureRegionTests(SimpleTestCase):
    def test_filled_bars_on_a_baseline_are_a_drawing(self):
        found = regions(bars)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].origin, "drawing")
        self.assertLess(found[0].x0, 100)
        self.assertGreater(found[0].x1, 290)

    def test_curves_and_a_diagonal_are_a_drawing(self):
        self.assertEqual([r.origin for r in regions(pie)], ["drawing"])

    def test_boxes_joined_by_connectors_with_a_diagonal_are_a_drawing(self):
        self.assertEqual([r.origin for r in regions(flowchart)], ["drawing"])

    def test_a_shaded_table_is_not_a_figure(self):
        self.assertEqual(regions(shaded_table), [])

    def test_a_bordered_paragraph_is_not_a_figure(self):
        self.assertEqual(regions(bordered_paragraph), [])

    def test_a_frame_around_the_page_is_not_a_figure(self):
        self.assertEqual(regions(frame), [])

    def test_a_logo_sized_mark_is_not_a_figure(self):
        self.assertEqual(regions(logo), [])

    def test_a_picture_is_a_figure_and_a_tiny_one_is_not(self):
        def draw(page):
            picture = Image.new("RGB", (400, 300), "white")
            ImageDraw.Draw(picture).rectangle((20, 20, 380, 280), outline="black")
            page.image(100, 250, 300, 225, picture)
            page.image(500, 40, 20, 20, Image.new("RGB", (40, 40), "red"))

        found = regions(draw)
        self.assertEqual([r.origin for r in found], ["picture"])

    def test_a_drawing_with_a_picture_inside_is_one_figure(self):
        def draw(page):
            bars(page)
            picture = Image.new("RGB", (300, 200), "white")
            ImageDraw.Draw(picture).rectangle((20, 20, 280, 180), outline="black")
            page.image(110, 250, 150, 100, picture)

        found = regions(draw)
        self.assertEqual(len(found), 1)

    def test_a_ruled_table_beside_a_chart_is_not_part_of_it(self):
        found = regions(bars, table_boxes=[(50, 180, 560, 420)])
        self.assertEqual(found, [])

    def test_two_separate_drawings_are_two_figures_in_reading_order(self):
        def draw(page):
            bars(page)
            for index, height in enumerate((70, 130, 100)):
                page.solid(400 + index * 50, 700 - height, 30, height)
            page.rule(390, 700, 560, 700)
            page.rule(390, 700, 390, 560)

        found = regions(draw)
        self.assertEqual(len(found), 2)
        self.assertLess(found[0].top, found[1].top)


class LabelTests(SimpleTestCase):
    """The words around a drawing are its labels; a paragraph beside it is not."""

    def test_the_title_axis_numbers_and_names_are_inside_the_region(self):
        def draw(page):
            bars(page)
            page.text(150, 205, "B.Tech admissions by year", size=11)
            page.text(100, 405, "2021     2022     2023     2024", size=9)
            page.text(60, 300, "400", size=9)

        [region] = regions(draw)
        self.assertLessEqual(region.top, 207)
        self.assertGreaterEqual(region.bottom, 410)
        self.assertLessEqual(region.x0, 62)

    def test_a_paragraph_beside_the_chart_is_not_pulled_into_it(self):
        def draw(page):
            bars(page)
            # A full-width line of prose that starts under the chart's left
            # edge and runs far past its right.
            page.text(
                80,
                412,
                "This sentence runs on well beyond the right-hand edge of the chart "
                "and is ordinary prose that has nothing to do with it at all.",
                size=9,
            )

        [region] = regions(draw)
        self.assertLess(region.bottom, 412)

    def test_text_far_from_the_chart_is_not_taken(self):
        def draw(page):
            bars(page)
            page.text(150, 600, "Far below", size=11)

        [region] = regions(draw)
        self.assertLess(region.bottom, 450)


class _Describer:
    """Stands in for FigureDescriber.block_for: records what it was shown."""

    def __init__(
        self,
        text="[Figure: chart - described automatically from the image]\n2021: 420\n[End of figure]",
    ):
        self.text = text
        self.seen = []

    def __call__(self, image):
        self.seen.append(image.size)
        return self.text


def extract(pages, **kwargs):
    path = write(pages)
    try:
        return pdf.extract_page_results(path, **kwargs)
    finally:
        os.unlink(path)


class PdfFigureTests(SimpleTestCase):
    def test_a_drawn_chart_is_shown_to_the_describer_and_its_reading_joins_the_page(
        self,
    ):
        describer = _Describer()
        [result] = extract([page_with(bars)], describe=describer, figure_dpi=100)
        self.assertEqual(len(describer.seen), 1)
        self.assertIn("2021: 420", result.text)
        self.assertIn("The admissions office publishes", result.text)
        self.assertEqual(result.metadata["figures_described"], 1)

    def test_the_figure_is_read_where_it_sits_on_the_page(self):
        def draw(page):
            page.lines(72, 520, ["A closing paragraph after the chart."])
            bars(page)

        [result] = extract([page_with(draw)], describe=_Describer(), figure_dpi=100)
        text = result.text
        self.assertLess(text.index("admissions office"), text.index("[Figure:"))
        self.assertLess(text.index("[Figure:"), text.index("closing paragraph"))

    def test_the_image_sent_is_the_figure_not_the_page(self):
        describer = _Describer()
        extract([page_with(bars)], describe=describer, figure_dpi=100)
        width, height = describer.seen[0]
        self.assertLess(width, 612 * 100 / 72)
        self.assertLess(height, 792 * 100 / 72 * 0.6)

    def test_a_describer_with_nothing_to_say_leaves_the_page_as_it_was(self):
        with_none = extract([page_with(bars)], describe=lambda image: "")
        without = extract([page_with(bars)])
        self.assertEqual(with_none[0].text, without[0].text)
        self.assertNotIn("figures_described", with_none[0].metadata)

    def test_no_describer_changes_nothing(self):
        [result] = extract([page_with(bars)])
        self.assertNotIn("[Figure:", result.text)

    def test_a_described_picture_is_not_also_read_by_ocr(self):
        def draw(page):
            picture = Image.new("RGB", (400, 300), "white")
            ImageDraw.Draw(picture).rectangle((20, 20, 380, 280), outline="black")
            page.image(100, 250, 300, 225, picture)

        calls = []

        def region_ocr(image):
            calls.append(image.size)
            return []

        extract(
            [page_with(draw)],
            describe=_Describer(),
            region_ocr=region_ocr,
            max_regions=5,
            figure_dpi=100,
        )
        self.assertEqual(calls, [])

    def test_a_picture_the_describer_could_not_read_still_goes_to_ocr(self):
        def draw(page):
            picture = Image.new("RGB", (400, 300), "white")
            ImageDraw.Draw(picture).rectangle((20, 20, 380, 280), outline="black")
            page.image(100, 250, 300, 225, picture)

        calls = []

        def region_ocr(image):
            calls.append(image.size)
            return []

        extract(
            [page_with(draw)],
            describe=lambda image: "",
            region_ocr=region_ocr,
            max_regions=5,
        )
        self.assertEqual(len(calls), 1)

    def test_a_page_of_plain_text_never_calls_the_describer(self):
        describer = _Describer()
        extract([page_with(lambda page: None)], describe=describer)
        self.assertEqual(describer.seen, [])


# -- the numbers behind a chart in an office file -------------------------------

C = "http://schemas.openxmlformats.org/drawingml/2006/chart"


def chart_xml(kind="barChart", series=None, title="Admissions by year"):
    series = series or [
        ("Students", ["2021", "2022", "2023"], ["420", "515", "610"], "General")
    ]
    sers = ""
    for number, (name, cats, vals, code) in enumerate(series):
        cat = "".join(
            f'<c:pt idx="{i}"><c:v>{c}</c:v></c:pt>' for i, c in enumerate(cats)
        )
        val = "".join(
            f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>' for i, v in enumerate(vals)
        )
        sers += (
            f'<c:ser><c:idx val="{number}"/><c:tx><c:strRef><c:strCache>'
            f'<c:pt idx="0"><c:v>{name}</c:v></c:pt></c:strCache></c:strRef></c:tx>'
            f"<c:cat><c:strRef><c:strCache>{cat}</c:strCache></c:strRef></c:cat>"
            f"<c:val><c:numRef><c:numCache><c:formatCode>{code}</c:formatCode>"
            f"{val}</c:numCache></c:numRef></c:val></c:ser>"
        )
    heading = (
        f'<c:title><c:tx><c:rich><a:p xmlns:a="http://schemas.openxmlformats.org/'
        f'drawingml/2006/main"><a:r><a:t>{title}</a:t></a:r></a:p></c:rich></c:tx></c:title>'
        if title
        else ""
    )
    return (
        f'<c:chartSpace xmlns:c="{C}"><c:chart>{heading}<c:plotArea>'
        f"<c:{kind}>{sers}</c:{kind}></c:plotArea></c:chart></c:chartSpace>"
    ).encode()


class ChartPartTests(SimpleTestCase):
    def test_categories_series_and_values_become_a_table(self):
        found = charts.read(chart_xml())
        self.assertEqual(found["title"], "Admissions by year")
        self.assertEqual(found["kind"], "bar chart")
        self.assertEqual(
            found["rows"],
            [["", "Students"], ["2021", "420"], ["2022", "515"], ["2023", "610"]],
        )

    def test_two_series_are_two_columns(self):
        found = charts.read(
            chart_xml(
                series=[
                    ("CSE", ["Sem 1", "Sem 2"], ["72", "75"], "General"),
                    ("ECE", ["Sem 1", "Sem 2"], ["68", "70"], "General"),
                ],
                kind="lineChart",
            )
        )
        self.assertEqual(found["kind"], "line chart")
        self.assertEqual(found["rows"][0], ["", "CSE", "ECE"])
        self.assertEqual(found["rows"][2], ["Sem 2", "75", "70"])

    def test_percent_formatted_values_read_as_percent_without_float_noise(self):
        found = charts.read(
            chart_xml(
                kind="pieChart",
                series=[
                    (
                        "Share",
                        ["Tuition", "Hostel"],
                        ["0.55", "0.30000000000000004"],
                        "0%",
                    )
                ],
            )
        )
        self.assertEqual(found["rows"][1], ["Tuition", "55%"])
        self.assertEqual(found["rows"][2], ["Hostel", "30%"])

    def test_a_decimal_value_keeps_its_decimals_and_a_whole_one_loses_none(self):
        found = charts.read(
            chart_xml(series=[("CGPA", ["A", "B"], ["8.25", "9.0"], "General")])
        )
        self.assertEqual(found["rows"][1:], [["A", "8.25"], ["B", "9"]])

    def test_a_missing_point_is_a_blank_cell_not_a_shifted_value(self):
        xml = chart_xml(series=[("S", ["a", "b", "c"], ["1", "2", "3"], "General")])
        xml = xml.replace(b'<c:pt idx="1"><c:v>2</c:v></c:pt>', b"", 1)
        rows = charts.read(xml)["rows"]
        self.assertEqual(rows[2], ["b", ""])
        self.assertEqual(rows[3], ["c", "3"])

    def test_a_scatter_chart_is_x_against_y(self):
        xml = (
            f'<c:chartSpace xmlns:c="{C}"><c:chart><c:plotArea><c:scatterChart>'
            f"<c:ser><c:xVal><c:numRef><c:numCache>"
            f'<c:pt idx="0"><c:v>1</c:v></c:pt><c:pt idx="1"><c:v>2</c:v></c:pt>'
            f"</c:numCache></c:numRef></c:xVal><c:yVal><c:numRef><c:numCache>"
            f'<c:pt idx="0"><c:v>10</c:v></c:pt><c:pt idx="1"><c:v>20</c:v></c:pt>'
            f"</c:numCache></c:numRef></c:yVal></c:ser></c:scatterChart>"
            f"</c:plotArea></c:chart></c:chartSpace>"
        ).encode()
        rows = charts.read(xml)["rows"]
        self.assertEqual(rows[0][0], "x")
        self.assertEqual(rows[1:], [["1", "10"], ["2", "20"]])

    def test_a_chart_with_no_data_is_nothing(self):
        self.assertIsNone(charts.read(b"<c:chartSpace xmlns:c='%s'/>" % C.encode()))
        self.assertIsNone(charts.read(b"not xml at all"))

    def test_a_very_long_chart_is_cut_and_says_so(self):
        n = charts.MAX_ROWS + 20
        found = charts.read(
            chart_xml(
                series=[
                    (
                        "S",
                        [str(i) for i in range(n)],
                        [str(i) for i in range(n)],
                        "General",
                    )
                ]
            )
        )
        self.assertTrue(found["truncated"])
        self.assertEqual(len(found["rows"]), charts.MAX_ROWS + 1)

    def test_an_entity_in_the_part_is_not_expanded(self):
        evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "BOOM">]>' + chart_xml(
            title="&e;"
        )
        found = charts.read(evil)
        self.assertTrue(found is None or "BOOM" not in found["title"])


def docx_with_chart(xml: bytes, before="Before the chart.", after="After the chart."):
    document = Document()
    document.add_paragraph(before)
    part = Part(
        PackURI("/word/charts/chart1.xml"),
        "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
        xml,
        document.part.package,
    )
    rid = document.part.relate_to(part, RT.CHART)
    paragraph = document.add_paragraph()
    run = paragraph.add_run()
    drawing = etree.fromstring(
        '<w:drawing xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        f'xmlns:c="{C}" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<wp:inline><wp:extent cx='4000000' cy='2500000'/>"
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">'
        f'<c:chart r:id="{rid}"/></a:graphicData></a:graphic></wp:inline></w:drawing>'
    )
    run._r.append(drawing)
    document.add_paragraph(after)
    buffer = io.BytesIO()
    document.save(buffer)
    handle = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    handle.write(buffer.getvalue())
    handle.close()
    return handle.name


class DocxChartTests(SimpleTestCase):
    def extract(self, xml):
        path = docx_with_chart(xml)
        try:
            return docx.extract_page_results(path)
        finally:
            os.unlink(path)

    def test_a_word_chart_is_read_as_a_table_between_its_paragraphs(self):
        pages, metadata = self.extract(chart_xml())
        text = pages[0].text
        self.assertIn("Bar chart: Admissions by year", text)
        self.assertIn("2023 | 610", text)
        self.assertLess(text.index("Before the chart"), text.index("Bar chart"))
        self.assertLess(text.index("2023 | 610"), text.index("After the chart"))
        self.assertEqual(metadata["charts"], 1)

    def test_the_chart_is_stored_as_a_table_with_exact_cells(self):
        pages, _ = self.extract(chart_xml())
        [table] = pages[0].tables
        self.assertEqual(table.header, ["", "Students"])
        self.assertEqual(table.rows[-1], ["2023", "610"])
        self.assertEqual(table.source, charts.SOURCE)
        span = pages[0].text[table.character_start : table.character_end]
        self.assertIn("2022 | 515", span)

    def test_a_chart_part_with_no_data_adds_nothing(self):
        pages, metadata = self.extract(b"<c:chartSpace xmlns:c='%s'/>" % C.encode())
        self.assertNotIn(
            "chart",
            pages[0]
            .text.lower()
            .replace("after the chart", "")
            .replace("before the chart", ""),
        )
        self.assertEqual(metadata["charts"], 0)
