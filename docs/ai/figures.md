# Reading figures: charts, flowcharts, diagrams

OCR reads the words in a picture and loses what the picture *shows*: which bar a
number belongs to, which box an arrow leaves, which branch says "No". This page is
how the pipeline recovers that, what it costs, and where it stops.

## Three layers, cheapest first

| Where the figure is | How it is read | Needs a key |
|---|---|---|
| A chart in a Word, PowerPoint or OpenDocument file | **Exactly**, as the table of numbers stored beside it (`extractors/charts.py`, python-pptx for decks) | no |
| A picture: pasted into Word, PowerPoint or OpenDocument; embedded in a PDF; an image file with few OCR words | A **vision model** writes down every label, data point and arrow (`figures/vision.py`) | yes |
| A diagram drawn with a PDF page's own shapes (an Excel chart exported to PDF, a Word flowchart) | Found from the page's vector shapes (`extractors/figure_regions.py`), rendered, and shown to the vision model | yes |

With no key, or `FIGURE_DESCRIPTIONS=False`, nothing changes from before: the first
row still works, the others leave the figure recorded as an unread figure, and no
picture leaves the server.

## What is stored

The model's reading goes into the **page text**, between two marker lines:

```
[Figure: chart - described automatically from the image]
B.Tech admissions by year
admissions, 2021: 420
admissions, 2023: 610
[End of figure]
```

so search finds it, chat quotes and cites it, and a person reading the page can see
it was machine-written. The chunker keeps a reading whole (one chunk, `kind: FIGURE`,
the figure's title in its metadata) and the answering prompt marks a passage that
holds one: *a value marked "about" was read off an axis and is approximate*.
`DocumentFigure` rows on a page that was read say so (`description_source` is the
model); a figure the budget did not reach stays undescribed, which is what lets chat
say "there is a figure on that page I could not read".

The model is told to copy visible text exactly, give every data point as
`series, category: value`, write "about" before a value it read off an axis, list a
flowchart's steps and `A -> B` arrows with their labels, and say "unreadable" instead
of guessing. It is asked to name the kind of figure first; logos, signatures,
decoration and blanks are not stored.

## Cost and limits

* **Its own chain**, `FIGURE_DESCRIBER_CHAIN` (default `groq:qwen/qwen3.8-27b`, then
  `gemini:gemini-3.5-flash-lite`), through the same gateway as chat under the
  `figures` purpose: the same counters, cooldowns and status page, but never chat's
  allowance. Groq's free tier allows 1,000 output tokens a minute for that model, so
  the describer waits out a short cooldown (up to 65 s) rather than drop the figure.
* **Per document**: at most `FIGURE_DESCRIBE_MAX` (20) figures within
  `FIGURE_DESCRIBE_DEADLINE` (240 s). The same picture twice (a crest on every page)
  is one call. Three failures in a row, a day with no allowance left, or no key end
  the document's describing with the reason in the stage's metadata
  (`figure_describer_stopped`).
* **Privacy**: the pictures are sent to the provider. Groq does not train on what it
  is sent; Gemini's and OpenRouter's free tiers may - the gateway logs that, as it
  does for chat.

## How a drawing is told from a table

A Word export paints a filled rectangle behind every shaded table cell, so rectangles
alone mean nothing. A region of shapes is a drawing only if it has curves (a pie
slice, a line series, an arrowhead), a diagonal line (an arrow, a connector), or
three or more filled bars on one baseline at different lengths standing apart. Ruled
tables, a frame around the page, anything smaller than 3% of the page and a region
of running text are not. The text labels around a drawing (title, axis numbers,
legend) are part of what is sent - without them a model invents a title - but a
paragraph beside a chart is not.

## Measured

Synthetic charts and flowcharts with known content (`bar`, `pie`, `line`, `flow`):
Qwen3.8-27B on Groq kept 11 of 11 required facts on the two it could be tried on within
the rate limit; Gemini 3.5 Flash-Lite 24 of 25 (it left out the "No" branch of one
flowchart). On a real vector bar chart the first version, shown only the bars,
invented a title ("Number of Employees"); after the labels were included it read
every value correctly. Reading a figure is an estimate by a model and is labelled as
one.
