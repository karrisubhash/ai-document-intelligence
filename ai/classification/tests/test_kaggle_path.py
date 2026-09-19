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
            "".join("images/a/b/one.tif 0\n" for _ in range(10)), encoding="utf-8"
        )
        items = train_vision.load_split(base / "train.txt", base, 3)

        self.assertEqual(len(items), 3)

    def test_describe_input_lists_what_is_there(self):
        self.build()
        described = train_vision.describe_input(self.root)
        self.assertIn("images", described)


class CanonicalTreeTests(unittest.TestCase):
    """Mirrors preserve RVL-CDIP's imagesa/../imagesz/ tree but ship no
    split files, so the canonical tree must be recognised on its own.

    Layout replicated from ducktard/rvlcdip:
        images/imagesa/a/a/a/aaa06d00/50486482-6482.tif
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def build(self, wrapper="images", letters=("a", "b", "c")):
        base = self.root / wrapper if wrapper else self.root
        for letter in letters:
            leaf = (
                base
                / f"images{letter}"
                / letter
                / letter
                / letter
                / f"{letter * 3}06d00"
            )
            leaf.mkdir(parents=True, exist_ok=True)
            (leaf / "50486482-6482.tif").write_bytes(b"\x00")
        return base

    def test_finds_the_canonical_tree_under_a_wrapper(self):
        base = self.build()
        self.assertEqual(train_vision.locate_canonical_images(self.root), base)

    def test_finds_it_when_nested_two_levels_deep(self):
        base = self.build(wrapper="rvl-cdip/images")
        self.assertEqual(train_vision.locate_canonical_images(self.root), base)

    def test_official_relative_paths_resolve_against_it(self):
        base = self.build()
        relative = "imagesa/a/a/a/aaa06d00/50486482-6482.tif"
        self.assertTrue((base / relative).exists())

    def test_a_single_images_dir_is_not_mistaken_for_the_tree(self):
        # One "images" folder is an ImageFolder mirror, not the canonical
        # 26-letter tree; requiring two prefixes avoids the false match.
        (self.root / "images").mkdir()
        self.assertIsNone(train_vision.locate_canonical_images(self.root))

    def test_returns_none_when_absent(self):
        (self.root / "unrelated").mkdir()
        self.assertIsNone(train_vision.locate_canonical_images(self.root))


class BoundedWalkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_walk_respects_max_depth(self):
        deep = self.root / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)

        names = {p.name for p in train_vision.iter_dirs(self.root, max_depth=2)}

        self.assertIn("b", names)
        # Depth 3+ must not be walked - an unbounded walk over a mounted
        # mirror cost 23 minutes of a GPU session.
        self.assertNotIn("d", names)
        self.assertNotIn("e", names)

    def test_split_file_search_is_depth_limited(self):
        buried = self.root / "a" / "b" / "c" / "d"
        buried.mkdir(parents=True)
        (buried / "train.txt").write_text("x.tif 0\n", encoding="utf-8")

        self.assertEqual(train_vision.find_split_files(self.root, max_depth=2), {})
        self.assertIn("train", train_vision.find_split_files(self.root, max_depth=5))


class ImageFolderLayoutTests(unittest.TestCase):
    """The Kaggle mirrors ship <split>/<class>/<image> and no split files.

    Layout replicated from the real listing of chan814/rvl-cdip:
        RVL_Classification/train/advertisement/0000000466.tif
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def build(
        self, splits=("train", "test"), class_names=None, wrapper="RVL_Classification"
    ):
        class_names = class_names or ["advertisement", "letter", "scientific_report"]
        base = self.root / wrapper if wrapper else self.root
        for split in splits:
            for name in class_names:
                directory = base / split / name
                directory.mkdir(parents=True, exist_ok=True)
                for i in range(4):
                    (directory / f"{i:010d}.tif").write_bytes(b"\x00")
        return base

    def test_finds_splits_and_classes(self):
        self.build()
        found = train_vision.find_imagefolder_splits(self.root)

        self.assertEqual(sorted(found), ["test", "train"])
        self.assertIn("advertisement", found["train"])

    def test_underscored_directory_names_map_to_schema_labels(self):
        self.build(class_names=["scientific_report", "file_folder", "news_article"])
        found = train_vision.find_imagefolder_splits(self.root)

        self.assertEqual(
            sorted(found["train"]),
            ["file folder", "news article", "scientific report"],
        )

    def test_val_is_normalised_to_validation(self):
        self.build(splits=("train", "val", "test"))
        found = train_vision.find_imagefolder_splits(self.root)

        self.assertIn("validation", found)

    def test_loads_items_with_correct_label_indices(self):
        from ai.classification.datasets.base import LABELS

        self.build(class_names=["letter", "advertisement"])
        found = train_vision.find_imagefolder_splits(self.root)
        items = train_vision.load_imagefolder_split(found["train"], None)

        self.assertEqual(len(items), 8)
        labels = {label for _, label in items}
        self.assertEqual(
            labels, {LABELS.index("letter"), LABELS.index("advertisement")}
        )

    def test_per_class_cap_applies(self):
        self.build()
        found = train_vision.find_imagefolder_splits(self.root)
        items = train_vision.load_imagefolder_split(found["train"], 2)

        self.assertEqual(len(items), 6)  # 3 classes x 2

    def test_directories_outside_the_schema_are_ignored(self):
        base = self.build()
        (base / "train" / "not_a_document_class").mkdir()
        found = train_vision.find_imagefolder_splits(self.root)

        self.assertNotIn("not a document class", found["train"])

    def test_no_layout_at_all_returns_empty(self):
        (self.root / "random").mkdir()
        self.assertEqual(train_vision.find_imagefolder_splits(self.root), {})


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
        source = submit_kaggle.build_kernel_source(
            ["--epochs", "7", "--arch", "resnet18"]
        )
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
