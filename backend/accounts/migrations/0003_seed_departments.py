"""Register every department name already in use.

Before the registry existed, a department was whatever string had been
typed onto a profile, a document, a conversation or an evaluation dataset.
Each distinct non-empty value becomes a registry row, so nothing that was
reachable before this migration becomes unassignable after it.

Near-duplicates ("Physics" / "physics ") are registered once - under a
spelling that is already trimmed if there is one, and always trimmed - and
the records themselves are NOT rewritten: merging two strings that were
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
            if not key:
                continue
            # Registered TRIMMED. resolve_department strips what it is given
            # and matches case-insensitively, which on PostgreSQL does not
            # ignore spaces - so a row stored as "physics " could never be
            # resolved, and the clean spelling got no row of its own. A
            # spelling that is already clean wins over a padded one.
            if key not in seen or (seen[key] != seen[key].strip() and name == name.strip()):
                seen[key] = name

    existing = {
        name.strip().lower() for name in Department.objects.values_list("name", flat=True)
    }
    Department.objects.bulk_create(
        [
            Department(name=name.strip())
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
