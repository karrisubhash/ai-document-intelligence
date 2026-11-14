"""Questions about tables: scoring, the relevance floor, and computed figures.

Each case here was a table question answered "I couldn't find enough
information in the provided documents" while the table was in the corpus
(observed on the v2 stack, 2026-11-14): a staff table scored 0.00-0.14 by the
cross-encoder against a floor of 0.30 for "who has the highest earned leave?",
and a total that no cell states was refused as an invented figure.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase

from documents.rag import context as ctx
from documents.rag import engine, grounding, tables
from documents.rag.claude import SYSTEM
from documents.rag.engine import Abstention
from documents.rag.llm import ExtractiveAnswerer
from documents.rag.local_rerank import LocalCrossEncoderReranker
from documents.rag.reranking import RerankedChunk
from documents.search.service import SearchResponse

HEADER = ["Employee", "Department", "Casual Leave", "Sick Leave", "Earned Leave"]
ROWS = (
    "Rahul Mehta | Finance | 12 | 8 | 30\n"
    "Anita Rao | Finance | 10 | 6 | 45\n"
    "Vikram Singh | Engineering | 9 | 12 | 22\n"
    "Priya Nair | Engineering | 14 | 4 | 60\n"
    "Neha Gupta | Finance | 15 | 3 | 41"
)
TABLE_TEXT = " | ".join(HEADER) + "\n" + ROWS

ITEMS = ("Item", "Qty")


@dataclass
class FakeDocument:
    id: int
    title: str


@dataclass
class FakeVersion:
    document_id: int
    document: FakeDocument


@dataclass
class FakeChunk:
    id: int
    text: str
    metadata: dict = field(default_factory=dict)
    page_number: int = 1
    character_start: int = 0
    character_end: int = 0
    document_version: object = None
    document_version_id: int = 1


@dataclass
class FakeHit:
    chunk: object
    score: float = 0.5


def make_chunk(
    identifier,
    text,
    *,
    title="Staff leave balances",
    document_id=None,
    table=True,
    header=tuple(HEADER),
    **metadata,
):
    document_id = identifier if document_id is None else document_id
    meta = dict(metadata)
    if table:
        meta.update(kind="TABLE", table_index=0)
        if header:
            meta["header"] = list(header)
    return FakeChunk(
        id=identifier,
        text=text,
        metadata=meta,
        character_end=len(text),
        document_version=FakeVersion(
            document_id=document_id,
            document=FakeDocument(id=document_id, title=title),
        ),
        document_version_id=document_id,
    )


def reranked(chunk, score):
    return RerankedChunk(chunk=chunk, score=score, retrieval_score=0.5)


def passage(marker, chunk):
    return ctx.Passage(
        marker=marker,
        chunk=chunk,
        text=chunk.text,
        relevance=1.0,
        table_header=tuple(chunk.metadata.get("header") or ()),
    )


def context_of(*chunks):
    return ctx.BuiltContext(
        passages=[passage(i + 1, c) for i, c in enumerate(chunks)],
        characters=sum(len(c.text) for c in chunks),
    )


# --- 1. what the reranker reads -----------------------------------------------


class ScoringTextTests(SimpleTestCase):
    def test_a_prose_chunk_is_scored_as_it_is(self):
        chunk = make_chunk(1, "  Attendance is 75 percent.  ", table=False)
        self.assertEqual(tables.scoring_text(chunk), "Attendance is 75 percent.")

    def test_a_table_chunk_is_scored_with_its_title_and_columns(self):
        text = tables.scoring_text(make_chunk(1, ROWS))

        self.assertTrue(text.startswith("Staff leave balances."))
        self.assertIn(
            "Table with columns: Employee, Department, Casual Leave, "
            "Sick Leave, Earned Leave.",
            text,
        )
        self.assertTrue(text.endswith(ROWS))

    def test_a_table_with_no_recorded_header_still_gets_its_title(self):
        text = tables.scoring_text(make_chunk(1, ROWS, header=()))
        self.assertTrue(text.startswith("Staff leave balances. Table."))

    def test_the_stored_chunk_is_not_touched(self):
        """A citation re-slices the page by offsets; the reranker's context
        must never leak into the chunk."""
        chunk = make_chunk(1, ROWS)
        tables.scoring_text(chunk)
        self.assertEqual(chunk.text, ROWS)

    def test_a_blank_chunk_is_blank(self):
        self.assertEqual(tables.scoring_text(make_chunk(1, "   ")), "")

    def test_the_local_cross_encoder_is_given_the_table_context(self):
        class Scores(list):
            def tolist(self):
                return list(self)

        class Model:
            def __init__(self):
                self.pairs = []

            def predict(self, pairs, **kwargs):
                self.pairs = list(pairs)
                return Scores([0.5] * len(self.pairs))

        model = Model()
        reranker = LocalCrossEncoderReranker()
        reranker._model = model
        reranker.rerank(
            "who has the highest earned leave?",
            [
                FakeHit(make_chunk(1, ROWS)),
                FakeHit(make_chunk(2, "prose", table=False)),
            ],
        )

        texts = [text for _, text in model.pairs]
        self.assertEqual(len(texts), 2)
        self.assertTrue(
            any(
                t.startswith("Staff leave balances.") and t.endswith(ROWS)
                for t in texts
            )
        )
        self.assertIn("prose", texts)


# --- 2. which tables may skip the floor ---------------------------------------


class AdmissionTests(SimpleTestCase):
    FLOOR = 0.30

    def admit(self, question, items, ranks):
        return tables.admit_below_floor(question, items, self.FLOOR, ranks)

    def test_a_table_the_question_is_about_is_admitted_below_the_floor(self):
        chunk = make_chunk(1, TABLE_TEXT)
        admitted = self.admit(
            "Who has the highest earned leave?", [reranked(chunk, 0.10)], {1: 1}
        )
        self.assertEqual([item.chunk.id for item in admitted], [1])

    def test_a_passing_table_is_not_in_the_admitted_list(self):
        """Admission is only for what the floor rejected."""
        chunk = make_chunk(1, TABLE_TEXT)
        self.assertEqual(
            self.admit("highest earned leave", [reranked(chunk, 0.9)], {1: 1}), []
        )

    def test_a_prose_passage_is_never_admitted(self):
        chunk = make_chunk(1, "Earned leave is accrued monthly.", table=False)
        self.assertEqual(
            self.admit("highest earned leave", [reranked(chunk, 0.1)], {1: 1}), []
        )

    def test_a_table_retrieval_ranked_low_is_not_admitted(self):
        chunk = make_chunk(1, TABLE_TEXT)
        rank = tables.ADMIT_WITHIN_RETRIEVAL_RANK + 1
        self.assertEqual(
            self.admit("highest earned leave", [reranked(chunk, 0.1)], {1: rank}), []
        )
        self.assertEqual(
            self.admit("highest earned leave", [reranked(chunk, 0.1)], {}), []
        )

    def test_a_table_sharing_no_term_with_the_question_is_not_admitted(self):
        chunk = make_chunk(1, TABLE_TEXT)
        self.assertEqual(
            self.admit(
                "What is the capital of France?", [reranked(chunk, 0.0)], {1: 1}
            ),
            [],
        )

    def test_at_most_two_tables_are_admitted(self):
        items = [reranked(make_chunk(i, TABLE_TEXT), 0.1) for i in (1, 2, 3)]
        admitted = self.admit("highest earned leave", items, {1: 1, 2: 2, 3: 3})
        self.assertEqual(len(admitted), tables.MAX_ADMITTED_TABLES)

    def test_one_shared_cell_value_is_a_coincidence_not_a_match(self):
        """Observed: "Q3 sales of Valves" admitted a staff table because
        "sales" is also a department in it, beside the sales table that had
        scored 0.99."""
        sales = make_chunk(
            1,
            "Region | Product | Q3\nSouth | Valves | 6317",
            title="Regional sales",
            header=("Region", "Product", "Q3"),
        )
        staff = make_chunk(2, TABLE_TEXT + "\nRohit Verma | Sales | 10 | 9 | 36")
        admitted = self.admit(
            "What were the Q3 sales of Valves?",
            [reranked(sales, 0.98), reranked(staff, 0.0)],
            {1: 1, 2: 3},
        )
        self.assertEqual(admitted, [])

    def test_two_shared_cell_values_are_a_match(self):
        """A question about a named row ("Rahul Mehta") has no column label
        to match, and two of its words are in the table."""
        admitted = self.admit(
            "What is the salary of Rahul Mehta?",
            [reranked(make_chunk(1, TABLE_TEXT), 0.0)],
            {1: 1},
        )
        self.assertEqual(len(admitted), 1)

    def test_a_plural_names_a_singular_column(self):
        """ "employees" against an Employee column: the stemmer alone leaves
        "employe" and "employee" apart."""
        admitted = self.admit(
            "How many employees are in Engineering?",
            [reranked(make_chunk(1, TABLE_TEXT), 0.002)],
            {1: 1},
        )
        self.assertEqual(len(admitted), 1)

    def test_a_passage_from_another_document_does_not_block_the_table(self):
        """Observed: the staff leave RULES passed the floor for "total earned
        leave of the Finance department", and a rule requiring the table to
        share their document refused the table that held the answer."""
        rules = make_chunk(
            1, "Earned leave may be carried forward.", table=False, document_id=9
        )
        table = make_chunk(2, TABLE_TEXT, document_id=7)
        admitted = self.admit(
            "What is the total earned leave of the Finance department?",
            [reranked(rules, 0.9), reranked(table, 0.24)],
            {1: 1, 2: 2},
        )
        self.assertEqual([item.chunk.id for item in admitted], [2])

    def test_a_table_of_a_document_the_reranker_accepted_is_admitted(self):
        """The title chunk passed; the rows of the same document did not."""
        title = make_chunk(
            1, "STAFF LEAVE BALANCES 2024-25", table=False, document_id=7
        )
        rows = make_chunk(2, TABLE_TEXT, document_id=7)
        admitted = self.admit(
            "total earned leave of Finance",
            [reranked(title, 0.8), reranked(rows, 0.2)],
            {1: 1, 2: 2},
        )
        self.assertEqual([item.chunk.id for item in admitted], [2])


# --- 3. figures a table implies -----------------------------------------------


class ParsingTests(SimpleTestCase):
    def test_rows_split_on_pipes_tabs_and_runs_of_spaces(self):
        self.assertEqual(tables.parse_rows("a | b | c"), [["a", "b", "c"]])
        self.assertEqual(tables.parse_rows("a\tb\tc"), [["a", "b", "c"]])
        self.assertEqual(tables.parse_rows("a   b   c"), [["a", "b", "c"]])

    def test_a_pipe_row_keeps_its_blank_cells(self):
        self.assertEqual(tables.parse_rows("a |  | c"), [["a", "", "c"]])

    def test_lines_that_are_not_rows_are_skipped(self):
        self.assertEqual(tables.parse_rows("Caption\n\na | b"), [["a", "b"]])

    def test_figures_are_numbers_and_labels_are_not(self):
        for cell, expected in [
            ("85000", "85000"),
            ("85,000", "85000"),
            ("Rs. 1,200", "1200"),
            ("₹ 2500", "2500"),
            ("75%", "75"),
            ("-3.5", "-3.5"),
        ]:
            self.assertEqual(tables.parse_number(cell), Decimal(expected), cell)
        for cell in ["B.Tech", "2024-25", "12 Mar 2025", "", "n/a", "1,2,x"]:
            self.assertIsNone(tables.parse_number(cell), cell)


class DerivedFiguresTests(SimpleTestCase):
    def figures(self, *chunks):
        return tables.derived_figures([passage(i + 1, c) for i, c in enumerate(chunks)])

    def test_a_column_total_average_highest_and_lowest(self):
        figures = self.figures(make_chunk(1, TABLE_TEXT))
        # Earned Leave: 30 45 22 60 41
        self.assertIn("198", figures)  # sum
        self.assertIn("39.6", figures)  # average
        self.assertIn("60", figures)  # highest
        self.assertIn("22", figures)  # lowest

    def test_a_total_for_the_rows_with_one_label(self):
        figures = self.figures(make_chunk(1, TABLE_TEXT))
        self.assertIn("116", figures)  # Finance earned: 30 + 45 + 41
        self.assertIn("12.33", figures)  # Finance casual: (12 + 10 + 15) / 3

    def test_how_many_rows_match_a_label(self):
        figures = self.figures(make_chunk(1, TABLE_TEXT))
        self.assertIn("5", figures)  # all rows
        self.assertIn("3", figures)  # Finance rows

    def test_the_header_row_is_not_a_row(self):
        text = "Item | Qty\nA | 10\nB | 20\nC | 30"
        figures = self.figures(make_chunk(1, text, header=ITEMS))
        self.assertIn("3", figures)
        self.assertNotIn("4", figures)

    def test_a_header_the_extractor_did_not_record_is_still_a_header(self):
        text = "Item | Qty\nA | 10\nB | 20\nC | 30"
        figures = self.figures(make_chunk(1, text, header=()))
        self.assertIn("3", figures)
        self.assertNotIn("4", figures)

    def test_labels_that_look_like_years_are_never_summed(self):
        text = "Year | Fee\n2024-25 | 100\n2025-26 | 120"
        figures = self.figures(make_chunk(1, text, header=("Year", "Fee")))
        self.assertIn("220", figures)
        self.assertNotIn("4049", figures)

    def test_parts_of_one_table_are_totalled_together(self):
        first = make_chunk(1, "Item | Qty\nA | 5\nB | 7", header=ITEMS, part=1, parts=2)
        second = make_chunk(2, "C | 11\nD | 13", header=ITEMS, part=2, parts=2)
        # 1 and 2 are chunks of one table: the same document, the same index.
        first.document_version_id = second.document_version_id = 1
        self.assertIn("36", self.figures(first, second))

    def test_a_different_documents_tables_are_not_mixed(self):
        a = make_chunk(1, "Item | Qty\nA | 5\nB | 7", header=ITEMS, document_id=1)
        b = make_chunk(2, "Item | Qty\nC | 11\nD | 13", header=ITEMS, document_id=2)
        figures = self.figures(a, b)
        self.assertIn("12", figures)
        self.assertIn("24", figures)
        self.assertNotIn("36", figures)

    def test_prose_implies_nothing(self):
        self.assertEqual(
            self.figures(make_chunk(1, "Fee is 100 | 200", table=False)), set()
        )

    def test_a_difference_is_not_a_derived_figure(self):
        """Only sums, averages, highest, lowest and counts are computed."""
        figures = self.figures(
            make_chunk(1, "Item | Qty\nA | 100\nB | 40", header=ITEMS)
        )
        self.assertIn("140", figures)
        self.assertNotIn("60", figures)  # 100 - 40


class GroundingTableTests(SimpleTestCase):
    def check(self, answer, *chunks):
        return grounding.validate(answer, context_of(*chunks))

    def test_a_correct_total_is_accepted(self):
        report = self.check(
            "The total earned leave of Finance is 116 days: Rahul Mehta 30 [1], "
            "Anita Rao 45 [1] and Neha Gupta 41 [1].",
            make_chunk(1, TABLE_TEXT),
        )
        self.assertTrue(report.ok, report.failures)

    def test_a_correct_count_and_highest_value_are_accepted(self):
        chunk = make_chunk(1, TABLE_TEXT)
        self.assertTrue(self.check("There are 3 employees in Finance [1].", chunk).ok)
        self.assertTrue(
            self.check("Priya Nair has the highest earned leave, 60 [1].", chunk).ok
        )

    def test_a_total_written_with_a_thousands_separator_is_accepted(self):
        chunk = make_chunk(1, "Item | Qty\nA | 9,000\nB | 6,000", header=ITEMS)
        self.assertTrue(self.check("The total quantity is 15,000 [1].", chunk).ok)

    def test_a_wrong_total_is_still_an_invented_figure(self):
        report = self.check(
            "The total earned leave of Finance is 117 days [1].",
            make_chunk(1, TABLE_TEXT),
        )
        self.assertFalse(report.ok)
        self.assertIn("117", report.unsupported_facts)

    def test_a_figure_that_is_nowhere_in_the_table_still_fails(self):
        report = self.check(
            "Rahul Mehta has 999 days of sick leave [1].", make_chunk(1, TABLE_TEXT)
        )
        self.assertFalse(report.ok)

    def test_a_prose_passage_does_not_gain_derived_figures(self):
        prose = make_chunk(1, "Fee is 100 | 200", table=False)
        self.assertFalse(self.check("The total is 300 [1].", prose).ok)

    def test_only_cited_passages_count(self):
        a = make_chunk(1, "Item | Qty\nA | 5\nB | 7", header=ITEMS, document_id=1)
        b = make_chunk(2, "Item | Qty\nC | 11\nD | 13", header=ITEMS, document_id=2)
        self.assertFalse(self.check("The total quantity is 24 [1].", a, b).ok)


# --- the prompt ---------------------------------------------------------------


class PromptTests(SimpleTestCase):
    def test_a_part_of_a_larger_table_says_so(self):
        chunk = make_chunk(1, ROWS, part=2, parts=3)
        prompt = ctx.render(context_of(chunk), "highest earned leave?")
        self.assertIn("table columns: Employee | Department", prompt)
        self.assertIn("part 2 of 3 of this table", prompt)

    def test_a_whole_table_does_not_claim_to_be_a_part(self):
        prompt = ctx.render(context_of(make_chunk(1, ROWS)), "highest earned leave?")
        passages = prompt.split("PASSAGES")[1].split("QUESTION")[0]
        self.assertNotIn("part ", passages)

    def test_a_table_with_no_header_still_reports_its_part(self):
        chunk = make_chunk(1, ROWS, header=(), part=1, parts=2)
        prompt = ctx.render(context_of(chunk), "q")
        self.assertIn("(part 1 of 2 of this table", prompt)

    def test_the_system_prompt_allows_table_arithmetic_and_nothing_else(self):
        self.assertIn("A table's rows may be compared, counted and added", SYSTEM)
        self.assertIn("differences, percentages, projections or conversions", SYSTEM)
        self.assertIn("exception is arithmetic over a table's rows", SYSTEM)


# --- the engine ---------------------------------------------------------------


class StubReranker:
    """A cross-encoder that scores everything the same, low by default: what
    MiniLM did to a table for a question that compares or counts."""

    name = "stub-cross-encoder"
    semantic_scores = True
    default_relevance_floor = 0.30

    def __init__(self, score=0.05):
        self.score = score

    def rerank(self, query, candidates):
        return [
            RerankedChunk(
                chunk=c.chunk, score=self.score, retrieval_score=c.score, signals={}
            )
            for c in candidates
        ]


class EngineTableTests(SimpleTestCase):
    FEES = (
        "Programme | Tuition | Exam Fee\nB.Tech CSE | 85000 | 2500\n"
        "B.Tech ECE | 82000 | 2500"
    )

    def ask(self, question, chunks, reranker=None):
        retrieval = SearchResponse(hits=[FakeHit(chunk) for chunk in chunks])
        with (
            mock.patch.object(engine.search_service, "search", return_value=retrieval),
            mock.patch.object(
                engine, "get_reranker", return_value=reranker or StubReranker()
            ),
            mock.patch.object(
                engine, "get_answerer", return_value=ExtractiveAnswerer()
            ),
        ):
            return engine.ask(None, question)

    def fees(self, **kwargs):
        return make_chunk(
            1,
            self.FEES,
            title="Fee structure",
            header=("Programme", "Tuition", "Exam Fee"),
            **kwargs,
        )

    def test_a_table_the_floor_would_have_rejected_reaches_the_answerer(self):
        answer = self.ask("What is the exam fee for B.Tech ECE?", [self.fees()])

        self.assertFalse(answer.abstained, answer.abstention_reason)
        self.assertEqual(answer.passages_used, 1)
        self.assertTrue(
            any(
                "table passage" in note and "below the relevance floor" in note
                for note in answer.notes
            ),
            answer.notes,
        )

    def test_a_question_the_table_shares_nothing_with_still_abstains(self):
        answer = self.ask("What is the capital of France?", [self.fees()])

        self.assertTrue(answer.abstained)
        self.assertEqual(answer.abstention_reason, Abstention.NO_RELEVANT_PASSAGES)

    def test_prose_below_the_floor_still_abstains(self):
        answer = self.ask(
            "highest earned leave",
            [make_chunk(1, "Earned leave is accrued monthly.", table=False)],
        )
        self.assertEqual(answer.abstention_reason, Abstention.NO_RELEVANT_PASSAGES)

    def test_a_table_the_reranker_accepts_is_not_reported_as_admitted(self):
        answer = self.ask(
            "What is the exam fee for B.Tech ECE?",
            [self.fees()],
            reranker=StubReranker(score=0.9),
        )
        self.assertFalse(answer.abstained, answer.abstention_reason)
        self.assertFalse(any("table passage" in note for note in answer.notes))
