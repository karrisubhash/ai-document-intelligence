"""OCR: scans, photographs, and pictures inside PDFs.

Tesseract's plain-text output has the same flaw as a PDF text layer read in
drawing order: it is a stream of words with the geometry thrown away. A blank
table cell leaves no trace, so "B.Tech ECE  [blank]  2500" comes back as
"B.Tech ECE 2500", and a scanned two-column page comes back interleaved. So
this module asks for what Tesseract actually knows - every word with its
rectangle and its own confidence - and hands those words to the same
`geometry.layout_page` that reads a PDF text layer. A scanned fee table and
a born-digital one are then read by one set of rules.

What it does to the image first, and why:

* **EXIF orientation.** A phone photo is stored sideways with a tag that says
  how to turn it; Tesseract ignores the tag.
* **Colour, transparency and bit depth** are flattened to grayscale with the
  contrast stretched. A transparent PNG is composited on WHITE: left alone,
  its transparent pixels read as black and the text vanishes into them.
* **Size.** A screenshot is small and its text a few pixels tall, which
  Tesseract cannot read; those are enlarged. A 100-megapixel photo is
  reduced, since it buys no accuracy and costs gigabytes.
* **Rotation**, but only when needed. Orientation detection takes about a
  second a page, so it runs when the first reading was poor - low
  confidence, or almost no words - and the rotated reading replaces the
  first only if it is actually better. An upright page never pays for it.
"""

import functools
from dataclasses import dataclass, field

import pytesseract
from django.conf import settings
from pdf2image import convert_from_path
from PIL import Image, ImageOps
from pytesseract import Output

from documents.processing.errors import ErrorCode, StageError
from documents.processing.extractors import compose, geometry

SOURCE = "tesseract:layout"

# Longest side, in pixels, past which an image is reduced; and below which it
# is enlarged. 4500 is a little over A4 at 400 DPI.
MAX_SIDE = 4500
SMALL_SIDE = 1200
UPSCALE = 2

# A reading under this mean word confidence, or with fewer words than this,
# is a poor one: it is tried again another way, and suspected of being a
# sideways page.
POOR_CONFIDENCE = 65.0
FEW_WORDS = 5

# Tesseract's automatic page layout is a gamble at some sizes: a dense
# landscape page, skewed and JPEG-compressed, read at 300 DPI returned
# nothing at all - not a bad reading, no words - while the same page a third
# smaller, or read as one uniform block of text, gave 400 of its 430. So a
# poor reading is retried at this fraction of the size, then as one block.
FALLBACK_SCALE = 0.67

# A word Tesseract is at least this sure of counts as read. Readings are
# compared by how many such words they have, not by how many words they
# have: a page read sideways, or as the wrong block, yields plenty of words
# - at 30-45 percent - and counting those lets junk outscore a clean reading
# with fewer words.
CONFIDENT_WORD = 60.0
FALLBACK_CONFIG = "--psm 6"

# Tesseract's orientation confidence below which it is saying nothing at all:
# ~9 on a clear page, 0.35 on a dense small-print one it still gets right.
MIN_ORIENTATION_CONFIDENCE = 0.2

# The other turns tried, in order, when Tesseract's own guess does not make
# the page read well. Upside-down last: it is the rarest and the one OSD
# most often reports as 0 rather than 180.
QUARTER_TURNS = (90, 270, 180)

# Fewer words than this and there is nothing to judge an orientation by.
MIN_WORDS_TO_TURN = 3

# Nonsense that looks like text: at least this many words, fewer than this
# share of them sound, at a mean confidence under this.
JUNK_MIN_WORDS = 10
JUNK_SOUND_SHARE = 0.5
JUNK_BELOW = 80.0

# Tesseract reads some sideways pages correctly on its own - but reports each
# word's box in the page's sideways frame, so line grouping and columns then
# come out scrambled. Words that are taller than they are wide are the tell:
# upright words are several times wider than tall.
TALL_WORD_RATIO = 1.2
TALL_WORD_SHARE = 0.6
MIN_WORDS_FOR_SHAPE = 5

# When the page was sideways, the upright reading is taken unless it is
# materially worse; when it merely read poorly, only if it is better.
SIDEWAYS_KEEPS_AT_LEAST = 0.9


