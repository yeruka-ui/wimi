import json
import logging

import requests

from schema import CARD_SCHEMA, validate

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an assistant that turns study notes into Anki flashcards. "
    "Read the provided section (from an Obsidian note) and produce a set of "
    "high-quality question/answer flashcards covering every important fact, "
    "definition, or idea in the text — not just the heading. "
    "Rules:\n"
    "- One card = one atomic fact.\n"
    "- Questions must be answerable from the section alone.\n"
    "- Answers must be concise but self-contained.\n"
    "- Skip meta-commentary, TODOs, and empty sections (return an empty cards array).\n"
    "- Output ONLY valid JSON matching the required schema. No prose."
)


def _build_user_prompt(heading_path, body):
    return (
        f"Section heading path: {heading_path}\n\n"
        f"Section body:\n\"\"\"\n{body}\n\"\"\"\n\n"
        "Return JSON of the form {\"cards\": [{\"question\": \"...\", "
        "\"answer\": \"...\", \"tags\": [\"...\"]}]}."
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
