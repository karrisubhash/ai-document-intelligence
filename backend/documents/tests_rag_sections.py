"""Which part of a document a passage is in, and abbreviations a document defines.

Both were found on the live site (2026-11-18). "What is the syllabus for
dynamics of machinery" and "what was the syllabus for mefa" were answered "I
couldn't find enough information" over a syllabus that holds both courses:

* the units of a course sit on the page after its heading table and never
  repeat the course's name, so they ranked 21st-28th of the candidates and
  the cut at twenty dropped them - while the same syllabus uploaded three
  times filled those twenty with seven passages said three times;
* the question said MEFA, which the document defines once, in an elective
  table, and never uses again.
"""

from dataclasses import dataclass, field

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase, override_settings

from documents.models import Document, DocumentChunk, DocumentContent
from documents.rag import context as ctx
from documents.rag import sections, tables
from documents.rag.reranking import RerankedChunk, _terms
from documents.search import service as search_service
from documents.tests import MEDIA_ROOT, make_user


class RunningTitleTests(SimpleTestCase):
    def titles(self, *contexts):
        return sections.running_titles(
            (index, {"table_context": context} if context else {})
            for index, context in enumerate(contexts)
        )

    def test_a_titled_table_starts_a_section_that_the_next_chunks_belong_to(self):
        found = self.titles("", "DYNAMICS OF MACHINERY", "", "", "")

        self.assertNotIn(0, found)
        self.assertEqual({found[i] for i in (1, 2, 3, 4)}, {"DYNAMICS OF MACHINERY"})

    def test_the_next_title_ends_it(self):
        found = self.titles(
            "DYNAMICS OF MACHINERY", "", "APPLIED THERMODYNAMICS-II", ""
        )

        self.assertEqual(found[1], "DYNAMICS OF MACHINERY")
        self.assertEqual(found[2], "APPLIED THERMODYNAMICS-II")
        self.assertEqual(found[3], "APPLIED THERMODYNAMICS-II")

    def test_a_heading_that_names_a_kind_of_block_is_not_a_title(self):
        """SYLLABUS and TEXT BOOKS head a block in every course; letting them
        replace the course title would file the units under "SYLLABUS"."""
        found = self.titles(
            "DYNAMICS OF MACHINERY", "SYLLABUS", "TEXT BOOKS:", "UNIT - IV", "Unit 2"
        )

        self.assertEqual(set(found.values()), {"DYNAMICS OF MACHINERY"})

    def test_only_the_title_of_a_title_and_section_counts(self):
        found = self.titles("BUDGET 2026 / Practical")

        self.assertEqual(found[0], "BUDGET 2026")

    def test_chunks_are_ordered_by_their_index_not_the_order_they_arrive_in(self):
        found = sections.running_titles(
            [(2, {}), (0, {"table_context": "COURSE A"}), (1, {})]
        )

        self.assertEqual(found, {0: "COURSE A", 1: "COURSE A", 2: "COURSE A"})


class NamingASectionTests(SimpleTestCase):
    def names(self, question, title):
        return sections.names_section(set(_terms(question)), title)

    def test_a_question_that_says_the_title_names_it(self):
        self.assertTrue(
            self.names(
                "what is syllabus for dynamics of machinery", "DYNAMICS OF MACHINERY"
            )
        )

    def test_a_title_the_question_only_partly_says_is_not_named(self):
        self.assertFalse(
            self.names("what is the syllabus for machinery", "DYNAMICS OF MACHINERY")
        )

    def test_words_that_name_a_category_need_not_be_repeated(self):
        self.assertTrue(
            self.names(
                "syllabus of managerial economics and financial accountancy",
                "MANAGERIAL ECONOMICS AND FINANCIAL ACCOUNTANCY (Professional Elective-I)",
            )
        )

    def test_a_short_one_word_title_is_not_named_by_any_question_with_the_word(self):
        self.assertFalse(self.names("what are the results", "Results"))

    def test_a_long_one_word_title_can_be_named(self):
        self.assertTrue(self.names("tell me about thermodynamics", "THERMODYNAMICS"))

    def test_a_heading_read_without_its_spaces_is_still_named(self):
        """The live syllabus has "APPLIEDTHERMODYNAMICS-II" as one word."""
        question = "what is the syllabus for applied thermodynamics-II"

        self.assertTrue(
            sections.names_section(
                set(_terms(question)),
                "APPLIEDTHERMODYNAMICS-II",
                sections._squash(question),
            )
        )

    def test_letters_alone_do_not_match_a_short_title_inside_other_words(self):
        question = "what is the syllabus for the grid"

        self.assertFalse(
            sections.names_section(
                set(_terms(question)), "THEGRID", sections._squash(question)
            )
        )

    def test_a_title_with_no_content_words_names_nothing(self):
        self.assertFalse(self.names("what is the first year", "Year - I"))


