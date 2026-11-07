"""The extractive document brief: summary, key points, entities, questions.

Pure functions over (page_number, text) pairs in `text`, `entities`,
`summary`, `stats` and `questions` - no Django, no database - so every rule
can be tested on a string. `service` is the ORM boundary that reads a
DocumentContent, runs them, verifies every span and stores the result.

Every span this package returns is (page_number, start, end) in Unicode code
points into the stored PageContent.text, and `service` refuses to store one
that does not re-slice to its own text - the same invariant fields, chunks
and tables are held to.
"""

GENERATOR_NAME = "extractive-insights"
# Bumped whenever the output changes for the same input, so a backfill can
# find briefs written by older rules (`build_insights --stale`).
GENERATOR_VERSION = "1.0.0"
# What the API reports as `method`. Major version only: a reader cares that
# the brief is extractive, not which bug-fix release of the rules wrote it.
METHOD = "extractive-v1"
