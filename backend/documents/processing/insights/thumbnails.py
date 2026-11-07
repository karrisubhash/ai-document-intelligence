"""A small picture of a version's first page, for cards and grids.

PDFs are rendered with pdf2image (poppler, already used by OCR) at a low DPI
- one page only, with a timeout - and images are scaled with Pillow. Word
and text files have no picture to take: the client draws a text card for
them, and no thumbnail is stored. Output is WebP when Pillow has it, PNG
otherwise, `DOCUMENT_THUMBNAIL_WIDTH` pixels wide, its height capped at
1.5x the width so a long scanned receipt does not become a banner.
"""

import io
import logging

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction

from documents import storage
from documents.models import DocumentThumbnail

logger = logging.getLogger("adi.insights")

GENERATOR = "first-page-thumbnail"
GENERATOR_VERSION = "1.0.0"
RENDER_TIMEOUT_SECONDS = 30
MAX_ASPECT = 1.5


class NoThumbnail(Exception):
    """This file type has no first page to picture (DOCX, TXT)."""


def _width():
    return max(64, min(int(getattr(settings, "DOCUMENT_THUMBNAIL_WIDTH", 320)), 1024))


def _first_page_image(path, file_type):
    from PIL import Image

    if file_type == "PDF":
        from pdf2image import convert_from_path

        dpi = max(24, min(int(getattr(settings, "DOCUMENT_THUMBNAIL_DPI", 60)), 150))
        images = convert_from_path(
            path, dpi=dpi, first_page=1, last_page=1, timeout=RENDER_TIMEOUT_SECONDS
        )
        if not images:
            raise ValueError("poppler rendered no page")
        return images[0]
    if file_type == "IMAGE":
        image = Image.open(path)
        # JPEG can decode straight at a fraction of its size.
        image.draft("RGB", (_width() * 2, _width() * 4))
        image.load()
        return image
    raise NoThumbnail(file_type or "unknown")


def _encode(image):
    from PIL import Image, features

    width = _width()
    image = image.convert("RGB")
    if image.width > width:
        height = max(1, round(image.height * width / image.width))
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    limit = round(image.width * MAX_ASPECT)
    if image.height > limit:
        image = image.crop((0, 0, image.width, limit))

    buffer = io.BytesIO()
    if features.check("webp"):
        image.save(buffer, format="WEBP", quality=80, method=4)
        return buffer.getvalue(), "image/webp", "webp", image.size
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue(), "image/png", "png", image.size


def render_thumbnail(version, file_type) -> DocumentThumbnail:
    """Render and store the thumbnail of `version`, replacing any earlier one.

    Raises NoThumbnail for types with no page image; any rendering error
    propagates for the caller to record - a thumbnail is never worth
    failing a document over, and the caller decides that.
    """
    with storage.local_copy(version.file) as path:
        image = _first_page_image(path, file_type)
    try:
        data, mime_type, extension, (width, height) = _encode(image)
    finally:
        image.close()

    previous = DocumentThumbnail.objects.filter(document_version=version).first()
    old_file = previous.file if previous and previous.file else None
    with transaction.atomic():
        thumbnail, _ = DocumentThumbnail.objects.update_or_create(
            document_version=version,
            defaults={
                "mime_type": mime_type,
                "width": width,
                "height": height,
                "source_page": 1,
                "generator": GENERATOR,
                "generator_version": GENERATOR_VERSION,
            },
        )
        # A fresh name per render, so a browser holding the old ETag never
        # gets the old bytes under a new validator, and the replaced file is
        # removed only once the new row has committed.
        thumbnail.file.save(f"page-1.{extension}", ContentFile(data), save=True)
    if old_file is not None and old_file.name != thumbnail.file.name:
        from documents.signals import remove_after_commit

        remove_after_commit(old_file)
    return thumbnail
