"""
Gemini evaluation service.

Sends collected hackathon content to Gemini 2.5 Flash together with the two
MLH reference URLs (fetched live via the url_context tool).  The model
generates the questions a first-time hacker would ask on event day and checks
each question against the organiser's provided content.

Environment variable required:
  GEMINI_API_KEY   your Google AI Studio API key
"""

from __future__ import annotations

import json
import os

MLH_GUIDE_URL = "https://guide.mlh.io/"
MLH_GUIDELINES_URL = (
    "https://github.com/MLH/mlh-policies/blob/main/member-event-guidelines.md"
)

_PROMPT_TEMPLATE = """\
You are an expert MLH hackathon evaluator helping organizers prepare their event.

YOUR TASK
---------
1. Read both MLH reference documents (URLs provided below) so you know exactly
   what a high-quality MLH Member Event must communicate to attendees.
2. Generate 15-25 practical questions a first-time hacker would want answered
   BEFORE OR ON THE DAY OF the event. Cover: check-in logistics, schedule,
   location/directions, food/meals, team formation, overnight policy, prizes,
   project submission/judging, Wi-Fi, code of conduct, and any other day-of
   concerns that MLH guidelines say organizers must address.
3. For each question, check whether the answer appears in the hackathon content
   provided below.

MLH REFERENCE DOCUMENTS (read both via their URLs):
  - Organizer Guide:        {guide_url}
  - Member Event Guidelines: {guidelines_url}

HACKATHON CONTENT PROVIDED BY THE ORGANISER:
---------------------------------------------
{content_block}
---------------------------------------------

RESPONSE FORMAT
---------------
Return ONLY a valid JSON array with no explanation text outside it.
Each element must have exactly these keys:

  "question"        string  the hacker's specific practical question
  "status"          string  one of exactly: "found", "partial", "not_found"
  "source"          string  which source answered it ("website", "instagram",
                            "document: <filename>") or null if not found
  "answer_excerpt"  string  brief quote or summary from the source, or null
  "recommendation"  string  if status is not "found": a specific, actionable
                            suggestion for where and how the organiser should
                            add this information; empty string if "found"
"""


def _build_content_block(context: dict) -> str:
    parts: list[str] = []
    sources = context.get("sources", {})

    if "website" in sources:
        site = sources["website"]
        text = (site.get("text") or "")[:25_000]
        parts.append(f"[WEBSITE - {site['url']}]\n{text}")

    if "instagram" in sources:
        ig = sources["instagram"]
        captions = ig.get("captions") or []
        if captions:
            lines = "\n".join(f"  - {c}" for c in captions[:60])
            parts.append(f"[INSTAGRAM - @{ig['username']}]\n{lines}")

    if "documents" in sources:
        for doc in sources["documents"]:
            text = (doc.get("text") or "")[:8_000]
            if text:
                parts.append(f"[DOCUMENT - {doc['filename']}]\n{text}")

    return "\n\n".join(parts) if parts else "(no content provided)"


def evaluate(context: dict) -> list[dict]:
    """Run the Gemini evaluation and return a list of question-answer dicts.

    Raises:
        ValueError   if GEMINI_API_KEY is not set.
        RuntimeError if the API returns non-JSON or an unexpected structure.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY is not set. Add it to your environment or .env file."
        )

    from google import genai  # noqa: PLC0415
    from google.genai.types import GenerateContentConfig  # noqa: PLC0415

    client = genai.Client(api_key=api_key)
    content_block = _build_content_block(context)
    prompt = _PROMPT_TEMPLATE.format(
        guide_url=MLH_GUIDE_URL,
        guidelines_url=MLH_GUIDELINES_URL,
        content_block=content_block,
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[prompt],
        config=GenerateContentConfig(tools=[{"url_context": {}}]),
    )

    raw = (response.text or "").strip()
    # Strip markdown code fences if the model wrapped JSON in ```json ... ```.
    if raw.startswith("```"):
        raw = raw.lstrip("`")
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rstrip("`").strip()

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Gemini returned non-JSON content: {raw[:300]!r}"
        ) from exc

    if not isinstance(result, list):
        raise RuntimeError(
            f"Expected a JSON array, got {type(result).__name__}"
        )

    # Normalise every item so the template can safely access all keys.
    return [
        {
            "question":       str(item.get("question", "")),
            "status":         item.get("status", "not_found"),
            "source":         item.get("source") or None,
            "answer_excerpt": item.get("answer_excerpt") or None,
            "recommendation": item.get("recommendation") or "",
        }
        for item in result
    ]
