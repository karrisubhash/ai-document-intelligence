"""One walk of the provider chain has a time budget.

Found in a real-model run: a question for which a provider hung took 61 seconds
to come back as "could not produce an answer". The timeout is per attempt, so a
chain of hung providers plus the SDK's retries on the last link outlasts the
proxy in front of the app (nginx stops waiting at 120 s) and the reader gets a
504 instead of the quoted fallback. RAG_ANSWERER_DEADLINE gives the whole walk a
budget; each attempt is given what is left of it.

A fake clock is advanced by the clients themselves: a client that "hangs" waits
for exactly the timeout it was handed, as a real one would.
"""

from types import SimpleNamespace
from unittest import mock

from django.test import override_settings

from documents.rag import gateway as gateway_module
from documents.rag.gateway import MIN_ATTEMPT_SECONDS
from documents.tests_llm_gateway import GatewayTestCase, ModelClient, ask, build, wire

CHAIN = "gemini,groq,openrouter"
SETTINGS = {
    "RAG_ANSWERER_DEADLINE": 50,
    "RAG_ANSWERER_TIMEOUT": 60,
    "RAG_ANSWERER_RETRIES": 2,
}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Provider(ModelClient):
    """A provider that answers, takes `seconds` to do it, or hangs until the
    timeout it was given runs out. Records the options each attempt was made
    with (the SDK's retries and timeout)."""

    def __init__(self, clock, *, hangs=False, seconds=0.0, text="Answer [1]."):
        super().__init__(text=text)
        self.clock = clock
        self.hangs = hangs
        self.seconds = seconds
        self.options = []

    def with_options(self, **options):
        self.options.append(options)
        return self

    def _create(self, **kwargs):
        timeout = self.options[-1].get("timeout") if self.options else None
        if self.hangs:
            self.calls.append(kwargs)
            self.clock.advance(timeout or 60.0)
            raise TimeoutError("timed out")
        if timeout is not None and self.seconds > timeout:
            # Silent for longer than the timeout it was handed: the SDK gives up.
            self.calls.append(kwargs)
            self.clock.advance(timeout)
            raise TimeoutError("timed out")
        self.clock.advance(self.seconds)
        return super()._create(**kwargs)


