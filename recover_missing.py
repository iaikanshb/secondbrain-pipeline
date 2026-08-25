#!/usr/bin/env python3
"""Retroactive coverage check: for every lecture source already processed
into 01-Notes/ (before the coverage check existed, or just as a periodic
sanity pass), re-extract it and verify nothing was silently dropped,
recovering anything missing. Re-extracts and re-runs OCR where needed, so
it costs real calls -- meant as a backfill / occasional check, not
something to run on every timer tick.

    ./.venv/bin/python recover_missing.py            # check every lecture source
    ./.venv/bin/python recover_missing.py <file.pdf> # check one specific source
"""
import os
import sys

import config
import coverage
import extract
import filing
import vault
import yaml


def _lecture_sources() -> dict:
    """source filename -> note paths currently filed with that source, for
    every PDF that went through the lecture path (has type: note entries
    in 01-Notes/ referencing it) -- excludes practice docs (already kept
    verbatim, no coverage risk) and pure grounding sources (never filed as
    notes at all). A merged note's `source` field can list several
    filenames comma-joined (see vault._write_markdown's merge path) -- each
    one needs to map back to this note individually, or coverage-checking
    one of its sources would silently miss it."""
    sources = {}
    if not os.path.isdir(config.NOTES):
        return sources
    for fname in os.listdir(config.NOTES):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(config.NOTES, fname)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        end = content.find("\n---", 3)
        if end == -1:
            continue
        try:
            fm = yaml.safe_load(content[3:end]) or {}
        except yaml.YAMLError:
            continue
        for src in (s.strip() for s in fm.get("source", "").split(",")):
            if src:
                sources.setdefault(src, []).append(path)
    return sources


def check_one(pdf_filename: str, note_paths: list) -> list:
    pdf_path = os.path.join(config.RESOURCES, pdf_filename)
    if not os.path.exists(pdf_path):
        print(f"[{pdf_filename}] source PDF not found in 02-Resources/, skipping")
        return []

    print(f"[{pdf_filename}] re-extracting for coverage check ({len(note_paths)} note(s) on file)...")
    result = extract.extract_pdf(pdf_path)

    note_bodies = []
    for p in note_paths:
        with open(p, encoding="utf-8") as f:
            note_bodies.append(f.read())

    missing = coverage.check_coverage(result.text, note_bodies)
    if not missing:
        print(f"[{pdf_filename}] fully covered, nothing to recover.")
        return []

    print(f"[{pdf_filename}] {len(missing)} page(s) with uncovered content, recovering...")
    recovered = filing.file_transcript(
        coverage.missing_text(result.pages, missing), f"{pdf_filename} (coverage recovery)", []
    )
    for p in recovered:
        print(f"  recovered: {os.path.relpath(p, config.VAULT)}")
    return recovered


def main() -> None:
    sources = _lecture_sources()
    target = sys.argv[1] if len(sys.argv) == 2 else None
    if target and target not in sources:
        print(f"'{target}' has no notes filed against it as a source -- nothing to check.")
        return

    all_recovered = []
    for fname, note_paths in sources.items():
        if target and fname != target:
            continue
        all_recovered += check_one(fname, note_paths)

    if all_recovered:
        vault.git_commit(
            f"Retroactive coverage recovery: {len(all_recovered)} note(s) from previously-dropped content",
            paths=all_recovered,
        )
    print(f"\n{len(all_recovered)} note(s) recovered total." if all_recovered else "\nnothing to recover.")


if __name__ == "__main__":
    main()
