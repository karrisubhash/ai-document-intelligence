"""Reading order and borderless tables, on synthetic page layouts.

Everything here is built from word rectangles, so each test states a layout
in numbers and the expected reading in words. Coordinates: a character is 6
wide, a word gap inside a phrase is 4, text is 10 high, consecutive lines
are 14 apart (4 of leading) and a paragraph break is 22 apart.

The point of most of them is a wrong answer that must NOT happen: a clause
list read as a table, prose columns cut into cells, a note under a table
swallowed as a row.
"""

from django.test import SimpleTestCase

from documents.processing.extractors.geometry import Block, Word, layout_page

CHAR = 6
SPACE = 4
HEIGHT = 10


def phrase(text, x, y):
    """Words of `text` laid out left to right from x."""
    words = []
    for token in text.split():
        words.append(Word(token, x, x + CHAR * len(token), y, y + HEIGHT))
        x += CHAR * len(token) + SPACE
    return words


def right_aligned(text, x1, y):
    return phrase(text, x1 - CHAR * len(text), y)


def row(y, *cells):
    """A table row: (x, text) cells are left aligned, ("r", x1, text) right."""
    words = []
    for cell in cells:
        if cell[0] == "r":
            words += right_aligned(cell[2], cell[1], y)
        else:
            words += phrase(cell[1], cell[0], y)
    return words


def texts(blocks):
    return [(b.kind, b.text if b.kind == "text" else b.rows) for b in blocks]


class ReadingOrderTests(SimpleTestCase):
    def test_prose_is_one_block_with_paragraph_breaks(self):
        words = (
            phrase("The committee met on Tuesday", 50, 100)
            + phrase("and reviewed the attendance policy", 50, 114)
            + phrase("A further meeting was scheduled", 50, 136)
        )

        blocks = layout_page(words)

        self.assertEqual(
            texts(blocks),
            [
                (
                    "text",
                    "The committee met on Tuesday\n"
                    "and reviewed the attendance policy\n"
                    "\n"
                    "A further meeting was scheduled",
                )
            ],
        )

    def test_an_empty_page_has_no_blocks(self):
        self.assertEqual(layout_page([]), [])

    def test_a_stray_wide_gap_in_prose_is_not_a_table(self):
        words = (
            phrase("The registrar", 50, 100)
            + phrase("noted that the rule", 150, 100)
            + phrase("had not changed since the previous", 50, 114)
            + phrase("academic year and no action followed", 50, 128)
        )

        blocks = layout_page(words)

        self.assertEqual([b.kind for b in blocks], ["text"])

    def test_two_page_columns_are_read_one_after_the_other(self):
        left = [
            "alpha beta gamma delta epsilon",
            "zeta eta theta iota kappa",
            "lambda mu nu xi omicron",
            "pi rho sigma tau upsilon",
        ]
        right = [
            "one two three four five",
            "six seven eight nine ten",
            "eleven twelve thirteen fourteen fifteen",
            "sixteen seventeen eighteen nineteen twenty",
        ]
        words = []
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            y = 100 + 14 * index
            words += phrase(a, 50, y) + phrase(b, 320, y)

        blocks = layout_page(words)

        self.assertEqual(
            texts(blocks),
            [("text", "\n".join(left)), ("text", "\n".join(right))],
        )

    def test_a_full_width_title_stays_above_the_columns(self):
        words = phrase("Annual Report of the Examination Cell", 50, 60)
        for index in range(4):
            y = 100 + 14 * index
            words += phrase("left column words that run on", 50, y)
            words += phrase("right column words that run on", 320, y)

        blocks = layout_page(words)

        self.assertEqual(blocks[0].text, "Annual Report of the Examination Cell")
        self.assertEqual(len(blocks), 3)
        self.assertTrue(blocks[1].text.startswith("left column"))
        self.assertTrue(blocks[2].text.startswith("right column"))


