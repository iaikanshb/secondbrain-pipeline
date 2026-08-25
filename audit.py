#!/usr/bin/env python3
"""Vault integrity check: broken wikilinks, orphan notes.

    ./.venv/bin/python audit.py

Read-only, no LLM calls, no cost -- safe to run any time. Broken links
should be rare going forward (filing.py/practice.py/rebuild.py all defuse
any link the model invents before writing), but this catches drift from
manual edits or older notes written before that enforcement existed.

Deliberately does NOT auto-rewrite anything it finds. A silent fuzzy-match
"repair" was tried and reverted -- it guessed wrong on real notes (turning
[[Title|good short alias]] into [[Title]], destroying the alias) with no
diff shown and no way to review before it landed. Report, don't guess.
"""
import os
import re

import config

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")


def _load_notes(dirpath: str) -> dict:
    notes = {}
    if not os.path.isdir(dirpath):
        return notes
    for fname in sorted(os.listdir(dirpath)):
        if not fname.endswith(".md"):
            continue
        with open(os.path.join(dirpath, fname), encoding="utf-8") as f:
            notes[fname[:-3]] = f.read()
    return notes


def audit() -> tuple[list, list]:
    notes = _load_notes(config.NOTES)
    practice = _load_notes(config.PRACTICE)
    all_docs = {**notes, **practice}
    note_titles = set(notes.keys())  # only 01-Notes/ are valid *link targets*

    outgoing = {t: set() for t in notes}  # orphan-check is over 01-Notes/ only
    broken = []

    for title, content in all_docs.items():
        for m in WIKILINK_RE.finditer(content):
            target = m.group(1).strip()
            if target not in note_titles:
                broken.append((title, target))
            elif title in outgoing:
                outgoing[title].add(target)

    incoming = {t: set() for t in notes}
    for src, targets in outgoing.items():
        for t in targets:
            incoming[t].add(src)

    orphans = sorted(t for t in note_titles if not outgoing[t] and not incoming[t])

    total_links = sum(len(v) for v in outgoing.values())
    print(f"{len(notes)} notes, {len(practice)} practice docs, {total_links} links between notes")

    if broken:
        print(f"\n{len(broken)} broken link(s):")
        for src, target in broken:
            print(f"  {src} -> [[{target}]]  (no such note)")
    else:
        print("\nno broken links.")

    if orphans:
        print(f"\n{len(orphans)} orphan note(s) (no incoming or outgoing links):")
        for o in orphans:
            print(f"  {o}")
    else:
        print("\nno orphans.")

    return broken, orphans


if __name__ == "__main__":
    audit()
