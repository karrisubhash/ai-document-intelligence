"""Build the sample library's PDFs and its scanned notice, reproducibly.

    docker run --rm -v <repo>:/repo -w /repo/backend adi-rel-ci-env:b77a9ca \
        python -I documents/samples/build.py

Writes into documents/samples/corpus/. The files are committed - seeding
reads them, never this script - and running it again produces the same
bytes: no dates, no random ids, zlib at a fixed level, and a seeded random
source for the scan's noise. Re-run it only to change a sample, and commit
what it writes.

Synthetic content, written for the samples - not any real institution's
documents. The facts agree with the text samples copied from the evaluation
corpus (75 percent attendance, revaluation within 15 days, a July
supplementary examination...), so a question can be answered from several
documents and the answer still holds together.

Why a hand-written PDF writer: the image has pypdf (which reads) and Pillow
(which draws pictures), but nothing that lays out text. Fifty lines of PDF
objects with the standard Type 1 fonts - which every reader has, so nothing
is embedded - give real text layers that the pipeline extracts, chunks and
cites. Tables are set in Courier, so their columns line up as runs of spaces
in the extracted text - which is exactly what the layout table detector
(documents.processing.tables.detector) recognises.
"""

import random
import sys
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent / "corpus"

PAGE_WIDTH, PAGE_HEIGHT = 595.28, 841.89  # A4, in points
LEFT = 66.0
RIGHT = PAGE_WIDTH - 66.0
TOP = 742.0
BOTTOM = 84.0

INSTITUTION = "Institute of Technology"

# Advance widths per 1000 units of the Adobe core fonts (from their AFM
# files), printable ASCII from the space onwards. Used only to wrap lines.
_HELVETICA = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 556 556 "
    "556 556 556 556 556 556 556 556 278 278 584 584 584 556 1015 667 667 722 "
    "722 667 611 778 722 278 500 667 556 833 722 778 667 778 722 667 611 722 "
    "667 944 667 667 611 278 278 278 469 556 333 556 556 500 556 556 278 556 "
    "556 222 222 500 222 833 556 556 556 556 333 500 278 556 500 722 500 500 "
    "500 334 260 334 584"
)
_HELVETICA_BOLD = (
    "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 556 556 "
    "556 556 556 556 556 556 556 556 333 333 584 584 584 611 975 722 722 722 "
    "722 667 611 778 722 278 556 722 611 833 722 778 667 778 722 667 611 722 "
    "667 944 667 667 611 333 278 333 584 556 333 556 611 556 611 556 333 611 "
    "611 278 278 556 278 889 611 611 611 611 389 556 333 611 556 778 556 556 "
    "500 389 280 389 584"
)
_EXTRA = {"–": 556, "•": 350, "’": 222, "‘": 222, "“": 333, "”": 333, "·": 278}


def _table(spec: str) -> dict:
    return {chr(32 + i): int(w) for i, w in enumerate(spec.split())}


WIDTHS = {
    "F1": _table(_HELVETICA),
    "F2": _table(_HELVETICA_BOLD),
    "F3": None,  # Courier: every glyph is 600
    "F4": _table(_HELVETICA),  # Helvetica-Oblique has Helvetica's widths
}
FONTS = {
    "F1": "Helvetica",
    "F2": "Helvetica-Bold",
    "F3": "Courier",
    "F4": "Helvetica-Oblique",
}


def width(text: str, font: str, size: float) -> float:
    table = WIDTHS[font]
    if table is None:
        return 600 * len(text) * size / 1000
    return sum(table.get(ch, _EXTRA.get(ch, 556)) for ch in text) * size / 1000


def wrap(text: str, font: str, size: float, room: float) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if width(candidate, font, size) <= room or not line:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _escape(text: str) -> bytes:
    raw = text.encode("cp1252")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _info_string(text: str) -> bytes:
    """A string for the document information dictionary, which is not in
    the fonts' WinAnsi encoding: plain when ASCII, UTF-16 with a byte-order
    mark otherwise, so a viewer's title bar shows the en dash."""
    if text.isascii():
        return b"(" + _escape(text) + b")"
    return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode("ascii") + b">"


