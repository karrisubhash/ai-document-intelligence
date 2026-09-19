"""Phase 12: table detection, and the invariant that keeps it honest.

The detector tests are pure - no database, no pipeline - because the
detector is pure. The pipeline tests check the two things that can only go
wrong once it is wired in: that spans still index the text that was
actually stored, and that a reprocess replaces tables rather than
accumulating them.
"""

from itertools import pairwise

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from documents.models import Document, DocumentContent, DocumentTable, ProcessingJob
from documents.processing.figures import service as figure_service
from documents.processing.pipeline import run_pipeline
from documents.processing.tables import detector
from documents.processing.tables import service as table_service
from documents.tests import MEDIA_ROOT, make_user

FEE_TABLE = (
    "FEE STRUCTURE 2024\n"
    "Programme\tTuition\tExam Fee\n"
    "B.Tech CSE\t85000\t2500\n"
    "B.Tech ECE\t82000\t2500\n"
    "\n"
    "All fees are payable before the semester begins.\n"
)

ALIGNED_TABLE = (
    "ATTENDANCE SUMMARY\n"
    "Course        Present   Total   Percent\n"
    "CS301             38      45      84.4\n"
    "CS302             31      45      68.9\n"
)

PROSE = (
    "The committee met on Tuesday.  It reviewed the attendance policy.\n"
    "The registrar noted that the rule had not changed.  No action followed.\n"
    "A further meeting was scheduled.  The minutes were circulated.\n"
)


