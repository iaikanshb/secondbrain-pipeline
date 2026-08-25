"""What kind of document was just dropped -- determines which filing path it
takes. Only relevant for 00-Inbox-Lectures/: anything in 00-Inbox-Sources/
always goes straight to local indexing regardless of content (see ingest.py)
-- which folder a file lands in is the classification, not a heuristic.
"""
import gemini_client

CLASSIFY_PROMPT = """Look at this excerpt from a document and classify it.

Excerpt:
---
{excerpt}
---

Is this primarily:
(a) instructional/explanatory material -- lecture slides, class notes, content that teaches concepts
(b) a problem set / exam paper / exercise sheet -- questions to be solved, without their answers being taught

Respond with exactly one word: "lecture" or "practice"."""


def classify_document(transcript: str) -> str:
    excerpt = transcript[:3000]
    raw = gemini_client.text_call(CLASSIFY_PROMPT.format(excerpt=excerpt))
    verdict = raw.strip().lower()
    return "practice" if "practice" in verdict else "lecture"


COURSE_PROMPT = """A file named "{filename}" needs to be filed under the right course.

Known courses (this is the user's real, complete course list -- pick one of these unless the \
content clearly doesn't belong to any of them): {known_courses}

The filename is often the strongest signal -- students routinely name files after the course code \
itself (e.g. "BDA_Lecture1.pdf", "ADA_2026_tut6.pdf"). Check it FIRST for a match against the known \
courses above before looking at content. Only fall through to the content excerpt below if the \
filename gives no clue.

Opening of the source (cover/preface/table of contents, or a paper's title/abstract):
---
{excerpt}
---

Respond with only the course code, nothing else."""


def infer_course_for_source(sample_text: str, known_courses: list[str], filename: str = "") -> str:
    excerpt = sample_text[:3000]
    known = ", ".join(sorted(set(known_courses))) or "(none yet)"
    raw = gemini_client.text_call(
        COURSE_PROMPT.format(filename=filename, excerpt=excerpt, known_courses=known)
    )
    return raw.strip().split()[0] if raw.strip() else ""
