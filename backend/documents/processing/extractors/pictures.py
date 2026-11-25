"""Pictures embedded in Office documents: when one is worth reading.

A Word file or a slide deck often carries the real content as a picture - a
scanned form pasted into a letter, a screenshot of a table on a slide. Left
alone that content is silently absent from the document, and so from every
search and answer. But most pictures in an Office file are not documents: a
logo, a bullet, a photograph of the campus. Reading all of them would spend
the OCR budget on decoration and then store the noise Tesseract finds in a
photograph as if it were text.

So a picture is read only when it clears three bars, in order of cost:

* it is big enough to carry text (a logo is not),
* it has not been read already (the same crest on every slide is read once),
* and what comes back reads like a document - enough words, at a confidence
  Tesseract itself believes. A photograph "reads" as a word or two at low
  confidence; a scanned form reads as dozens at high.

All of it is bounded by a per-document budget, so one image-heavy file
cannot hold a worker.
"""

import hashlib
from io import BytesIO

# A picture smaller than this on either side is a bullet, a rule, a logo.
MIN_SIDE = 100

# Two words is a legitimate short caption or a total; the real filter against
# a photograph is the confidence.
MIN_WORDS = 2
MIN_CONFIDENCE = 50.0


class PictureReader:
    """Reads the pictures of one document, within a budget."""

    def __init__(self, image_ocr=None, budget: int = 0, describe=None):
        self.image_ocr = image_ocr
        # `describe(PIL.Image) -> str`: a vision model's reading of a figure
        # (chart, diagram), as text for the page, or "". Tried first; OCR is
        # what a picture gets when no model read it.
        self.describe = describe
        self.budget = budget if (image_ocr or describe) else 0
        self.seen: set = set()
        self.read = 0
        self.described = 0

    def text(self, blob: bytes) -> str:
        """The text of a picture, or "" when it is not worth keeping."""
        if self.budget <= 0:
            return ""

        digest = hashlib.sha1(blob, usedforsecurity=False).hexdigest()
        if digest in self.seen:
            return ""
        self.seen.add(digest)

        try:
            from PIL import Image

            with Image.open(BytesIO(blob)) as image:
                if min(image.size) < MIN_SIDE:
                    return ""
                picture = image.copy()
        except Exception:
            return ""  # EMF/WMF and the like: not something Pillow reads here

        self.budget -= 1
        if self.describe is not None:
            described = self.describe(picture)
            if described:
                self.described += 1
                return described
        if self.image_ocr is None:
            return ""
        page = self.image_ocr(picture)
        if page.words < MIN_WORDS:
            return ""
        if (page.confidence or 0.0) < MIN_CONFIDENCE:
            return ""

        self.read += 1
        return page.text
