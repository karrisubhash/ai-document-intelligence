"""Does an answer contain what the benchmark says the answer is?

The metrics this project already reported - groundedness, citation accuracy,
token overlap - say whether an answer was SUPPORTED, not whether it was RIGHT:
a fluent answer built from the right vocabulary scores well on overlap, and a
grounded answer to the wrong question scores well on groundedness. Without a
number for "right", an improvement to answers could not be told from a change
that merely made them longer.

This is a heuristic and says so. An answer is correct when it contains the key
facts of the expected answer:

- every FIGURE the expected answer states (a number, an amount, a date, a
  year range), in any spelling the grounding check treats as the same fact:
  "75 percent" is "75%", "four books" is "4 books", "31 March 2025" is
  "31.03.2025";
- and, when the expected answer states no figure at all, at least
  CONTENT_RECALL of its content words, since there is nothing exact to look
  for.

What it does not do: it does not penalise an answer for ALSO saying
something wrong, and it cannot read a negation ("the fee is NOT 5000"). A
human or a model reading the answers is the only real judge; this exists so a
change can be measured across dozens of questions without either.
"""

from documents.rag import facts as fact_forms
from documents.rag import grounding

# Of the expected answer's content words, the share an answer must contain when
# the expected answer has no figure to look for. Lenient on purpose: a
# paraphrase is a correct answer.
CONTENT_RECALL = 0.6


def expected_facts(expected: str) -> set:
    """The figures an answer must contain, as grounding compares them."""
    prepared = fact_forms.prepare(expected)
    stated = grounding._answer_facts(prepared)
    # "four books" states 4, which grounding only reads on the passage side.
    return stated | fact_forms.number_words(prepared)


def is_correct(answer: str, expected: str) -> bool:
    """Whether `answer` contains the key facts of `expected`."""
    if not (answer or "").strip() or not (expected or "").strip():
        return False

    required = expected_facts(expected)
    if required:
        present = grounding._support_facts(answer)
        cores = {grounding._numeric_core(fact) for fact in present} - {None}
        return all(
            fact in present or grounding._numeric_core(fact) in cores
            for fact in required
        )

    wanted = set(grounding._prose_terms(expected))
    if not wanted:
        return False
    found = set(grounding._prose_terms(answer))
    return len(wanted & found) / len(wanted) >= CONTENT_RECALL
