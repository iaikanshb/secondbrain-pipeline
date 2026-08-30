import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv() -> None:
    """Auto-loads .env if present, without overriding anything already set
    in the real environment -- explicit `set -a; source .env; set +a` (as
    documented in the README) still works exactly as before and takes
    precedence. This exists so interactive one-off scripts (e.g. search.py)
    don't need manual sourcing every session; the systemd timer loads the
    same file via its own EnvironmentFile= directive instead."""
    env_path = os.path.join(PROJECT_ROOT, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip()


_load_dotenv()

VAULT = os.path.expanduser(
    os.environ.get("SECONDBRAIN_VAULT", "~/Documents/second-brain")
)
# Two drop zones, not one -- which folder a file lands in decides how it's
# processed, not a page-count guess. A long lecture deck is still a lecture,
# even well past a naive page-count heuristic; a short research paper is
# still a grounding source. Explicit beats inferred.
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

# Optional semantic layer (search.py, dedupe.py's second candidate pass).
# Two provider modes, both opt-in -- with neither configured, every
# embeddings.py function is a no-op and the vault behaves exactly as it
# did before this existed:
#
#   openai_compatible (default once EMBEDDING_URL is set) -- any endpoint
#   that answers POST <url> {"input": "..."} the way /v1/embeddings does:
#   a local llama.cpp/Ollama server, a cloud provider, anything.
#
#   gemini (EMBEDDING_PROVIDER=gemini) -- no local server or separate
#   account needed: reuses the exact same GEMINI_API_KEY / key-rotation
#   already required for the rest of the pipeline, via gemini_client's
#   embed_call(). The zero-extra-setup option for anyone without a local
#   embedding server. Deliberately NOT the default even when a Gemini key
#   is already configured (which is everyone) -- opting into it must be
#   explicit, since it adds real load to the same free-tier quota the
#   filing pipeline depends on.
EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "openai_compatible").strip().lower()
EMBEDDING_URL = os.environ.get("EMBEDDING_URL", "")
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
# Lives inside the vault itself (VAULT, not PROJECT_ROOT) so it rides
# along on the same sync as everything else -- built only on whichever
# machine runs ingest.py, read from any machine that has the vault.
EMBEDDINGS_DB = os.path.join(VAULT, ".embeddings.db")

# gemini-flash-latest and gemini-2.5-flash both failed live (503 / 404).
# gemini-3.6-flash worked initially but its free-tier quota (20 req,
# tracked per-model per-project) was fully exhausted across all configured
# keys by heavy same-day testing, with no relief after real waits --
# confirmed live that quota is tracked per model, not just per key:
# gemini-3.7-flash returned 200 on the same exhausted key, both text and
# vision. Switched to it to actually unblock the pipeline rather than wait
# out an unknown recovery window.
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
# rotation, one per line. Kept as a local, gitignored file dedicated to
# this project rather than shared with anything else. Optional: fine for
# this to not exist.
EXTRA_GEMINI_KEYS_FILES = (
    os.path.join(PROJECT_ROOT, "gemini-keys.txt"),
    os.path.join(PROJECT_ROOT, "gemini_keys.txt"),
)


def load_gemini_keys() -> list[str]:
    """Load the primary key from the environment, plus the first available
    local rotation-key file."""
    keys = []
    primary_key = os.environ.get("GEMINI_API_KEY", "").strip()
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
