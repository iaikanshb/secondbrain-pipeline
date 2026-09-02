"""Pulls embedded exercise/question sections out of a document that was
classified as lecture material and files them into 04-Practice/ verbatim.

filing.py's atomization is concept-oriented (a note is "a concept together
with its explanation") -- a "Tutorial Questions" or "Think About It" list
has no explanation attached to it, so it was never turned into a note and
silently vanished, along with any coverage-recovery pass over the same
text (recovery re-runs the same concept-oriented prompt). This module
targets exactly that gap: it doesn't reclassify the document, it just
recovers the practice-shaped fragment of a lecture-shaped document.
"""
import gemini_client

EXTRACT_PROMPT = """The document below was filed as instructional/lecture material. Some lecture \
documents also contain one or more sections that pose questions, problems, or tasks for the \
student to work through themselves -- e.g. "Tutorial Questions", "Practice Questions", \
"Exercises", "Hands-on Exercises", "Hands-on Task", "Think About It", a numbered problem set at \
the end, or similar. These are NOT worked examples explained inline as part of the teaching (skip \
those) -- only sections explicitly posed as something the student answers or completes on their \
own, with no answer given in the document.

Document:
---
{transcript}
---

If the document contains any such section(s), output them verbatim -- original wording and \
numbering preserved, concatenated in source order, with the original section heading kept before \
each one if the source has one. If the document contains no such section at all, output exactly \
the single word NONE.

Output only the extracted text (or NONE), nothing else."""


def extract_embedded_exercises(transcript: str) -> str:
    raw = gemini_client.text_call(EXTRACT_PROMPT.format(transcript=transcript))
    text = raw.strip()
    if not text or text.upper() == "NONE":
        return ""
    return text
