"""What the third review found in the evaluation tooling, and the fixes.

E1  correctness credited the question itself, a pasted wall of text and the
    right number with the wrong unit.
E2  correctness rejected correct answers for how they spell a clock time, a
    per-unit price, a hyphenated unit, a ".00" tail, a year span and a bare
    number word.
E3  the default gate dropped the whole `system` block, so a run that silently
    fell back to quoting passed.
E4  capture_configuration left out settings that change a score.
E5  benchmark v3 repeated two v2 questions and mislabelled two answers.
"""

import json
import re
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings

from documents import rag
from documents.evaluation import correctness, metrics, runner
from documents.processing.chunking import service as chunking_service
from documents.tests import MEDIA_ROOT

EMBEDDER = "documents.processing.embedding.hashing.HashingEmbeddingProvider"
EVALUATION_DIR = Path(__file__).resolve().parents[2] / "evaluation"


class CorrectnessSpellingTests(SimpleTestCase):
    """E2: two spellings of the SAME figure are one figure."""

    def test_equivalent_spellings_are_correct(self):
        for expected, answer in (
            # Clock times: colon, dot, 24-hour clock and am/pm are one time.
            ("after 9.15 am", "Arrival after 9:15 am is late."),
            ("after 9.15 am", "Arrival after 09:15 is late."),
            ("after 9.15 am", "Late means after 9.15 AM."),
            ("11 pm", "Be back by 11:00 p.m."),
            ("11 pm", "Be back by 11.00 pm."),
            ("11 pm", "Be back by 23:00."),
            ("11 pm", "Be back by 11pm."),
            # A per-unit price.
            ("5 rupees per day per book", "The fine is Rs 5/day per book."),
            ("5 rupees per day per book", "Rs.5 per day per book."),
            ("500 rupees per week", "A surcharge of Rs.500/week applies."),
            # N-unit as an adjective.
            ("at least 12 characters", "It must be a 12-character password."),
            ("30 days", "A 30-day limit applies."),
            # A ".00" tail.
            ("82000", "The fee is Rs 82,000.00."),
            ("82000", "The fee is 82000.00."),
            # A year span.
            ("2024-25", "It covers 2024/25."),
            ("2024-25", "It covers 2024 - 25."),
            ("2024-25", "It covers 2024–25."),
            ("2024-25", "It covers 2024-2025."),
            # A range written with a dash.
            (
                "condonation on medical grounds, between 65 and 75 percent",
                "Between 65-75% with medical grounds, condonation may be sought.",
            ),
            # A bare expected number word is its digit.
            ("four", "They may borrow 4 books [1]."),
            ("six", "It is 6."),
            ("four", "Four."),
            ("twenty-five", "That is 25 days."),
        ):
            with self.subTest(expected=expected, answer=answer):
                self.assertTrue(correctness.is_correct(answer, expected))

    def test_a_wrong_figure_is_still_wrong_in_every_spelling(self):
        """The negative half: no spelling above may let a DIFFERENT figure in."""
        for expected, answer in (
            ("after 9.15 am", "Arrival after 9:20 am is late."),
            ("after 9.15 am", "Arrival after 21:15 is late."),
            ("after 9.15 am", "Late means after 9.15 pm."),
            ("11 pm", "Be back by 11:00 a.m."),
            ("11 pm", "Be back by 11 am."),
            ("11 pm", "Be back by 22:00."),
            ("11 pm", "Be back by 11:30 pm."),
            ("5 rupees per day per book", "The fine is Rs 6/day per book."),
            ("500 rupees per week", "A surcharge of Rs.50/week applies."),
            ("at least 12 characters", "It must be a 10-character password."),
            ("30 days", "A 60-day limit applies."),
            ("82000", "The fee is Rs 82,001.00."),
            ("82000", "The fee is 8200.00."),
            ("2024-25", "It covers 2023/24."),
            ("2024-25", "It covers 2024/26."),
            (
                "condonation on medical grounds, between 65 and 75 percent",
                "Between 65-70% with medical grounds, condonation may be sought.",
            ),
            ("four", "They may borrow 5 books."),
            ("six", "It is 4."),
            ("four", "They may borrow books."),
            ("twenty-five", "That is 52 days."),
        ):
            with self.subTest(expected=expected, answer=answer):
                self.assertFalse(correctness.is_correct(answer, expected))

    def test_a_dotted_figure_is_a_time_only_when_the_expected_answer_says_so(self):
        """ "9.15" is also a decimal. It stands for a time only against an
        expected answer that states that time, never on its own."""
        self.assertFalse(correctness.is_correct("The fee is 9.15 rupees.", "9.20"))
        self.assertFalse(correctness.is_correct("The ratio is 9.15.", "11 pm"))
        self.assertTrue(correctness.is_correct("Late after 9.15.", "after 9.15 am"))
        # A decimal the expected answer states as a decimal stays one.
        self.assertTrue(
            correctness.is_correct("The ratio is 9.15.", "The ratio is 9.15")
        )
        # ...and is not a clock time just because the answer writes one.
        self.assertFalse(correctness.is_correct("It is 09:15.", "The ratio is 9.15"))

    def test_a_year_span_must_be_consecutive_years(self):
        """ "2024/26" is not the academic year 2024-25: only a span of
        consecutive years is one academic year."""
        self.assertFalse(correctness.is_correct("It covers 2024/26.", "2024-25"))
        self.assertFalse(correctness.is_correct("It covers 2024-27.", "2024-25"))

    def test_a_range_is_not_rescued_by_one_end_alone(self):
        self.assertFalse(
            correctness.is_correct("Between 65-75%.", "between 65 and 80 percent")
        )

    def test_a_model_s_no_break_space_and_hyphen_are_the_ordinary_ones(self):
        """A real-model run flagged six right answers wrong because gpt-oss writes
        a NARROW NO-BREAK SPACE between a number and its unit or month."""
        narrow = chr(0x202F)
        hyphen = chr(0x2011)
        for answer, expected in (
            (f"The curfew is 11{narrow}pm.", "11 pm"),
            (f"Register before 15{narrow}August.", "15 August"),
            (f"The fine is 5{narrow}rupees per day.", "5 rupees per day"),
            (f"It covers 2024{hyphen}25.", "2024-25"),
        ):
            with self.subTest(answer=answer):
                self.assertTrue(correctness.is_correct(answer, expected))
        self.assertFalse(
            correctness.is_correct(f"Register before 15{narrow}September.", "15 August")
        )
        self.assertFalse(
            correctness.is_correct(f"It covers 2023{hyphen}24.", "2024-25")
        )