class Canvas:
    """One page's drawing operations, in PDF content-stream syntax."""

    def __init__(self):
        self.ops: list[bytes] = []

    def text(self, x, y, value, font="F1", size=10.5, grey=0.12):
        self.ops.append(
            b"BT %.3f g /%s %.2f Tf 1 0 0 1 %.2f %.2f Tm ("
            % (grey, font.encode(), size, x, y)
            + _escape(value)
            + b") Tj ET"
        )

    def rule(self, x1, y, x2, grey=0.75, weight=0.6):
        self.ops.append(
            b"%.3f G %.2f w %.2f %.2f m %.2f %.2f l S" % (grey, weight, x1, y, x2, y)
        )

    def band(self, y, height, rgb):
        self.ops.append(
            b"%.3f %.3f %.3f rg %.2f %.2f %.2f %.2f re f"
            % (*rgb, 0.0, y, PAGE_WIDTH, height)
        )


class Document:
    """Lays blocks out onto pages, then writes the PDF.

    Blocks: ("title", text) ("lead", text) ("h", text) ("p", text)
    ("bullets", [text]) ("table", caption, header, rows) ("note", text)
    ("break",).
    """

    def __init__(self, title: str, code: str, blocks: list):
        self.title = title
        self.code = code
        self.blocks = blocks
        self.pages: list[Canvas] = []
        self.y = TOP

    # --- layout -------------------------------------------------------------

    def _page(self) -> Canvas:
        self.pages.append(Canvas())
        self.y = TOP
        return self.pages[-1]

    def _room(self, needed: float) -> Canvas:
        if not self.pages or self.y - needed < BOTTOM:
            return self._page()
        return self.pages[-1]

    def layout(self):
        for block in self.blocks:
            kind = block[0]
            if kind == "break":
                self._page()
            elif kind == "title":
                page = self._room(60)
                for line in wrap(block[1], "F2", 21, RIGHT - LEFT):
                    page.text(LEFT, self.y, line, "F2", 21, grey=0.08)
                    self.y -= 27
                self.y -= 4
            elif kind == "lead":
                for line in wrap(block[1], "F1", 11.5, RIGHT - LEFT):
                    page = self._room(17)
                    page.text(LEFT, self.y, line, "F1", 11.5, grey=0.32)
                    self.y -= 17
                self.y -= 10
            elif kind == "h":
                page = self._room(46)
                self.y -= 10
                page.text(LEFT, self.y, block[1], "F2", 13)
                self.y -= 8
                page.rule(LEFT, self.y, RIGHT, grey=0.82, weight=0.5)
                self.y -= 17
            elif kind == "p":
                for line in wrap(block[1], "F1", 10.5, RIGHT - LEFT):
                    page = self._room(15)
                    page.text(LEFT, self.y, line, "F1", 10.5)
                    self.y -= 15
                self.y -= 7
            elif kind == "bullets":
                for item in block[1]:
                    lines = wrap(item, "F1", 10.5, RIGHT - LEFT - 16)
                    for index, line in enumerate(lines):
                        page = self._room(15)
                        if index == 0:
                            page.text(LEFT + 3, self.y, "•", "F1", 10.5)
                        page.text(LEFT + 16, self.y, line, "F1", 10.5)
                        self.y -= 15
                    self.y -= 3
                self.y -= 6
            elif kind == "table":
                self._table(*block[1:])
            elif kind == "note":
                for line in wrap(block[1], "F4", 9.5, RIGHT - LEFT):
                    page = self._room(14)
                    page.text(LEFT, self.y, line, "F4", 9.5, grey=0.35)
                    self.y -= 14
                self.y -= 8
            else:  # pragma: no cover - a typo in the content below
                raise ValueError(kind)

    def _table(self, caption: str, header: list, rows: list):
        columns = [
            max(len(str(row[i])) for row in [header, *rows]) for i in range(len(header))
        ]

        def line(cells):
            # Three spaces between columns: a corridor the detector needs at
            # least two wide, in every row.
            return "   ".join(
                str(cell).ljust(columns[i]) for i, cell in enumerate(cells)
            ).rstrip()

        size = 9.5
        needed = 36 + 14 * (len(rows) + 1)
        page = self._room(min(needed, TOP - BOTTOM))
        page.text(LEFT, self.y, caption, "F2", 10.5)
        self.y -= 16
        page.rule(LEFT, self.y + 10, RIGHT, grey=0.55, weight=0.8)
        page.text(LEFT, self.y, line(header), "F3", size, grey=0.08)
        self.y -= 5
        page.rule(LEFT, self.y, RIGHT, grey=0.7)
        self.y -= 12
        for row in rows:
            page = self._room(14)
            page.text(LEFT, self.y, line(row), "F3", size)
            self.y -= 14
        page.rule(LEFT, self.y + 9, RIGHT, grey=0.55, weight=0.8)
        self.y -= 14

    # --- output -------------------------------------------------------------

    def _decorate(self):
        total = len(self.pages)
        for number, page in enumerate(self.pages, start=1):
            # Drawn in reading order - running head, the page, then the
            # footer - because text is extracted in drawing order.
            head = Canvas()
            head.band(PAGE_HEIGHT - 6, 6, (0.31, 0.27, 0.9))
            head.text(LEFT, PAGE_HEIGHT - 40, INSTITUTION.upper(), "F2", 8.5, grey=0.3)
            head.text(
                RIGHT - width(self.code, "F1", 8.5),
                PAGE_HEIGHT - 40,
                self.code,
                "F1",
                8.5,
                grey=0.45,
            )
            head.rule(LEFT, PAGE_HEIGHT - 50, RIGHT, grey=0.85)
            foot = Canvas()
            foot.rule(LEFT, 58, RIGHT, grey=0.85)
            foot.text(
                LEFT,
                44,
                f"{self.title} · Page {number} of {total}",
                "F1",
                8.5,
                grey=0.45,
            )
            page.ops = head.ops + page.ops + foot.ops

    def write(self, path: Path):
        self.layout()
        self._decorate()

        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        catalog = add(b"")  # filled in below
        pages_id = add(b"")
        fonts = {
            name: add(
                b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>"
                % base.encode()
            )
            for name, base in FONTS.items()
        }
        font_dict = b" ".join(b"/%s %d 0 R" % (n.encode(), i) for n, i in fonts.items())
        kids = []
        for page in self.pages:
            stream = zlib.compress(b"\n".join(page.ops), 9)
            content = add(
                b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream)
                + stream
                + b"\nendstream"
            )
            kids.append(
                add(
                    b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] "
                    b"/Resources << /Font << %s >> >> /Contents %d 0 R >>"
                    % (pages_id, PAGE_WIDTH, PAGE_HEIGHT, font_dict, content)
                )
            )
        objects[pages_id - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
            b" ".join(b"%d 0 R" % kid for kid in kids),
            len(kids),
        )
        objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
        info = add(
            b"<< /Title %s /Author %s /Producer (Document Intelligence sample library) >>"
            % (
                _info_string(self.title),
                _info_string(f"{INSTITUTION} (synthetic sample)"),
            )
        )

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        for offset in offsets:
            out += b"%010d 00000 n \n" % offset
        out += (
            b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (
                len(objects) + 1,
                catalog,
                info,
                xref,
            )
        )
        path.write_bytes(bytes(out))
        sys.stdout.write(
            f"wrote {path.name}: {len(self.pages)} pages, {len(out)} bytes\n"
        )