class AbbreviationTests(SimpleTestCase):
    def test_the_words_whose_initials_spell_it_are_its_full_form(self):
        text = (
            "STREAM-4: Managerial Economics and Financial Accountancy (MEFA); "
            "STREAM-5: *Nano Technology (NT)"
        )

        self.assertEqual(
            sections._full_form(text, "MEFA"),
            "Managerial Economics and Financial Accountancy",
        )
        self.assertEqual(sections._full_form(text, "NT"), "Nano Technology")

    def test_initials_that_do_not_spell_it_are_not_a_definition(self):
        self.assertEqual(
            sections._full_form("The Library Committee (MEFA) met", "MEFA"), ""
        )

    def test_a_word_in_brackets_that_is_not_that_abbreviation_is_ignored(self):
        self.assertEqual(
            sections._full_form("Managerial Economics (ME) and more", "MEFA"), ""
        )

    def test_a_definition_must_start_on_a_word_not_a_connector(self):
        self.assertEqual(
            sections._full_form("results of Ranking Methods (RM)", "RM"),
            "Ranking Methods",
        )


@dataclass
class Document_:
    title: str = "R23 syllabus"


@dataclass
class Version_:
    document: object = field(default_factory=Document_)
    document_id: int = 1


@dataclass
class Chunk_:
    id: int
    text: str
    metadata: dict = field(default_factory=dict)
    section_title: str = ""
    document_version: object = field(default_factory=Version_)
    page_number: int = 7
    character_start: int = 0
    character_end: int = 0


class WhatTheRerankerAndTheModelSeeTests(SimpleTestCase):
    def test_a_passage_is_scored_with_the_section_it_is_in(self):
        chunk = Chunk_(
            1, "UNIT - I: GYROSCOPE & GOVERNORS", section_title="DYNAMICS OF MACHINERY"
        )

        text = tables.scoring_text(chunk)

        self.assertIn("DYNAMICS OF MACHINERY", text)
        self.assertTrue(text.endswith("UNIT - I: GYROSCOPE & GOVERNORS"))

    def test_a_section_the_passage_already_says_is_not_repeated(self):
        chunk = Chunk_(
            1,
            "Table 11 (DYNAMICS OF MACHINERY), row 1: Code: 23ME4119",
            section_title="DYNAMICS OF MACHINERY",
        )

        self.assertEqual(tables.scoring_text(chunk), chunk.text)

    def test_an_unlabelled_passage_is_scored_as_it_always_was(self):
        chunk = Chunk_(1, "Attendance must be 75 percent.")

        self.assertEqual(tables.scoring_text(chunk), "Attendance must be 75 percent.")

    def test_the_prompt_names_the_section_in_the_passage_header(self):
        chunk = Chunk_(
            1,
            "UNIT - I: GYROSCOPE & GOVERNORS. Gyroscopic torque, gyroscopic "
            "effect on aeroplane, ship and four wheeler.",
            section_title="DYNAMICS OF MACHINERY",
        )
        built = ctx.build([RerankedChunk(chunk=chunk, score=0.9)], budget=5000)

        prompt = ctx.render(built, "syllabus for dynamics of machinery")

        self.assertIn("R23 syllabus, page 7 (section: DYNAMICS OF MACHINERY)", prompt)

    def test_a_header_without_a_section_is_what_it_always_was(self):
        chunk = Chunk_(
            1, "Attendance must be 75 percent of the classes held in each course."
        )
        built = ctx.build([RerankedChunk(chunk=chunk, score=0.9)], budget=5000)

        prompt = ctx.render(built, "attendance")

        self.assertIn("[1] R23 syllabus, page 7\n", prompt)
        self.assertNotIn("(section:", prompt)


