"""Reading figures: what a chart, a flowchart or a photographed diagram SHOWS.

OCR reads the words inside a picture and loses everything else: which bar a
number belongs to, which box an arrow leaves, which branch says "No". A
question about a figure ("how many students were admitted in 2023?", "what
happens if the documents are not verified?") then has nothing to be answered
from. A vision model can say what the figure shows, so this module asks one
and stores its reading as text the rest of the pipeline treats like any
other: it is chunked, embedded, searched, quoted and cited.

WHAT IS STORED. The model's reading goes into the page text between two
markers (BEGIN/END below), so a reader of the page, a passage shown in chat
and the answering model can all see that it is a machine description of a
picture and not words the document printed. The DocumentFigure row records
which model wrote it.

WHAT IT COSTS. A call per figure, on free tiers whose limits are per minute
as much as per day (Groq's vision model allows 1,000 output tokens a minute).
So every document gets a budget - a number of figures and a number of
seconds - and the describer waits out a short rate-limit cooldown rather than
dropping the figure, but never past its deadline. A figure the budget does
not reach stays recorded without a description, exactly as before this
module existed: an answer can still point at it.

The chain is its own (FIGURE_DESCRIBER_CHAIN), of vision models chat does
not use, so describing figures never spends chat's daily allowance. It goes
through the same gateway as every other generative call: the same budget
counters, cooldowns and status reporting.
"""

import base64
import hashlib
import io
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings

from documents.processing import figure_markers as markers

logger = logging.getLogger("adi.figures")

# The markers around a description in the page text. Changing them changes
# stored text, so they are versioned with the describer.
BEGIN = markers.BEGIN_PREFIX + "{kind} - described automatically from the image]"
END = markers.END_LINE
DESCRIBER_VERSION = "v1"

# What the first line of a reply may name. A figure of one of the SKIPPED
# kinds carries nothing anyone would ask about, and its description would only
# dilute the page it sits on.
KINDS = (
    "chart",
    "flowchart",
    "diagram",
    "map",
    "table",
    "form",
    "screenshot",
    "photograph",
    "logo",
    "decoration",
    "signature",
    "blank",
    "other",
)
SKIPPED_KINDS = frozenset({"logo", "decoration", "signature", "blank"})

PROMPT = """This image is a figure taken from a document. Write down what it shows so that someone who cannot see it can answer questions from your text alone.

Rules:
- The first line must be exactly "Kind: X", where X is one of: chart, flowchart, diagram, map, table, form, screenshot, photograph, logo, decoration, signature, blank, other.
- The second line is the figure's title or caption if it has one; otherwise a few words saying what it is about.
- Copy every piece of visible text exactly as written: labels, axis names, legend entries, numbers, units, notes.
- Chart: give every data point on its own line as "series, category: value unit". Use the printed value when there is one; otherwise read it off the axis and write "about" before it.
- Flowchart or process diagram: list every step in order, then every arrow as "A -> B", with the label on the arrow (for example Yes or No).
- Diagram, organisation chart, mind map or network: list every box or node and what it is connected to.
- Table: write each row on its own line with cells separated by " | ".
- Photograph: one or two lines saying what it shows. Logo, decoration, signature or blank: nothing after the first two lines.
- Do not explain, interpret or add anything that is not in the image. If something is too small or blurred to read, write "unreadable" instead of guessing.
- Plain text only: no markdown, no headings, no preamble."""

# A picture is resized so its longer side is at most this before it is sent:
# more pixels cost tokens on some providers and add nothing a model can read.
MAX_SIDE = 1600
# Smaller than this on either side and there is nothing to read.
MIN_SIDE = 48
# A PNG larger than this is sent as JPEG instead. Providers cap base64 images
# at a few megabytes.
MAX_PNG_BYTES = 2_500_000

# A photographed document with at least this many OCR words is text, and OCR
# has read it; fewer and the picture is probably a chart or a diagram.
MAX_WORDS_FOR_FIGURE = 90

# A reply longer than this is cut: one figure must not crowd out its page.
MAX_DESCRIPTION_CHARACTERS = 4000

# The longest cooldown worth waiting for inside one document's budget.
MAX_WAIT_SECONDS = 65.0
# Consecutive failures after which the rest of the document is not tried.
MAX_FAILURES = 3

_FENCE = re.compile(r"^```[a-zA-Z]*\s*$")
_KIND_LINE = re.compile(r"^\W*kind\s*[:=-]\s*([a-z ]+?)\W*$", re.IGNORECASE)


@dataclass(frozen=True)
class VisionModel:
    """What is known about one vision model on one provider."""

    request_extras: tuple = ()
    daily_limit: int = 0
    max_output_tokens: int = 0


