import contextlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai.classification.datasets import integrity, rvl_cdip
from ai.classification.datasets.base import (
    LABELS,
    DatasetSample,
    balanced_subset,
    parse_split_lines,
    read_manifest,
    sample_id_from_path,
    write_manifest,
)
from ai.common import download as downloader


def sample(sample_id, split="train", label="invoice", input_type="text", path="/x.txt"):
    return DatasetSample(sample_id, split, label, input_type, path)


class LabelSchemaTests(unittest.TestCase):
    def test_sixteen_canonical_classes(self):
        self.assertEqual(len(LABELS), 16)
        self.assertEqual(LABELS[0], "letter")
        self.assertEqual(LABELS[11], "invoice")
        self.assertEqual(LABELS[15], "memo")

    def test_labels_are_unique(self):
        self.assertEqual(len(set(LABELS)), len(LABELS))


class SampleIdTests(unittest.TestCase):
    def test_strips_the_extension(self):
        self.assertEqual(
            sample_id_from_path("imagesq/q/o/c/qoc54c00/80035521.tif"),
            "imagesq/q/o/c/qoc54c00/80035521",
        )

    def test_image_and_text_of_one_page_share_an_id(self):
        # This is what lets the text and vision baselines be compared on
        # identical samples rather than two loosely-related subsets.
        stem = "imagesq/q/o/c/qoc54c00/80035521"
        self.assertEqual(
            sample_id_from_path(f"{stem}.tif"), sample_id_from_path(f"{stem}.txt")
        )

    def test_only_the_final_extension_is_stripped(self):
        # Real RVL-CDIP filenames contain dots, e.g. ton01613.93_ton01613.94.tif.
        # Stripping greedily would collapse distinct documents onto one id.
        base = "imagesd/d/z/x/dzx02f00/ton01613.93_ton01613.94"
        self.assertEqual(sample_id_from_path(f"{base}.txt"), base)
        self.assertEqual(sample_id_from_path(f"{base}.tif"), base)


class SplitFileParsingTests(unittest.TestCase):
    def test_parses_path_and_label_index(self):
        text = "imagesa/a/b/c/x/1.tif 11\nimagesa/a/b/c/x/2.tif 0\n"
        samples = parse_split_lines(text, "train", Path("/data"), "image")

        self.assertEqual([s.label for s in samples], ["invoice", "letter"])
        self.assertEqual(samples[0].input_type, "image")
        self.assertTrue(samples[0].input_path.endswith("1.tif"))

    def test_ignores_blank_lines(self):
        samples = parse_split_lines(
            "a.tif 1\n\n\nb.tif 2\n", "test", Path("/d"), "image"
        )
        self.assertEqual(len(samples), 2)

    def test_rejects_out_of_range_label(self):
        with self.assertRaises(ValueError):
            parse_split_lines("a.tif 99", "train", Path("/d"), "image")

    def test_rejects_non_numeric_label(self):
        with self.assertRaises(ValueError):
            parse_split_lines("a.tif invoice", "train", Path("/d"), "image")

    def test_rejects_negative_label(self):
        # Python indexing would otherwise read -1 as LABELS[15], "memo".
        with self.assertRaises(ValueError):
            parse_split_lines("a.tif -1", "train", Path("/d"), "image")

    def test_handles_spaces_in_filenames(self):
        samples = parse_split_lines("dir/a file.tif 3", "train", Path("/d"), "image")
        self.assertEqual(samples[0].label, LABELS[3])


class BalancedSubsetTests(unittest.TestCase):
    def make_pool(self, per_class=50):
        return [
            sample(f"{label}/{i}", label=label)
            for label in LABELS
            for i in range(per_class)
        ]

    def test_is_balanced_across_classes(self):
        subset = balanced_subset(self.make_pool(), per_class=10, seed=0)

        counts = dict.fromkeys(LABELS, 0)
        for s in subset:
            counts[s.label] += 1

        self.assertEqual(set(counts.values()), {10})
        self.assertEqual(len(subset), 160)

    def test_same_seed_gives_the_same_subset(self):
        pool = self.make_pool()
        first = balanced_subset(pool, 10, seed=7)
        second = balanced_subset(list(reversed(pool)), 10, seed=7)

        # Determinism must not depend on input ordering.
        self.assertEqual([s.sample_id for s in first], [s.sample_id for s in second])

    def test_different_seed_gives_a_different_subset(self):
        pool = self.make_pool()
        self.assertNotEqual(
            [s.sample_id for s in balanced_subset(pool, 10, seed=1)],
            [s.sample_id for s in balanced_subset(pool, 10, seed=2)],
        )

    def test_takes_everything_when_asked_for_more_than_exists(self):
        subset = balanced_subset(self.make_pool(per_class=3), per_class=10, seed=0)
        self.assertEqual(len(subset), 48)


