from django.db import migrations, models


class Migration(migrations.Migration):
    """A database default for AuditLog.document_department.

    0017 added the column NOT NULL with a Python-side default only, so a
    writer that predates it - the previous release, still serving while
    the new one starts - could not insert an audit row at all.
    """

    dependencies = [
        ("documents", "0017_auditlog_document_snapshot"),
    ]

    operations = [
        migrations.AlterField(
            model_name="auditlog",
            name="document_department",
            field=models.CharField(
                blank=True, db_default="", default="", editable=False, max_length=255
            ),
        ),
    ]