# Models measured on 2026-11-17 against synthetic charts and flowcharts with
# known content (see docs/ai/figures.md). Others may be named in the chain;
# they are sent no extras and take the provider preset's limits.
KNOWN_MODELS = {
    # Groq's free tier for this model: 30 requests and 1,000 output tokens a
    # minute, 1,000 requests a day. Thinking is switched off: its tokens
    # count against the per-minute output allowance and add nothing here.
    "groq:qwen/qwen3.8-27b": VisionModel(
        request_extras=(("reasoning_effort", "none"),),
        daily_limit=1000,
        max_output_tokens=900,
    ),
    # Refuses reasoning_effort (HTTP 400), so it is sent none.
    "gemini:gemini-3.5-flash-lite": VisionModel(daily_limit=20),
    "gemini:gemini-3.1-flash-lite": VisionModel(daily_limit=20),
    "openrouter:google/gemma-4-31b-it:free": VisionModel(daily_limit=50),
}


@dataclass(frozen=True)
class Description:
    """One figure, as a vision model read it."""

    kind: str
    text: str
    source: str

    @property
    def useful(self) -> bool:
        return self.kind not in SKIPPED_KINDS and bool(self.text.strip())

    def block(self) -> str:
        """The description as it goes into the page text."""
        return f"{BEGIN.format(kind=self.kind)}\n{self.text}\n{END}"


def enabled() -> bool:
    return bool(getattr(settings, "FIGURE_DESCRIPTIONS", False)) and bool(
        (getattr(settings, "FIGURE_DESCRIBER_CHAIN", "") or "").strip()
    )


def links_from_settings() -> tuple:
    """FIGURE_DESCRIBER_CHAIN as gateway links: "groq:qwen/qwen3.8-27b, ..."."""
    from documents.rag import budget
    from documents.rag.gateway import Link
    from documents.rag.providers import PRESETS

    links = []
    for item in (getattr(settings, "FIGURE_DESCRIBER_CHAIN", "") or "").split(","):
        item = item.strip()
        if not item:
            continue
        provider, _, model = item.partition(":")
        provider, model = provider.strip().lower(), model.strip()
        preset = PRESETS.get(provider)
        if preset is None or not model:
            # A typo must not send pictures somewhere nobody chose; it is
            # logged and that link is left out.
            logger.warning(
                "figures.unknown_link",
                extra={
                    "link": item,
                    "reason": "use provider:model with a known provider",
                },
            )
            continue
        known = KNOWN_MODELS.get(f"{provider}:{model}", VisionModel())
        links.append(
            Link(
                provider=provider,
                model=model,
                base_url=preset.base_url,
                key_env=preset.key_env,
                placeholder_key=preset.placeholder_key,
                trains_on_input=preset.trains_on_free_tier,
                request_extras=known.request_extras,
                daily_limit=budget.daily_limit_for(
                    provider, model, known.daily_limit or preset.daily_limit
                ),
                quota_tz=preset.quota_tz,
                max_output_tokens=known.max_output_tokens or preset.max_output_tokens,
            )
        )
    unique = {}
    for link in links:
        unique.setdefault(link.id, link)
    return tuple(unique.values())


def default_gateway():
    from documents.rag.gateway import Gateway

    return Gateway(
        links_from_settings(),
        timeout=float(getattr(settings, "FIGURE_DESCRIBE_TIMEOUT", 45.0) or 45.0),
        retries=0,
        deadline=0.0,
    )


def _flatten(image):
    """RGB on white: a transparent PNG's background is whatever the viewer
    paints, and black on black is what some providers would see."""
    from PIL import Image

    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return image.convert("RGB")


def fingerprint(image) -> str:
    """The same picture, wherever it appears: a crest on every page is read
    once. Coarse on purpose - a re-encoded copy of a picture is still that
    picture."""
    small = image.convert("L").resize((24, 24))
    return hashlib.sha1(
        small.tobytes() + f"{image.width // 8}x{image.height // 8}".encode()
    ).hexdigest()


def encode(image) -> str | None:
    """A data URL for the picture, or None when it is too small to read."""
    if min(image.size) < MIN_SIDE:
        return None
    picture = _flatten(image)
    if max(picture.size) > MAX_SIDE:
        picture.thumbnail((MAX_SIDE, MAX_SIDE))
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG", optimize=True)
    mime = "image/png"
    if buffer.tell() > MAX_PNG_BYTES:
        buffer = io.BytesIO()
        picture.save(buffer, format="JPEG", quality=88)
        mime = "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def parse(reply: str, source: str) -> Description | None:
    """A model's reply as a Description; None when it said nothing usable."""
    lines = [line.rstrip() for line in (reply or "").replace("\r", "").split("\n")]
    lines = [line for line in lines if not _FENCE.match(line.strip())]
    # A reply must not be able to close the block early or open another one:
    # the markers are ours.
    lines = [
        line
        for line in lines
        if not line.strip().startswith(("[Figure:", "[End of figure"))
    ]
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None

    kind = "other"
    match = _KIND_LINE.match(lines[0].strip())
    if match:
        named = match.group(1).strip().lower()
        kind = next((k for k in KINDS if named.startswith(k)), "other")
        lines = lines[1:]

    # Markdown the prompt asked not to get: bold markers and heading hashes.
    cleaned = []
    for line in lines:
        line = line.replace("**", "")
        line = re.sub(r"^#{1,6}\s+", "", line)
        cleaned.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()
    if len(text) > MAX_DESCRIPTION_CHARACTERS:
        cut = text[:MAX_DESCRIPTION_CHARACTERS]
        text = cut[: cut.rfind("\n")] if "\n" in cut else cut
    return Description(kind=kind, text=text, source=source)


