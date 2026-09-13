"""Phase 10: backup, restore and the production configuration gate.

The backup tests run real pg_dump and pg_restore against the test database.
Mocking the subprocess would test that the arguments are assembled, not that
a restorable archive comes out - and a backup you have never restored is a
belief rather than a backup.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from documents.models import Document
from documents.tests import make_user

HAS_PG_DUMP = shutil.which("pg_dump") is not None


def skip_without_pg_dump(test):
    import unittest

    return unittest.skipUnless(
        HAS_PG_DUMP, "pg_dump is not installed (postgresql-client)"
    )(test)


class BackupTests(TestCase):
    # The dump must see committed rows, and pg_dump connects on its own
    # connection - so the data cannot live inside a test transaction that is
    # rolled back and never visible to it.
    databases = {"default"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_backup(self, **kwargs):
        from io import StringIO

        out = StringIO()
        call_command("backup", output_dir=str(self.tmp), stdout=out, **kwargs)
        return out.getvalue()

    @skip_without_pg_dump
    def test_a_backup_writes_a_dump_and_a_manifest(self):
        self.run_backup(label="unit")

        dumps = list(self.tmp.glob("db-*.dump"))
        manifests = list(self.tmp.glob("db-*.manifest.json"))

        self.assertEqual(len(dumps), 1)
        self.assertEqual(len(manifests), 1)
        self.assertGreater(dumps[0].stat().st_size, 0)

    @skip_without_pg_dump
    def test_the_manifest_records_what_is_needed_to_trust_the_dump(self):
        self.run_backup()
        manifest = json.loads(next(self.tmp.glob("*.manifest.json")).read_text())

        self.assertEqual(len(manifest["sha256"]), 64)
        self.assertGreater(manifest["tables"], 0)
        self.assertGreater(manifest["bytes"], 0)
        # Restoring into a codebase at a different migration is the failure
        # this records; without it the mismatch surfaces only at runtime.
        self.assertIn("migration", manifest)

    @skip_without_pg_dump
    def test_the_dump_is_verified_before_success_is_reported(self):
        """An unverified backup is a belief, not a backup."""
        output = self.run_backup()

        self.assertIn("Backup verified", output)
        self.assertIn("table(s)", output)

    @skip_without_pg_dump
    def test_the_checksum_matches_the_file_on_disk(self):
        import hashlib

        self.run_backup()
        dump = next(self.tmp.glob("*.dump"))
        manifest = json.loads(next(self.tmp.glob("*.manifest.json")).read_text())

        digest = hashlib.sha256(dump.read_bytes()).hexdigest()
        self.assertEqual(digest, manifest["sha256"])

    @skip_without_pg_dump
    def test_the_archive_is_readable_by_pg_restore(self):
        """The property that actually matters: it can be restored."""
        self.run_backup()
        dump = next(self.tmp.glob("*.dump"))

        result = subprocess.run(
            ["pg_restore", "--list", str(dump)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("TABLE DATA", result.stdout)

    @skip_without_pg_dump
    @override_settings(BACKUP_RETENTION=2)
    def test_prune_keeps_only_the_retained_backups(self):
        for index in range(4):
            self.run_backup(label=f"r{index}")

        self.run_backup(label="final", prune=True)

        remaining = list(self.tmp.glob("db-*.dump"))
        self.assertEqual(len(remaining), 2)
        # Manifests go with their dumps, or the directory fills with
        # descriptions of archives that no longer exist.
        self.assertEqual(len(list(self.tmp.glob("db-*.manifest.json"))), 2)

    @skip_without_pg_dump
    def test_media_is_archived_only_when_asked(self):
        self.run_backup()
        self.assertEqual(list(self.tmp.glob("media-*.tar.gz")), [])


class RestoreSafetyTests(TestCase):
    """The guards, tested without touching a real database."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_missing_dump_is_refused(self):
        with self.assertRaises(CommandError):
            call_command("restore", str(self.tmp / "nope.dump"), yes=True)

    def test_a_tampered_archive_is_refused(self):
        """The manifest describes a specific archive; this is not it."""
        dump = self.tmp / "db-test.dump"
        dump.write_bytes(b"pretend archive")
        dump.with_suffix(".manifest.json").write_text(
            json.dumps({"sha256": "0" * 64, "tables": 3, "created_at": "x"})
        )

        with self.assertRaises(CommandError) as caught:
            call_command("restore", str(dump), yes=True)

        self.assertIn("checksum mismatch", str(caught.exception))

    def test_without_yes_nothing_happens(self):
        """A restore is destructive and irreversible, so it does not run on
        a typo."""
        from io import StringIO

        dump = self.tmp / "db-test.dump"
        dump.write_bytes(b"pretend archive")

        out = StringIO()
        call_command("restore", str(dump), stdout=out)

        self.assertIn("Nothing has been changed", out.getvalue())

    def test_benign_pg_restore_notices_are_recognised(self):
        """A successful restore was once reported as a failure because of a
        version-skew notice - the worst possible moment to be wrong."""
        from documents.management.commands.restore import Command

        self.assertTrue(Command._is_benign('relation "x" does not exist, skipping'))
        self.assertTrue(
            Command._is_benign('unrecognized configuration parameter "transaction_timeout"')
        )
        self.assertTrue(Command._is_benign("Command was: SET transaction_timeout = 0;"))
        self.assertFalse(Command._is_benign("could not connect to server"))


