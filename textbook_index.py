"""Local, offline full-text index over reference sources (textbooks, papers
-- anything dropped in 00-Inbox-Sources/), for grounding terse slide
content. No LLM calls for the indexing itself, no GPU, no rate-limit
exposure -- SQLite FTS5 over directly-extracted text (sources are typed
PDFs; the rare non-text page is skipped, not OCR'd -- best-effort grounding,
not the lossless-transcript requirement lecture material has).
"""
import datetime
import re
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


def index_textbook(pdf_path: str, course: str,
                    result: "extract.ExtractResult | None" = None) -> tuple[int, str]:
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
    p.add_argument("pdf")
    p.add_argument("--course", required=True)
    args = p.parse_args()

    n, course_used = index_textbook(args.pdf, args.course)
    print(f"indexed {n} pages under course '{course_used}'")
