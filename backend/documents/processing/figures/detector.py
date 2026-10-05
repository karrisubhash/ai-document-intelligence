"""Figure detection: the content that is not text at all (§11.3, phase 12).

A scanned chart, a signature block, a campus map and a photograph are all
"an image on page 4" to this pipeline. OCR reads the words inside them when
there are words; a bar chart with no labels leaves nothing behind at all,
and the document silently loses whatever it was showing.

What this records is deliberately a *fact*, not an interpretation: page 4 of
this file embeds two images, of these pixel dimensions. It does not say
"chart", because nothing here can tell a chart from a photograph, and a
`kind` field reading CHART would be a guess that every downstream reader
would take for a measurement.

The value of recording the fact is what the RAG engine does with it. When a
question cannot be answered from the text, "I could not find that" and "I
could not find that, and there is an image on the page your question is
about which I cannot read" are very different answers. Only one of them
tells the reader where to look.

Bounded on purpose: the image XObject dictionary is inspected, never
decoded. Decoding is where a hostile PDF gets to allocate memory, and the
width and height are in the dictionary anyway.
"""

from dataclasses import dataclass, field

from celery.exceptions import SoftTimeLimitExceeded

DETECTOR_NAME = "embedded-image"
DETECTOR_VERSION = "v1"

# Per page. A PDF that maps every glyph to its own image can declare
# thousands; past this the count is recorded as a lower bound rather than
# walked to the end, because the interesting fact ("there are images here")
# is established long before.
MAX_FIGURES_PER_PAGE = 50

# Smaller than this is furniture - a rule, a bullet, a logo fragment - not
# something a reader would call a figure.
MIN_DIMENSION = 32


@dataclass(frozen=True)
class DetectedFigure:
    """One embedded image, and where it is."""

    page_number: int
    figure_index: int
    width: int = 0
    height: int = 0
    # What the image is OF. Empty unless a describer is configured; this
    # pipeline ships none, and an empty description is the honest record of
    # that. See figures/service.py.
    description: str = ""
    description_source: str = ""
    metadata: dict = field(default_factory=dict)


def _image_xobjects(page):
    """Image XObjects declared by a page, without decoding any of them."""
    try:
        resources = page.get("/Resources")
        if resources is None:
            return []
        xobjects = resources.get_object().get("/XObject")
        if xobjects is None:
            return []
        xobjects = xobjects.get_object()
    except SoftTimeLimitExceeded:
        # Swallowed with the rest, the task would run on to the HARD limit,
        # be killed, and leave the job PROCESSING until someone noticed.
        raise
    except Exception:
        # A malformed resource dictionary means "no figures we can vouch
        # for", not a failed document. The text extraction that matters has
        # already succeeded by this point.
        return []

    found = []
    for name in list(xobjects.keys())[:MAX_FIGURES_PER_PAGE]:
        try:
            obj = xobjects[name].get_object()
            if obj.get("/Subtype") != "/Image":
                continue
            found.append(
                (
                    int(obj.get("/Width") or 0),
                    int(obj.get("/Height") or 0),
                )
            )
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            continue

    return found


def detect_pdf_figures(file_path: str, open_reader) -> list:
    """Every embedded image in a PDF, in page order.

    `open_reader` is injected rather than imported so this module stays
    free of the extractor package, and so a test can drive it without a
    file on disk.
    """
    reader = open_reader(file_path)
    figures = []

    for page_number, page in enumerate(reader.pages, start=1):
        index = 0
        for width, height in _image_xobjects(page):
            if width < MIN_DIMENSION or height < MIN_DIMENSION:
                continue
            figures.append(
                DetectedFigure(
                    page_number=page_number,
                    figure_index=index,
                    width=width,
                    height=height,
                )
            )
            index += 1

    return figures