class CollapsingDuplicatesTests(SimpleTestCase):
    def fused(self, collapse):
        # Three uploads of the same two passages, then a different one.
        chunks = [
            Chunk_(1, "UNIT I  Gyroscope"),
            Chunk_(2, "unit i gyroscope"),
            Chunk_(3, "UNIT I Gyroscope"),
            Chunk_(4, "UNIT II  Engine force"),
            Chunk_(5, "UNIT II Engine force"),
            Chunk_(6, "Text books"),
        ]
        response = search_service.SearchResponse()
        hits = search_service._fuse(chunks, [], 4, collapse=collapse, response=response)
        return [hit.chunk.id for hit in hits], response

    def test_by_default_the_same_passage_is_as_many_hits_as_it_has_copies(self):
        ids, response = self.fused(collapse=False)

        self.assertEqual(ids, [1, 2, 3, 4])
        self.assertEqual(response.collapsed, 0)

    def test_collapsing_keeps_the_best_ranked_copy_and_fills_the_depth_with_others(
        self,
    ):
        ids, response = self.fused(collapse=True)

        self.assertEqual(ids, [1, 4, 6])
        self.assertEqual(response.collapsed, 3)


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class SectionsInTheDatabaseTests(TestCase):
    """The queries: sections read from stored chunks, and abbreviations from
    the documents the asker may read - and only those."""

    COURSE_A = [
        (
            "Table 11 (DYNAMICS OF MACHINERY), row 1: Code: 23ME4119; Credits: 3",
            "DYNAMICS OF MACHINERY",
        ),
        ("Prerequisite: Engineering Mathematics", ""),
        ("Table 15, row 1: SYLLABUS\nTable 15, row 2: UNIT - I; Periods: 6L+3T=9", ""),
        ("Table 15, row 13: Vibrations: Definitions - Types of vibrations", ""),
        ("Table 16, row 1: TEXT BOOKS:\nTable 16, row 2: 1.; Rattan, S. S.", ""),
    ]
    COURSE_B = [
        (
            "Table 17 (APPLIED THERMODYNAMICS), row 1: Code: 23ME4120; Credits: 3",
            "APPLIED THERMODYNAMICS",
        ),
        ("Table 19, row 1: UNIT - I; Periods: 6L", ""),
    ]

    def setUp(self):
        self.owner = make_user("sections_owner", department="Mechanical")
        self.outsider = make_user("sections_outsider", department="Physics")
        self.document = self.make_document("R23 syllabus", "Mechanical")
        self.chunks = self.fill(self.document, [*self.COURSE_A, *self.COURSE_B])
        self.other = self.make_document("Another syllabus", "Mechanical")
        self.fill(
            self.other,
            [
                (
                    "Table 1 (STRENGTH OF MATERIALS), row 1: Code: 23ME3101",
                    "STRENGTH OF MATERIALS",
                ),
                ("Table 2, row 1: UNIT - I; Periods: 6L", ""),
            ],
        )

    def make_document(self, title, department):
        document = Document.objects.create(
            title=title, document_type="OTHER", owner=self.owner, department=department
        )
        version = document.versions.create(
            version_number=1,
            file=ContentFile(b"x", name="x.txt"),
            file_name="x.txt",
            file_size=1,
            uploaded_by=self.owner,
        )
        document.current_version = version
        document.save(update_fields=["current_version"])
        version.current_content = DocumentContent.objects.create(
            document_version=version, processing_version="test"
        )
        version.save(update_fields=["current_content"])
        return document

    def fill(self, document, rows):
        version = document.current_version
        made = []
        for index, (text, context) in enumerate(rows):
            made.append(
                DocumentChunk.objects.create(
                    document_version=version,
                    document_content=version.current_content,
                    chunk_index=index,
                    page_number=6 + index // 4,
                    character_start=0,
                    character_end=len(text),
                    text=text,
                    character_count=len(text),
                    chunking_strategy="test",
                    chunking_version="test",
                    metadata={"table_context": context} if context else {},
                )
            )
        return made

    def reranked(self, chunks):
        return [RerankedChunk(chunk=chunk, score=0.1) for chunk in chunks]

    def test_every_chunk_of_a_course_is_labelled_with_its_title(self):
        items = self.reranked(
            DocumentChunk.objects.filter(
                document_version__document=self.document
            ).order_by("chunk_index")
        )

        sections.annotate(items)

        found = {item.chunk.chunk_index: item.chunk.section_title for item in items}
        self.assertEqual([found[i] for i in range(5)], ["DYNAMICS OF MACHINERY"] * 5)
        self.assertEqual(found[5], "APPLIED THERMODYNAMICS")
        self.assertEqual(found[6], "APPLIED THERMODYNAMICS")

    def test_a_question_naming_a_course_brings_its_whole_section_in_order(self):
        candidates = self.reranked([self.chunks[3]])

        named = sections.expand(
            self.owner, "what is syllabus for dynamics of machinery", candidates
        )

        self.assertEqual([item.chunk.chunk_index for item in named], [0, 1, 2, 3, 4])
        self.assertEqual(
            {item.chunk.section_title for item in named}, {"DYNAMICS OF MACHINERY"}
        )

    def test_a_passage_retrieval_already_scored_keeps_its_score(self):
        candidates = [RerankedChunk(chunk=self.chunks[3], score=0.77)]

        named = sections.expand(
            self.owner, "syllabus for dynamics of machinery", candidates
        )

        scored = [item for item in named if item.chunk.id == self.chunks[3].id]
        self.assertIs(scored[0], candidates[0])
        self.assertEqual(scored[0].score, 0.77)

    def test_a_question_that_names_no_section_brings_nothing(self):
        candidates = self.reranked([self.chunks[3]])

        self.assertEqual(
            sections.expand(self.owner, "what is the syllabus", candidates), []
        )

    def test_a_section_of_a_document_retrieval_never_touched_is_not_brought(self):
        """ "Strength of materials" is a section of ANOTHER file; the
        candidates came from this one."""
        candidates = self.reranked([self.chunks[3]])

        named = sections.expand(
            self.owner, "syllabus for strength of materials", candidates
        )

        self.assertEqual(named, [])

    def test_a_course_in_the_same_file_as_a_candidate_is_found(self):
        candidates = self.reranked([self.chunks[3]])

        named = sections.expand(
            self.owner, "syllabus for applied thermodynamics", candidates
        )

        self.assertEqual([item.chunk.chunk_index for item in named], [5, 6])

    def test_a_reader_who_may_not_see_the_document_gets_nothing(self):
        candidates = self.reranked([self.chunks[3]])

        named = sections.expand(
            self.outsider, "syllabus for dynamics of machinery", candidates
        )

        self.assertEqual(named, [])

    def test_an_abbreviation_the_documents_define_is_expanded(self):
        self.fill_defining_chunk()

        question, forms = sections.with_expansions(self.owner, "syllabus for mefa")

        self.assertEqual(forms, ["Managerial Economics and Financial Accountancy"])
        self.assertTrue(question.startswith("syllabus for mefa"))
        self.assertIn("Managerial Economics and Financial Accountancy", question)

    def test_an_abbreviation_defined_in_a_document_the_asker_cannot_see_is_not(self):
        self.fill_defining_chunk()

        question, forms = sections.with_expansions(self.outsider, "syllabus for mefa")

        self.assertEqual((question, forms), ("syllabus for mefa", []))

    def test_a_question_with_nothing_to_expand_is_unchanged(self):
        question, forms = sections.with_expansions(self.owner, "who teaches physics")

        self.assertEqual((question, forms), ("who teaches physics", []))

    def fill_defining_chunk(self):
        DocumentChunk.objects.create(
            document_version=self.document.current_version,
            document_content=self.document.current_version.current_content,
            chunk_index=50,
            page_number=3,
            character_start=0,
            character_end=10,
            text="Table 4, row 1: STREAM-4: Managerial Economics and Financial Accountancy (MEFA)",
            character_count=10,
            chunking_strategy="test",
            chunking_version="test",
        )