@dataclass
class OcrPage:
    """One page as OCR read it."""

    text: str = ""
    tables: list = field(default_factory=list)
    # Mean word confidence, 0-100, or None when nothing was read.
    confidence: float | None = None
    # Degrees clockwise the page had to be turned to read it upright.
    rotation: int = 0
    words: int = 0
    languages: str = "eng"

    @property
    def metadata(self) -> dict:
        data = {"languages": self.languages, "words": self.words}
        if self.confidence is not None:
            data["confidence"] = round(self.confidence, 1)
        if self.rotation:
            data["rotation"] = self.rotation
        if self.tables:
            data["tables"] = len(self.tables)
        return data


@functools.cache
def tesseract_version() -> str:
    try:
        return str(pytesseract.get_tesseract_version())
    except Exception:
        return "unknown"


@functools.cache
def _installed_languages() -> frozenset:
    try:
        return frozenset(pytesseract.get_languages(config=""))
    except Exception:
        return frozenset({"eng"})


def resolve_languages(requested: str | None = None) -> str:
    """The configured packs that are installed; "eng" when none are."""
    requested = settings.OCR_LANGUAGES if requested is None else requested
    wanted = [name for name in requested.split("+") if name.strip()]
    usable = [name for name in wanted if name in _installed_languages()]
    return "+".join(usable) or "eng"


# -- the image --------------------------------------------------------------


def prepare(image: Image.Image) -> tuple:
    """(image ready for Tesseract, scale applied relative to the input)."""
    image = ImageOps.exif_transpose(image)

    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        flat = Image.new("RGBA", rgba.size, "white")
        flat.alpha_composite(rgba)
        image = flat.convert("RGB")
    elif image.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        image = image.convert("I").point(lambda value: value / 256).convert("L")

    gray = ImageOps.autocontrast(image.convert("L"), cutoff=1)

    longest = max(gray.size)
    scale = 1.0
    if longest > MAX_SIDE:
        scale = MAX_SIDE / longest
    elif longest < SMALL_SIDE:
        scale = float(UPSCALE)

    if scale != 1.0:
        size = (max(1, round(gray.width * scale)), max(1, round(gray.height * scale)))
        gray = gray.resize(size, Image.Resampling.LANCZOS)

    return gray, scale


def _recognise(image: Image.Image, languages: str, config: str = "") -> tuple:
    """([geometry.Word], mean confidence or None) for a prepared image."""
    try:
        data = pytesseract.image_to_data(
            image, lang=languages, config=config, output_type=Output.DICT
        )
    except Exception as exc:
        raise StageError(
            f"OCR failed: {exc}",
            code=ErrorCode.OCR_FAILED,
        ) from exc

    minimum = settings.OCR_MIN_WORD_CONFIDENCE
    words = []
    confidences = []

    for index, text in enumerate(data["text"]):
        text = text.strip()
        if not text:
            continue
        confidence = float(data["conf"][index])
        # -1 marks a box that is not a word; below the floor it is speckle.
        if confidence < 0 or confidence < minimum:
            continue
        left, top = data["left"][index], data["top"][index]
        words.append(
            geometry.Word(
                text=text,
                x0=left,
                x1=left + data["width"][index],
                top=top,
                bottom=top + data["height"][index],
                confidence=confidence,
            )
        )
        confidences.append(confidence)

    mean = sum(confidences) / len(confidences) if confidences else None
    return words, mean


def _poor(words: list, confidence) -> bool:
    return confidence is None or confidence < POOR_CONFIDENCE or len(words) < FEW_WORDS


def _read(image: Image.Image, languages: str) -> tuple:
    """([geometry.Word], confidence) for an image, read the ordinary way.

    One reading when it is good. When it is not - no words, few, or doubtful -
    the same image is read smaller as well, and the better is kept. Words are
    in the coordinates of the image passed in. Reading as one block of text is
    not tried here: it is the mode that turns a sideways page into confident
    noise, so it waits until the orientation has been checked.
    """
    best = _recognise(image, languages)
    if not _poor(*best):
        return best

    size = (
        max(1, round(image.width * FALLBACK_SCALE)),
        max(1, round(image.height * FALLBACK_SCALE)),
    )
    smaller, confidence = _recognise(
        image.resize(size, Image.Resampling.LANCZOS), languages
    )
    scaled = [
        geometry.Word(
            text=w.text,
            x0=w.x0 / FALLBACK_SCALE,
            x1=w.x1 / FALLBACK_SCALE,
            top=w.top / FALLBACK_SCALE,
            bottom=w.bottom / FALLBACK_SCALE,
            confidence=w.confidence,
        )
        for w in smaller
    ]
    return (scaled, confidence) if _worth(scaled, confidence) > _worth(*best) else best