# --- The documents ----------------------------------------------------------------

HANDBOOK = Document(
    "Student Handbook 2025–26",
    "DSW/HB/2025-26",
    [
        ("title", "Student Handbook 2025–26"),
        (
            "lead",
            "Office of the Dean of Students. What every student needs to know about "
            "studying, examinations, campus services and support, in one place.",
        ),
        ("h", "About this handbook"),
        (
            "p",
            "This handbook summarises the rules that matter most in day-to-day student "
            "life. It does not replace them: where it differs from the Academic "
            "Regulations, the Examination Policy or any other regulation of the "
            "institute, the regulation prevails. Each section names the document to "
            "read for the full rule.",
        ),
        (
            "p",
            "The handbook is revised every July. Changes made during the year are "
            "announced by circular and take effect from the date the circular gives.",
        ),
        ("h", "Key offices"),
        (
            "bullets",
            [
                "Office of the Dean of Students, Administrative Block, ground floor - "
                "student welfare, grievances and the counselling centre.",
                "Office of the Registrar - registration, transcripts, certificates and "
                "the academic calendar.",
                "Controller of Examinations - examination schedules, hall tickets, "
                "results and revaluation.",
                "Office of the Bursar - fees, refunds and the late payment surcharge.",
                "Training and Placement Cell - registration for campus placement and "
                "internship credit.",
            ],
        ),
        ("h", "The year at a glance"),
        (
            "p",
            "The odd semester runs from August to December and the even semester from "
            "January to May. Supplementary examinations are held once a year, in July. "
            "Exact dates for registration, examinations and results are published in "
            "the Academic Calendar 2025–26.",
        ),
        ("break",),
        ("h", "Registration and credits"),
        (
            "p",
            "A full-time undergraduate registers for between 18 and 26 credits in each "
            "semester. Courses may be added or dropped until the tenth instructional "
            "day; after that date a course can only be withdrawn with the approval of "
            "the head of department, and it is shown on the transcript as withdrawn.",
        ),
        ("h", "Attendance"),
        (
            "p",
            "A student must maintain a minimum of 75 percent attendance in each course "
            "to be eligible for the end-semester examination. A student whose "
            "attendance is between 65 and 75 percent may apply for condonation on "
            "medical grounds, with a certificate from a registered practitioner. Below "
            "65 percent, condonation is not possible (Academic Regulations, clauses 4.2 "
            "and 4.3).",
        ),
        (
            "p",
            "Scholarship holders should note that their scholarship carries a higher "
            "attendance condition of 85 percent, and that condonation for the "
            "examination does not restore a scholarship.",
        ),
        ("h", "Examinations"),
        (
            "bullets",
            [
                "Carry your institute identity card into every examination hall. Entry "
                "is refused without it.",
                "A candidate found using unauthorised material is debarred from all "
                "examinations of that semester.",
                "A result can be challenged by applying for revaluation within 15 days "
                "of its publication, with the prescribed fee.",
                "A student who secures less than 40 percent in the end-semester "
                "examination fails the course, whatever their internal marks.",
            ],
        ),
        ("break",),
        ("h", "Library"),
        (
            "p",
            "An undergraduate may borrow up to four books at a time and a postgraduate "
            "up to six. The late return fine is 5 rupees per day per book, and "
            "borrowing is suspended once fines exceed 500 rupees. During the "
            "end-semester examinations the main reading room stays open from 8 am to "
            "midnight; the Librarian announces the exact dates by notice.",
        ),
        ("h", "Hostel"),
        (
            "p",
            "The hostel curfew is 10 pm on weekdays and 11 pm on Saturdays. Visitors "
            "are allowed in the common room only, between 4 pm and 7 pm. A resident "
            "who returns late on a campus bus delayed by an institute event is not "
            "marked late.",
        ),
        ("h", "Transport"),
        (
            "p",
            "Campus buses run on six city routes. A bus pass costs 4500 rupees per "
            "semester and must be shown with the identity card when boarding. A lost "
            "pass is replaced for 200 rupees.",
        ),
        ("h", "Computing"),
        (
            "p",
            "Every student account has a monthly data allowance of 50 gigabytes and a "
            "printing allowance of 200 pages per semester. Never share your password: "
            "the Computer Centre will never ask for it by email.",
        ),
        ("break",),
        ("h", "Wellbeing and support"),
        (
            "p",
            "The counselling centre offers free and confidential appointments to every "
            "student, usually within two working days of a request. In an emergency, "
            "the centre's duty counsellor can be reached through the security office "
            "at any hour.",
        ),
        (
            "p",
            "A grievance about assessment, accommodation, harassment or the conduct of "
            "staff is submitted in writing to the Dean of Students within 30 days of "
            "the incident. The office acknowledges it within 3 working days and "
            "appoints a committee within 10 working days. An appeal against the "
            "committee's decision lies to the Director within 15 days.",
        ),
        ("h", "Placements and internships"),
        (
            "p",
            "Register with the Training and Placement Cell before 15 August of your "
            "final year; registration is not automatic. A student with an aggregate "
            "below 60 percent, or with a pending backlog, is not eligible for campus "
            "placement. A summer internship of at least 8 weeks may be credited "
            "towards the degree.",
        ),
        (
            "note",
            "Synthetic sample document, written for demonstration. Every rule it "
            "summarises is stated in full in the regulation it names.",
        ),
    ],
)

