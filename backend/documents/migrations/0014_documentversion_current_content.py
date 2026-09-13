import django.db.models.deletion
from django.db import migrations, models


def backfill_current_content(apps, schema_editor):
    """Point every existing version at its newest extraction pass.

    Without this the field ships as NULL everywhere, and because retrieval
    filters on it, every chunk already in the database would silently drop
    out of search - a migration that empties the index while reporting
    success. The newest pass is the right choice because it is what a
    reprocess was run to produce.

    DocumentContent.Meta.ordering is ["-created_at"], but ordering is not
    relied on here: migrations run against historical models and an
    explicit order_by is cheaper to verify than a Meta option that could
    change later.
    """
    DocumentVersion = apps.get_model("documents", "DocumentVersion")
    DocumentContent = apps.get_model("documents", "DocumentContent")

    updated = []
    for version in DocumentVersion.objects.all().only("id"):
        newest = (
            DocumentContent.objects.filter(document_version_id=version.id)
            .order_by("-created_at", "-id")
            .first()
        )
        if newest is not None:
            version.current_content_id = newest.id
            updated.append(version)

    if updated:
        DocumentVersion.objects.bulk_update(updated, ["current_content"], batch_size=500)


def clear_current_content(apps, schema_editor):
    """Reverse is a no-op beyond the column drop that follows it."""
    DocumentVersion = apps.get_model("documents", "DocumentVersion")
    DocumentVersion.objects.update(current_content=None)


class Migration(migrations.Migration):

    dependencies = [
        ('documents', '0013_documentfigure'),
    ]

    operations = [
        migrations.AddField(
            model_name='documentversion',
            name='current_content',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='documents.documentcontent'),
        ),
        migrations.RunPython(backfill_current_content, clear_current_content),
    ]