class CorrectnessGuardTests(SimpleTestCase):
    """E1: what a number must not be credited for."""

    QUESTION = "What happens if my attendance is under sixty-five percent?"
    EXPECTED = "condonation is not permitted below 65 percent"

    def test_the_question_itself_is_not_an_answer(self):
        self.assertFalse(
            correctness.is_correct(self.QUESTION, self.EXPECTED, question=self.QUESTION)
        )
        # ...also without its question mark, and with another case.
        self.assertFalse(
            correctness.is_correct(
                "what happens if my attendance is under sixty-five percent",
                self.EXPECTED,
                question=self.QUESTION,
            )
        )

    def test_a_restatement_of_the_question_is_not_an_answer(self):
        self.assertFalse(
            correctness.is_correct(
                "Attendance under sixty-five percent.",
                self.EXPECTED,
                question=self.QUESTION,
            )
        )

    def test_a_restatement_does_not_pass_on_word_recall_either(self):
        """With no figure to find, the question's own words would otherwise
        satisfy the content-word recall."""
        question = "Does condonation on medical grounds need a certificate?"
        expected = "condonation on medical grounds with a certificate"
        self.assertFalse(
            correctness.is_correct(
                "Condonation, medical grounds, certificate.",
                expected,
                question=question,
            )
        )
        self.assertTrue(
            correctness.is_correct(
                "Yes: condonation on medical grounds needs a certificate from a "
                "registered practitioner.",
                expected,
                question=question,
            )
        )

    def test_the_answer_after_an_echo_is_what_counts(self):
        answer = f"{self.QUESTION} Condonation is not permitted below 65 percent."
        self.assertTrue(
            correctness.is_correct(answer, self.EXPECTED, question=self.QUESTION)
        )
        # An echo followed by the WRONG figure does not become right.
        self.assertFalse(
            correctness.is_correct(
                f"{self.QUESTION} Condonation is possible from 55 percent.",
                self.EXPECTED,
                question=self.QUESTION,
            )
        )

    def test_a_short_correct_answer_is_not_a_restatement(self):
        self.assertTrue(
            correctness.is_correct(
                "75 percent.",
                "75 percent",
                question="What is the minimum attendance in percent?",
            )
        )

    def test_the_questions_own_figure_is_not_credited_to_a_bare_restatement(self):
        question = "Is it true that attendance under 65 percent gets condonation?"
        self.assertFalse(
            correctness.is_correct("65 percent.", "65 percent", question=question)
        )
        # An answer that says something of its own is judged on its figure.
        self.assertTrue(
            correctness.is_correct(
                "No, below 65 percent is never condoned.",
                "65 percent",
                question=question,
            )
        )

    def test_the_runner_hands_the_question_to_the_metric(self):
        """score_case must pass the question, or the echo guard is dead code."""
        from types import SimpleNamespace

        question = "What happens if my attendance is under sixty-five percent?"
        case = SimpleNamespace(
            question=question,
            expected_answer="condonation is not permitted below 65 percent",
            relevant_documents=[],
            expect_abstention=False,
        )

        def answered(text):
            return SimpleNamespace(
                retrieved_document_ids=[],
                citations=[],
                support_ratio=1.0,
                abstained=False,
                answer=text,
            )

        self.assertFalse(runner.score_case(answered(question), case)["correct"])
        self.assertTrue(
            runner.score_case(
                answered("Condonation is not permitted below 65 percent. [1]"), case
            )["correct"]
        )

    def test_a_pasted_wall_of_text_is_not_a_correct_answer(self):
        document = " ".join(
            f"Clause {n}.{n} A student shall comply with regulation {n}x on "
            f"matter{n} subject{n} procedure{n} committee{n} record{n} notice{n}."
            for n in range(1, 25)
        )
        wall = document + " Condonation is not permitted below 65 percent."
        self.assertFalse(correctness.is_correct(wall, self.EXPECTED))
        self.assertFalse(correctness.is_correct(wall, "75 percent attendance"))
        # The same words with figure-free expected text: recall alone passes it.
        prose = document + " You may carry the institute identity card."
        self.assertFalse(correctness.is_correct(prose, "the institute identity card"))

    def test_helpful_context_around_the_fact_is_still_correct(self):
        answer = (
            "A student must maintain a minimum of 75 percent attendance in each "
            "course to be eligible for the end-semester examination. Attendance "
            "is computed up to the last instructional day of the semester, and "
            "a student with attendance between 65 and 75 percent may apply for "
            "condonation on medical grounds. [1]"
        )
        self.assertTrue(correctness.is_correct(answer, "75 percent"))

    def test_the_right_number_with_the_wrong_unit_is_not_correct(self):
        for expected, answer in (
            ("30 days", "You have 30 months to file a complaint."),
            ("12 days", "Employees get 12 weeks of casual leave."),
            ("200 rupees", "The printing allowance is 200 pages."),
            ("8 weeks", "An internship must run 8 months."),
        ):
            with self.subTest(answer=answer):
                self.assertFalse(correctness.is_correct(answer, expected))

    def test_the_right_unit_among_several_is_enough(self):
        self.assertTrue(
            correctness.is_correct(
                "You have 30 days, and the committee has 30 days more.", "30 days"
            )
        )
        self.assertTrue(correctness.is_correct("Within 30 working days.", "30 days"))

    def test_am_is_not_pm(self):
        self.assertFalse(correctness.is_correct("Be back by 11 am.", "11 pm"))
        self.assertTrue(correctness.is_correct("Be back by 11 pm.", "11 pm"))

    def test_the_documented_limits_are_real(self):
        """The docstring says a figure is not tied to its entity. If this
        starts failing the docstring is out of date, not the test."""
        self.assertTrue(
            correctness.is_correct("The PhD tuition fee is 82000.", "82000")
        )