CALENDAR = Document(
    "Academic Calendar 2025–26",
    "REG/CAL/2025-26",
    [
        ("title", "Academic Calendar 2025–26"),
        (
            "lead",
            "Office of the Registrar. Dates for registration, teaching, examinations "
            "and results in both semesters of the academic year 2025–26.",
        ),
        (
            "p",
            "The calendar is approved by the Academic Council. A change to any date "
            "below is announced by circular, which then supersedes this calendar for "
            "that date.",
        ),
        (
            "table",
            "Table 1. Semester dates",
            ["Event", "Week", "Odd semester", "Even semester"],
            [
                ["Registration", "0", "28 Jul 2025", "2 Jan 2026"],
                ["Classes begin", "1", "4 Aug 2025", "5 Jan 2026"],
                ["Last day to add or drop", "2", "14 Aug 2025", "15 Jan 2026"],
                ["Mid-semester examinations", "8", "22 Sep 2025", "23 Feb 2026"],
                ["Last instructional day", "15", "14 Nov 2025", "17 Apr 2026"],
                ["End-semester examinations", "16", "20 Nov 2025", "24 Apr 2026"],
                ["Results published", "20", "22 Dec 2025", "29 May 2026"],
            ],
        ),
        (
            "note",
            "Week counts teaching weeks from the first day of classes; week 0 is "
            "registration week.",
        ),
        (
            "p",
            "Applications for revaluation close 15 days after results are published: "
            "on 6 January 2026 for the odd semester and on 13 June 2026 for the even "
            "semester. Supplementary examinations for both semesters are held in July "
            "2026.",
        ),
        (
            "p",
            "Attendance is computed up to the last instructional day of each semester. "
            "Students below 75 percent attendance on that day are not eligible for the "
            "end-semester examination unless condonation is granted.",
        ),
        ("break",),
        (
            "table",
            "Table 2. Institute holidays",
            ["Holiday", "Date", "Day", "Days closed"],
            [
                ["Independence Day", "15 Aug 2025", "Friday", "1"],
                ["Gandhi Jayanti", "2 Oct 2025", "Thursday", "1"],
                ["Diwali", "20 Oct 2025", "Monday", "2"],
                ["Christmas", "25 Dec 2025", "Thursday", "1"],
                ["Republic Day", "26 Jan 2026", "Monday", "1"],
                ["Holi", "4 Mar 2026", "Wednesday", "1"],
            ],
        ),
        (
            "p",
            "Laboratories and the central workshop close on institute holidays. The "
            "library's reading room remains open on holidays that fall during the "
            "end-semester examinations.",
        ),
        (
            "table",
            "Table 3. Fee payment deadlines",
            ["Instalment", "Due by", "Surcharge per week (Rs)"],
            [
                ["Odd semester tuition", "31 Jul 2025", "500"],
                ["Even semester tuition", "31 Dec 2025", "500"],
                ["Hostel, full year", "31 Jul 2025", "500"],
            ],
        ),
        (
            "note",
            "The late payment surcharge applies for each week, or part of a week, "
            "after the due date. Synthetic sample document, written for "
            "demonstration.",
        ),
    ],
)

