"""Single source of truth for course-code normalization. Used everywhere a
course gets written or matched -- not just at DB-query time -- so 'CS101',
'CS101 2026', and 'cs101' collapse to the same stored value instead of
silently fragmenting into lookalike buckets (seen live: three separate
spellings for one course before this existed)."""
import os
import re

CANONICAL_FILE = os.path.join(os.path.dirname(__file__), "courses.txt")


def normalize(course: str) -> str:
    m = re.match(r"[A-Za-z]+", course or "")
    return m.group(0).upper() if m else ""


def canonical() -> list[str]:
    """The user's actual course list (courses.txt), normalized. Empty list
    if the file doesn't exist -- course inference then falls back to
    whatever's already in the vault, same as before this existed."""
    if not os.path.exists(CANONICAL_FILE):
        return []
    with open(CANONICAL_FILE) as f:
        return sorted({normalize(line) for line in f if line.strip() and not line.startswith("#")})


RECONCILE_PROMPT = """A document was tentatively guessed as course code "{guess}", but that doesn't \
exactly match any of the user's real courses: {canonical}

Content sample:
---
{excerpt}
---

Does this content genuinely belong to one of the real courses listed above? Prompting alone has \
already been observed inventing near-miss codes here (e.g. "CNS" instead of "CN") even when told \
to reuse an existing one -- so choose carefully. If yes, respond with that EXACT code from the list \
and nothing else. If it genuinely matches none of them, respond with exactly: NONE"""


def reconcile(guess: str, excerpt: str) -> str:
    """If `guess` already matches the canonical list (or there is no
    canonical list yet), return it as-is -- zero extra cost, this is the
    common case. Only on an actual mismatch does this spend one more call,
    forcing a strict choice from the real list instead of trusting the
    original free-text guess to have gotten it right."""
    norm_guess = normalize(guess)
    canon = canonical()
    if not canon or norm_guess in canon:
        return norm_guess

    import gemini_client  # deferred: avoids a hard dependency for pure-normalization callers
    raw = gemini_client.text_call(
        RECONCILE_PROMPT.format(guess=norm_guess, canonical=", ".join(canon), excerpt=excerpt[:1500])
    ).strip()
    if raw.upper() == "NONE":
        return norm_guess
    reconciled = normalize(raw)
    return reconciled if reconciled in canon else norm_guess
