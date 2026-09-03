"""Turn a verified transcript into atomic notes, cross-linked and tagged
against the *whole* existing note pool -- not scoped to one course, since
the whole point of this vault is connections that cut across courses.

Notes the filing model flags as topic-label-only (a slide that names a
concept without explaining it) get expanded before being written: grounded
against the course's indexed textbook when one exists (matches the actual
notation/treatment taught), falling back to the model's general knowledge
-- clearly flagged as such -- only when no textbook covers that course.
"""
import datetime

import dedupe
import embeddings
import gemini_client
import textbook_index
import vault

FILING_PROMPT_TEMPLATE = """You are filing new source material into an existing Zettelkasten-style \
note vault. The vault has no folders-per-topic and no hub pages -- notes connect purely through \
tags and [[wikilinks]], and a note can and should be tagged/linked across course boundaries \
whenever there's a real conceptual connection (e.g. a probability idea used in both an economics \
and a stats course should link both ways).

Existing notes in the vault (title -- tags), for linking against. Do NOT invent links to titles \
not in this list, and do not force a link/tag where there's no genuine conceptual overlap:
{existing_notes}

Courses already known in this vault: {known_courses}. For the "course" field below, reuse one of \
these exactly (matching existing naming matters more than precision) if it genuinely matches, \
rather than inventing a new spelling/variant of a course that already exists.

Source material to file (from "{source_name}"):
---
{transcript}
---

Split this into topic-sized notes -- each note should cover one coherent subtopic (a concept \
together with its explanation, definitions, and any worked examples/derivations that belong \
with it), not a single isolated fact and not the whole document. Prefer fewer, fuller notes: \
group closely-related sub-points into the same note instead of splitting them out separately \
-- a good note should be complete enough that a student could study exam material from it \
alone, roughly the size of a textbook subsection. Reuse the vault's existing conventions: \
LaTeX math with $...$, concise prose, [[wikilinks]] to genuinely related existing notes from \
the list above.

Course logistics count as content too -- instructor/TA names, office hours, schedule, assessment \
weights, exam format, deadlines, attendance/plagiarism policy. Don't silently drop this just \
because it isn't an academic concept: capture it as one "Course Logistics" note (title format: \
"<course> Course Logistics") rather than atomizing it further -- it's reference material, not \
something that benefits from being split into pieces.

You are producing multiple notes in this one response -- they can and should [[link]] to EACH \
OTHER where genuinely related (e.g. two notes from the same source that reference one another), \
using the exact "title" value another note in this same array will have. Don't miss this just \
because neither note exists as a file yet.

Some source material (lecture slides especially) only names a topic without actually explaining \
it -- a heading, a term, a bullet with no real definition/mechanism/derivation behind it. Flag \
those honestly rather than padding them out yourself.

Respond with a JSON array, each element:
{{
  "title": "short, specific note title",
  "course": "best-guess course/subject code from context, or empty string if unclear",
  "tags": ["lowercase-kebab-case", "concept", "tags"],
  "body": "markdown body, starting with a ## heading, using [[links]] where genuinely relevant. \
If needs_expansion is true, this should be the terse source content as-is -- do not try to expand \
it yourself, that happens in a separate grounded pass.",
  "needs_expansion": true or false,
  "expansion_query": "if needs_expansion is true, a short search phrase (2-6 words) naming the \
concept, for looking it up in the course textbook -- else empty string"
}}

This is JSON: every literal backslash inside "body" (LaTeX commands like \\text, \\begin{{cases}}, \\frac) \
MUST be written doubled (\\\\text, \\\\begin{{cases}}, \\\\frac) so it parses as valid JSON.

Output only the JSON array, nothing else."""

GROUND_EXPAND_PROMPT = """The following is a terse, label-only note extracted from lecture slides -- \
it names a topic without actually explaining it. Expand it into a real, self-contained explanation, \
grounded in the textbook excerpt(s) below from the same course. Match the textbook's notation and \
treatment. Cite the page(s) you drew from inline, e.g. "(see {book}, p.{page})".

This vault connects notes via [[wikilinks]], not folders. Existing notes (title -- tags), for \
linking against where genuinely relevant -- do NOT invent links to titles not in this list:
{existing_notes}

Terse source content:
---
{terse}
---

Textbook excerpt(s):
---
{excerpt}
---

Output only the expanded markdown body (starting with a ## heading), using [[links]] where \
genuinely relevant. Nothing else."""

FALLBACK_EXPAND_PROMPT = """The following is a terse, label-only note extracted from lecture slides -- \
it names a topic without actually explaining it. No matching textbook passage was found for this \
course, so expand it using your own general knowledge instead. Be accurate and use standard \
terminology for this field.

This vault connects notes via [[wikilinks]], not folders. Existing notes (title -- tags), for \
linking against where genuinely relevant -- do NOT invent links to titles not in this list:
{existing_notes}

Terse source content:
---
{terse}
---

Output only the expanded markdown body (starting with a ## heading), using [[links]] where \
genuinely relevant. Nothing else."""


