import json
import logging

import requests

from schema import CARD_SCHEMA, validate

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You turn study notes into Anki flashcards. You ONLY use information written "
    "in the section body. You NEVER add outside knowledge.\n\n"
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
    "B8. Cards from general world knowledge, even for 'obvious' facts.\n\n"
    "=== REQUIRED CARD SHAPE ===\n"
    "R1. Every card must teach a general concept, definition, mechanism, rule, "
    "categorization, or numeric fact that is stated in the section body.\n"
    "R2. Every answer must be a direct paraphrase or quote of body text.\n"
    "R3. One card = one atomic fact. No compound questions.\n"
    "R4. If the section body has fewer than ~15 words of substantive content, or "
    "is meta-commentary / TODOs / file names / raw code / narrative anecdote with "
    "no clear lesson, return {\"cards\": []}.\n"
    "R5. Output ONLY the JSON. No prose, no explanation, no markdown fences.\n\n"
    "=== WORKED EXAMPLES ===\n"
    "Body: \"Growing up in the Philippines, I studied Filipino throughout school "
    "but by age 9 I still could not speak it fluently. That taught me a lesson: "
    "what is TAUGHT is not necessarily what is LEARNED. Only about 20% of learning "
    "comes from direct teaching; the other 80% comes from acquisition through use.\"\n"
    "  BAD: Q='Where did the author grow up?' A='Philippines'  [B2, B3]\n"
    "  BAD: Q='What is the author's native language?' A='Filipino'  [B1, B2]\n"
    "  BAD: Q='What is the turning point in the author's story?'  [B1, B3]\n"
    "  GOOD: Q='What percentage of learning comes from direct teaching vs. acquisition?' "
    "A='About 20% from teaching, 80% from acquisition.'\n"
    "  GOOD: Q='What is the lesson: what is TAUGHT is not necessarily what is what?' "
    "A='LEARNED.'\n\n"
    "Body: \"Accuracy focuses on Vocabulary, Pronunciation, Sentence Structure, "
    "and Rules of Language.\"\n"
    "  BAD: four separate cards all Q='What does Accuracy focus on?'  [B7]\n"
    "  GOOD: ONE card Q='What four things does Accuracy focus on?' "
    "A='Vocabulary, Pronunciation, Sentence Structure, and Rules of Language.'\n\n"
    "Body: \"The debouncer coalesces bursty events per-path into a single delayed "
    "callback.\"\n"
    "  GOOD: Q='What does the debouncer do?' "
    "A='Coalesces bursty events per-path into a single delayed callback.'\n\n"
    "Body: \"We call the local model via Ollama's API.\"\n"
    "  BAD: Q='What does API stand for?' A='Application Programming Interface'  [B5, B8]"
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


_SELF_REF_PHRASES = (
    "the author", "the writer", "the note", "the section", "the text",
    "the passage", "the article", "the story", "the essay",
)


def _is_self_referential(q, a):
    qa = f"{q} {a}".lower()
    return any(p in qa for p in _SELF_REF_PHRASES)


def _clean_cards(obj):
    """Drop duplicate questions and tautological/echo/self-referential cards."""
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
