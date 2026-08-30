"""Structural notes on top of the atomic/topic layer: one per lecture (a
short narrated walkthrough tying together the notes that lecture produced,
in reading order) and one per course (a deterministic roll-up of every
lecture MOC for that course). Purely additive -- these link out to the
existing notes and never hold the content itself, so they carry none of the
duplication/merge risk the notes do, and they're what "study this lecture"
or "review this whole course" actually opens.
"""
import os
import re

import yaml

import config
import gemini_client
import vault

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")

LECTURE_MOC_PROMPT = """You are writing a "map of content" for one lecture in a student's \
study vault -- not new material, just a short guided walkthrough tying together the notes \
already filed from this lecture so a student can read this one document top-to-bottom to \
study the lecture, then follow links for depth.

Notes filed from "{source_name}" (title -- opening line):
{notes_desc}

Write a short connective narrative in a sensible teaching order: introduce each note with \
[[exact title]] and one or two sentences on what it covers and how it connects to the notes \
before/after it. Do not restate the notes' content in depth -- that's what the links are for. \
Start with a ## heading naming the lecture's overall topic.

Output only the markdown body, nothing else."""


def _opening_line(title: str) -> str:
    path = os.path.join(config.NOTES, vault.sanitize_title(title) + ".md")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        content = f.read()
    end = content.find("\n---", 3)
    body = content[end + 4:].lstrip("\n") if end != -1 else content
    lines = [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith("#")]
    return lines[0][:150] if lines else ""


def build_lecture_moc(source_name: str, course: str, note_titles: list[str], today: str) -> str | None:
    """note_titles: titles of every note (initial + coverage-recovered)
    filed from this one source, in the order they were written. Skipped
    entirely if filing produced nothing (e.g. every page was pure
    boilerplate) or exactly one note (a walkthrough of one note is noise)."""
    note_titles = list(dict.fromkeys(note_titles))  # de-dupe, preserve order
    if len(note_titles) < 2:
        return None

    notes_desc = "\n".join(f'- "{t}" -- {_opening_line(t)}' for t in note_titles)
    narrative = gemini_client.text_call(
        LECTURE_MOC_PROMPT.format(source_name=source_name, notes_desc=notes_desc)
    )
    narrative = vault.defuse_invalid_links(narrative, set(note_titles))

    # Structural completeness check -- this MOC only means anything as a
    # study checklist if it's actually guaranteed to link every note the
    # lecture produced, and prompting alone isn't reliable enough to trust
    # for that (same reasoning as coverage.py's page-vs-notes check on the
    # filing side). No extra LLM call: anything the narrative didn't weave
    # in gets a plain link appended instead of silently going missing.
    linked = set(WIKILINK_RE.findall(narrative))
    missing = [t for t in note_titles if t not in linked]
    if missing:
        narrative += "\n\n### Also from this lecture\n" + "\n".join(f"- [[{t}]]" for t in missing)

    base = os.path.splitext(source_name)[0]
    moc_title = f"{course} - {base} MOC" if course else f"{base} MOC"
    return vault.write_moc(
        title=moc_title, course=course, tags=["moc", "lecture-moc"], source=source_name,
        created=today, moc_type="lecture-moc", body=narrative,
    )


def build_course_moc(course: str, today: str) -> str | None:
    """Deterministic (no LLM) roll-up of every lecture MOC for this course,
    sorted by creation date -- regenerated in full each time so it can
    never drift from what actually exists on disk."""
    if not course or not os.path.isdir(config.MOCS):
        return None

    lecture_mocs = []
    for fname in sorted(os.listdir(config.MOCS)):
        if not fname.endswith(".md"):
            continue
        with open(os.path.join(config.MOCS, fname), encoding="utf-8") as f:
            content = f.read()
        end = content.find("\n---", 3)
        if end == -1:
            continue
        try:
            fm = yaml.safe_load(content[3:end]) or {}
        except yaml.YAMLError:
            continue
        if fm.get("type") == "lecture-moc" and fm.get("course") == course:
            lecture_mocs.append((fm.get("created", ""), fm.get("title", fname[:-3])))

    if not lecture_mocs:
        return None
    lecture_mocs.sort()

    lines = [f"## {course} -- Course Overview", "", "Every lecture processed for this course, in order:", ""]
    lines += [f"- [[{title}]] ({created})" for created, title in lecture_mocs]

    return vault.write_moc(
        title=f"{course} Course MOC", course=course, tags=["moc", "course-moc"],
        source="", created=today, moc_type="course-moc", body="\n".join(lines), overwrite=True,
    )