def _status(review_flagged: bool, llm_expanded: bool) -> str:
    parts = []
    if review_flagged:
        parts.append("review-needed")
    if llm_expanded:
        parts.append("llm-expanded")
    return "+".join(parts) if parts else "clean"


def _expand_if_needed(note: dict, existing_desc: str) -> tuple[str, bool]:
    """Returns (body, llm_expanded_without_textbook)."""
    if not note.get("needs_expansion"):
        return note["body"], False

    course = note.get("course", "")
    query = note.get("expansion_query") or note["title"]
    hits = textbook_index.search(course, query) if textbook_index.has_book_for(course) else []

    if hits:
        excerpt = "\n\n".join(f"[p.{h['page']} of {h['book']}]\n{h['text'][:2000]}" for h in hits)
        expanded = gemini_client.text_call(
            GROUND_EXPAND_PROMPT.format(terse=note["body"], excerpt=excerpt, existing_notes=existing_desc,
                                         book=hits[0]["book"], page=hits[0]["page"])
        )
        return expanded, False

    expanded = gemini_client.text_call(
        FALLBACK_EXPAND_PROMPT.format(terse=note["body"], existing_notes=existing_desc)
    )
    flagged_body = (
        "> [!note] Expanded from general knowledge, not sourced from the course textbook "
        "(none indexed for this course) -- verify before exam use.\n\n" + expanded
    )
    return flagged_body, True


def file_transcript(transcript: str, source_name: str, review_flags: list[int]) -> list[str]:
    existing = vault.existing_notes()
    existing_desc = "\n".join(
        f"- {n['title']} -- {', '.join(n['tags']) if n['tags'] else '(no tags)'}"
        for n in existing
    ) or "(vault is empty so far)"
    known_courses = ", ".join(vault.known_courses()) or "(none yet)"

    prompt = FILING_PROMPT_TEMPLATE.format(
        existing_notes=existing_desc,
        known_courses=known_courses,
        source_name=source_name,
        transcript=transcript,
    )
    proposed = gemini_client.json_call(prompt)

    today = datetime.date.today().isoformat()
    review_flagged = bool(review_flags)

    # Pass 1: expand every note and resolve the title it will *actually*
    # land under -- dedupe.resolve_title() can redirect a proposed note into
    # an existing note (a semantic-duplicate merge), so the on-disk title
    # can differ from what the model proposed. This has to be settled for
    # the whole batch before any sibling link is checked, or a link to a
    # proposed title that got redirected away would look invalid (or, worse,
    # coincidentally match a same-named but wrong target) instead of being
    # repointed at where that content actually ended up.
    #
    # Grows as we go, so a later note in the same batch (or the
    # coverage-recovery call, which re-reads existing_notes() fresh from
    # disk and so already sees everything the initial pass wrote) gets
    # checked against siblings too, not just notes that predate this run.
    dedupe_pool = list(existing)
    resolved = []  # (note, expanded_body, llm_expanded, final_title)
    title_map = {}  # proposed title (sanitized) -> final on-disk title

    for note in proposed:
        body, llm_expanded = _expand_if_needed(note, existing_desc)
        proposed_title = vault.sanitize_title(note["title"])
        final_title = vault.sanitize_title(
            dedupe.resolve_title(note["title"], body, note.get("tags", []), dedupe_pool)
        )
        title_map[proposed_title] = final_title
        resolved.append((note, body, llm_expanded, final_title))
        dedupe_pool.append({"title": final_title, "tags": note.get("tags", []), "course": note.get("course", "")})

    # Valid link targets: existing notes, plus every title this batch will
    # actually produce (not the titles it merely proposed -- see title_map).
    valid_titles = {n["title"] for n in existing} | set(title_map.values())

    written_paths = []
    for note, body, llm_expanded, title in resolved:
        body = vault.remap_links(body, title_map)
        body = vault.defuse_invalid_links(body, valid_titles)

        if review_flagged:
            flag_note = (
                f"> [!review] OCR verification flagged page(s) "
                f"{', '.join(map(str, review_flags))} of `{source_name}` as uncertain "
                f"(illegible handwriting or a correction was needed). Check against the "
                f"source in 02-Resources/ before trusting this note fully.\n\n"
            )
            body = flag_note + body

        path = vault.write_note(
            title=title,
            course=note.get("course", ""),
            tags=note.get("tags", []),
            source=source_name,
            created=today,
            status=_status(review_flagged, llm_expanded),
            body=body,
        )
        written_paths.append(path)

        # Re-read from disk rather than embed the pre-write `body` -- a
        # dedupe redirect means the file on disk may be an LLM-merged
        # combination of this content and an existing note, not what was
        # just proposed. No-ops entirely if embeddings aren't configured.
        with open(path, encoding="utf-8") as f:
            embeddings.reindex_note(title, f.read())

    return written_paths
