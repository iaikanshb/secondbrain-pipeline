#!/usr/bin/env python3
"""Re-ground notes that were expanded from general knowledge (status
contains 'llm-expanded') against source material that didn't exist yet at
filing time -- the "lecture parsed before its textbook" case. Runs
automatically after every source finishes indexing (see ingest.py); this
is also the manual/standalone entrypoint.

    ./.venv/bin/python rebuild.py              # check every course
    ./.venv/bin/python rebuild.py --course BDA # limit to one course
    ./.venv/bin/python rebuild.py --sources ADA_2026_tut4.pdf,transmission_rate_physics_text_focused.pdf # limit to notes citing specific sources

"""
import os
import sys

import config
import courses
import gemini_client
import textbook_index
import vault
import yaml

REGROUND_PROMPT = """This note was previously expanded from general knowledge because no course \
textbook/source was available at filing time -- one now exists. Rewrite it to be properly grounded \
in the textbook excerpt(s) below: correct anything that doesn't match, add inline page citations \
(e.g. "(see {{book}}, p.{{page}})"), keep a similar level of detail. Preserve genuinely relevant \
[[wikilinks]] already present; do not invent new ones beyond this list of existing notes:
{existing_notes}

Do NOT include any "expanded from general knowledge" disclaimer -- that's no longer true.

Existing note body:
---
{body}
---

Textbook excerpt(s):
---
{excerpt}
---

Output only the corrected markdown body (starting with a ## heading). Nothing else."""


def _rewrite_note(path: str, fm: dict, new_body: str) -> None:
    frontmatter = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
    content = f"---\n{frontmatter}---\n\n{new_body.strip()}\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _strip_status_flag(status: str, flag: str) -> str:
    return "+".join(p for p in status.split("+") if p != flag) or "clean"


def _parse_source_fm(source_field: str) -> list[str]:
    """Parse a note's source field into individual filenames (handles comma-separated values)."""
    if not source_field:
        return []
    return [s.strip() for s in source_field.split(",") if s.strip()]


def rebuild(course_filter: str = "", source_filter: str = "") -> list[str]:
    course_filter = courses.normalize(course_filter) if course_filter else ""
    source_filter_set = {s.strip() for s in source_filter.split(",") if s.strip()} if source_filter else None

    existing = vault.existing_notes()
    existing_desc = "\n".join(
        f"- {n['title']} -- {', '.join(n['tags']) if n['tags'] else '(no tags)'}"
        for n in existing
    ) or "(vault is empty so far)"
    valid_titles = {n["title"] for n in existing}

    regrounded = []
    if not os.path.isdir(config.NOTES):
        return regrounded

    for fname in sorted(os.listdir(config.NOTES)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(config.NOTES, fname)
        fm, body = vault._read_markdown(path)

        status = fm.get("status", "")
        if "llm-expanded" not in status:
            continue
        course = fm.get("course", "")
        if course_filter and course != course_filter:
            continue
        # If source-filtering, only process notes whose 'source' field matches
        if source_filter_set and not source_filter_set.intersection(_parse_source_fm(fm.get("source", ""))):
            continue
        if not textbook_index.has_book_for(course):
            continue  # still nothing to ground against -- leave as-is

        hits = textbook_index.search(course, fm.get("title", ""))
        if not hits:
            continue

        excerpt = "\n\n".join(f"[p.{h['page']} of {h['book']}]\n{h['text'][:2000]}" for h in hits)
        new_body = gemini_client.text_call(
            REGROUND_PROMPT.format(existing_notes=existing_desc, body=body, excerpt=excerpt)
        )
        new_body = vault.defuse_invalid_links(new_body, valid_titles)

        fm["status"] = _strip_status_flag(status, "llm-expanded")
        _rewrite_note(path, fm, new_body)
        regrounded.append(fname)
        print(f"  regrounded: {fname}")

    if regrounded:
        vault.git_commit(
            f"Re-ground {len(regrounded)} note(s) now that source material exists: "
            f"{', '.join(regrounded)}",
            paths=[os.path.join(config.NOTES, fname) for fname in regrounded],
        )
    return regrounded


def periodic_rebuild() -> list[str]:
    """Run a rebuild without limiting to a specific course -- safe to call
    on a schedule (e.g. via a systemd timer) since it's filtered to only
    'llm-expanded' notes that still have textbooks to ground against."""
    print("running scheduled rebuild...")
    result = rebuild()
    print(f"{len(result)} note(s) regrounded." if result else "nothing to reground.")
    return result


if __name__ == "__main__":
    course_arg = ""
    source_arg = ""
    args = sys.argv[1:]
    if "--course" in args:
        idx = args.index("--course")
        if idx + 1 < len(args):
            course_arg = args[idx + 1]
    if "--sources" in args:
        idx = args.index("--sources")
        if idx + 1 < len(args):
            source_arg = args[idx + 1]
    result = rebuild(course_arg, source_arg)
    print(f"\n{len(result)} note(s) regrounded." if result else "\nnothing to reground.")