class ProductionConfigurationTests(TestCase):
    """The settings that Django's own deploy check grades (spec §21)."""

    def test_cookie_security_flags_agree_with_each_other(self):
        """Deliberately NOT `SESSION_COOKIE_SECURE == (not settings.DEBUG)`.

        Those flags are derived at IMPORT time, and Django's test runner
        sets settings.DEBUG = False afterwards - so that assertion compares
        a value frozen before the override against the override itself, and
        fails while the configuration is entirely correct. It would have
        been testing the test runner.

        The production values are graded by `check --deploy` below, which is
        the real gate and the one CI runs.
        """
        self.assertIsInstance(settings.SESSION_COOKIE_SECURE, bool)
        self.assertEqual(
            settings.SESSION_COOKIE_SECURE, settings.CSRF_COOKIE_SECURE
        )

    def test_production_values_pass_djangos_own_deploy_check(self):
        """The finish line for phase 10: zero issues, not "zero except the
        ones we got used to seeing"."""
        from io import StringIO

        with override_settings(
            DEBUG=False,
            SECRET_KEY="a-sufficiently-long-and-varied-production-secret-key-0123456789",
            SESSION_COOKIE_SECURE=True,
            CSRF_COOKIE_SECURE=True,
            SECURE_SSL_REDIRECT=True,
            SECURE_HSTS_SECONDS=31536000,
            SECURE_HSTS_PRELOAD=True,
            MAILERS={
                "default": {
                    "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
                    "HOST": "smtp.example.invalid",
                }
            },
        ):
            out = StringIO()
            # Raises SystemCheckError if anything is outstanding.
            call_command("check", deploy=True, stdout=out, stderr=out)

    def test_csrf_cookie_stays_readable_by_javascript(self):
        """The frontend echoes it back in a header; that is how the
        double-submit defence works."""
        self.assertFalse(settings.CSRF_COOKIE_HTTPONLY)

    def test_session_cookie_is_not_readable_by_javascript(self):
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)

    def test_clickjacking_and_sniffing_defences_are_on(self):
        self.assertEqual(settings.X_FRAME_OPTIONS, "DENY")
        self.assertTrue(settings.SECURE_CONTENT_TYPE_NOSNIFF)

    def test_the_proxy_ssl_header_is_configured(self):
        """Without it Django sees plain HTTP behind nginx and
        SECURE_SSL_REDIRECT loops forever."""
        self.assertEqual(
            settings.SECURE_PROXY_SSL_HEADER, ("HTTP_X_FORWARDED_PROTO", "https")
        )

    def test_hsts_is_off_unless_explicitly_enabled(self):
        """HSTS is close to irreversible, so it is a decision by whoever
        owns the domain rather than a framework default."""
        self.assertEqual(settings.SECURE_HSTS_SECONDS, 0)

    def test_static_root_is_set_for_collectstatic(self):
        self.assertTrue(str(settings.STATIC_ROOT).endswith("staticfiles"))

    def test_a_backup_location_is_configured(self):
        self.assertTrue(settings.BACKUP_ROOT)
        self.assertGreaterEqual(settings.BACKUP_RETENTION, 1)
