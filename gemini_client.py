import base64
import itertools
import json
import os
import time

import json_repair
import requests

import config

_keys = None
_key_cycle = None

COOLDOWN_FILE = os.path.join(os.path.dirname(__file__), "key_cooldowns.json")


def _get_keys() -> list[str]:
    global _keys, _key_cycle
    if _keys is None:
        _keys = config.load_gemini_keys()
        _key_cycle = itertools.cycle(range(len(_keys)))
    return _keys


def _models() -> list[str]:
    # Confirmed live: free-tier quota is tracked per (key, model), not just
    # per key -- a key fully exhausted on one model tag returned 200
    # immediately on another. Trying the fallbacks before giving up uses
    # the whole key x model quota matrix instead of stalling the moment
    # the primary model's bucket empties, which happened live within the
    # same session the primary model was first switched to.
    return [config.GEMINI_MODEL] + list(getattr(config, "GEMINI_MODEL_FALLBACKS", []))


def _load_cooldowns() -> dict:
    # Each systemd timer tick is a fresh process -- without this, rotation
    # state (and knowledge of which keys are already exhausted) resets every
    # 2 minutes, so a run can waste its first request on a key another run
    # just found dead seconds ago. Keyed by key prefix, not the full key,
    # so this file never holds a complete credential.
    if not os.path.exists(COOLDOWN_FILE):
        return {}
    try:
        with open(COOLDOWN_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cooldowns(cooldowns: dict) -> None:
    try:
        with open(COOLDOWN_FILE, "w") as f:
            json.dump(cooldowns, f)
    except OSError:
        pass


def _key_id(key: str, model: str) -> str:
    return f"{key[:12]}:{model}"


def _mark_cooldown(key: str, model: str, seconds: float) -> None:
    cooldowns = _load_cooldowns()
    cooldowns[_key_id(key, model)] = time.time() + seconds
    _save_cooldowns(cooldowns)


def _is_cooling_down(key: str, model: str) -> bool:
    cooldowns = _load_cooldowns()
    until = cooldowns.get(_key_id(key, model))
    return until is not None and time.time() < until


def _retry_delay(resp: requests.Response, attempt: int) -> float:
    # 429 responses carry a structured RetryInfo with the actual quota-reset
    # delay -- honor it instead of guessing. Seen live: free-tier flash
    # models are capped at 20 req within a window that ran well past 60s
    # under real usage -- a burst of filing calls (split, then one
    # expansion call per terse note) hits this in real usage, not just
    # hypothetically.
    try:
        for detail in resp.json()["error"].get("details", []):
            if detail.get("@type", "").endswith("RetryInfo"):
                delay_str = detail["retryDelay"]  # e.g. "2.83s"
                return float(delay_str.rstrip("s")) + 1
    except (ValueError, KeyError, TypeError):
        pass
    return 5 * (attempt + 1)


def _try_model(model: str, keys: list[str], payload: dict, timeout: int,
                url_template: str = config.GEMINI_URL,
                validate=None) -> tuple[dict | None, str | None]:
    """Returns (response_json, None) on success, (None, last_error) if every
    key is exhausted/failing for this model -- caller moves on to the next
    model rather than sleep-retrying a model already confirmed dead.

    url_template lets a caller point this same rotation/retry machinery
    (cooldowns, 429/401/403/503 handling) at a different Gemini API
    surface -- e.g. embedContent instead of generateContent -- without
    duplicating any of the failure-handling logic below, which exists
    entirely because of bugs confirmed live on the generateContent path
    and would be exactly as likely to recur on any other endpoint.

    validate: optional callable checked against every 200 body -- returns
    None when the body is actually usable, or a short reason string when
    it isn't. Gemini can return HTTP 200 with NO usable content
    (prompt blocked, SAFETY/RECITATION truncation, or a bare
    finishReason=STOP with zero parts -- all confirmed live). Those are
    model behavior, not key failures, so they retry within the model with
    the same bounded backoff as a 503 and then fall through to the next
    fallback model; before validate existed they raised straight out of
    text_call(), never trying another key or model, and a file that
    reliably triggered them retried forever on the ingest timer."""
    max_attempts = len(keys) + 2
    last_err = None
    keys_tried = 0

    key_idx = next(_key_cycle)
    scanned = 0
    while _is_cooling_down(keys[key_idx], model) and scanned < len(keys) - 1:
        key_idx = next(_key_cycle)
        scanned += 1

    for attempt in range(max_attempts):
        url = url_template.format(model=model, key=keys[key_idx])
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
        except requests.exceptions.RequestException as e:
            # Connection-level failure (DNS blip, read timeout) -- no HTTP
            # response exists to inspect, unlike 429/503 below, but both
            # of these have been hit live in the same session, not
            # hypothetically. Same bounded backoff as the 503 path.
            last_err = str(e)
            if attempt < max_attempts - 1:
                time.sleep(5 * (attempt + 1))
                continue
            return None, last_err

        if resp.status_code == 200:
            body = resp.json()
            problem = validate(body) if validate else None
            if problem is None:
                return body, None
            # A 200 with unusable content is model behavior (recitation/
            # safety truncation, a blocked or empty generation), not a key
            # failure -- same treatment as a 503: bounded retry within this
            # model, then fall through so the caller can try a different
            # model. No cooldown: the key did exactly what it was asked.
            last_err = f"{model}: 200 with unusable content ({problem})"
            if attempt < max_attempts - 1:
                time.sleep(_retry_delay(resp, attempt))
                continue
            return None, last_err

        if resp.status_code in (429, 401, 403):
            # 401/403 means this key is invalid/revoked (for this model or
            # entirely) -- not a transient quota issue, so there's no
            # RetryInfo to honor, but the fix is the same as 429: it's a
            # per-key problem, not a per-call one, so rotate to the next
            # key instead of hard-failing the whole request on one dead
            # key. Confirmed live: 32 straight 401s in one session that
            # should have rotated past a bad key and didn't.
            _mark_cooldown(
                keys[key_idx], model,
                _retry_delay(resp, attempt) if resp.status_code == 429 else 300,
            )
            last_err = resp.text
            if keys_tried < len(keys) - 1:
                key_idx = next(_key_cycle)
                keys_tried += 1
                continue
            # every key exhausted/invalid for this model -- stop burning
            # attempts on it and let the caller try the next model.
            return None, last_err

        if resp.status_code == 503:
            last_err = resp.text
            if attempt < max_attempts - 1:
                time.sleep(_retry_delay(resp, attempt))
                continue
            # Retries within this model exhausted -- a different model may
            # not be under the same load. Confirmed live this matters: a
            # 503 that outlasted every retry used to hard-raise here
            # instead of ever giving the fallback models a chance.
            return None, last_err

        raise RuntimeError(f"Gemini call failed: HTTP {resp.status_code}: {resp.text[:500]}")

    return None, last_err


def _post(payload: dict, timeout: int = 90, models: list[str] | None = None,
           url_template: str = config.GEMINI_URL,
           validate=None) -> dict:
    keys = _get_keys()
    last_err = None
    for model in (models if models is not None else _models()):
        result, err = _try_model(model, keys, payload, timeout,
                                 url_template=url_template, validate=validate)
        if result is not None:
            return result
        last_err = err
    raise RuntimeError(f"Gemini call failed on every model/key: {last_err[:500] if last_err else 'unknown'}")


def _require_text(response: dict) -> "str | None":
    """validate callback for generateContent responses: HTTP 200 alone is
    not success -- Gemini can return a bare finishReason=STOP with no
    parts at all (confirmed live on a Lecture 4 deck that then retried
    every timer tick indefinitely). Naming _text_of's failure conditions
    here keeps them in one place: the rotation now refuses to accept a
    body that text_call would only reject after the fact."""
    candidates = response.get("candidates") or []
    if not candidates:
        return f"no candidates (promptFeedback={response.get('promptFeedback')})"
    if not candidates[0].get("content", {}).get("parts"):
        return f"no content (finishReason={candidates[0].get('finishReason')})"
    return None


def _text_of(response: dict) -> str:
    # Gemini can return 200 with no usable text (prompt blocked before
    # generation, or a candidate that hit SAFETY/RECITATION/MAX_TOKENS with
    # nothing generated) -- confirmed live as a bare `KeyError: 'parts'`
    # that gave no clue what actually happened. Surface the reason instead.
    # (The rotation itself now validates the same conditions via
    # _require_text, so a response reaching here is normally already
    # guaranteed usable -- this stays as the backstop for direct callers
    # and for any provider-side surprise.)
    candidates = response.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini returned no candidates (promptFeedback={response.get('promptFeedback')})")
    candidate = candidates[0]
    parts = candidate.get("content", {}).get("parts")
    if not parts:
        raise RuntimeError(f"Gemini returned no content (finishReason={candidate.get('finishReason')})")
    return parts[0]["text"]


def text_call(prompt: str, json_mode: bool = False) -> str:
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    if json_mode:
        payload["generationConfig"] = {"responseMimeType": "application/json"}
    return _text_of(_post(payload, validate=_require_text))


def vision_call(prompt: str, image_bytes: bytes, mime_type: str = "image/png") -> str:
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": mime_type, "data": b64}},
            ]
        }]
    }
    return _text_of(_post(payload, validate=_require_text))


def json_call(prompt: str) -> dict:
    raw = text_call(prompt, json_mode=True)
    # LLM-generated JSON containing LaTeX routinely ships single backslashes
    # (\text, \begin{cases}, ...) where JSON needs \\ -- seen live, not
    # hypothetical. json_repair tolerates this instead of hard-failing.
    return json_repair.loads(raw)


# gemini-embedding-001 confirmed live (2026-08-30): a real GA model (not
# -preview), returns 3072-dim vectors via embedContent. This is the "no
# extra credential" cloud embedding path -- reuses the exact same keys
# already required for the rest of the pipeline to work at all, so it's
# genuinely zero additional setup for anyone who doesn't have a local
# embedding server (see embeddings.py / EMBEDDING_PROVIDER=gemini).
EMBED_MODEL = "gemini-embedding-001"
EMBED_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:embedContent?key={key}"
)


def embed_call(text: str) -> list[float]:
    payload = {"content": {"parts": [{"text": text}]}}
    response = _post(payload, timeout=30, models=[EMBED_MODEL], url_template=EMBED_URL)
    return response["embedding"]["values"]