class FigureDescriber:
    """Describes the figures of ONE document, within that document's budget."""

    def __init__(
        self,
        gateway=None,
        *,
        budget: int | None = None,
        deadline: float | None = None,
        max_tokens: int | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        self.gateway = gateway if gateway is not None else default_gateway()
        self.remaining = (
            int(getattr(settings, "FIGURE_DESCRIBE_MAX", 20))
            if budget is None
            else budget
        )
        self.deadline = (
            float(getattr(settings, "FIGURE_DESCRIBE_DEADLINE", 240.0))
            if deadline is None
            else deadline
        )
        self.max_tokens = (
            int(getattr(settings, "FIGURE_DESCRIBE_MAX_TOKENS", 700))
            if max_tokens is None
            else max_tokens
        )
        self.clock = clock
        self.sleep = sleep
        self.started = clock()
        self.cache: dict = {}
        self.failures = 0
        self.stopped = ""
        # What happened, for the stage's event metadata.
        self.described = 0
        self.not_useful = 0
        self.reused = 0
        self.skipped = 0
        self.sources: set = set()

    @property
    def active(self) -> bool:
        return not self.stopped

    def _time_left(self) -> float:
        return self.deadline - (self.clock() - self.started)

    def describe(self, image) -> Description | None:
        """What `image` shows, or None. Never raises for a figure: a figure
        that could not be read is a figure left undescribed, not a failed
        document."""
        if self.stopped:
            self.skipped += 1
            return None
        try:
            key = fingerprint(image)
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            return None
        if key in self.cache:
            self.reused += 1
            return self.cache[key]
        if self.remaining <= 0:
            self.skipped += 1
            return None
        if self._time_left() <= 0:
            self.stopped = "deadline"
            self.skipped += 1
            return None

        try:
            payload = encode(image)
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            payload = None
        if payload is None:
            return None

        self.remaining -= 1
        result = self._ask(payload)
        self.cache[key] = result
        if result is not None:
            self.sources.add(result.source)
            if result.useful:
                self.described += 1
            else:
                self.not_useful += 1
        return result

    def block_for(self, image) -> str:
        """The text to put in the page for `image`: its description between
        the markers, or "" when there is nothing worth putting there."""
        description = self.describe(image)
        if description is None or not description.useful:
            return ""
        return description.block()

    def _ask(self, payload: str) -> Description | None:
        from documents.rag.gateway import (
            ALL_COOLING,
            FAILED,
            FIGURES,
            Unavailable,
        )

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": payload}},
                ],
            }
        ]
        while True:
            try:
                response = self.gateway.complete(
                    messages,
                    purpose=FIGURES,
                    max_tokens=self.max_tokens,
                    temperature=0.0,
                )
            except SoftTimeLimitExceeded:
                raise
            except Unavailable as exc:
                wait = _seconds_until(exc.retry_at)
                left = self._time_left()
                if (
                    exc.reason == ALL_COOLING
                    and wait is not None
                    and wait <= MAX_WAIT_SECONDS
                    and wait < left - 5
                ):
                    # A per-minute limit: the next minute is worth waiting for.
                    self.sleep(max(wait, 0.5))
                    continue
                if exc.reason == FAILED:
                    self.failures += 1
                    if self.failures >= MAX_FAILURES:
                        self.stopped = "failed"
                else:
                    # Nothing configured, the day's allowance gone, or a
                    # cooldown longer than the budget: the rest of this
                    # document would only find the same.
                    self.stopped = exc.reason
                logger.info(
                    "figures.not_described",
                    extra={"reason": exc.reason, "detail": str(exc)[:200]},
                )
                return None
            except Exception as exc:  # a bug in a reply's shape, not capacity
                self.failures += 1
                if self.failures >= MAX_FAILURES:
                    self.stopped = "failed"
                logger.warning(
                    "figures.describe_failed", extra={"error": type(exc).__name__}
                )
                return None

            self.failures = 0
            if response.finish_reason == "length":
                # Cut off: what came back is still a reading of the figure, as
                # far as it goes. Kept; the cut is at a line boundary.
                text = response.text.rsplit("\n", 1)[0]
            else:
                text = response.text
            return parse(text, response.model_version or response.model)

    def metadata(self) -> dict:
        found = {
            "figures_described": self.described,
            "figures_not_useful": self.not_useful,
        }
        if self.reused:
            found["figures_reused"] = self.reused
        if self.skipped:
            found["figures_not_described"] = self.skipped
        if self.stopped:
            found["figure_describer_stopped"] = self.stopped
        if self.sources:
            found["figure_describers"] = sorted(self.sources)
        return found


def _seconds_until(moment) -> float | None:
    if moment is None:
        return None
    if isinstance(moment, datetime):
        return (moment - datetime.now(UTC)).total_seconds()
    return None


def for_document():
    """A describer for one document, or None when describing is off."""
    if not enabled():
        return None
    gateway = default_gateway()
    if not gateway.links:
        return None
    return FigureDescriber(gateway)
