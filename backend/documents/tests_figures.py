"""Phase 12: figures, and the abstention that points at one.

A figure is recorded as a fact - "page 4 embeds a 640x480 image" - and
never as an interpretation. Nothing here can tell a chart from a
photograph, so nothing here says which it is.

What the fact buys is the last test class: an abstention that tells the
reader where to look instead of only that the system failed.
"""

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from documents.models import Document, DocumentFigure, ProcessingJob
from documents.processing.context import FileType
from documents.processing.figures import detector
from documents.processing.figures import service as figure_service
from documents.processing.pipeline import run_pipeline
from documents.tests import MEDIA_ROOT, make_user
from documents.tests_ingestion import make_png_bytes


class FakePage(dict):
    """A pypdf page is a dict-like whose values resolve via get_object."""

    def get_object(self):
        return self


class FakeIndirect:
    def __init__(self, value):
        self.value = value

    def get_object(self):
        return self.value


def image_xobject(width, height, subtype="/Image"):
    return FakeIndirect(
        FakePage({"/Subtype": subtype, "/Width": width, "/Height": height})
    )


def page_with(*objects):
    xobjects = FakePage({f"/Im{index}": obj for index, obj in enumerate(objects)})
    return FakePage({"/Resources": FakePage({"/XObject": FakeIndirect(xobjects)})})


class FakeReader:
    def __init__(self, pages):
        self.pages = pages


class PdfFigureDetectionTests(TestCase):
    def reader_for(self, *pages):
        return lambda _path: FakeReader(list(pages))

    def test_an_embedded_image_is_recorded_with_its_dimensions(self):
        figures = detector.detect_pdf_figures(
            "ignored.pdf", self.reader_for(page_with(image_xobject(640, 480)))
        )

        self.assertEqual(len(figures), 1)
        self.assertEqual(figures[0].page_number, 1)
        self.assertEqual((figures[0].width, figures[0].height), (640, 480))

    def test_the_description_is_empty_rather_than_guessed(self):
        """An empty description is the record that nothing read the image.
        A caption invented here would be indistinguishable downstream from
        one a vision model produced."""
        figures = detector.detect_pdf_figures(
            "ignored.pdf", self.reader_for(page_with(image_xobject(640, 480)))
        )

        self.assertEqual(figures[0].description, "")
        self.assertEqual(figures[0].description_source, "")

    def test_furniture_is_not_a_figure(self):
        """A 4x4 image is a bullet or a rule, not something a reader would
        go and look at."""
        figures = detector.detect_pdf_figures(
            "ignored.pdf", self.reader_for(page_with(image_xobject(4, 4)))
        )

        self.assertEqual(figures, [])

    def test_non_image_xobjects_are_ignored(self):
        figures = detector.detect_pdf_figures(
            "ignored.pdf",
            self.reader_for(page_with(image_xobject(640, 480, subtype="/Form"))),
        )

        self.assertEqual(figures, [])

    def test_figures_are_numbered_within_their_page(self):
        figures = detector.detect_pdf_figures(
            "ignored.pdf",
            self.reader_for(
                page_with(image_xobject(640, 480), image_xobject(320, 240)),
                page_with(image_xobject(100, 100)),
            ),
        )

        self.assertEqual(
            [(f.page_number, f.figure_index) for f in figures],
            [(1, 0), (1, 1), (2, 0)],
        )

    def test_a_page_with_no_resources_is_not_an_error(self):
        self.assertEqual(
            detector.detect_pdf_figures("ignored.pdf", self.reader_for(FakePage({}))),
            [],
        )

    def test_a_malformed_resource_dictionary_is_not_an_error(self):
        """The text has already been extracted by the time this runs.
        Failing the job over an image dictionary would throw away a
        perfectly good document for a detail nobody asked about."""

        class Broken(FakePage):
            def get(self, *args, **kwargs):
                raise ValueError("malformed")

        self.assertEqual(
            detector.detect_pdf_figures("ignored.pdf", self.reader_for(Broken())),
            [],
        )

    def test_a_page_declaring_thousands_of_images_is_bounded(self):
        """A PDF that maps every glyph to its own image should not turn
        this into an unbounded walk."""
        many = page_with(*[image_xobject(64, 64) for _ in range(500)])

        figures = detector.detect_pdf_figures("ignored.pdf", self.reader_for(many))

        self.assertLessEqual(len(figures), detector.MAX_FIGURES_PER_PAGE)


class DetectorDispatchTests(TestCase):
    def setUp(self):
        figure_service.reset_detector_cache()
        self.addCleanup(figure_service.reset_detector_cache)

    def test_an_image_file_is_itself_one_figure(self):
        instance = figure_service.EmbeddedImageDetector()

        figures = instance.detect("ignored.png", FileType.IMAGE, [])

        self.assertEqual(len(figures), 1)
        self.assertTrue(figures[0].metadata["whole_page"])

    def test_docx_records_no_figures_rather_than_wrong_pages(self):
        """A DOCX has no page numbers - see the docx extractor - so a
        figure recorded against page 3 would be an invented location."""
        instance = figure_service.EmbeddedImageDetector()

        self.assertEqual(instance.detect("ignored.docx", FileType.DOCX, []), [])

    def test_a_detector_missing_the_protocol_is_refused_at_the_boundary(self):
        with override_settings(
            DOCUMENT_FIGURE_DETECTOR="documents.tests_figures.NotADetector"
        ):
            figure_service.reset_detector_cache()
            with self.assertRaises(figure_service.FigureDetectorUnavailable):
                figure_service.get_detector()