class DeadlineTests(GatewayTestCase):
    def chain(self, chain=CHAIN, **settings):
        self.clock = Clock()
        patcher = mock.patch.object(
            gateway_module, "time", SimpleNamespace(monotonic=self.clock)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return build(chain, **{**SETTINGS, **settings})

    def providers(self, gateway, **hanging):
        return {
            name: wire(
                gateway, name, Provider(self.clock, hangs=hanging.get(name, False))
            )
            for name in ("gemini", "groq", "openrouter")
            if any(link.provider == name for link in gateway.links)
        }

    def test_hung_providers_cannot_outlast_the_budget(self):
        gateway = self.chain()
        clients = self.providers(gateway, gemini=True, groq=True, openrouter=True)
        start = self.clock.now

        with self.assertRaises(gateway_module.Unavailable) as failed:
            ask(gateway)

        self.assertIsInstance(failed.exception.__cause__, TimeoutError)
        # Each attempt was given what was left, half of it while another provider
        # could still be tried: 25 s, 12.5 s, then the rest on the last link.
        self.assertEqual(
            clients["gemini"].options[0], {"max_retries": 0, "timeout": 25.0}
        )
        self.assertEqual(
            clients["groq"].options[0], {"max_retries": 0, "timeout": 12.5}
        )
        self.assertEqual(
            clients["openrouter"].options[0], {"max_retries": 0, "timeout": 12.5}
        )
        self.assertEqual([len(c.calls) for c in clients.values()], [1, 1, 1])
        self.assertLessEqual(self.clock.now - start, 50.0)

    def test_no_new_provider_is_started_when_too_little_is_left(self):
        gateway = self.chain(RAG_ANSWERER_DEADLINE=30)
        clients = self.providers(gateway, gemini=True, groq=True)
        clients["openrouter"].hangs = False

        with self.assertLogs("adi.llm", level="WARNING") as logged:
            with self.assertRaises(gateway_module.Unavailable) as failed:
                ask(gateway)

        # 15 s on Gemini, 8 s (the floor) on Groq, 7 s left: OpenRouter is never
        # asked, and the walk says why. The failure that stands is Groq's.
        self.assertIsInstance(failed.exception.__cause__, TimeoutError)
        self.assertEqual(len(clients["openrouter"].calls), 0)
        self.assertTrue(
            any("llm.deadline" in line for line in logged.output), logged.output
        )
        self.assertLess(30.0 - (self.clock.now - 1000.0), MIN_ATTEMPT_SECONDS)

    def test_the_first_provider_is_always_tried(self):
        gateway = self.chain(RAG_ANSWERER_DEADLINE=1)
        clients = self.providers(gateway)

        ask(gateway)

        self.assertEqual(len(clients["gemini"].calls), 1)
        self.assertEqual(clients["gemini"].options[0]["timeout"], MIN_ATTEMPT_SECONDS)

    def test_a_slow_answer_is_not_cut_off_when_no_other_provider_could_answer(self):
        gateway = self.chain("gemini")
        clients = {
            "gemini": wire(gateway, "gemini", Provider(self.clock, seconds=40.0))
        }

        completion = ask(gateway)

        self.assertIn("Answer", completion.text)
        self.assertEqual(len(clients["gemini"].calls), 1)

    def test_a_request_silent_past_its_half_is_treated_as_hung_if_another_remains(
        self,
    ):
        """What the half-share costs, stated rather than hoped away: a non-streamed
        request cannot be told from a hung one before it answers, so with another
        provider to try it gets half the budget (25 s) and a 40 s answer is cut
        off. The alternative, the whole budget for the first link, lets one hung
        provider starve every healthy one behind it."""
        gateway = self.chain()
        clients = {
            "gemini": wire(gateway, "gemini", Provider(self.clock, seconds=40.0)),
            "groq": wire(gateway, "groq", Provider(self.clock)),
            "openrouter": wire(gateway, "openrouter", Provider(self.clock)),
        }

        completion = ask(gateway)

        self.assertIn("Answer", completion.text)
        self.assertEqual(len(clients["gemini"].calls), 1)
        self.assertEqual(len(clients["groq"].calls), 1)
        self.assertEqual(completion.provider, "groq")

    def test_a_provider_with_no_rival_is_given_the_whole_budget(self):
        """A hang marks its endpoint dead, so its other models would be skipped
        anyway: splitting the budget among them would only shorten it."""
        gateway = self.chain("gemini")
        clients = self.providers(gateway, gemini=True)

        with self.assertRaises(gateway_module.Unavailable) as failed:
            ask(gateway)

        self.assertIsInstance(failed.exception.__cause__, TimeoutError)
        self.assertEqual(
            clients["gemini"].options[0], {"max_retries": 0, "timeout": 50.0}
        )
        self.assertEqual(len(clients["gemini"].calls), 1)

    def test_the_sdk_does_not_retry_under_a_deadline(self):
        """Between its retries the SDK sleeps for the provider's Retry-After (up
        to two minutes), which no arithmetic on the request time can count: a 429
        on the last link ran 60 s past a 50 s budget when the timeout was lowered.
        The chain is the retry."""
        for deadline, timeout, retries in (
            (100, 20, 2),
            (50, 20, 2),
            (30, 20, 2),
            (50, 10, 1),
            (50, 20, 0),
        ):
            with self.subTest(deadline=deadline, timeout=timeout, retries=retries):
                gateway = self.chain(
                    "groq",
                    RAG_ANSWERER_DEADLINE=deadline,
                    RAG_ANSWERER_TIMEOUT=timeout,
                    RAG_ANSWERER_RETRIES=retries,
                )
                clients = self.providers(gateway)

                ask(gateway)

                options = clients["groq"].options[0]
                self.assertEqual(options["max_retries"], 0)
                self.assertEqual(options["timeout"], float(min(timeout, deadline)))

    def test_without_a_deadline_nothing_changes(self):
        gateway = self.chain(RAG_ANSWERER_DEADLINE=0)
        clients = self.providers(gateway, gemini=True, groq=True)

        completion = ask(gateway)

        # No timeout is passed on; links before the last get no SDK retries (as
        # before), and the last is left exactly as the client was built.
        self.assertEqual(clients["gemini"].options, [{"max_retries": 0}])
        self.assertEqual(clients["groq"].options, [{"max_retries": 0}])
        self.assertEqual(clients["openrouter"].options, [])
        self.assertIn("Answer", completion.text)
        self.assertEqual(
            getattr(gateway, "_deadline", None), 0.0, "a deadline of 0 means off"
        )

    def test_the_default_is_a_budget_well_inside_the_proxy_limit(self):
        from django.conf import settings

        self.assertGreater(settings.RAG_ANSWERER_DEADLINE, 0)
        # A question can take two walks (a corrective retry, or the general reply
        # after a "not found"), and retrieval, reranking and embedding come before
        # the first: two walks leave at least 30 s of nginx's 120.
        self.assertLessEqual(2 * settings.RAG_ANSWERER_DEADLINE, 90)

    def test_a_hung_provider_is_rested_so_the_next_question_goes_elsewhere_first(
        self,
    ):
        """A timeout left no trace beyond its walk, so while a provider was hung
        EVERY question paid its wait before moving on."""
        gateway = self.chain("gemini,groq")
        clients = self.providers(gateway, gemini=True)

        first = ask(gateway)
        before = self.clock.now
        second = ask(gateway)

        self.assertEqual(first.provider, "groq")
        self.assertEqual(second.provider, "groq")
        self.assertEqual(len(clients["gemini"].calls), 1, "tried again, still hung")
        self.assertEqual(
            self.clock.now, before, "the second question waited for nobody"
        )
        order = [link.provider for link in gateway.plan(gateway_module.CHAT).links]
        self.assertEqual(order[0], "groq")
        self.assertEqual(order[-1], "gemini")

    def test_every_model_of_a_hung_endpoint_is_rested(self):
        gateway = self.chain("gemini,groq")
        self.providers(gateway, gemini=True)

        ask(gateway)

        order = [link.provider for link in gateway.plan(gateway_module.CHAT).links]
        self.assertEqual(order, ["groq"] + ["gemini"] * (len(order) - 1))

    def test_a_timeout_is_a_timeout(self):
        from documents.rag import budget

        class ReadTimeout(Exception):
            pass

        for exc in (TimeoutError("x"), ReadTimeout("x")):
            self.assertTrue(budget.is_timeout(exc), exc)
            self.assertEqual(
                budget._cooldown_seconds(exc), budget.OVERLOAD_COOLDOWN_SECONDS
            )
        for exc in (ValueError("x"), ConnectionError("x"), KeyError("x")):
            self.assertFalse(budget.is_timeout(exc), exc)

    def test_a_deadline_that_is_not_a_number_of_seconds_is_not_one(self):
        """inf crashed every walk (int of NaN) after reserving a request; a negative
        number or NaN quietly meant "8 seconds"."""
        for value in (float("inf"), float("-inf"), float("nan"), -5, "abc", [1]):
            with self.subTest(value=value):
                with self.assertLogs("adi.llm", level="ERROR"):
                    self.assertEqual(gateway_module._walk_budget(value), 0.0)
        self.assertEqual(gateway_module._walk_budget(0), 0.0)
        self.assertEqual(gateway_module._walk_budget(None), 0.0)
        self.assertEqual(gateway_module._walk_budget(40), 40.0)

    def test_a_deadline_too_short_for_a_second_provider_says_so(self):
        with self.assertLogs("adi.llm", level="WARNING") as logged:
            self.assertEqual(gateway_module._walk_budget(10), 10.0)
        self.assertTrue(any("llm.deadline_short" in line for line in logged.output))

    def test_an_infinite_deadline_does_not_break_a_walk(self):
        with self.assertLogs("adi.llm", level="ERROR"):
            gateway = self.chain(RAG_ANSWERER_DEADLINE=float("inf"))
        clients = self.providers(gateway)

        completion = ask(gateway)

        self.assertIn("Answer", completion.text)
        self.assertEqual(len(clients["gemini"].calls), 1)

    @override_settings(RAG_ANSWERER_DEADLINE=50)
    def test_a_stub_without_with_options_passes_through(self):
        """Tests and the Claude path drive the chain with stubs that have no
        with_options; the budget must not break them."""
        gateway = self.chain()
        stub = wire(gateway, "gemini", ModelClient())

        completion = ask(gateway)

        self.assertIn("Answer", completion.text)
        self.assertEqual(len(stub.calls), 1)
