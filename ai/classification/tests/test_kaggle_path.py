"""Tests for the parts of the Kaggle path that can be checked without a GPU.

The training loop itself needs torch and a GPU, so it is not covered
here. What IS covered is the layout discovery - the code most likely to
break, because the Kaggle RVL-CDIP mirrors are community re-uploads
whose internal structure has not been verified from this machine.
"""

import tempfile
import unittest
from pathlib import Path

from ai.classification.training import submit_kaggle, train_vision
from ai.common import kaggle


class LayoutDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def build(self, prefix="", split_names=("train.txt", "val.txt", "test.txt")):
        """Fake an RVL-CDIP-style tree, optionally nested under a folder
        the way a Kaggle mirror might wrap it."""
        base = self.root / prefix if prefix else self.root
        images = base / "images" / "a" / "b"
        images.mkdir(parents=True, exist_ok=True)

        for name in ("one", "two"):
            (images / f"{name}.tif").write_bytes(b"\x00")

        for index, split in enumerate(split_names):
            (base / split).write_text(
                f"images/a/b/one.tif {index}\nimages/a/b/two.tif {index + 1}\n",
                encoding="utf-8",
            )
        return base

    def test_finds_split_files_at_the_root(self):
        self.build()
        found = train_vision.find_split_files(self.root)

        self.assertEqual(set(found), {"train", "validation", "test"})
        self.assertTrue(found["train"].name == "train.txt")

    def test_finds_split_files_nested_inside_a_wrapper_directory(self):
        # Kaggle mirrors commonly wrap the dataset in an extra folder.
        self.build(prefix="rvl-cdip-v2")
        found = train_vision.find_split_files(self.root)

        self.assertEqual(set(found), {"train", "validation", "test"})

    def test_accepts_qs_ocr_style_split_filenames(self):
        self.build(split_names=("text_train.txt", "text_val.txt", "text_test.txt"))
        found = train_vision.find_split_files(self.root)

        self.assertEqual(set(found), {"train", "validation", "test"})

    def test_locates_the_image_root_when_nested(self):
        self.build(prefix="wrapper")
        image_root = train_vision.find_image_root(self.root, "images/a/b/one.tif")

        self.assertTrue((image_root / "images/a/b/one.tif").exists())

    def test_missing_images_fail_loudly_with_context(self):
        (self.root / "something-else").mkdir()

        with self.assertRaises(FileNotFoundError) as caught:
            train_vision.find_image_root(self.root, "images/a/b/one.tif")

        # The error must say what it actually found, or debugging a
        # remote kernel is guesswork.
        self.assertIn("something-else", str(caught.exception))

    def test_load_split_resolves_paths_and_labels(self):
        base = self.build()
        items = train_vision.load_split(base / "train.txt", base, None)

        self.assertEqual(len(items), 2)
        self.assertEqual([label for _, label in items], [0, 1])
        self.assertTrue(items[0][0].exists())

    def test_per_class_cap_is_applied(self):
        base = self.build()
        (base / "train.txt").write_text(
            "".join(f"images/a/b/one.tif 0\n" for _ in range(10)), encoding="utf-8"
        )
        items = train_vision.load_split(base / "train.txt", base, 3)

        self.assertEqual(len(items), 3)

    def test_describe_input_lists_what_is_there(self):
        self.build()
        described = train_vision.describe_input(self.root)
        self.assertIn("images", described)


class KernelAssemblyTests(unittest.TestCase):
    def test_assembled_kernel_is_self_contained(self):
        source = submit_kaggle.build_kernel_source(["--smoke"])

        # Nothing may import the repo package - Kaggle only gets this file.
        self.assertNotIn("from ai.classification", source)
        self.assertNotIn("from ai.common", source)

        self.assertIn("LABELS = (", source)
        self.assertIn("def evaluate(", source)
        self.assertIn("def main(", source)
        self.assertTrue(source.rstrip().endswith("main()"))

    def test_assembled_kernel_parses(self):
        import ast

        ast.parse(submit_kaggle.build_kernel_source(["--smoke"]))

    def test_labels_match_the_dataset_module_exactly(self):
        from ai.classification.datasets.base import LABELS

        namespace: dict = {}
        exec(submit_kaggle.extract_labels(), namespace)

        # A drifting label order would silently mislabel every prediction.
        self.assertEqual(namespace["LABELS"], LABELS)

    def test_training_arguments_are_forwarded(self):
        source = submit_kaggle.build_kernel_source(["--epochs", "7", "--arch", "resnet18"])
        self.assertIn('"--epochs", "7"', source.replace("'", '"'))
        self.assertIn("resnet18", source)


class CredentialTests(unittest.TestCase):
    def test_missing_credentials_explain_where_to_get_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "kaggle.json"

            with self.assertRaises(kaggle.CredentialsMissing) as caught:
                kaggle.load_credentials(missing)

        message = str(caught.exception)
        self.assertIn("KAGGLE_USERNAME", message)
        self.assertIn("kaggle.com/settings", message)

    def test_credentials_build_a_basic_auth_header(self):
        header = kaggle.Credentials("someone", "secret").header
        self.assertTrue(header.startswith("Basic "))

    def test_file_without_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "kaggle.json"
            path.write_text('{"username": "someone"}', encoding="utf-8")

            with self.assertRaises(kaggle.CredentialsMissing):
                kaggle.load_credentials(path)


if __name__ == "__main__":
    unittest.main()
