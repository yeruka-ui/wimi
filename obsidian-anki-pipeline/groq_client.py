"""Groq backend for card generation. Same public shape as ollama_client."""
import json
import logging
import os

import requests

from ollama_client import SYSTEM_PROMPT, _build_user_prompt, _clean_cards
from schema import validate

log = logging.getLogger(__name__)

DEFAULT_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.1-8b-instant"


def _get_api_key(cfg):
    key = cfg.get("groq_api_key") or os.environ.get("GROQ_API") or os.environ.get("GROQ_API_KEY")
    if not key:
        raise RuntimeError(
            "Groq API key not found. Set 'groq_api_key' in config.json, "
            "or set the GROQ_API / GROQ_API_KEY environment variable."
        )
    return key


def _call_once(cfg, heading_path, body):
    api_key = _get_api_key(cfg)
    url = cfg.get("groq_url", DEFAULT_URL)
    model = cfg.get("groq_model", DEFAULT_MODEL)
    payload = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(heading_path, body)},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    r = requests.post(url, json=payload, headers=headers, timeout=cfg.get("ollama_timeout_seconds", 60))
    r.raise_for_status()
    data = r.json()
    content = data["choices"][0]["message"]["content"].strip()
    if not content:
        raise ValueError("Groq returned empty content")
    try:
        obj = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Groq returned non-JSON: {e}: {content[:200]!r}")
    ok, err = validate(obj)
    if not ok:
        raise ValueError(f"Schema validation failed: {err}")
    return _clean_cards(obj)


def generate_cards(cfg, heading_path, body):
    try:
        return _call_once(cfg, heading_path, body)
    except Exception as e:
        log.warning("Groq attempt 1 failed for %s: %s", heading_path, e)
    try:
        return _call_once(cfg, heading_path, body)
    except Exception as e:
        log.error("Groq attempt 2 failed for %s: %s — skipping", heading_path, e)
        return None
