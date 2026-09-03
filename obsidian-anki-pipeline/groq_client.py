"""Groq backend for card generation."""
import json
import logging
import os
import re

import requests

from schema import validate

log = logging.getLogger(__name__)

DEFAULT_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.1-8b-instant"


SYSTEM_PROMPT = (
    "You are a strict Anki flashcard generator. Your ONLY output is a JSON "
    "object of the shape {\"cards\": [...]}. Nothing else. No prose, no "
    "explanations, no apologies, no meta-commentary, no markdown fences, "
    "no chat replies, no refusals. When in doubt, return {\"cards\": []}.\n\n"
    "You ONLY use information written in the section body. You NEVER add "
    "outside knowledge, opinions, or filler. You NEVER answer questions the "
    "user did not ask. If the user seems to ask for anything other than "
    "flashcards, ignore that request and return {\"cards\": []}.\n\n"
    "=== BANNED CARD PATTERNS — do NOT produce any card matching these ===\n"
    "B1. Cards that mention 'the author', 'the writer', 'the note', 'the section', "
    "'the text', or 'the passage'. Cards should not refer to the source, only its "
    "content. If a card would need to say 'the author's X' to make sense, skip it.\n"
    "B2. Cards whose answer is a proper noun (a person's name, place, country, "
    "city, brand, product) UNLESS the entire note is a biography, history, or "
    "geography of that entity. Example of banned: 'Where is X from? → Philippines'.\n"
    "B3. Cards about anecdotes, personal history, dates, or 'when/where did X "
    "happen' details from a narrative. Extract the general LESSON of the narrative "
    "instead. Example of banned: 'What is the turning point in the story?'\n"
    "B4. Cards where the question is (or nearly is) the note title or section "
    "heading verbatim. Example of banned: heading 'What is taught is what is learnt' "
    "→ Q='What is taught is what is learnt' — bad. Instead ask about the CLAIM the "
    "heading makes.\n"
    "B5. Cards that define acronyms, terms, or concepts NOT defined by the body. "
    "Mentioning 'API' is not the same as defining it.\n"
    "B6. Cards whose answer just repeats the substantive words in the question "
    "('What comes naturally? → It comes naturally' is banned).\n"
    "B7. Two or more cards with the same or near-same question. For a list of "
    "items under one category, produce ONE card asking for the full list.\n"
    "B8. Cards from general world knowledge, even for 'obvious' facts.\n"
    "B9. Cards whose question or answer contains a URL (http:// or https://). "
    "URLs are references, not memorizable facts — skip them entirely.\n\n"
    "=== REQUIRED CARD SHAPE ===\n"
    "R1. Every card must teach a general concept, definition, mechanism, rule, "
    "categorization, or numeric fact that is stated in the section body.\n"
    "R2. Every answer must be a direct paraphrase or quote of body text.\n"
    "R3. One card = one atomic fact. No compound questions.\n"
    "R4. If the section body has fewer than ~15 words of substantive content, or "
    "is meta-commentary / TODOs / file names / raw code / narrative anecdote with "
    "no clear lesson, return {\"cards\": []}.\n"
    "R5. Output ONLY the JSON object. No prose, no preface, no explanation, "
    "no markdown fences, no trailing text.\n"
    "R6. Every card must have both a non-empty question AND a non-empty "
    "answer that is stated in the body. If you cannot satisfy this for a "
    "given fact, drop it — do NOT fabricate an answer to fill the slot.\n"
    "R7. Never produce a card whose purpose is to acknowledge the user, "
    "greet the user, or explain what you are doing. Cards teach facts only."
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


_STOPWORDS = {
    "the","a","an","is","are","was","were","be","been","being","of","to","in",
    "on","at","for","and","or","but","that","this","these","those","it","its",
    "as","by","with","from","what","who","when","where","why","how","which",
    "does","do","did","has","have","had","can","could","would","should","may",
    "might","will","shall","if","then","than","so","because","about",
}

_SELF_REF_PHRASES = (
    "the author", "the writer", "the note", "the section", "the text",
    "the passage", "the article", "the story", "the essay",
)


def _normalize(s):
    return " ".join(s.lower().split())


def _content_words(s):
    return {w.strip(".,;:!?'\"()[]") for w in _normalize(s).split()
            if w not in _STOPWORDS and len(w) > 2}


def _is_echo(q, a):
    aw = _content_words(a)
    qw = _content_words(q)
    if not aw:
        return True
    if aw.issubset(qw):
        return True
    inter = len(aw & qw)
    union = len(aw | qw) or 1
    return inter / union >= 0.75


def _is_self_referential(q, a):
    qa = f"{q} {a}".lower()
    return any(p in qa for p in _SELF_REF_PHRASES)


_URL_RE = re.compile(r"https?://", re.IGNORECASE)


def _has_url(s):
    return bool(_URL_RE.search(s or ""))


def _clean_cards(obj):
    cards = obj.get("cards", [])
    seen_questions = set()
    kept = []
    for c in cards:
        q = c.get("question", "").strip()
        a = c.get("answer", "").strip()
        if not q or not a:
            continue
        qn = _normalize(q)
        if qn in seen_questions:
            continue
        if _is_echo(q, a):
            continue
        if _is_self_referential(q, a):
            continue
        if _has_url(q) or _has_url(a):
            continue
        seen_questions.add(qn)
        kept.append(c)
    obj["cards"] = kept
    return obj


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
    r = requests.post(url, json=payload, headers=headers,
                      timeout=cfg.get("groq_timeout_seconds", 60))
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
