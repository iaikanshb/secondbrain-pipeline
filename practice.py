"""Files exam papers / exercise sheets -- kept intact as one document with
original question numbering, not atomized into separate concept notes.
Individual questions get linked to whatever concept notes they test. No
answers are generated (deliberate: you practice against these yourself).
"""
import datetime

import gemini_client
import vault

PRACTICE_PROMPT_TEMPLATE = """This is a problem set / exam paper / exercise sheet from "{source_name}". \
File it into a Zettelkasten-style vault where connectivity comes from tags and [[wikilinks]]. Keep the \
whole paper as ONE document with its original question structure and numbering preserved verbatim -- \
do NOT split it into separate notes per question.

Existing concept notes in the vault (title -- tags), for linking each question to what it tests. \
Do NOT invent links to titles not in this list, and don't force a link where there's no real match:
{existing_notes}

Courses already known in this vault: {known_courses}. For the "course" field below, reuse one of \
these exactly (matching existing naming matters more than precision) if it genuinely matches, \
rather than inventing a new spelling/variant of a course that already exists.

Source material:
---
{transcript}
---

Respond with a single JSON object:
{{
  "title": "short descriptive title for this paper/sheet",
  "course": "best-guess course/subject code from context, or empty string if unclear",
  "tags": ["lowercase-kebab-case", "concept", "tags", "covered", "in", "this", "paper"],
  "body": "markdown body: the content verbatim with original numbering, a short *(tests: \
[[Concept Note]], [[Other Concept]])* line added wherever a genuine match exists in the list above \
-- at the FINEST granularity the source actually has: after each sub-question (1(a), 1(b), ...) if \
the source has sub-parts, not just after the parent question; after each topic/section/deliverable \
if this is a project brief with no numbered questions at all. Omit the line entirely for any \
question, sub-question, or section with no clear match -- don't force one. NO answers, NO solutions."
}}

This is JSON: every literal backslash inside "body" (LaTeX commands like \\text, \\begin{{cases}}) \
MUST be written doubled (\\\\text, \\\\begin{{cases}}) so it parses as valid JSON.

Output only the JSON object, nothing else."""


def file_practice(transcript: str, source_name: str, review_flags: list[int]) -> str:
    existing = vault.existing_notes()
    existing_desc = "\n".join(
        f"- {n['title']} -- {', '.join(n['tags']) if n['tags'] else '(no tags)'}"
        for n in existing
    ) or "(vault is empty so far)"

    known_courses = ", ".join(vault.known_courses()) or "(none yet)"
    prompt = PRACTICE_PROMPT_TEMPLATE.format(
        existing_notes=existing_desc,
        known_courses=known_courses,
        source_name=source_name,
        transcript=transcript,
    )
    proposed = gemini_client.json_call(prompt)

    status = "review-needed" if review_flags else "clean"
    today = datetime.date.today().isoformat()

    valid_titles = {n["title"] for n in existing}
    body = vault.defuse_invalid_links(proposed["body"], valid_titles)
    if review_flags:
        flag_note = (
            f"> [!review] OCR verification flagged page(s) "
            f"{', '.join(map(str, review_flags))} of `{source_name}` as uncertain "
            f"(illegible handwriting or a correction was needed). Check against the "
            f"source in 02-Resources/ before trusting this fully.\n\n"
        )
        body = flag_note + body

    return vault.write_practice(
        title=proposed["title"],
        course=proposed.get("course", ""),
        tags=proposed.get("tags", []),
        source=source_name,
        created=today,
        status=status,
        body=body,
    )
