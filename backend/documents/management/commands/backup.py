"""Database backup (spec §21.3: "Back up databases and critical object storage").

Shells out to pg_dump rather than serializing through Django. `dumpdata`
produces JSON that has to round-trip through the ORM to restore, which means
a restore depends on the models still matching - exactly the assumption that
fails when you are restoring because a migration went wrong. A pg_dump
custom-format archive restores without Django's cooperation.

Every dump is verified by reading its own table of contents back before it
is reported as a success. An unverified backup is a belief, not a backup,
and the moment you discover the difference is the worst possible moment.
"""

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone


class Command(BaseCommand):
    help = "Dump the database (and optionally media) to BACKUP_ROOT."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            help="Where to write. Defaults to the BACKUP_ROOT setting.",
        )
        parser.add_argument(
            "--label", default="", help="Suffix for the backup filename."
        )
        parser.add_argument(
            "--include-media",
            action="store_true",
            help="Also archive MEDIA_ROOT (the original uploaded files).",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Delete backups beyond BACKUP_RETENTION after a successful run.",
        )

    def handle(self, *args, **options):
        destination = Path(
            options["output_dir"] or getattr(settings, "BACKUP_ROOT", "backups")
        )
        destination.mkdir(parents=True, exist_ok=True)

        stamp = timezone.now().strftime("%Y%m%dT%H%M%SZ")
        label = f"-{options['label']}" if options["label"] else ""
        dump_path = destination / f"db-{stamp}{label}.dump"

        database = settings.DATABASES["default"]
        started = time.monotonic()

        self.stdout.write(f"Dumping {database['NAME']} -> {dump_path.name}")
        self._pg_dump(database, dump_path)

        # Verify before claiming success.
        tables = self._verify(database, dump_path)

        digest = self._sha256(dump_path)
        manifest = {
            "created_at": stamp,
            "database": database["NAME"],
            "dump_file": dump_path.name,
            "bytes": dump_path.stat().st_size,
            "sha256": digest,
            "tables": tables,
            # The migration the dump was taken at. Restoring into a codebase
            # at a different migration is the failure this records, and
            # without it the mismatch is only discovered at runtime.
            "migration": self._latest_migration(),
            "duration_seconds": round(time.monotonic() - started, 2),
        }

        media_archive = None
        if options["include_media"]:
            media_archive = self._archive_media(destination, stamp, label)
            if media_archive:
                manifest["media_file"] = media_archive.name
                manifest["media_bytes"] = media_archive.stat().st_size

        manifest_path = dump_path.with_suffix(".manifest.json")
        import json

        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        self.stdout.write(
            self.style.SUCCESS(
                f"Backup verified: {manifest['bytes']} bytes, "
                f"{tables} table(s), sha256 {digest[:16]}..."
            )
        )
        self.stdout.write(f"  manifest: {manifest_path}")
        if media_archive:
            self.stdout.write(f"  media:    {media_archive}")

        if options["prune"]:
            self._prune(destination)

    def _pg_dump(self, database, dump_path):
        command = [
            "pg_dump",
            "--format=custom",
            # Restores can then run against a fresh database without the
            # ownership and role errors a plain dump produces.
            "--no-owner",
            "--no-privileges",
            f"--host={database['HOST']}",
            f"--port={database['PORT']}",
            f"--username={database['USER']}",
            f"--file={dump_path}",
            database["NAME"],
        ]

        # The password goes through the environment, never the command line:
        # argv is readable by any process on the host.
        env = {**os.environ, "PGPASSWORD": database["PASSWORD"] or ""}

        try:
            result = subprocess.run(
                command, env=env, capture_output=True, text=True, timeout=3600
            )
        except FileNotFoundError as exc:
            raise CommandError(
                "pg_dump is not installed in this container. It is provided by "
                "the postgresql-client package - see the Dockerfile."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CommandError("pg_dump timed out after an hour.") from exc

        if result.returncode != 0:
            # Remove the partial file: a half-written dump that looks like a
            # backup is worse than no file at all.
            dump_path.unlink(missing_ok=True)
            raise CommandError(f"pg_dump failed: {result.stderr.strip()}")

    def _verify(self, database, dump_path) -> int:
        """Read the archive's own table of contents back.

        Cheap, and it catches a truncated or corrupt dump now rather than
        during the restore nobody wants to be doing.
        """
        env = {**os.environ, "PGPASSWORD": database["PASSWORD"] or ""}
        result = subprocess.run(
            ["pg_restore", "--list", str(dump_path)],
            env=env,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise CommandError(
                f"the dump was written but is not readable by pg_restore: "
                f"{result.stderr.strip()}"
            )

        return sum(1 for line in result.stdout.splitlines() if " TABLE DATA " in line)

    @staticmethod
    def _latest_migration() -> str:
        try:
            latest = (
                MigrationRecorder(connection).migration_qs.order_by("-applied").first()
            )
            return f"{latest.app}.{latest.name}" if latest else ""
        except Exception:  # pragma: no cover - defensive
            return ""

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
        return digest.hexdigest()

    def _archive_media(self, destination: Path, stamp: str, label: str):
        media_root = Path(settings.MEDIA_ROOT)
        if not media_root.exists() or not any(media_root.iterdir()):
            self.stdout.write(self.style.WARNING("  media is empty; nothing archived"))
            return None

        base = destination / f"media-{stamp}{label}"
        archive = shutil.make_archive(str(base), "gztar", root_dir=str(media_root))
        return Path(archive)

    def _prune(self, destination: Path):
        keep = getattr(settings, "BACKUP_RETENTION", 7)
        dumps = sorted(destination.glob("db-*.dump"), reverse=True)

        for stale in dumps[keep:]:
            stale.with_suffix(".manifest.json").unlink(missing_ok=True)
            stale.unlink(missing_ok=True)
            self.stdout.write(f"  pruned {stale.name}")

        if len(dumps) > keep:
            self.stdout.write(
                self.style.SUCCESS(f"Kept the {keep} most recent backup(s)")
            )