def _orientation(image: Image.Image) -> int:
    """Degrees clockwise Tesseract says the page must be turned, 0 if it cannot say.

    Only a first guess. Its confidence is a poor guide: it is 0.35 on a dense,
    small-print page it gets exactly right and 1.8 on an upright one it calls
    upside down. What decides is whether the turned page then reads better.
    """
    try:
        osd = pytesseract.image_to_osd(image, output_type=Output.DICT)
    except Exception:
        # "Too few characters" and friends: nothing to judge by.
        return 0
    if float(osd.get("orientation_conf", 0)) < MIN_ORIENTATION_CONFIDENCE:
        return 0
    return int(osd.get("rotate", 0)) % 360


def _turn(image: Image.Image, clockwise: int) -> Image.Image:
    # PIL rotates counter-clockwise.
    return image.rotate(-clockwise, expand=True, fillcolor=255)


def _sound(word) -> bool:
    """A word that looks like one: three characters or more, mostly letters or
    digits, and one Tesseract is confident of. A page read the wrong way up or
    the wrong way round yields plenty of one-character tokens - bars, equals
    signs, stray letters - that it is surprisingly sure of."""
    text = word.text
    return (
        len(text) >= 3
        and sum(char.isalnum() for char in text) >= 0.6 * len(text)
        and word.confidence >= CONFIDENT_WORD
    )


def _worth(words: list, confidence) -> float:
    """One number to compare two readings of the same page by.

    How many sound words it has, with mean confidence as the tie-break (it is
    at most 100, so it can never outweigh a whole word).
    """
    return sum(1 for word in words if _sound(word)) + (confidence or 0.0) / 1000.0


def _suspect(words: list, confidence) -> bool:
    """Whether a reading looks wrong enough to try turning the page.

    Not when there is next to nothing to read: a blank page - the back of a
    sheet - has no orientation, and turning it four ways would cost a dozen
    recognition passes to learn that. Nor a sparse page read confidently, such
    as a cover or a signature sheet.

    Otherwise: doubtful by confidence; or shaped like sideways text; or
    plausible-looking nonsense - plenty of words, but mostly one or two
    characters, at a confidence that is middling rather than high. A numeric
    table is mostly short tokens too, but Tesseract reads it at high
    confidence, so it is not suspected.
    """
    if len(words) < MIN_WORDS_TO_TURN:
        return False
    if (confidence or 0.0) < POOR_CONFIDENCE or _looks_sideways(words):
        return True
    if len(words) < JUNK_MIN_WORDS:
        return False
    sound = sum(1 for word in words if _sound(word))
    return sound < JUNK_SOUND_SHARE * len(words) and (confidence or 0.0) < JUNK_BELOW


def _looks_sideways(words: list) -> bool:
    """Whether the word boxes say the text runs up or down the page."""
    long_words = [w for w in words if len(w.text) >= 3 and w.height > 0]
    if len(long_words) < MIN_WORDS_FOR_SHAPE:
        return False
    tall = sum(
        1 for w in long_words if (w.bottom - w.top) > TALL_WORD_RATIO * (w.x1 - w.x0)
    )
    return tall >= TALL_WORD_SHARE * len(long_words)


