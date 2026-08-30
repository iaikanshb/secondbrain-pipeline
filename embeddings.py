"""Local semantic layer on top of the vault, via any OpenAI-compatible
embeddings endpoint (this project has no opinion on which -- a local
llama.cpp/Ollama server, a cloud provider, whatever EMBEDDING_URL points
at). Entirely optional and additive: every function here degrades to a
no-op/None on any failure (unset config, unreachable server, corrupt
index) rather than raising, because nothing that depends on this --
dedupe's candidate search, the standalone search.py tool -- may ever be
allowed to break the vault itself if the embedding server is off or
misconfigured.

The index (EMBEDDINGS_DB) lives inside the vault on purpose, not next to
this script, so it syncs the same way everything else does: written only
by whichever machine runs ingest.py, read from any machine that has the
vault synced. Plain rollback-journal SQLite (not WAL, which needs
sidecar -wal/-shm files copied atomically -- a real risk for a
Syncthing-synced file, same reasoning as excluding .git from sync
entirely) so a mid-write file-sync snapshot is at worst a stale read, not
a multi-file inconsistency.
"""
import json
import math
import os
import sqlite3
import urllib.error
import urllib.request

import config


def available() -> bool:
    return bool(config.EMBEDDING_URL)


def embed(text: str) -> list[float] | None:
    if not available():
        return None
    payload = json.dumps({"input": text}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.EMBEDDING_API_KEY:
        headers["Authorization"] = f"Bearer {config.EMBEDDING_API_KEY}"
    req = urllib.request.Request(config.EMBEDDING_URL, data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data["data"][0]["embedding"]
    except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError):
        return None


def _db() -> sqlite3.Connection | None:
    try:
        conn = sqlite3.connect(config.EMBEDDINGS_DB, timeout=5)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("CREATE TABLE IF NOT EXISTS vectors (title TEXT PRIMARY KEY, vector TEXT)")
        return conn
    except sqlite3.Error:
        return None


def store(title: str, vector: list[float]) -> None:
    conn = _db()
    if conn is None:
        return
    try:
        conn.execute("INSERT OR REPLACE INTO vectors (title, vector) VALUES (?, ?)",
                     (title, json.dumps(vector)))
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def reindex_note(title: str, body: str) -> None:
    """Best-effort: embed and store one note. Called after every write in
    filing.py (fresh notes and dedupe merges alike) so the index never
    drifts from what's actually on disk. Silently does nothing if
    embeddings aren't configured/reachable -- never blocks or fails the
    actual note write that triggered it."""
    if not available():
        return
    vector = embed(f"{title}\n\n{body[:4000]}")
    if vector:
        store(title, vector)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def search(query_vector: list[float], limit: int = 10, exclude: str | None = None) -> list[tuple[str, float]]:
    conn = _db()
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT title, vector FROM vectors").fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    scored = []
    for title, vec_json in rows:
        if title == exclude:
            continue
        try:
            vec = json.loads(vec_json)
        except (json.JSONDecodeError, TypeError):
            continue
        scored.append((title, _cosine(query_vector, vec)))
    scored.sort(key=lambda x: -x[1])
    return scored[:limit]


def indexed_titles() -> set[str]:
    conn = _db()
    if conn is None:
        return set()
    try:
        return {r[0] for r in conn.execute("SELECT title FROM vectors").fetchall()}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()