class TimingOnlyGateTests(SimpleTestCase):
    """E3: only the timings leave the gate."""

    RUN = {
        "generation": {"groundedness": 0.9},
        "system": {
            "cases": 10,
            "latency_ms_mean": 30.0,
            "latency_ms_p95": 48,
            "duration_ms": 99,
            "degraded_reranks": 0,
            "answerer_fallbacks": 0,
            "general_replies": 0,
        },
    }

    def test_only_timings_are_dropped(self):
        stripped = metrics.without_timings(self.RUN)
        self.assertEqual(
            set(stripped["system"]),
            {"cases", "degraded_reranks", "answerer_fallbacks", "general_replies"},
        )
        self.assertEqual(stripped["generation"], self.RUN["generation"])
        # The input is not modified.
        self.assertIn("latency_ms_mean", self.RUN["system"])

    def test_a_run_without_a_system_block_is_left_alone(self):
        self.assertEqual(
            metrics.without_timings({"generation": {}}), {"generation": {}}
        )
        self.assertEqual(metrics.without_timings({}), {})

    def test_a_fallback_fails_the_gate_without_gate_latency(self):
        worse = json.loads(json.dumps(self.RUN))
        worse["system"]["answerer_fallbacks"] = 32
        worse["system"]["degraded_reranks"] = 5
        worse["system"]["general_replies"] = 9
        worse["system"]["latency_ms_mean"] = 5000.0

        report = metrics.compare(
            metrics.without_timings(self.RUN), metrics.without_timings(worse)
        )

        self.assertEqual(
            sorted(report["regressions"]),
            [
                "system.answerer_fallbacks",
                "system.degraded_reranks",
                "system.general_replies",
            ],
        )

    def test_latency_alone_still_does_not_fail_it(self):
        slower = json.loads(json.dumps(self.RUN))
        slower["system"]["latency_ms_mean"] = 5000.0
        slower["system"]["latency_ms_p95"] = 9000

        report = metrics.compare(
            metrics.without_timings(self.RUN), metrics.without_timings(slower)
        )

        self.assertEqual(report["regressions"], [])


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DOCUMENT_EMBEDDING_PROVIDER=EMBEDDER)
class TimingOnlyGateCommandTests(TestCase):
    """E3 through the real command: a baseline that says nothing fell back, a
    run where something did."""

    def setUp(self):
        chunking_service.reset_provider_cache()
        self.addCleanup(chunking_service.reset_provider_cache)

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.benchmark = Path(self.directory.name) / "benchmark.json"
        self.baseline = Path(self.directory.name) / "baseline.json"

        call_command("seed_evaluation", output=str(self.benchmark), verbosity=0)
        call_command("evaluate", load=str(self.benchmark), verbosity=0)

    def run_evaluation(self, **kwargs):
        options = {
            "dataset": "institutional-seed",
            "dataset_version": "v1",
            "as_user": "ci-evaluator",
            "stdout": StringIO(),
        }
        options.update(kwargs)
        call_command("evaluate", **options)
        return options["stdout"].getvalue()

    def test_a_counter_in_the_system_block_fails_the_default_gate(self):
        self.run_evaluation(write_baseline=str(self.baseline))
        # The baseline claims the previous run needed no fallback and made no
        # general replies. The offline run here records 0 fallbacks too, so a
        # baseline of -1 makes "0" a regression in the lower-is-better sense.
        payload = json.loads(self.baseline.read_text(encoding="utf-8"))
        payload["metrics"]["system"]["answerer_fallbacks"] = -1
        payload["metrics"]["system"]["degraded_reranks"] = -1
        self.baseline.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(CommandError) as caught:
            self.run_evaluation(baseline=str(self.baseline), fail_on_regression=True)

        self.assertIn("system.answerer_fallbacks", str(caught.exception))
        self.assertIn("system.degraded_reranks", str(caught.exception))

    def test_compare_to_has_the_same_rule(self):
        from documents.models import EvaluationRun

        self.run_evaluation(label="earlier")
        earlier = EvaluationRun.objects.get(label="earlier")
        earlier.metrics["system"]["answerer_fallbacks"] = -1
        earlier.metrics["system"]["latency_ms_mean"] = -1
        earlier.save(update_fields=["metrics"])

        with self.assertRaises(CommandError) as caught:
            self.run_evaluation(compare_to=earlier.id, fail_on_regression=True)

        self.assertIn("regressed", str(caught.exception))
        # ...and only the counter is why: latency alone would have passed.
        earlier.metrics["system"]["answerer_fallbacks"] = 0
        earlier.save(update_fields=["metrics"])
        self.run_evaluation(compare_to=earlier.id, fail_on_regression=True)