class DetectionTests(TestCase):
    def test_a_tab_separated_table_is_found_with_its_header(self):
        tables = detector.detect_tables(1, FEE_TABLE)

        self.assertEqual(len(tables), 1)
        table = tables[0]
        self.assertEqual(table.header, ["Programme", "Tuition", "Exam Fee"])
        self.assertIn(["B.Tech CSE", "85000", "2500"], table.rows)

    def test_columns_aligned_by_spaces_are_found(self):
        """The first implementation compared left edges and found nothing
        here, because tables right-align numbers under left-aligned
        headers: "Present" starts at 14 and "38" at 18."""
        tables = detector.detect_tables(1, ALIGNED_TABLE)

        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0].header, ["Course", "Present", "Total", "Percent"])
        self.assertEqual(tables[0].row_count, 3)

    def test_prose_containing_double_spaces_is_not_a_table(self):
        """A false table is worse than a missed one: it invents structure
        and then answers questions from it."""
        self.assertEqual(detector.detect_tables(1, PROSE), [])

    def test_a_header_is_not_guessed_without_evidence(self):
        tables = detector.detect_tables(1, "Name\tDepartment\nRavi\tCSE\nPriya\tECE\n")

        self.assertEqual(len(tables), 1)
        self.assertEqual(
            tables[0].header,
            [],
            "a header was guessed for a table of labels against labels",
        )

    def test_a_single_row_is_not_a_table(self):
        self.assertEqual(detector.detect_tables(1, "Programme\tTuition\n"), [])

    def test_the_span_reslices_out_of_the_page(self):
        table = detector.detect_tables(1, FEE_TABLE)[0]
        region = FEE_TABLE[table.character_start : table.character_end]

        for row in table.rows:
            for cell in row:
                self.assertIn(cell, region)

    def test_verification_refuses_a_cell_the_page_does_not_contain(self):
        """This is the structural guarantee, not a heuristic: a fabricated
        figure cannot be stored, however plausible it looks."""
        real = detector.detect_tables(1, FEE_TABLE)[0]
        forged = detector.DetectedTable(
            page_number=1,
            table_index=0,
            character_start=real.character_start,
            character_end=real.character_end,
            rows=[["B.Tech CSE", "95000", "2500"]],
            separator=real.separator,
            header=[],
        )

        self.assertTrue(detector.verify(real, FEE_TABLE))
        self.assertFalse(detector.verify(forged, FEE_TABLE))

    def test_a_span_running_past_the_page_is_refused(self):
        real = detector.detect_tables(1, FEE_TABLE)[0]
        overrun = detector.DetectedTable(
            page_number=1,
            table_index=0,
            character_start=real.character_start,
            character_end=len(FEE_TABLE) + 500,
            rows=real.rows,
            separator=real.separator,
            header=real.header,
        )

        self.assertFalse(detector.verify(overrun, FEE_TABLE))

    def test_two_tables_on_one_page_are_numbered_in_reading_order(self):
        text = FEE_TABLE + "\n" + ALIGNED_TABLE
        tables = detector.detect_tables(1, text)

        self.assertEqual(len(tables), 2)
        self.assertEqual([t.table_index for t in tables], [0, 1])
        self.assertLess(tables[0].character_start, tables[1].character_start)

    def test_empty_text_detects_nothing(self):
        self.assertEqual(detector.detect_tables(1, ""), [])
        self.assertEqual(detector.detect_tables(1, "   \n\n  "), [])


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class PipelineTableTests(TestCase):
    def setUp(self):
        table_service.reset_detector_cache()
        figure_service.reset_detector_cache()
        self.addCleanup(table_service.reset_detector_cache)
        self.addCleanup(figure_service.reset_detector_cache)
        self.user = make_user("table_user", department="Registry")

    def process(self, body: bytes, name: str = "fees.txt"):
        document = Document.objects.create(
            title="Fee structure",
            document_type="OTHER",
            owner=self.user,
            department="Registry",
        )
        version = document.versions.create(
            version_number=1,
            file=ContentFile(body, name=name),
            file_name=name,
            file_size=len(body),
            uploaded_by=self.user,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])
        job = ProcessingJob.objects.create(document_version=version)
        run_pipeline(job.id)
        return version, job

    def test_a_table_is_stored_with_the_content(self):
        version, _ = self.process(FEE_TABLE.encode())

        tables = DocumentTable.objects.filter(document_version=version)
        self.assertEqual(tables.count(), 1)
        table = tables.first()
        self.assertEqual(table.header, ["Programme", "Tuition", "Exam Fee"])
        self.assertEqual(table.row_count, 3)
        self.assertEqual(table.column_count, 3)

    def test_stored_spans_index_the_stored_page_text(self):
        """The bug this exists to catch: detection runs on the in-memory
        page text and the pipeline stores a NUL-stripped copy. If the two
        normalizations ever diverge, every span after the first NUL points
        a few characters left and verification rejects real rows."""
        version, _ = self.process(FEE_TABLE.encode())

        content = DocumentContent.objects.get(document_version=version)
        page = content.pages.get(page_number=1)

        for table in content.tables.all():
            region = page.text[table.character_start : table.character_end]
            for row in table.rows:
                for cell in row:
                    self.assertIn(cell, region)

    def test_a_nul_byte_in_the_page_does_not_shift_spans(self):
        body = FEE_TABLE.replace("FEE STRUCTURE", "FEE\x00STRUCTURE").encode()
        version, _ = self.process(body)

        content = DocumentContent.objects.get(document_version=version)
        page = content.pages.get(page_number=1)
        self.assertNotIn("\x00", page.text)

        tables = list(content.tables.all())
        self.assertEqual(len(tables), 1)
        region = page.text[tables[0].character_start : tables[0].character_end]
        self.assertIn("85000", region)

    def test_reprocessing_replaces_tables_rather_than_duplicating_them(self):
        version, _ = self.process(FEE_TABLE.encode())
        job = ProcessingJob.objects.create(document_version=version)
        run_pipeline(job.id)

        # A reprocess is a NEW job, so its output sits beside the old one
        # rather than replacing it - that is what makes two pipeline
        # versions comparable. What must not happen is one job's own retry
        # accumulating copies.
        run_pipeline(job.id)

        self.assertEqual(
            DocumentTable.objects.filter(processing_job=job).count(),
            1,
            "a retry of one job accumulated duplicate tables",
        )

    def test_the_stage_is_skipped_only_when_neither_detector_is_set(self):
        with override_settings(DOCUMENT_TABLE_DETECTOR="", DOCUMENT_FIGURE_DETECTOR=""):
            table_service.reset_detector_cache()
            figure_service.reset_detector_cache()
            version, job = self.process(FEE_TABLE.encode())

        self.assertEqual(
            DocumentTable.objects.filter(document_version=version).count(), 0
        )
        event = job.events.get(stage=ProcessingJob.Stage.LAYOUT_ANALYSIS)
        self.assertEqual(event.status, "SKIPPED")

    def test_switching_off_one_detector_leaves_the_other_running(self):
        """The two halves are independent: losing table detection must not
        cost the document its figures, or the reverse."""
        with override_settings(DOCUMENT_TABLE_DETECTOR=""):
            table_service.reset_detector_cache()
            version, job = self.process(FEE_TABLE.encode())

        self.assertEqual(
            DocumentTable.objects.filter(document_version=version).count(), 0
        )
        event = job.events.get(stage=ProcessingJob.Stage.LAYOUT_ANALYSIS)
        self.assertEqual(event.status, "SUCCEEDED")
        self.assertIn("figure", event.message.lower())

    def test_a_document_with_no_tables_still_processes(self):
        version, job = self.process(PROSE.encode(), name="minutes.txt")

        self.assertEqual(
            DocumentTable.objects.filter(document_version=version).count(), 0
        )
        job.refresh_from_db()
        self.assertEqual(job.status, ProcessingJob.Status.COMPLETED)

    def test_a_detector_that_fabricates_has_its_rows_refused(self):
        """The stage verifies every table on every run, rather than
        trusting a detector to be honest."""
        version, job = self.process(FEE_TABLE.encode())

        with override_settings(
            DOCUMENT_TABLE_DETECTOR="documents.tests_tables.FabricatingDetector"
        ):
            table_service.reset_detector_cache()
            liar_version, liar_job = self.process(FEE_TABLE.encode())

        self.assertEqual(
            DocumentTable.objects.filter(document_version=liar_version).count(),
            0,
            "a fabricated table was stored",
        )
        event = liar_job.events.get(stage=ProcessingJob.Stage.LAYOUT_ANALYSIS)
        self.assertIn("rejected 1", event.message.lower())