SAFETY = Document(
    "Laboratory Safety Manual",
    "MECH/LAB/SAFETY-3",
    [
        ("title", "Laboratory Safety Manual"),
        (
            "lead",
            "Department of Mechanical Engineering. Rules for every person who works in "
            "the department's laboratories and the central workshop, edition 3.",
        ),
        ("h", "1. Before you start"),
        (
            "bullets",
            [
                "Complete the laboratory safety induction before your first session. "
                "The induction is valid for one academic year.",
                "Wear closed shoes and a laboratory coat at all times; tie back long "
                "hair. Safety glasses are compulsory at every machine and bench.",
                "Never work alone. At least two people must be present in a laboratory "
                "whenever a machine is running.",
                "Food and drink are not allowed in any laboratory.",
            ],
        ),
        ("h", "2. Machines and tools"),
        (
            "p",
            "Only operate a machine you have been trained on, and only with the "
            "laboratory in-charge's permission. Before switching on, check that every "
            "guard is in place and that the emergency stop works. Never leave a "
            "running machine unattended, and never remove swarf by hand - use the "
            "brush provided.",
        ),
        (
            "p",
            "Report damaged tools and machine faults to the laboratory in-charge "
            "immediately. A machine with a fault is tagged out of service and may not "
            "be used until it is repaired and the tag is removed by the in-charge.",
        ),
        ("h", "3. Chemicals and fluids"),
        (
            "p",
            "Cutting fluids, solvents and adhesives are kept in the labelled cabinet "
            "and returned after use. Read the safety data sheet before using any "
            "chemical for the first time. Spills are cleaned at once with the spill "
            "kit beside the cabinet; spills larger than one litre are reported.",
        ),
        ("break",),
        ("h", "4. Fire"),
        (
            "p",
            "Raise the alarm first, then fight a fire only if it is small and you have "
            "been trained to use the extinguisher. Use the right extinguisher for the "
            "fire - water on an electrical fire is dangerous.",
        ),
        (
            "table",
            "Table 1. Choosing an extinguisher",
            ["Class", "What is burning", "Use"],
            [
                ["A", "Paper, wood, textiles", "Water or foam"],
                ["B", "Oils, solvents, fuels", "Foam or CO2"],
                ["C", "Gases", "Dry powder"],
                ["E", "Electrical equipment", "CO2 only"],
            ],
        ),
        ("h", "5. Accidents and incidents"),
        (
            "p",
            "Every accident, injury or near miss is reported to the laboratory "
            "in-charge within 24 hours on the incident form, however minor it seems. "
            "First-aid kits are at the entrance of every laboratory; the eye-wash "
            "station is beside the chemical cabinet.",
        ),
        (
            "table",
            "Table 2. Emergency contacts",
            ["Service", "Extension", "Hours"],
            [
                ["Security office", "2100", "24 hours"],
                ["Medical centre", "2155", "24 hours"],
                ["Laboratory in-charge", "3412", "8 am - 6 pm"],
                ["Estate maintenance", "2230", "8 am - 8 pm"],
            ],
        ),
        ("break",),
        ("h", "6. After you finish"),
        (
            "bullets",
            [
                "Switch off and isolate every machine you used, and clean the bed and "
                "the floor around it.",
                "Return tools to the tool room and sign them back in.",
                "Dispose of oily rags in the metal bin with a lid, never in the "
                "ordinary waste.",
                "Sign the laboratory register on leaving.",
            ],
        ),
        ("h", "7. Breaches"),
        (
            "p",
            "A student who breaches these rules may be asked to leave the laboratory "
            "for the rest of the session, which is then marked absent. A repeated or "
            "serious breach is reported to the head of department.",
        ),
        (
            "note",
            "Synthetic sample document, written for demonstration.",
        ),
    ],
)