class NotADetector:
    """No name, no version, no detect()."""


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class PipelineFigureTests(TestCase):
    def setUp(self):
        figure_service.reset_detector_cache()
        self.addCleanup(figure_service.reset_detector_cache)
        self.user = make_user("figure_user", department="Registry")

    def process(self, body, name):
        document = Document.objects.create(
            title="Scanned notice",
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
        return document, version, job

    def test_an_uploaded_image_is_recorded_as_a_figure(self):
        _, version, _ = self.process(make_png_bytes("NOTICE"), "notice.png")

        figures = DocumentFigure.objects.filter(document_version=version)
        self.assertEqual(figures.count(), 1)
        self.assertEqual(figures.first().description, "")

    def test_a_retry_does_not_accumulate_figures(self):
        _, version, job = self.process(make_png_bytes("NOTICE"), "notice.png")
        run_pipeline(job.id)

        self.assertEqual(DocumentFigure.objects.filter(processing_job=job).count(), 1)

    def test_nothing_is_recorded_when_the_detector_is_off(self):
        with override_settings(DOCUMENT_FIGURE_DETECTOR=""):
            figure_service.reset_detector_cache()
            _, version, job = self.process(make_png_bytes("NOTICE"), "notice.png")

        self.assertEqual(
            DocumentFigure.objects.filter(document_version=version).count(), 0
        )
        event = job.events.get(stage=ProcessingJob.Stage.LAYOUT_ANALYSIS)
        self.assertIn("figure", event.message.lower())


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class FigureAbstentionTests(TestCase):
    """§12.4: abstention is a first-class outcome, and this is the most
    useful kind - one that says where to look."""

    def setUp(self):
        figure_service.reset_detector_cache()
        self.addCleanup(figure_service.reset_detector_cache)

        self.owner = make_user("figure_owner", department="Registry")
        self.outsider = make_user("figure_outsider", department="Physics")

        self.document = Document.objects.create(
            title="Enrolment Chart",
            document_type="OTHER",
            owner=self.owner,
            department="Registry",
        )
        body = make_png_bytes("ENROLMENT BY YEAR")
        version = self.document.versions.create(
            version_number=1,
            file=ContentFile(body, name="chart.png"),
            file_name="chart.png",
            file_size=len(body),
            uploaded_by=self.owner,
        )
        self.document.current_version = version
        self.document.save(update_fields=["current_version"])
        run_pipeline(ProcessingJob.objects.create(document_version=version).id)

    def test_an_abstention_names_the_figure_it_could_not_read(self):
        from documents.rag import engine

        hints = engine._figure_hints([self.document.id])

        self.assertTrue(hints)
        self.assertIn("Enrolment Chart", hints[0])
        self.assertIn("page 1", hints[0])
        self.assertIn("only text found inside them was extracted", hints[0])

    def test_an_ungrounded_answer_keeps_its_reason(self):
        """UNGROUNDED is the hallucination signal; an image in a retrieved
        document is not a reason to relabel it."""
        from documents.rag import engine

        answer = engine._abstain(
            "q",
            engine.Abstention.UNGROUNDED,
            0.0,
            retrieved_document_ids=[self.document.id],
        )

        self.assertEqual(answer.abstention_reason, engine.Abstention.UNGROUNDED)
        self.assertTrue(any("Enrolment Chart" in note for note in answer.notes))

    def test_an_unanswered_question_is_still_pointed_at_the_figure(self):
        from documents.rag import engine

        answer = engine._abstain(
            "q",
            engine.Abstention.NO_RELEVANT_PASSAGES,
            0.0,
            retrieved_document_ids=[self.document.id],
        )
        self.assertEqual(answer.abstention_reason, engine.Abstention.MAYBE_IN_A_FIGURE)

    def test_no_documents_means_no_hint_to_give(self):
        from documents.rag import engine

        self.assertEqual(engine._figure_hints([]), [])

    def test_the_hint_only_covers_documents_retrieval_already_found(self):
        """The safety property. A hint about a document the caller cannot
        see would disclose that it exists, so this must never widen the
        set - it takes the reranked document ids, which came through the
        permission filter inside search."""
        from documents.rag import engine

        self.assertEqual(engine._figure_hints([self.document.id + 9999]), [])

    def test_a_described_figure_produces_no_hint(self):
        """A described figure is readable; its text would have been
        retrievable in the ordinary way."""
        from documents.rag import engine

        DocumentFigure.objects.filter(document_version__document=self.document).update(
            description="Bar chart of enrolment by year", description_source="test"
        )

        self.assertEqual(engine._figure_hints([self.document.id]), [])

    def test_figures_on_a_superseded_version_are_not_pointed_at(self):
        """Sending a reader to page 3 of a version that is no longer the
        current one is worse than saying nothing."""
        from documents.rag import engine

        body = b"Plain text now, no chart at all.\n"
        replacement = self.document.versions.create(
            version_number=2,
            file=ContentFile(body, name="notice.txt"),
            file_name="notice.txt",
            file_size=len(body),
            uploaded_by=self.owner,
        )
        self.document.current_version = replacement
        self.document.save(update_fields=["current_version"])
        run_pipeline(ProcessingJob.objects.create(document_version=replacement).id)

        self.assertEqual(engine._figure_hints([self.document.id]), [])
