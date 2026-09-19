"""Loading evaluation datasets from JSON (spec section 19.1).

    {
      "question": "...",
      "expected_answer": "...",
      "relevant_documents": ["doc_123"],
      "relevant_pages": [17]
    }

One addition to that shape: `expect_abstention`. A benchmark made only of
answerable questions cannot tell a well-calibrated system from one that
never abstains, so the negative cases have to be expressible.
"""

import json
from pathlib import Path

from django.db import transaction

from documents.models import EvaluationCase, EvaluationDataset


class DatasetError(Exception):
    """The dataset file is malformed.

    Raised at load time rather than discovered mid-run: a benchmark that
    half-loads produces a score over an unknown subset of questions, which
    is worse than no score.
    """


def _document_ids(raw, where):
    """Accept "doc_123", "123" or 123 - the spec writes ids as strings."""
    ids = []
    for value in raw or []:
        text = str(value)
        digits = text.rsplit("_", 1)[-1]
        if not digits.isdigit():
            raise DatasetError(f"{where}: {value!r} is not a document id")
        ids.append(int(digits))
    return ids


def load_cases(data, dataset, *, source="<dict>"):
    cases = data.get("cases", data if isinstance(data, list) else None)
    if not isinstance(cases, list) or not cases:
        raise DatasetError(f"{source}: no 'cases' list")

    built = []
    for index, raw in enumerate(cases):
        where = f"{source}[{index}]"
        question = (raw.get("question") or "").strip()
        if not question:
            raise DatasetError(f"{where}: case has no question")

        expect_abstention = bool(raw.get("expect_abstention", False))
        relevant = _document_ids(raw.get("relevant_documents"), where)

        if expect_abstention and relevant:
            raise DatasetError(
                f"{where}: a case cannot both expect an abstention and name "
                f"relevant documents - one of the two is wrong, and guessing "
                f"which would silently score the wrong thing"
            )
        if not expect_abstention and not relevant:
            raise DatasetError(
                f"{where}: an answerable case needs relevant_documents, or "
                f"retrieval cannot be scored for it"
            )

        built.append(
            EvaluationCase(
                dataset=dataset,
                question=question,
                expected_answer=raw.get("expected_answer", "") or "",
                relevant_documents=relevant,
                relevant_pages=[int(p) for p in raw.get("relevant_pages", [])],
                expect_abstention=expect_abstention,
                notes=raw.get("notes", "") or "",
            )
        )
    return built


@transaction.atomic
def load_dataset(
    data, *, name=None, version=None, department="", user=None, source="<dict>"
):
    name = name or data.get("name")
    version = version or data.get("version")
    if not name or not version:
        raise DatasetError(f"{source}: dataset needs a name and a version")

    if EvaluationDataset.objects.filter(name=name, version=version).exists():
        raise DatasetError(
            f"{name} {version} already exists. Datasets are immutable once "
            f"loaded - publish a new version rather than editing questions, "
            f"or last month's score and this month's describe different exams."
        )

    dataset = EvaluationDataset.objects.create(
        name=name,
        version=version,
        description=data.get("description", "") or "",
        department=department or data.get("department", "") or "",
        created_by=user,
    )
    EvaluationCase.objects.bulk_create(load_cases(data, dataset, source=source))
    return dataset


def load_file(path, **kwargs):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DatasetError(f"No dataset at {path}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{path}: not valid JSON: {exc}") from exc

    return load_dataset(data, source=str(path), **kwargs)