def read_page(image: Image.Image, languages: str | None = None) -> OcrPage:
    """Read one page image: prepare it, OCR it, lay the words out.

    In order, stopping as soon as a reading is good: the ordinary reading, that
    again smaller; then, if it looks wrong, the page turned - the way Tesseract
    guesses first, then the other quarter turns - keeping a turn only if the
    page reads better for it; and last, the page read as one uniform block.
    """
    languages = resolve_languages(languages)
    prepared, _ = prepare(image)

    words, confidence = _read(prepared, languages)
    rotation = 0
    upright = prepared

    sideways = _looks_sideways(words)
    if settings.OCR_DETECT_ROTATION and _suspect(words, confidence):
        guess = _orientation(prepared)
        for turn in dict.fromkeys([guess, *QUARTER_TURNS]):
            if not turn or turn == rotation:
                continue
            rotated = _turn(prepared, turn)
            again, again_confidence = _read(rotated, languages)
            before = _worth(words, confidence)
            after = _worth(again, again_confidence)
            # A page that was plainly sideways takes the upright reading unless
            # it is materially worse; otherwise it must simply be better.
            keep = after > before or (
                sideways and after >= SIDEWAYS_KEEPS_AT_LEAST * before
            )
            if keep:
                words, confidence, rotation = again, again_confidence, turn
                upright = rotated
                sideways = False
            if not _suspect(words, confidence):
                break

    if _poor(words, confidence):
        block, block_confidence = _recognise(upright, languages, FALLBACK_CONFIG)
        if _worth(block, block_confidence) > _worth(words, confidence):
            words, confidence = block, block_confidence

    return _compose(words, confidence, rotation, languages)


def _compose(words, confidence, rotation, languages) -> OcrPage:
    builder = compose.PageBuilder(source=SOURCE, source_version=tesseract_version())

    for block in geometry.layout_page(words):
        if block.kind == "table":
            builder.add_table(block.rows)
        else:
            builder.add_text(block.text)

    text, tables = builder.build()
    return OcrPage(
        text=text,
        tables=tables,
        confidence=confidence,
        rotation=rotation,
        words=len(words),
        languages=languages,
    )


def read_words(image: Image.Image, languages: str | None = None) -> list:
    """Words of a picture, in the picture's OWN pixel coordinates.

    The hook the PDF extractor uses for a picture inside a page. Prepared
    like any page, but never rotated: a picture inside a page is upright with
    the page. Coordinates are scaled back, since `prepare` may have enlarged
    a small picture and the caller maps them onto the page by the size it
    passed in.
    """
    prepared, scale = prepare(image)
    words, _ = _read(prepared, resolve_languages(languages))
    if scale == 1.0:
        return words
    return [
        geometry.Word(
            text=word.text,
            x0=word.x0 / scale,
            x1=word.x1 / scale,
            top=word.top / scale,
            bottom=word.bottom / scale,
            confidence=word.confidence,
        )
        for word in words
    ]


# -- files ------------------------------------------------------------------


def image_frame_count(file_path: str) -> int:
    """Pages in an image file: more than one for a multi-page TIFF.

    An image Pillow cannot open counts as one page rather than raising: the
    stage that actually reads it reports the failure, with the same error code
    a corrupt image has always had, instead of it surfacing from a counting
    step earlier in the pipeline.
    """
    try:
        with Image.open(file_path) as image:
            return max(1, int(getattr(image, "n_frames", 1)))
    except Exception:
        return 1


def open_frame(file_path: str, frame: int = 0):
    """One frame of an image file as a PIL image, or None when unreadable."""
    try:
        with Image.open(file_path) as image:
            if frame:
                image.seek(frame)
            return image.copy()
    except Exception:
        return None


def ocr_image_file(file_path: str, frame: int = 0) -> OcrPage:
    try:
        with Image.open(file_path) as image:
            if frame:
                image.seek(frame)
            page = image.copy()
    except Exception as exc:
        raise StageError(
            f"OCR failed for image: {exc}",
            code=ErrorCode.OCR_FAILED,
        ) from exc
    return read_page(page)


def ocr_pdf_page(file_path: str, page_number: int, dpi: int) -> OcrPage:
    """Render a single PDF page and OCR it.

    Rendered one page at a time on purpose - converting a whole scanned
    document at once is what makes OCR memory usage blow up.
    """
    try:
        images = convert_from_path(
            file_path,
            dpi=dpi,
            first_page=page_number,
            last_page=page_number,
        )
    except Exception as exc:
        raise StageError(
            f"Could not render page {page_number} for OCR: {exc}",
            code=ErrorCode.OCR_FAILED,
        ) from exc

    if not images:
        raise StageError(
            f"No rendered image produced for page {page_number}.",
            code=ErrorCode.OCR_FAILED,
        )

    try:
        with images[0] as image:
            return read_page(image)
    except StageError:
        raise
    except Exception as exc:
        raise StageError(
            f"OCR failed for page {page_number}: {exc}",
            code=ErrorCode.OCR_FAILED,
        ) from exc
