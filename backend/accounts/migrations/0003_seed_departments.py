"""Register every department name already in use.

Before the registry existed, a department was whatever string had been
typed onto a profile, a document, a conversation or an evaluation dataset.
Each distinct non-empty value becomes a registry row, so nothing that was
reachable before this migration becomes unassignable after it.

Near-duplicates ("Physics" / "physics ") are registered once, under the
first spelling seen, and are NOT rewritten: merging two strings that were
two separate tenants would silently widen access between them, and that is
a decision for an administrator, not for a migration.
"""

from django.db import migrations


def seed(apps, schema_editor):
    Department = apps.get_model("accounts", "Department")
    UserProfile = apps.get_model("accounts", "UserProfile")
    Document = apps.get_model("documents", "Document")
    Conversation = apps.get_model("documents", "Conversation")
    EvaluationDataset = apps.get_model("documents", "EvaluationDataset")

    seen = {}
    for model in (UserProfile, Document, Conversation, EvaluationDataset):
        for name in (
            model.objects.exclude(department="")
            .values_list("department", flat=True)
            .distinct()
        ):
            key = name.strip().lower()
            if key and key not in seen:
                seen[key] = name

    existing = {
        name.lower() for name in Department.objects.values_list("name", flat=True)
    }
    Department.objects.bulk_create(
        [
            Department(name=name)
            for key, name in sorted(seen.items())
            if key not in existing
        ]
    )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_department_registry"),
        ("documents", "0016_archival_audit_indexes"),
    ]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
