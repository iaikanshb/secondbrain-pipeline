"""Local, offline full-text index over reference sources (textbooks, papers
-- anything dropped in 00-Inbox-Sources/), for grounding terse slide
content. No LLM calls for the indexing itself, no GPU, no rate-limit
exposure -- SQLite FTS5 over directly-extracted text (sources are typed
PDFs; the rare non-text page is skipped, not OCR'd -- best-effort grounding,
not the lossless-transcript requirement lecture material has).
"""
import datetime
import json
import os
import re
import shutil
import sqlite3

import config
import courses
import extract


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.TEXTBOOK_DB)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY,
            filename TEXT NOT NULL,
            course TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
            book_id UNINDEXED, course UNINDEXED, page UNINDEXED, text
        )"""
    )
    return conn


normalize_course = courses.normalize  # re-exported: existing call sites use this name


def _manifest_path() -> str:
    return config.TEXTBOOK_MANIFEST


def _load_manifest() -> dict:
    if not os.path.exists(_manifest_path()):
        return {}
    try:
        with open(_manifest_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_manifest(manifest: dict) -> None:
    with open(_manifest_path(), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")


def _record_in_manifest(filename: str, course: str, pages: int) -> None:
    manifest = _load_manifest()
    entry = manifest.get(filename, {})
    if entry.get("course") == course and entry.get("pages") == pages:
        return
    manifest[filename] = {"course": course, "pages": pages}
    _save_manifest(manifest)


def _migrate_legacy_db() -> None:
    """The DB used to live in the project root (gitignored) -- a lost repo
    regeneration silently emptied the index (confirmed live 2026-08-25: the
    EML/CNS/HMC/DS/MMD/CN sources all "disappeared" while their archived
    PDFs sat untouched in 02-Resources/). Move it into the vault, where it
    rides the same sync as the sources it indexes. No-op when there's
    nothing legacy to move or the vault copy already exists."""
    legacy = getattr(config, "LEGACY_TEXTBOOK_DB", "")
    if not legacy or not os.path.exists(legacy):
        return
    if os.path.exists(config.TEXTBOOK_DB):
        os.remove(legacy)
        return
    shutil.move(legacy, config.TEXTBOOK_DB)


def _verify_indexed_pages(book_id: int, expected_pages: int) -> bool:
    """A crash between the books INSERT and the last pages INSERT (or a
    truncated sync of the DB file) can leave a book row whose page rows are
    incomplete -- has_book_for() then says yes while search() quietly
    misses most of the book. Cheap COUNT(*) check."""
    conn = _connect()
    try:
        (actual,) = conn.execute(
            "SELECT COUNT(*) FROM pages WHERE book_id = ?", (book_id,)
        ).fetchone()
    finally:
        conn.close()
    return actual >= expected_pages


def self_heal() -> list[str]:
    """Re-index any source recorded in the manifest but missing (or
    under-page-count) in the live DB, migrating a legacy project-root DB
    first. Pure local extraction against PDFs already archived in
    02-Resources/ -- no LLM calls, so it's safe to run before every ingest
    pass. Returns the list of filenames that were (re)indexed; [] when the
    index is already healthy.

    This is the structural close of the empty-index failure mode: the DB
    is a rebuildable cache, but the manifest (committed in the vault) is
    durable, so a vanished cache is now an event the pipeline repairs on
    its next run instead of a silent capability loss."""
    _migrate_legacy_db()

    manifest = _load_manifest()
    if not manifest:
        return []

    conn = _connect()
    books = {
        row[0]: {"id": row[1], "pages": row[2]}
        for row in conn.execute(
            "SELECT filename, id, (SELECT COUNT(*) FROM pages WHERE book_id = books.id) "
            "FROM books"
        ).fetchall()
    }
    conn.close()

    healed = []
    for filename, meta in sorted(manifest.items()):
        expected = meta.get("pages", 0)
        existing = books.get(filename)
        if existing and expected and existing["pages"] >= expected:
            continue

        pdf_path = os.path.join(config.RESOURCES, filename)
        if not os.path.exists(pdf_path):
            # Source PDF gone from the archive -- nothing to rebuild from.
            # Manifest entry stays (it's historical truth); the gap is
            # reported here on every run instead of failing silently.
            print(f"[self-heal] {filename} missing from {config.RESOURCES}, cannot rebuild")
            continue

        pages, course_used = index_textbook(pdf_path, meta.get("course", ""), skip_manifest=True)
        healed.append(filename)
        print(f"[self-heal] re-indexed {filename}: {pages} pages (course={course_used})")

    return healed


def rebuild_manifest() -> dict:
    """Reconstruct the manifest from the live DB -- only needed once, when
    adopting this system with an existing index that predates the manifest
    (there is no other durable record of what was indexed). Not run
    automatically: by design, self_heal() trusts the manifest, so writing
    it from a possibly-incomplete DB is a deliberate human decision."""
    conn = _connect()
    rows = conn.execute(
        "SELECT filename, course, (SELECT COUNT(*) FROM pages WHERE book_id = books.id) "
        "FROM books"
    ).fetchall()
    conn.close()
    manifest = {r[0]: {"course": r[1], "pages": r[2]} for r in rows}
    _save_manifest(manifest)
    return manifest


def index_textbook(pdf_path: str, course: str,
                   result: "extract.ExtractResult | None" = None,
                   skip_manifest: bool = False) -> tuple[int, str]:
    """Returns (pages indexed, the actual course used) -- course goes through
    courses.reconcile() internally, so it can differ from the `course`
    argument the caller passed in; return it so the caller's own logging/
    commit message reflects reality, not the pre-reconciliation guess.

    result: pass an already-computed ExtractResult to avoid re-extracting
    (ingest.py does this, since it needs the same extraction for course
    inference); otherwise extracted fresh here (used by the standalone CLI)."""
    filename = pdf_path.split("/")[-1]
    if result is None:
        result = extract.extract_pdf(pdf_path, skip_ocr=True)

    excerpt = "\n".join(p for p in result.pages[:5] if p)
    course_norm = courses.reconcile(course, excerpt)

    conn = _connect()
    # Idempotent: re-dropping the same book (same filename+course) replaces
    # its entries rather than accumulating duplicate rows that would double
    # up search results.
    for (old_id,) in conn.execute(
        "SELECT id FROM books WHERE filename = ? AND course = ?", (filename, course_norm)
    ).fetchall():
        conn.execute("DELETE FROM pages WHERE book_id = ?", (old_id,))
        conn.execute("DELETE FROM books WHERE id = ?", (old_id,))

    cur = conn.execute(
        "INSERT INTO books (filename, course, indexed_at) VALUES (?, ?, ?)",
        (filename, course_norm, datetime.datetime.now().isoformat()),
    )
    book_id = cur.lastrowid

    indexed = 0
    for page_num, text in enumerate(result.pages, start=1):
        if not text:
            continue
        conn.execute(
            "INSERT INTO pages (book_id, course, page, text) VALUES (?, ?, ?, ?)",
            (book_id, course_norm, page_num, text),
        )
        indexed += 1
    conn.commit()
    conn.close()

    # Record what SHOULD be in the index after this succeeds. The manifest
    # is the durable half of the design (see self_heal()); skip it for
    # self-heal's own re-index calls, which are manifest-driven already --
    # recording there would be a no-op, but skipping keeps the invariant
    # explicit: manifest entries come from fresh ingests only.
    if not skip_manifest:
        _record_in_manifest(filename, course_norm, indexed)

    return indexed, course_norm


def has_book_for(course: str) -> bool:
    course_norm = normalize_course(course)
    if not course_norm:
        return False
    conn = _connect()
    row = conn.execute("SELECT COUNT(*) FROM books WHERE course = ?", (course_norm,)).fetchone()
    conn.close()
    return row[0] > 0


def search(course: str, query: str, top_k: int = 3) -> list[dict]:
    course_norm = normalize_course(course)
    if not course_norm:
        return []
    tokens = re.findall(r"\w+", query)
    if not tokens:
        return []
    fts_query = " OR ".join(f'"{t}"' for t in tokens)

    conn = _connect()
    rows = conn.execute(
        """
        SELECT pages.page, pages.text, books.filename, bm25(pages) AS rank
        FROM pages JOIN books ON pages.book_id = books.id
        WHERE pages.course = ? AND pages MATCH ?
        ORDER BY rank LIMIT ?
        """,
        (course_norm, fts_query, top_k),
    ).fetchall()
    conn.close()
    return [{"page": r[0], "text": r[1], "book": r[2]} for r in rows]


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pdf", nargs="?")
    p.add_argument("--course")
    p.add_argument("--rebuild-manifest", action="store_true",
                   help="reconstruct .textbook_manifest.json from the live DB "
                        "(one-time adoption for an index that predates the manifest)")
    args = p.parse_args()

    if args.rebuild_manifest:
        manifest = rebuild_manifest()
        print(f"manifest rebuilt from live DB: {len(manifest)} source(s)")
    elif args.pdf and args.course:
        n, course_used = index_textbook(args.pdf, args.course)
        print(f"indexed {n} pages under course '{course_used}'")
    else:
        p.error("provide a pdf and --course, or --rebuild-manifest")