@override_settings(MEDIA_ROOT=MEDIA_ROOT, DOCUMENT_EMBEDDING_PROVIDER=EMBEDDER)
class ConfigurationCaptureTests(TestCase):
    """E4: every setting that moves a score is in the record."""

    def setUp(self):
        chunking_service.reset_provider_cache()
        rag.reset_caches()
        self.addCleanup(chunking_service.reset_provider_cache)
        self.addCleanup(rag.reset_caches)

    def test_each_setting_that_changes_results_changes_the_record(self):
        before = runner.capture_configuration()

        for name, value in (
            ("RAG_MIN_SUPPORT_RATIO", 0.9),
            ("RAG_ADMIT_RETRIEVAL_TOP", 5),
            ("RAG_ANSWERER_TEMPERATURE", 0.7),
            ("RAG_ANSWERER_CHAIN", "gemini,groq,openrouter"),
            ("RAG_ANSWERER_FALLBACK_MODELS", "gemini-3-flash-preview"),
            ("RAG_ANSWERER_REASONING_EFFORT", "low"),
            ("RAG_ANSWERER_EFFORT", "high"),
            ("LLM_DAILY_LIMITS", "groq=5"),
            ("RAG_PLAN_WHEN_GENERATIVE", True),
            ("RAG_FOLLOWUP_HISTORY_TURNS", 0),
            ("RAG_GROUNDING_RETRY", False),
            ("RAG_MODEL_JUDGES_RELEVANCE", False),
        ):
            with self.subTest(setting=name), override_settings(**{name: value}):
                rag.reset_caches()
                self.assertNotEqual(
                    runner.capture_configuration(),
                    before,
                    f"changing {name} left the recorded configuration unchanged",
                )

    def test_the_support_floor_is_recorded_as_it_is_applied(self):
        """Unset, grounding uses its own constant: the record shows THAT, not
        None beside a score that ran at 0.25."""
        from documents.rag import grounding

        with override_settings(RAG_MIN_SUPPORT_RATIO=None):
            recorded = runner.capture_configuration()["retrieval"]["min_support_ratio"]
        self.assertEqual(recorded, grounding.MIN_SUPPORT_RATIO)

        with override_settings(RAG_MIN_SUPPORT_RATIO=0.4):
            recorded = runner.capture_configuration()["retrieval"]["min_support_ratio"]
        self.assertEqual(recorded, 0.4)

    def test_a_chain_is_recorded_link_by_link(self):
        """The answerer's version names only the first link, so a chain that
        keeps the same first link looked identical."""
        with override_settings(
            RAG_ANSWERER="documents.rag.openai_compat.OpenAICompatibleAnswerer",
            RAG_ANSWERER_PROVIDER="gemini",
            RAG_ANSWERER_CHAIN="gemini,groq",
        ):
            rag.reset_caches()
            both = runner.capture_configuration()["answerer"]
        with override_settings(
            RAG_ANSWERER="documents.rag.openai_compat.OpenAICompatibleAnswerer",
            RAG_ANSWERER_PROVIDER="gemini",
            RAG_ANSWERER_CHAIN="gemini",
        ):
            rag.reset_caches()
            one = runner.capture_configuration()["answerer"]

        self.assertEqual(both["version"], one["version"])
        self.assertNotEqual(both["links"], one["links"])
        self.assertTrue(any(link["link"].startswith("groq:") for link in both["links"]))
        self.assertFalse(any(link["link"].startswith("groq:") for link in one["links"]))

    def test_the_chat_path_records_its_temperature(self):
        """CHAT_GENERAL_TEMPERATURE changes the general replies a chat-path run
        counts. The runner records it beside the other chat-path settings."""
        import inspect

        self.assertIn("general_temperature", inspect.getsource(runner.run_dataset))