# --- The scanned notice -------------------------------------------------------------

NOTICE = [
    ("INSTITUTE OF TECHNOLOGY", 46, "center"),
    ("OFFICE OF THE LIBRARIAN", 34, "center"),
    ("", 20, None),
    ("Notice No. LIB/2025/31                    Date: 10 November 2025", 30, "left"),
    ("", 24, None),
    ("EXTENDED READING-ROOM HOURS", 38, "center"),
    ("DURING THE END-SEMESTER EXAMINATIONS", 38, "center"),
    ("", 30, None),
    ("From 17 November to 6 December 2025 the main reading", 31, "left"),
    ("room of the Central Library will remain open from", 31, "left"),
    ("8 am to midnight on all days, including Sundays and", 31, "left"),
    ("institute holidays.", 31, "left"),
    ("", 18, None),
    ("The borrowing and return counters close at 8 pm as", 31, "left"),
    ("usual. Reference volumes and bound journals may not", 31, "left"),
    ("be removed from the library at any time.", 31, "left"),
    ("", 18, None),
    ("Students must carry the institute identity card and", 31, "left"),
    ("show it at the entrance after 8 pm.", 31, "left"),
    ("", 70, None),
    ("Librarian", 31, "right"),
    ("Central Library", 28, "right"),
]


