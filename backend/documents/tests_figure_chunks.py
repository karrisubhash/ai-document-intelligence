"""A figure reading in a page: kept whole when chunked, and said to be a reading.

The reading a vision model wrote sits in the page text between two marker
lines. Chunked like prose it was cut at 1000 characters with the numbers in
one chunk and the chart's name in another; and in a prompt it read as the
document's own printed words. These pin that it is one chunk, still an exact
slice of its page, and that the answering model is told what it is.
"""

from django.test import SimpleTestCase

from documents.processing import figure_markers as markers
from documents.processing.chunking import splitter
from documents.rag import context as ctx
from documents.tests_rag_tables import make_chunk

READING = (
    "[Figure: chart - described automatically from the image]\n"
    "Admissions by year\n"
    "Students, 2021: 420\n"
    "Students, 2022: 515\n"
    "Students, 2023: 610\n"
    "[End of figure]"
)
BEFORE = "The college admitted students in four branches this year. " * 3
AFTER = "Admissions peaked in 2023 and fell slightly in 2024. " * 3


def page(*parts) -> str:
    return "\n\n".join(parts)


class MarkerTests(SimpleTestCase):
    def test_a_reading_is_found_with_its_exact_span(self):
        text = page(BEFORE, READING, AFTER)
        [(start, end)] = markers.spans(text)
        self.assertEqual(text[start:end], READING)

    def test_two_readings_are_two_spans(self):
        text = page(READING, BEFORE, READING)
        self.assertEqual(len(markers.spans(text)), 2)

    def test_a_reading_with_its_end_line_lost_runs_to_the_end_of_the_text(self):
        text = page(BEFORE, READING.replace("\n[End of figure]", ""))
        [(start, end)] = markers.spans(text)
        self.assertEqual(end, len(text))

    def test_text_without_a_marker_has_no_spans(self):
        self.assertEqual(markers.spans(BEFORE), [])
        self.assertFalse(markers.contains(BEFORE))
        self.assertTrue(markers.contains(READING))

    def test_the_title_is_the_line_after_the_opening_marker(self):
        self.assertEqual(markers.title(READING), "Admissions by year")
        self.assertEqual(markers.title("[Figure: chart - x]\n[End of figure]"), "")


class ChunkingTests(SimpleTestCase):
    def chunks(self, text, **kwargs):
        return splitter.split_page(1, text, **kwargs)

    def test_a_reading_is_one_chunk_with_its_title_in_metadata(self):
        text = page(BEFORE, READING, AFTER)
        figures = [c for c in self.chunks(text) if c.metadata.get("kind") == "FIGURE"]
        self.assertEqual(len(figures), 1)
        self.assertEqual(figures[0].text, READING)
        self.assertEqual(figures[0].metadata["figure_title"], "Admissions by year")
        self.assertNotIn("parts", figures[0].metadata)

    def test_the_prose_around_it_is_not_mixed_into_it(self):
        text = page(BEFORE, READING, AFTER)
        chunks = self.chunks(text)
        for chunk in chunks:
            if chunk.metadata.get("kind") != "FIGURE":
                self.assertNotIn("[Figure:", chunk.text)
                self.assertNotIn("Students, 2022", chunk.text)

    def test_every_chunk_still_re_slices_out_of_its_page(self):
        text = page(BEFORE, READING, AFTER, READING)
        splitter.verify(self.chunks(text), [(1, text)])

    def test_a_long_reading_is_split_at_lines_and_says_which_part(self):
        rows = "\n".join(f"Series, point {n}: {n * 17}" for n in range(200))
        reading = (
            "[Figure: chart - described automatically from the image]\n"
            "Long chart\n" + rows + "\n[End of figure]"
        )
        text = page(BEFORE, reading)
        figures = [c for c in self.chunks(text) if c.metadata.get("kind") == "FIGURE"]
        self.assertGreater(len(figures), 1)
        for number, chunk in enumerate(figures, start=1):
            self.assertEqual(chunk.metadata["part"], number)
            self.assertEqual(chunk.metadata["parts"], len(figures))
            self.assertEqual(chunk.metadata["figure_title"], "Long chart")
            # Cut between rows, never inside one.
            for line in chunk.text.split("\n"):
                self.assertTrue(
                    line.startswith(("Series,", "[Figure:", "Long chart", "[End"))
                )
        splitter.verify(self.chunks(text), [(1, text)])

    def test_text_with_no_figure_chunks_exactly_as_before(self):
        text = page(BEFORE, AFTER)
        self.assertTrue(all(not c.metadata for c in self.chunks(text)))

    def test_a_reading_inside_a_table_span_is_left_to_the_table(self):
        class Table:
            character_start = 0
            character_end = len(READING)
            table_index = 0
            header = []

        chunks = self.chunks(READING, tables=[Table()])
        self.assertEqual([c.metadata.get("kind") for c in chunks], ["TABLE"])


class PromptTests(SimpleTestCase):
    def prompt(self, text, **metadata):
        chunk = make_chunk(1, text, table=False, **metadata)
        built = ctx.BuiltContext(
            passages=[ctx.Passage(marker=1, chunk=chunk, text=text, relevance=1.0)],
            characters=len(text),
        )
        passages = ctx.render(built, "How many in 2023?").split("PASSAGES")[1]
        return passages.split("QUESTION")[0]

    def test_a_reading_is_flagged_as_machine_written_and_approximate(self):
        shown = self.prompt(READING, kind="FIGURE")
        self.assertIn("machine-written description of a figure", shown)
        self.assertIn("marked 'about'", shown)

    def test_a_passage_without_a_reading_gets_no_such_note(self):
        self.assertNotIn("machine-written", self.prompt(BEFORE))

    def test_a_part_of_a_figure_says_so_and_does_not_call_itself_a_table(self):
        shown = self.prompt(
            READING, kind="FIGURE", figure_title="Long chart", part=2, parts=3
        )
        self.assertIn("part 2 of 3 of this figure's description (Long chart)", shown)
        self.assertNotIn("of this table", shown)
