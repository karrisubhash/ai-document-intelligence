from django.db import migrations, models
from django.db.models import F, OuterRef, Subquery


def snapshot_existing_rows(apps, schema_editor):
    """Give every existing row the snapshot new rows get when written.

    Rows whose document was already deleted have nothing left to copy from;
    their subject is gone, which is the defect this migration stops.
    """
    AuditLog = apps.get_model("documents", "AuditLog")
    Document = apps.get_model("documents", "Document")
    AuditLog.objects.filter(document__isnull=False).update(
        document_ref=F("document_id"),
        document_department=Subquery(
            Document.objects.filter(pk=OuterRef("document_id")).values("department")[:1]
        ),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0016_archival_audit_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditlog",
            name="document_ref",
            field=models.PositiveBigIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="auditlog",
            name="document_department",
            field=models.CharField(
                blank=True, default="", editable=False, max_length=255
            ),
        ),
        migrations.RunPython(snapshot_existing_rows, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(
                fields=["document_ref", "-created_at"], name="auditlog_document_idx"
            ),
        ),
    ]