class BorderlessTableTests(SimpleTestCase):
    def fee_table(self, y=130):
        return (
            row(y, (50, "Programme"), (200, "Tuition"), (300, "Exam Fee"))
            + row(y + 14, (50, "B.Tech CSE"), ("r", 250, "85000"), ("r", 350, "2500"))
            + row(y + 28, (50, "B.Tech ECE"), ("r", 250, "82000"), ("r", 350, "2500"))
            + row(y + 42, (50, "M.Tech VLSI"), ("r", 250, "95000"), ("r", 350, "3000"))
        )

    def test_a_table_is_found_with_its_title_left_as_text(self):
        words = phrase("FEE STRUCTURE 2024", 50, 100) + self.fee_table()

        blocks = layout_page(words)

        self.assertEqual(
            texts(blocks),
            [
                ("text", "FEE STRUCTURE 2024"),
                (
                    "table",
                    [
                        ["Programme", "Tuition", "Exam Fee"],
                        ["B.Tech CSE", "85000", "2500"],
                        ["B.Tech ECE", "82000", "2500"],
                        ["M.Tech VLSI", "95000", "3000"],
                    ],
                ),
            ],
        )

    def test_an_empty_cell_keeps_its_column(self):
        words = (
            row(100, (50, "Item"), (200, "Qty"), (300, "Note"))
            + row(114, (50, "Pens"), ("r", 220, "10"))
            + row(128, (50, "Paper"), (300, "out of stock"))
            + row(142, (50, "Ink"), ("r", 220, "4"), (300, "fragile"))
        )

        table = [b for b in layout_page(words) if b.kind == "table"][0]

        self.assertEqual(
            table.rows,
            [
                ["Item", "Qty", "Note"],
                ["Pens", "10", ""],
                ["Paper", "", "out of stock"],
                ["Ink", "4", "fragile"],
            ],
        )

    def test_a_wrapped_cell_folds_into_its_row(self):
        words = (
            row(100, (50, "Course"), (150, "Description"), ("r", 450, "Marks"))
            + row(114, (50, "CS301"), (150, "Introduction to"), ("r", 450, "84"))
            + row(126, (150, "data structures"))
            + row(148, (50, "CS302"), (150, "Operating systems"), ("r", 450, "69"))
        )

        table = [b for b in layout_page(words) if b.kind == "table"][0]

        self.assertEqual(
            table.rows,
            [
                ["Course", "Description", "Marks"],
                ["CS301", "Introduction to data structures", "84"],
                ["CS302", "Operating systems", "69"],
            ],
        )

    def test_a_note_under_the_table_is_not_a_row(self):
        words = self.fee_table() + phrase("Note: all fees are in INR.", 50, 200)

        blocks = layout_page(words)

        self.assertEqual([b.kind for b in blocks], ["table", "text"])
        self.assertEqual(blocks[1].text, "Note: all fees are in INR.")
        self.assertEqual(len(blocks[0].rows), 4)

    def test_a_header_and_one_numeric_row_is_a_table(self):
        words = row(100, (50, "Course"), (200, "Marks")) + row(
            114, (50, "CS301"), ("r", 240, "84")
        )

        blocks = layout_page(words)

        self.assertEqual(
            texts(blocks), [("table", [["Course", "Marks"], ["CS301", "84"]])]
        )

    def test_a_header_and_one_row_of_words_is_not(self):
        words = row(100, (50, "Name"), (200, "Place")) + row(
            114, (50, "Asha"), (200, "Pune")
        )

        self.assertEqual([b.kind for b in layout_page(words)], ["text"])

    def test_a_form_of_labels_and_values_is_a_table(self):
        words = (
            row(100, (50, "Name"), (200, "Asha Rao"))
            + row(114, (50, "Department"), (200, "Computer Science and Engineering"))
            + row(128, (50, "Roll No"), (200, "21CS045"))
        )

        table = layout_page(words)[0]

        self.assertEqual(table.kind, "table")
        self.assertEqual(
            table.rows[1], ["Department", "Computer Science and Engineering"]
        )

    def test_three_columns_of_short_text_are_a_table(self):
        words = (
            row(100, (50, "Name"), (200, "Dept"), (320, "Role"))
            + row(114, (50, "Asha Rao"), (200, "CSE"), (320, "Dean"))
            + row(128, (50, "Ravi Kumar"), (200, "ECE"), (320, "Proctor"))
            + row(142, (50, "Meera Nair"), (200, "Admin"), (320, "Registrar"))
        )

        table = layout_page(words)[0]

        self.assertEqual(table.kind, "table")
        self.assertEqual(table.rows[3], ["Meera Nair", "Admin", "Registrar"])


class ListsAreNotTablesTests(SimpleTestCase):
    def test_numbered_clauses_with_a_hanging_indent_stay_as_lines(self):
        words = (
            row(
                100,
                (50, "1.1"),
                (100, "The student shall attend at least seventy five"),
            )
            + row(114, (100, "percent of the classes held in each course"))
            + row(
                136, (50, "1.2"), (100, "The examination fee shall be paid before the")
            )
            + row(150, (100, "semester begins and is not refundable"))
            + row(
                172, (50, "1.3"), (100, "A student who is absent may apply for leave")
            )
        )

        blocks = layout_page(words)

        self.assertEqual([b.kind for b in blocks], ["text"])
        self.assertIn("1.1 The student shall attend", blocks[0].text)
        self.assertIn("1.2 The examination fee", blocks[0].text)

    def test_bullets_stay_as_lines(self):
        words = (
            row(100, (50, "•"), (70, "Library cards are issued in July"))
            + row(114, (50, "•"), (70, "Hostel forms are due in August"))
            + row(128, (50, "•"), (70, "Exam forms are due in October"))
        )

        blocks = layout_page(words)

        self.assertEqual([b.kind for b in blocks], ["text"])
        self.assertIn("• Library cards", blocks[0].text)


class ExtraBlockTests(SimpleTestCase):
    def test_an_extra_block_lands_between_the_paragraphs_around_it(self):
        words = phrase("Before the table", 50, 100) + phrase("After the table", 50, 300)
        ruled = Block(kind="table", top=180, bottom=240, rows=[["a", "b"], ["1", "2"]])

        blocks = layout_page(words, extra_blocks=[ruled])

        self.assertEqual(
            [(b.kind, b.text) for b in blocks],
            [("text", "Before the table"), ("table", ""), ("text", "After the table")],
        )

    def test_extras_alone_are_returned_in_order(self):
        lower = Block(kind="text", top=200, bottom=210, text="lower")
        upper = Block(kind="text", top=50, bottom=60, text="upper")

        blocks = layout_page([], extra_blocks=[lower, upper])

        self.assertEqual([b.text for b in blocks], ["upper", "lower"])

    def test_an_extra_after_the_last_line_goes_last(self):
        words = phrase("Only line", 50, 100)
        extra = Block(kind="text", top=500, bottom=510, text="footer")

        blocks = layout_page(words, extra_blocks=[extra])

        self.assertEqual([b.text for b in blocks], ["Only line", "footer"])