def build_scan(path: Path):
    """A notice printed, signed and scanned slightly askew - no text layer,
    so its words reach the index only through OCR. Needs Pillow >= 10.1."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    width, height = 1240, 1754  # A4 at 150 dpi
    page = Image.new("L", (width, height), 250)
    draw = ImageDraw.Draw(page)
    y = 150
    for text, size, align in NOTICE:
        if not text:
            y += size
            continue
        font = ImageFont.load_default(size=size)
        line_width = draw.textlength(text, font=font)
        x = {
            "center": (width - line_width) / 2,
            "right": width - 150 - line_width,
        }.get(align, 150)
        draw.text((x, y), text, fill=22, font=font)
        y += int(size * 1.55)
        if text == "OFFICE OF THE LIBRARIAN":
            draw.line((150, y + 4, width - 150, y + 4), fill=60, width=3)
            y += 18

    # A signature's scrawl above "Librarian", and the speckle of a scanner.
    noise = random.Random(31)
    points = [(width - 330 + i * 9, 1290 + noise.randint(-14, 14)) for i in range(19)]
    draw.line(points, fill=40, width=3)
    for _ in range(900):
        x, y = noise.randrange(width), noise.randrange(height)
        draw.point((x, y), fill=noise.randrange(120, 200))

    page = page.rotate(0.45, resample=Image.Resampling.BICUBIC, fillcolor=250)
    page = page.filter(ImageFilter.GaussianBlur(0.45))
    page.save(path, format="PNG", optimize=True, dpi=(150, 150))
    sys.stdout.write(f"wrote {path.name}: {path.stat().st_size} bytes\n")


def main():
    OUT.mkdir(exist_ok=True)
    HANDBOOK.write(OUT / "student-handbook-2025-26.pdf")
    CALENDAR.write(OUT / "academic-calendar-2025-26.pdf")
    SAFETY.write(OUT / "laboratory-safety-manual.pdf")
    build_scan(OUT / "library-notice-scan.png")


if __name__ == "__main__":
    main()
