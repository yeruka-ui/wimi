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
    "5. NEVER produce two cards with the same or nearly-identical question. If a "
    "section lists several items belonging to one category, produce ONE card asking "
    "for the full list — do NOT create one card per list item with the same question.\n"
    "6. Do NOT produce cards whose answer restates the question or is a single term "
    "already in the question. Skip trivial or tautological cards.\n"
    "7. If the section body is meta-commentary, a TODO list, a list of file names, "
    "code without prose, or fewer than ~15 words of substantive content, "
    "return {\"cards\": []}.\n"
    "8. Focus on the LESSON, not the story. For narrative or biographical text, "
    "extract only the general concepts, definitions, mechanisms, or rules being "
    "taught. Do NOT create cards about the author's personal history, specific "
    "people mentioned in passing, place names, dates, or anecdotal details unless "
    "the note's core topic IS that biography/history. If in doubt, skip.\n"
    "9. Output ONLY the JSON. No prose, no explanation, no markdown fences.\n\n"
    "EXAMPLE of a BAD hallucination card (do not produce):\n"
    "  Body: \"We call the local model via Ollama's API.\"\n"
    "  BAD: Q=\"What does API stand for?\" A=\"Application Programming Interface\"\n"
    "  Why bad: body mentions API but never defines it.\n\n"
    "EXAMPLE of BAD list-splitting (do not produce):\n"
    "  Body: \"Accuracy focuses on: Vocabulary, Pronunciation, Sentence Structure, "
    "Rules of language.\"\n"
    "  BAD: four cards all with Q=\"What does Accuracy focus on?\" and different answers.\n"
    "  GOOD: ONE card, Q=\"What four things does Accuracy focus on?\" "
    "A=\"Vocabulary, Pronunciation, Sentence Structure, and Rules of Language.\"\n\n"
    "EXAMPLE of a GOOD paraphrase card:\n"
    "  Body: \"The debouncer coalesces bursty events per-path into a single delayed callback.\"\n"
    "  GOOD: Q=\"What does the debouncer do?\" "
    "A=\"Coalesces bursty events per-path into a single delayed callback.\""
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


_STOPWORDS = {
    "the","a","an","is","are","was","were","be","been","being","of","to","in",
    "on","at","for","and","or","but","that","this","these","those","it","its",
    "as","by","with","from","what","who","when","where","why","how","which",
    "does","do","did","has","have","had","can","could","would","should","may",
    "might","will","shall","if","then","than","so","because","about",
}


def _normalize(s):
    return " ".join(s.lower().split())


def _content_words(s):
    return {w.strip(".,;:!?'\"()[]") for w in _normalize(s).split() if w not in _STOPWORDS and len(w) > 2}


def _is_echo(q, a):
    """True if the answer just repeats the substantive words of the question."""
    aw = _content_words(a)
    qw = _content_words(q)
    if not aw:
        return True
    # Answer content-words are a subset of the question's — nothing new.
    if aw.issubset(qw):
        return True
    # Jaccard overlap is very high.
    inter = len(aw & qw)
    union = len(aw | qw) or 1
    return inter / union >= 0.75


def _clean_cards(obj):
    """Drop duplicate questions and tautological/echo cards. Mutates and returns obj."""
    cards = obj.get("cards", [])
    seen_questions = set()
    kept = []
    for c in cards:
        q = c.get("question", "").strip()
        a = c.get("answer", "").strip()
        if not q or not a:
            continue
        qn = _normalize(q)
        # Drop dup questions (case/whitespace insensitive).
        if qn in seen_questions:
            continue
        # Drop echoes: answer restates the substantive words of the question.
        if _is_echo(q, a):
            continue
        seen_questions.add(qn)
        kept.append(c)
    obj["cards"] = kept
    return obj


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
    return _clean_cards(obj)


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
