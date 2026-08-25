"""Verifies every page of a source is reflected in the notes filed from it.

Closes the exact failure mode found live: filing.py's prompt only ever
asked for "concepts," so a whole category of real content (course
logistics -- instructor, schedule, grading weights, policies) got silently
dropped, because nothing checked afterward whether everything made it in.
Prompting alone wasn't enough to prevent that in the first place, so this
is a structural check, same pattern as OCR's transcribe-then-verify.

Not a mathematical guarantee -- an LLM checking its own filing pass shares
some blind spots with the pass that produced it -- but it catches whole
skipped categories/pages, which is the actual failure observed so far.
"""
import gemini_client

COVERAGE_PROMPT = """Here is a source transcript, broken into pages (each section starts with \
"--- page N ..."):
---
{transcript}
---

Here are all the notes that were filed from it:
---
{notes}
---

Go through the transcript page by page. For each page, check whether its substantive content -- \
even partially, paraphrased is fine -- is reflected somewhere in the notes above. Ignore pure \
boilerplate (page numbers, repeated slide-deck furniture) but do NOT ignore administrative/\
logistics content (schedule, grading weights, policies, names, dates) -- that counts as real \
content, not boilerplate.

List every page where genuine content is NOT reflected anywhere in the notes.

Respond with a JSON object: {{"missing": [{{"page": N, "summary": "what's missing"}}]}} -- empty \
"missing" array if everything is covered."""


def check_coverage(transcript: str, note_bodies: list[str]) -> list[dict]:
    notes_desc = "\n\n---\n\n".join(note_bodies) or "(none)"
    result = gemini_client.json_call(
        COVERAGE_PROMPT.format(transcript=transcript, notes=notes_desc)
    )
    return result.get("missing", []) if isinstance(result, dict) else []


def missing_text(pages: list[str], missing: list[dict]) -> str:
    """Reassemble just the flagged pages' raw text, for feeding back into a
    recovery filing pass."""
    page_nums = sorted({m["page"] for m in missing if isinstance(m.get("page"), int)})
    parts = []
    for p in page_nums:
        if 0 < p <= len(pages):
            parts.append(f"--- page {p} ---\n{pages[p - 1]}")
    return "\n\n".join(parts)
