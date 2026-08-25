"""Vision OCR for pages with no extractable text layer (scanned/handwritten).

Two passes per page, not one:
  1. transcribe -- verbatim transcription against the raw image.
  2. verify     -- shown the image AND its own transcript, asked to correct
                    anything wrong rather than re-derive from scratch.
No pipeline can guarantee zero missed characters on real handwriting; the
second pass catches a meaningful share of first-pass slips, and anything
still uncertain is surfaced explicitly rather than silently trusted.
"""
from dataclasses import dataclass

import gemini_client

TRANSCRIBE_PROMPT = """Transcribe this page image verbatim, line by line, in reading order.

Rules:
- Preserve every line break as in the source.
- Render mathematical notation as LaTeX, wrapped in $...$ or $$...$$.
- If a word or phrase is illegible, write [illegible: your best guess] inline, do not skip it.
- If there is a diagram/figure with no transcribable text, write [DIAGRAM: brief description] at that point.
- Do not summarize, do not skip anything, do not add commentary. Output only the transcription."""

VERIFY_PROMPT_TEMPLATE = """Here is a page image and a draft transcription of it made in a separate pass.
Re-check the draft against the image line by line. Fix any wrong, missing, or extra text.
Do not shorten or summarize -- only correct.

Draft transcription:
---
{draft}
---

Output the corrected full transcription. If you found and fixed at least one real error, \
start your response with the exact line "CORRECTED: yes" on its own, otherwise start with \
"CORRECTED: no" on its own, then a blank line, then the transcription."""


@dataclass
class PageResult:
    text: str
    needed_correction: bool
    has_illegible: bool


def transcribe_and_verify(image_bytes: bytes) -> PageResult:
    draft = gemini_client.vision_call(TRANSCRIBE_PROMPT, image_bytes)
    verified_raw = gemini_client.vision_call(
        VERIFY_PROMPT_TEMPLATE.format(draft=draft), image_bytes
    )

    first_line, _, rest = verified_raw.partition("\n")
    if first_line.strip().upper().startswith("CORRECTED:"):
        needed_correction = "yes" in first_line.lower()
        text = rest.lstrip("\n")
    else:
        # Model didn't follow the format -- fail safe: treat as corrected
        # so it gets flagged for human review rather than trusted blindly.
        needed_correction = True
        text = verified_raw

    return PageResult(
        text=text.strip(),
        needed_correction=needed_correction,
        has_illegible="[illegible" in text.lower(),
    )
