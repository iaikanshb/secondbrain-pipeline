"""Semantic duplicate check, run before a new note is written -- catches
content that restates an existing note under a different title. This is the
actual cause of duplication observed live: coverage-recovery (or a later,
unrelated lecture) independently re-derives a title for content a sibling
note already covers, and vault.py's merge only fires on an *exact* title
match, so a differently-worded title just becomes a second, overlapping
file.

Deliberately conservative: only fires on genuine restatement of the same
idea. A related-but-distinct idea from a different course (the same concept
taught two different ways) must stay two notes that link to each other --
collapsing that would destroy the exact cross-course comparison this vault
exists to preserve. When in doubt, this says "distinct."
"""
import os
import re

import config
import embeddings

STOP = {"a", "an", "the", "of", "in", "on", "and", "or", "to", "for", "with", "vs", "via", "using", "is", "are"}


def _tokens(title: str) -> set:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return {w for w in words if w not in STOP and len(w) > 1}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_candidates(title: str, tags: list[str], existing: list[dict], limit: int = 3) -> list[dict]:
    """Cheap, local, no-LLM narrowing -- title-token overlap, or weaker
    title overlap backed by strong tag overlap -- so the expensive semantic
    check below only runs against plausible matches, not every note in the
    vault. Thresholds match what was confirmed live to separate real
    candidates from noise on this vault's actual note titles."""
    tok = _tokens(title)
    tagset = {t.lower() for t in tags}
    scored = []
    for n in existing:
        if n["title"] == title:
            continue
        tj = _jaccard(tok, _tokens(n["title"]))
        gj = _jaccard(tagset, {t.lower() for t in n.get("tags", [])})
        if tj >= 0.4 or (tj >= 0.2 and gj >= 0.5):
            scored.append((tj + gj, n))
    scored.sort(key=lambda x: -x[0])
    return [n for _, n in scored[:limit]]


def find_semantic_candidates(title: str, body: str, existing: list[dict], limit: int = 3,
                              min_score: float = 0.65) -> list[dict]:
    """Catches the case find_candidates() structurally can't: a genuine
    duplicate worded so differently that titles/tags share nothing (e.g.
    two lectures covering the same idea with no vocabulary overlap at
    all). No-ops to an empty list if embeddings aren't configured -- purely
    additive on top of the heuristic above, never a replacement for it, so
    a missing/unreachable embedding server only means fewer candidates
    checked, never a behavior change beyond that."""
    if not embeddings.available():
        return []
    vector = embeddings.embed(f"{title}\n\n{body[:4000]}")
    if not vector:
        return []
    by_title = {n["title"]: n for n in existing}
    hits = embeddings.search(vector, limit=limit, exclude=title)
    return [by_title[t] for t, score in hits if score >= min_score and t in by_title]


DUP_PROMPT = """Two notes from a student's exam-study vault. Judge honestly -- most \
title-similar notes are actually distinct, correctly-separate concepts that just share \
vocabulary (e.g. two different courses' "Course Logistics" notes, or "HTTP Request Format" \
vs "HTTP Response Format" -- different concepts, not duplicates).

A TRUE duplicate means: the new content substantively restates what the existing note \
already says, such that a student reading both is re-reading the same material. This can \
include the same concept from a different course IF the explanation is genuinely the same \
content -- but if the two courses teach it with different notation, emphasis, or examples, \
that is NOT a duplicate, it's a valuable cross-course comparison and must stay two separate, \
linked notes. When genuinely unsure, answer false.

Existing note, titled "{existing_title}":
---
{existing_body}
---

New content proposed under the title "{new_title}":
---
{new_body}
---

Respond with JSON: {{"duplicate": true or false, "reasoning": "one sentence"}}"""


def _read_body(title: str) -> str | None:
    path = os.path.join(config.NOTES, title + ".md")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        content = f.read()
    end = content.find("\n---", 3)
    return content[end + 4:].lstrip("\n") if end != -1 else content


def check_duplicate(new_title: str, new_body: str, candidate: dict) -> bool:
    existing_body = _read_body(candidate["title"])
    if existing_body is None:
        return False
    import gemini_client
    result = gemini_client.json_call(
        DUP_PROMPT.format(
            existing_title=candidate["title"], existing_body=existing_body,
            new_title=new_title, new_body=new_body,
        )
    )
    return bool(isinstance(result, dict) and result.get("duplicate"))


def resolve_title(title: str, body: str, tags: list[str], existing: list[dict]) -> str:
    """Returns the title this note should actually be written under: the
    proposed title if genuinely new/distinct, or an existing note's exact
    title if this is a real duplicate -- so vault.py's existing same-title
    merge path fires and combines them instead of creating a near-duplicate
    file. Checks candidates from both the cheap heuristic and (if
    configured) semantic search, deduplicated, first confirmed duplicate
    wins."""
    seen_titles = set()
    candidates = []
    for candidate in find_candidates(title, tags, existing) + find_semantic_candidates(title, body, existing):
        if candidate["title"] not in seen_titles:
            seen_titles.add(candidate["title"])
            candidates.append(candidate)

    for candidate in candidates:
        if check_duplicate(title, body, candidate):
            return candidate["title"]
    return title