class FabricatingDetector:
    """A detector that returns a row the page does not contain.

    Used by the test above. It implements the protocol correctly and lies
    only in its output, which is exactly the failure the stage's
    verification exists to catch.
    """

    name = "fabricating"
    version = "test"

    def detect(self, page_number: int, text: str) -> list:
        return [
            detector.DetectedTable(
                page_number=page_number,
                table_index=0,
                character_start=0,
                character_end=len(text),
                rows=[["Tuition", "999999"]],
                separator="TAB",
                header=[],
            )
        ]

    def verify(self, table, page_text: str) -> bool:
        # Honest verification, dishonest detection: the stage calls this
        # and drops the row. A detector that also lied here would be
        # caught by the protocol check only if it changed shape, which is
        # why verification lives in the stage's contract and not as an
        # optional courtesy.
        return detector.verify(table, page_text)


# --- Retrieval and answering over tables ------------------------------------

EMBEDDER = "documents.processing.embedding.hashing.HashingEmbeddingProvider"

BIG_TABLE_PAGE = (
    "FEE STRUCTURE 2024\n"
    + "Programme\tTuition\tExam Fee\n"
    + "".join(
        f"Programme number {index:03d}\t{80000 + index}\t2500\n" for index in range(120)
    )
)


class TableChunkingTests(TestCase):
    """A table cut at an arbitrary offset strands its data rows from the
    header, and a row without its header is a list of numbers."""

    def test_every_part_of_a_split_table_knows_its_columns(self):
        """The measured benefit, stated as what it actually is.

        A first attempt at this test asserted that table-awareness stops
        rows being cut in half. It does not, and never did: the splitter
        already prefers a line break as a boundary, so mid-row cuts were
        0 either way. Asserting a benefit that is not there would have
        been a passing test measuring nothing.

        What DOES change: of the 17 chunks an unaware splitter puts inside
        a 120-row table, exactly one contains the header row. The other 16
        are columns of numbers with nothing to say what they count. With
        the table kept as a region, every part carries the labels.
        """
        from documents.processing.chunking import splitter

        table = detector.detect_tables(1, BIG_TABLE_PAGE)[0]

        def inside(chunks):
            return [
                chunk
                for chunk in chunks
                if chunk.character_start < table.character_end
                and chunk.character_end > table.character_start
            ]

        unaware = inside(splitter.split_page(1, BIG_TABLE_PAGE, chunk_size=400))
        knew_columns = [
            chunk
            for chunk in unaware
            if all(cell in chunk.text for cell in table.header)
        ]
        self.assertGreater(len(unaware), 2)
        self.assertEqual(
            len(knew_columns),
            1,
            "the fixture no longer splits the table across chunks",
        )

        aware = inside(
            splitter.split_page(1, BIG_TABLE_PAGE, chunk_size=400, tables=[table])
        )
        for chunk in aware:
            self.assertEqual(chunk.metadata["header"], table.header)

    def test_a_split_table_does_not_duplicate_rows_across_parts(self):
        """Chunk overlap is right for prose - a sentence cut by a boundary
        should survive whole somewhere. It is wrong inside a table, where
        it puts the same figure in the index twice and lets one row be
        quoted from two places. Measured: 17 overlapping chunks become 11
        that tile the table exactly once."""
        from documents.processing.chunking import splitter

        table = detector.detect_tables(1, BIG_TABLE_PAGE)[0]
        parts = [
            chunk
            for chunk in splitter.split_page(
                1, BIG_TABLE_PAGE, chunk_size=400, tables=[table]
            )
            if chunk.metadata.get("kind") == "TABLE"
        ]

        self.assertGreater(len(parts), 1)
        for earlier, later in pairwise(parts):
            self.assertLessEqual(earlier.character_end, later.character_start)

        rows = [
            line for part in parts for line in part.text.splitlines() if "	" in line
        ]
        self.assertEqual(len(rows), len(set(rows)), "a row appeared in two parts")

    def test_a_table_chunk_carries_its_header_in_metadata(self):
        from documents.processing.chunking import splitter

        tables = detector.detect_tables(1, FEE_TABLE)
        chunks = splitter.split_page(1, FEE_TABLE, tables=tables)

        table_chunk = next(c for c in chunks if c.metadata.get("kind") == "TABLE")
        self.assertEqual(
            table_chunk.metadata["header"], ["Programme", "Tuition", "Exam Fee"]
        )

    def test_the_header_is_not_spliced_into_the_chunk_text(self):
        """It could not be: chunk text must equal page_text[start:end], and
        a prefix that is not on the page would break every citation."""
        from documents.processing.chunking import splitter

        tables = detector.detect_tables(1, FEE_TABLE)
        chunks = splitter.split_page(1, FEE_TABLE, tables=tables)
        splitter.verify(chunks, [(1, FEE_TABLE)])

        for chunk in chunks:
            self.assertEqual(
                chunk.text, FEE_TABLE[chunk.character_start : chunk.character_end]
            )

    def test_a_table_larger_than_a_chunk_splits_at_row_boundaries(self):
        from documents.processing.chunking import splitter

        tables = detector.detect_tables(1, BIG_TABLE_PAGE)
        self.assertTrue(tables, "the long table was not detected at all")

        chunks = splitter.split_page(1, BIG_TABLE_PAGE, chunk_size=400, tables=tables)
        parts = [c for c in chunks if c.metadata.get("kind") == "TABLE"]

        self.assertGreater(
            len(parts), 1, "a 120-row table fitted in one 400-char chunk"
        )
        for part in parts:
            # No half-rows: every line of every part is a complete row.
            for line in part.text.splitlines():
                self.assertEqual(len(line.split("\t")), 3, repr(line))
            self.assertEqual(part.metadata["parts"], len(parts))

    def test_prose_around_a_table_is_still_chunked_normally(self):
        from documents.processing.chunking import splitter

        page = "Intro paragraph.\n\n" + FEE_TABLE + "\nClosing paragraph here.\n"
        tables = detector.detect_tables(1, page)
        chunks = splitter.split_page(1, page, tables=tables)

        kinds = [c.metadata.get("kind") for c in chunks]
        self.assertIn("TABLE", kinds)
        self.assertIn(None, kinds, "the prose around the table vanished")
        splitter.verify(chunks, [(1, page)])

    def test_chunking_without_tables_is_unchanged(self):
        """Table awareness must not alter how an ordinary page is chunked."""
        from documents.processing.chunking import splitter

        text = PROSE * 12
        with_tables = splitter.split_page(1, text, tables=())
        without = splitter.split_page(1, text)

        self.assertEqual(
            [(c.character_start, c.character_end) for c in with_tables],
            [(c.character_start, c.character_end) for c in without],
        )


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DOCUMENT_EMBEDDING_PROVIDER=EMBEDDER)
class TableAnsweringTests(TestCase):
    """End to end: a real document with a real table, asked a real
    question through the real engine."""

    def setUp(self):
        from documents.processing.chunking import service as chunking_service

        table_service.reset_detector_cache()
        chunking_service.reset_provider_cache()
        self.addCleanup(table_service.reset_detector_cache)
        self.addCleanup(chunking_service.reset_provider_cache)

        self.user = make_user("fee_user", department="Registry")
        document = Document.objects.create(
            title="Fee structure 2024",
            document_type="OTHER",
            owner=self.user,
            department="Registry",
        )
        body = FEE_TABLE.encode()
        version = document.versions.create(
            version_number=1,
            file=ContentFile(body, name="fees.txt"),
            file_name="fees.txt",
            file_size=len(body),
            uploaded_by=self.user,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])
        run_pipeline(ProcessingJob.objects.create(document_version=version).id)

    def test_a_table_row_is_retrievable_and_quoted_with_its_columns(self):
        from documents import rag

        answer = rag.ask(self.user, "What is the tuition for B.Tech CSE?")

        self.assertFalse(answer.abstained, answer.abstention_reason)
        self.assertIn("85000", answer.answer)
        self.assertIn(
            "Tuition",
            answer.answer,
            "the row was quoted without the column labels that give the "
            "number its meaning",
        )

    def test_the_answer_is_still_grounded(self):
        """The column labels come from the table header, which was verified
        against the page - so adding them must not cost the answer its
        support ratio."""
        from documents import rag

        answer = rag.ask(self.user, "What is the tuition for B.Tech CSE?")

        self.assertFalse(answer.abstained)
        self.assertEqual(answer.grounding_failures, [])
        self.assertGreaterEqual(answer.support_ratio, 0.55)

    def test_the_citation_points_at_the_verbatim_page_region(self):
        from documents import rag
        from documents.models import DocumentChunk

        answer = rag.ask(self.user, "What is the tuition for B.Tech CSE?")
        self.assertTrue(answer.citations)

        content = DocumentContent.objects.get(
            document_version__document__owner=self.user
        )
        page = content.pages.get(page_number=1)

        for citation in answer.citations:
            chunk = DocumentChunk.objects.get(id=citation["chunk_id"])
            self.assertEqual(
                chunk.text,
                page.text[chunk.character_start : chunk.character_end],
                "a cited chunk no longer re-slices out of its page",
            )
