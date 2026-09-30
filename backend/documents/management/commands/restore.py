"""Restore a database backup (spec §21.3).

A restore is destructive and irreversible, so this command refuses to run
without an explicit `--yes` and prints exactly what it is about to overwrite
first. The checksum in the manifest is verified before anything is dropped:
discovering a corrupt archive halfway through a restore leaves you with
neither the old database nor the new one.
"""

import hashlib
import json
import os
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Restore the database from a pg_dump archive written by `backup`."

    def add_arguments(self, parser):
        parser.add_argument("dump", help="Path to the .dump file.")
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm. Without it the command reports and exits.",
        )
        parser.add_argument(
            "--skip-checksum",
            action="store_true",
            help="Restore even if the manifest checksum does not match.",
        )

    def handle(self, *args, **options):
        dump_path = Path(options["dump"])
        if not dump_path.is_file():
            raise CommandError(f"No dump at {dump_path}")

        database = settings.DATABASES["default"]
        manifest = self._manifest(dump_path)

        self.stdout.write("About to restore:")
        self.stdout.write(f"  from    : {dump_path}")
        self.stdout.write(f"  into    : {database['NAME']} on {database['HOST']}")
        if manifest:
            self.stdout.write(f"  taken   : {manifest.get('created_at')}")
            self.stdout.write(f"  tables  : {manifest.get('tables')}")
            self.stdout.write(f"  migration: {manifest.get('migration')}")

        if manifest and not options["skip_checksum"]:
            self._check_checksum(dump_path, manifest)

        if not options["yes"]:
            self.stdout.write(
                self.style.WARNING(
                    "\nThis REPLACES every table listed above. Nothing has been "
                    "changed. Re-run with --yes to proceed."
                )
            )
            return

        self.stdout.write("Restoring...")
        self._pg_restore(database, dump_path)

        self.stdout.write(
            self.style.SUCCESS(f"Restored {database['NAME']} from {dump_path.name}")
        )
        if manifest:
            self._report_schema_gap(manifest)

    def _report_schema_gap(self, manifest: dict):
        """Say which apps the code is ahead of the restored schema in.

        Compared against the migration graph on disk, not assumed: a restore
        into a newer codebase is routine (the backup predates a deploy), and
        the operator needs to know whether `migrate` has work to do.
        """
        recorded = manifest.get("migrations") or {}
        if not recorded:
            if manifest.get("migration"):
                self.stdout.write(
                    "Older manifest without per-app migrations; run "
                    "`manage.py migrate` to bring the schema up to this code."
                )
            return

        from django.db.migrations.loader import MigrationLoader

        leaves = dict(
            MigrationLoader(None, ignore_no_migrations=True).graph.leaf_nodes()
        )
        behind = {
            app: (recorded.get(app, "(none)"), code)
            for app, code in sorted(leaves.items())
            if recorded.get(app, "") < code
        }
        if not behind:
            self.stdout.write("  schema matches this codebase's migrations")
            return
        self.stdout.write(
            self.style.WARNING("The code is ahead of the restored schema. Run migrate:")
        )
        for app, (dump, code) in behind.items():
            self.stdout.write(f"    {app}: restored at {dump}, code at {code}")

    @staticmethod
    def _manifest(dump_path: Path):
        path = dump_path.with_suffix(".manifest.json")
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _check_checksum(self, dump_path: Path, manifest: dict):
        expected = manifest.get("sha256")
        if not expected:
            return

        digest = hashlib.sha256()
        with dump_path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)

        if digest.hexdigest() != expected:
            raise CommandError(
                "checksum mismatch: this archive is not the one the manifest "
                "describes. Restoring it could leave the database in a state "
                "nobody has a record of. Use --skip-checksum only if you know "
                "why it differs."
            )

        self.stdout.write("  checksum: OK")

    def _pg_restore(self, database, dump_path: Path):
        command = [
            "pg_restore",
            # --clean --if-exists drops each object before recreating it, so
            # a restore over a live database converges rather than colliding.
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            f"--host={database['HOST']}",
            f"--port={database['PORT']}",
            f"--username={database['USER']}",
            f"--dbname={database['NAME']}",
            str(dump_path),
        ]

        env = {**os.environ, "PGPASSWORD": database["PASSWORD"] or ""}

        try:
            result = subprocess.run(
                command, env=env, capture_output=True, text=True, timeout=3600
            )
        except FileNotFoundError as exc:
            raise CommandError(
                "pg_restore is not installed - see postgresql-client in the Dockerfile."
            ) from exc

        # pg_restore exits non-zero for notices that are not failures, so
        # neither the exit code nor the stderr text decides the outcome on
        # its own. What decides it is whether the data is there afterwards -
        # the same "verify, do not believe" rule the backup command follows.
        #
        # This matters more than it sounds: the first version of this command
        # reported a COMPLETELY SUCCESSFUL restore as a failure because of a
        # `SET transaction_timeout` notice, which is emitted when the pg_dump
        # client is newer than the server (a v17 client against Postgres 16
        # here). An operator told their restore failed, during the incident
        # that made them restore, is likely to make things worse.
        notices = [line for line in result.stderr.splitlines() if line.strip()]
        benign = [line for line in notices if self._is_benign(line)]
        unexplained = [line for line in notices if not self._is_benign(line)]

        tables = self._count_tables(database)

        if tables == 0:
            raise CommandError(
                "pg_restore left the database empty. Output was:\n  "
                + "\n  ".join(notices[:10])
            )

        if unexplained:
            self.stdout.write(
                self.style.WARNING(
                    f"  pg_restore reported {len(unexplained)} message(s) that are "
                    f"not in the known-benign list; the data restored, but read "
                    f"these before trusting it:"
                )
            )
            for line in unexplained[:10]:
                self.stdout.write(f"    {line}")
        elif benign:
            self.stdout.write(
                f"  {len(benign)} benign notice(s) ignored "
                f"(version skew or objects absent from an empty target)"
            )

        self.stdout.write(f"  verified: {tables} table(s) present after restore")

    # Notices that do not mean the restore failed.
    BENIGN = (
        # Restoring into an empty database: --clean tries to drop first.
        "does not exist",
        # The dumping client is newer than the server and emits a SET for a
        # parameter the server has never heard of.
        "unrecognized configuration parameter",
        "errors ignored on restore",
        # pg_restore prints the offending statement on the line after the
        # message. Without this the continuation of a benign notice gets
        # counted as an unexplained one of its own.
        "Command was:",
    )

    @classmethod
    def _is_benign(cls, line: str) -> bool:
        return any(marker in line for marker in cls.BENIGN)

    @staticmethod
    def _count_tables(database) -> int:
        """Ask the target database what it actually contains."""
        from django.db import connections

        connections["default"].close()
        with connections["default"].cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
            return cursor.fetchone()[0]