class BenchmarkV3Tests(SimpleTestCase):
    """E5: the labels and the coverage claim of benchmark v3."""

    @classmethod
    def load(cls, name):
        return json.loads((EVALUATION_DIR / name).read_text(encoding="utf-8"))

    def test_it_is_still_32_cases_with_5_refusals(self):
        cases = self.load("benchmark-v3.template.json")["cases"]
        self.assertEqual(len(cases), 32)
        self.assertEqual(sum(1 for case in cases if case.get("expect_abstention")), 5)

    def test_the_description_no_longer_promises_what_it_does_not_have(self):
        description = self.load("benchmark-v3.template.json")["description"]
        self.assertNotIn("no shared words", description)
        self.assertNotIn("mostly new", description.lower())
        self.assertIn("overlaps v2", description)

    def test_no_v3_question_is_a_near_copy_of_a_v2_question(self):
        def words(text):
            return set(
                "".join(c if c.isalnum() else " " for c in text.lower()).split()
            ) - {"the", "a", "an", "is", "of", "what", "my", "i", "do", "does", "to"}

        v2 = [
            case["question"]
            for case in self.load("benchmark-v2.template.json")["cases"]
        ]
        for case in self.load("benchmark-v3.template.json")["cases"]:
            question = case["question"]
            for other in v2:
                overlap = len(words(question) & words(other)) / len(
                    words(question) | words(other)
                )
                # v3 case 17 deliberately asks v2's ECE tuition fact with the
                # year in it and v2#44 etc. are paraphrases (<= 0.6); only a
                # near-verbatim copy is refused here.
                self.assertLess(
                    overlap, 0.75, f"{question!r} is a near copy of v2 {other!r}"
                )

    def test_the_two_replaced_questions_ask_for_new_facts(self):
        cases = {
            c["question"]: c for c in self.load("benchmark-v3.template.json")["cases"]
        }
        password = cases["How often do I have to change my account password?"]
        self.assertEqual(password["expected_answer"], "every 180 days")
        tuition = cases["How much does an M.Tech CSE student pay for tuition?"]
        self.assertEqual(tuition["expected_answer"], "95000")

    def test_those_expected_answers_are_in_the_corpus(self):
        corpus = EVALUATION_DIR / "corpus"
        it = (corpus / "it-acceptable-use.txt").read_text(encoding="utf-8")
        fees = (corpus / "fee-structure.txt").read_text(encoding="utf-8")
        self.assertIn("changed every 180 days", it)
        self.assertIn("M.Tech CSE\t95000", fees)

    def test_the_placement_label_says_registration_is_required_before_15_august(self):
        placement = next(
            case
            for case in self.load("benchmark-v3.template.json")["cases"]
            if case["question"].startswith("Is it mandatory to register")
        )
        expected = placement["expected_answer"].lower()
        self.assertIn("must register", expected)
        self.assertIn("before 15 august of the final year", expected)
        self.assertIn("not automatic", expected)

    def test_the_fee_deadline_label_keeps_the_word_before(self):
        fees = next(
            case
            for case in self.load("benchmark-v3.template.json")["cases"]
            if case["question"].startswith("When must fees be paid by")
        )
        self.assertIn("before the last working day of July", fees["expected_answer"])

    def test_every_expected_figure_of_v3_is_in_the_corpus(self):
        """Each answerable label's figures appear in the document it names."""
        corpus_text = " ".join(
            path.read_text(encoding="utf-8")
            for path in (EVALUATION_DIR / "corpus").glob("*.txt")
        )
        for case in self.load("benchmark-v3.template.json")["cases"]:
            if case.get("expect_abstention"):
                continue
            for figure in correctness.expected_facts(case["expected_answer"]):
                # Plain numbers; dates, clock times, year spans and the digit
                # behind a number word are spelled differently in the text.
                if not re.fullmatch(r"\d{2,}(?:\.\d+)?", figure):
                    continue
                with self.subTest(question=case["question"], figure=figure):
                    self.assertIn(figure, corpus_text.replace(",", ""))
