import json
import logging

import requests

from schema import CARD_SCHEMA, validate

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You turn study notes into Anki flashcards. You ONLY use information that is "
    "explicitly written in the provided section body. You NEVER add outside knowledge.\n\n"
    "HARD RULES — violating any of these means the card must be discarded:\n"
    "1. Every answer must be a direct paraphrase or quote of text in the section body. "
    "If the body does not state the answer, do NOT create the card.\n"
    "2. Do NOT define acronyms, terms, or concepts unless the section body itself "
    "defines them. Mentioning a term is NOT the same as defining it.\n"
    "3. Do NOT use general world knowledge, even for things that seem obvious.\n"
    "4. One card = one atomic fact. No compound questions.\n"
    "5. If the section body is meta-commentary, a TODO list, a list of file names, "
    "code without prose, or otherwise not study material, return {\"cards\": []}.\n"
    "6. Output ONLY the JSON. No prose, no explanation, no markdown fences.\n\n"
    "EXAMPLE of a BAD card (do not produce):\n"
    "  Section body: \"We call the local model via Ollama's API.\"\n"
    "  BAD card: {\"question\": \"What does API stand for?\", "
    "\"answer\": \"Application Programming Interface\"}\n"
    "  Why it's bad: the body mentions \"API\" but never defines it. The answer "
    "comes from outside knowledge.\n\n"
    "EXAMPLE of a GOOD card:\n"
    "  Section body: \"The debouncer coalesces bursty events per-path into a "
    "single delayed callback.\"\n"
    "  GOOD card: {\"question\": \"What does the debouncer do?\", "
    "\"answer\": \"Coalesces bursty events per-path into a single delayed callback.\"}"
)


def _build_user_prompt(heading_path, body):
    return (
        f"Section heading path: {heading_path}\n\n"
        f"Section body:\n\"\"\"\n{body}\n\"\"\"\n\n"
        "Produce cards ONLY for facts stated directly in the section body above. "
        "If the body defines nothing worth memorizing, return an empty cards array. "
        "Return JSON of the form "
        "{\"cards\": [{\"question\": \"...\", \"answer\": \"...\", \"tags\": [\"...\"]}]}."
    )


_STRIP_KEYS = {"minLength", "maxLength", "minItems", "maxItems", "additionalProperties"}


def _grammar_safe(schema):
    """Strip JSON Schema keywords that Ollama's grammar generator rejects."""
    if isinstance(schema, dict):
        return {k: _grammar_safe(v) for k, v in schema.items() if k not in _STRIP_KEYS}
    if isinstance(schema, list):
        return [_grammar_safe(v) for v in schema]
    return schema


def _call_once(cfg, heading_path, body):
    url = cfg["ollama_url"].rstrip("/") + "/api/chat"
    payload = {
        "model": cfg["ollama_model"],
        "stream": False,
        "format": _grammar_safe(CARD_SCHEMA),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(heading_path, body)},
        ],
        "options": {"temperature": 0.2},
    }
    r = requests.post(url, json=payload, timeout=cfg["ollama_timeout_seconds"])
    r.raise_for_status()
    data = r.json()
    content = data.get("message", {}).get("content", "").strip()
    if not content:
        raise ValueError("Ollama returned empty content")
    try:
        obj = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Ollama returned non-JSON: {e}: {content[:200]!r}")
    ok, err = validate(obj)
    if not ok:
        raise ValueError(f"Schema validation failed: {err}")
    return obj


def generate_cards(cfg, heading_path, body):
    """
    Send a section to Ollama, expect JSON matching CARD_SCHEMA.
    Retries once on failure. On second failure returns None (caller logs + skips).
    """
    try:
        return _call_once(cfg, heading_path, body)
    except Exception as e:
        log.warning("Ollama attempt 1 failed for %s: %s", heading_path, e)
    try:
        return _call_once(cfg, heading_path, body)
    except Exception as e:
        log.error("Ollama attempt 2 failed for %s: %s — skipping", heading_path, e)
        return None