class ManifestTests(unittest.TestCase):
    def test_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "train.csv"
            original = [sample("a/1"), sample("a/2", label="memo")]

            self.assertEqual(write_manifest(original, path), 2)
            self.assertEqual(read_manifest(path), original)


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifests = self.root / "manifests"

    def write_split(self, split, samples):
        write_manifest(samples, self.manifests / f"{split}.csv")

    def make_file(self, name, content="some text"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def full_set(self, prefix="s", content="some text"):
        """One readable sample per class, per split."""
        for split in ("train", "validation", "test"):
            samples = []
            for label in LABELS:
                sid = f"{prefix}/{split}/{label}"
                path = self.make_file(f"{sid}.txt", content)
                samples.append(sample(sid, split, label, "text", str(path)))
            self.write_split(split, samples)

    def test_clean_dataset_passes(self):
        self.full_set()
        report = integrity.check(self.manifests)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.splits["train"], 16)

    def test_detects_split_leakage(self):
        self.full_set()
        shared = read_manifest(self.manifests / "train.csv")[0]
        test_rows = read_manifest(self.manifests / "test.csv")
        self.write_split(
            "test",
            test_rows[:-1]
            + [
                DatasetSample(
                    shared.sample_id,
                    "test",
                    *((shared.label, shared.input_type, shared.input_path)),
                )
            ],
        )

        report = integrity.check(self.manifests)

        self.assertFalse(report.ok)
        self.assertTrue(any("leakage" in e for e in report.errors))

    def test_detects_duplicates_within_a_split(self):
        self.full_set()
        rows = read_manifest(self.manifests / "train.csv")
        self.write_split("train", rows + [rows[0]])

        report = integrity.check(self.manifests)

        self.assertFalse(report.ok)
        self.assertEqual(report.duplicates["train"], 1)

    def test_detects_missing_files(self):
        self.full_set()
        rows = read_manifest(self.manifests / "train.csv")
        Path(rows[0].input_path).unlink()

        report = integrity.check(self.manifests)

        self.assertFalse(report.ok)
        self.assertEqual(report.missing_files["train"]["missing"], 1)

    def test_reports_empty_inputs_as_a_warning_not_a_failure(self):
        # Empty OCR is inherent to the corpus, so it must be visible
        # without failing the run.
        self.full_set()
        rows = read_manifest(self.manifests / "train.csv")
        Path(rows[0].input_path).write_text("   ", encoding="utf-8")

        report = integrity.check(self.manifests)

        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.empty_inputs["train"]["empty"], 1)
        self.assertTrue(any("no text" in w for w in report.warnings))

    def test_detects_labels_outside_the_schema(self):
        self.full_set()
        rows = read_manifest(self.manifests / "train.csv")
        rows[0] = DatasetSample(
            rows[0].sample_id, "train", "tax return", "text", rows[0].input_path
        )
        self.write_split("train", rows)

        report = integrity.check(self.manifests)

        self.assertFalse(report.ok)
        self.assertEqual(report.unknown_labels["train"], ["tax return"])

    def test_warns_on_missing_classes(self):
        self.full_set()
        rows = read_manifest(self.manifests / "train.csv")
        self.write_split("train", rows[:-1])

        report = integrity.check(self.manifests)

        self.assertTrue(any("no samples for" in w for w in report.warnings))

    def test_missing_manifest_is_an_error(self):
        report = integrity.check(self.manifests / "nope")
        self.assertFalse(report.ok)


class RvlCdipLayoutTests(unittest.TestCase):
    """The image tarball unpacks to images/imagesX/..., while the split
    files list imagesX/... - so manifests must resolve against images/."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "rvl_cdip"
        self.data_dir.mkdir()
        self.manifests = Path(self.tmp.name) / "manifests"

    def build_archive(self, relative_paths):
        archive = self.data_dir / rvl_cdip.IMAGES_ARCHIVE
        with tarfile.open(archive, "w:gz") as tar:
            for relative in relative_paths:
                info = tarfile.TarInfo(f"images/{relative}")
                info.size = 4
                tar.addfile(info, io.BytesIO(b"II*\x00"))
        return archive

    def test_manifests_point_at_the_extracted_images(self):
        relative = {
            "train": "imagesa/a/b/c/abc01a00/1.tif",
            "validation": "imagesb/b/c/d/bcd02b00/2.tif",
            "test": "imagesc/c/d/e/cde03c00/3.tif",
        }
        archive = self.build_archive(relative.values())
        for split, filename in rvl_cdip.SPLIT_FILES.items():
            (self.data_dir / filename).write_text(
                f"{relative[split]} 11\n", encoding="utf-8"
            )

        with (
            mock.patch.object(rvl_cdip, "download_split_files"),
            mock.patch.object(rvl_cdip, "download", return_value=archive),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            rvl_cdip.prepare(self.data_dir, self.manifests, with_images=True)

        for split in rvl_cdip.SPLIT_FILES:
            rows = read_manifest(self.manifests / f"{split}.csv")
            self.assertEqual(len(rows), 1)
            self.assertTrue(Path(rows[0].input_path).is_file(), rows[0].input_path)

    def test_default_paths_are_under_the_ignored_data_root(self):
        # ai/classification/data/ is git-ignored; data/ at the repo root is
        # not, and that is where 38.76 GB of images would otherwise land.
        with (
            mock.patch.object(rvl_cdip, "prepare", return_value={}) as prepare,
            mock.patch("sys.argv", ["rvl_cdip"]),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            rvl_cdip.main()

        data_dir, manifest_dir = prepare.call_args.args
        root = Path("ai/classification/data")
        self.assertTrue(data_dir.is_relative_to(root), data_dir)
        self.assertTrue(manifest_dir.is_relative_to(root), manifest_dir)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.destination = Path(self.tmp.name) / "archive.tar.gz"

    def fetch(self, body, content_length):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Length": str(content_length)}
        # Ends in b"", which is also what http.client returns - rather than
        # raising - when the server hangs up before Content-Length bytes.
        response.read.side_effect = io.BytesIO(body).read

        with (
            mock.patch.object(
                downloader.urllib.request, "urlopen", return_value=response
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return downloader.download("https://example.invalid/a", self.destination)

    def test_a_truncated_transfer_is_not_cached(self):
        with self.assertRaises(OSError):
            self.fetch(b"hello", content_length=10)

        # Otherwise every later run reports it as cached and skips the fetch.
        self.assertFalse(self.destination.exists())

    def test_a_complete_transfer_lands_in_place(self):
        self.fetch(b"hello", content_length=5)
        self.assertEqual(self.destination.read_bytes(), b"hello")


if __name__ == "__main__":
    unittest.main()
