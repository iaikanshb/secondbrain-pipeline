import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
KEYFILE = os.path.expanduser(
    os.environ.get("SECONDBRAIN_KEYFILE", "~/llmstack/config/cloud-keys.env")
)
VAULT = os.path.expanduser(
    os.environ.get("SECONDBRAIN_VAULT", "~/Documents/second-brain")
)
# Two drop zones, not one -- which folder a file lands in decides how it's
# processed, not a page-count guess. A long lecture deck (this user's decks
# routinely exceed the old 40-page heuristic) is still a lecture; a short
# research paper is still a grounding source. Explicit beats inferred.
INBOX_LECTURES = os.path.join(VAULT, "00-Inbox-Lectures")
INBOX_SOURCES = os.path.join(VAULT, "00-Inbox-Sources")
NOTES = os.path.join(VAULT, "01-Notes")
RESOURCES = os.path.join(VAULT, "02-Resources")
ATTACHMENTS = os.path.join(VAULT, "03-Attachments")
PRACTICE = os.path.join(VAULT, "04-Practice")
# One MOC per source lecture (narrated links to the topic notes it produced)
# plus one rolled-up MOC per course -- the actual "read this to study" entry
# points, additive on top of the atomic/topic note layer, not a replacement.
MOCS = os.path.join(VAULT, "05-MOCs")
# Anki-importable front\tback TSV, one file per course, appended to per
# lecture -- retrieval practice piggybacks on Anki's own scheduler instead
# of this project reimplementing spaced repetition.
FLASHCARDS = os.path.join(VAULT, "06-Flashcards")

TEXTBOOK_DB = os.path.join(PROJECT_ROOT, "textbook_index.db")

# gemini-flash-latest and gemini-2.5-flash both failed live (503 / 404).
# gemini-3.6-flash worked initially but its free-tier quota (20 req,
# tracked per-model per-project) was fully exhausted across all 5 keys by
# heavy same-day testing, with no relief after real waits -- confirmed live
# that quota is tracked per model, not just per key: gemini-3.7-flash
# (same model llmstack's own router already uses successfully) returned
# 200 on the same exhausted key, both text and vision. Switched to it
# 2026-08-19 to actually unblock the pipeline rather than wait out an
# unknown recovery window.
GEMINI_MODEL = "gemini-3.7-flash"

# gemini-3.7-flash's own quota then also ran dry the same session (confirmed
# live: 20-req free tier, same as every other flash tag tried). Since quota
# is tracked per (key, model), gemini_client tries these in order, across
# every key, before giving up entirely -- both confirmed live to return 200
# on a key already exhausted for gemini-3.7-flash.
GEMINI_MODEL_FALLBACKS = ["gemini-3.5-flash", "gemini-flash-lite-latest"]

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent?key={key}"
)

# A page's extracted text layer shorter than this is treated as "no real
# text layer" (scanned/handwritten) rather than "typed page with little
# text" -- short enough not to misclassify a sparse typed page (e.g. a
# title page), long enough that OCR garbage/noise doesn't pass for text.
TEXT_LAYER_MIN_CHARS = 20


# Extra Gemini API keys (separate Google Cloud projects) for rate-limit
# rotation, one per line. Deliberately NOT llmstack/config/cloud-keys.env --
# that file is shared with the coding-agent stack and stays untouched by
# this project. Optional: fine for this to not exist.
EXTRA_GEMINI_KEYS_FILES = (
    os.path.join(PROJECT_ROOT, "gemini-keys.txt"),
    os.path.join(PROJECT_ROOT, "gemini_keys.txt"),
)


def load_key(name: str) -> str | None:
    """Load a key from the environment, then the optional legacy key file."""
    value = os.environ.get(name, "").strip()
    if value:
        return value

    if os.path.exists(KEYFILE):
        with open(KEYFILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip()
    return None


def load_gemini_keys() -> list[str]:
    """Load the primary key and the first available local rotation-key file."""
    keys = []
    primary_key = load_key("GEMINI_API_KEY")
    if primary_key:
        keys.append(primary_key)

    for keys_file in EXTRA_GEMINI_KEYS_FILES:
        if not os.path.exists(keys_file):
            continue
        with open(keys_file) as f:
            keys += [line.strip() for line in f if line.strip() and not line.startswith("#")]
        break

    keys = list(dict.fromkeys(keys))
    if not keys:
        raise RuntimeError(
            "No Gemini API key configured. Set GEMINI_API_KEY or add "
            "gemini-keys.txt (legacy: gemini_keys.txt)."
        )
    return keys
