# Obsidian → Local LLM → Anki auto-flashcard pipeline — CONFIRMED PLAN

Status: **planning locked, not yet implemented.**
LLM = Large Language Model. AI = Artificial Intelligence. GUID = Globally Unique
Identifier. UID = Unique Identifier. API = Application Programming Interface.

---

## 1. Locked decisions (from user answers)

| # | Topic | Decision |
|---|-------|----------|
| A | Cards per section | **Many cards allowed per heading.** The AI must also make cards from important body text, not just the heading title. |
| B | Card identity | **Hidden permanent label (UID) stored in each note's frontmatter.** Falls back to file path only if a note has no UID. |
| C | Deleted notes/sections | **Soft delete** — mark inactive, drop from future deck, log to a manual-cleanup list. |
| D | Deck layout | **One deck per folder.** Deck hierarchy mirrors the vault folder tree. Still packaged into a single `deck.apkg` file (a `.apkg` can hold many decks). |
| E | Chunking | **Split at every heading (H1–H6); sub-sections get their own cards; important body text under a heading also gets its own cards.** Heading title alone is NOT assumed to cover the section. |
| F | Runtime | **Background process**, Ollama at `http://localhost:11434`, model `llama3.1:8b`, all configurable. |
| G | Change detection | **Per-section fingerprint (hash).** Only re-send the section that actually changed. |

---

## 2. What "section" means (chunking rule — revised for E)

A note is split into **sections at every heading line** (`#` through `######`).
A section's body is all text from its heading down to the *next heading of any
level*. This means:

- A parent heading's own intro text becomes its own section (kept, not lost).
- Every sub-heading becomes its own section (so sub-topics get their own cards).
- Text above the very first heading becomes an `(intro)` section (so important
  un-headed text is still turned into cards).

The AI is instructed to extract **multiple cards per section**, one per important
idea / bullet / fact — not a single summary card. This satisfies A and E
together: headings, sub-sections, and loose body text all produce cards.

Example note and the sections it produces:

```markdown
---
uid: 7f3a-...        <- hidden permanent label (question B)
---
# Databases          -> section "Databases"                 body = text below, until "## CAP theorem"
Some intro text.

## CAP theorem       -> section "Databases::CAP theorem"     body = "In a partition..."
In a partition, choose consistency or availability.

### Proof            -> section "Databases::CAP theorem::Proof"  body = "Assume..."
Assume a partition...
```

## 3. Card identity (revised for A + B + E)

```
guid = stable_hash( note_uid + "::" + heading_path + "::" + ordinal )
```

- `note_uid` — the hidden frontmatter UID (survives rename/move). File path only
  as fallback.
- `heading_path` — the full breadcrumb (e.g. `Databases::CAP theorem::Proof`) so
  two sub-sections that happen to share a title never collide.
- `ordinal` — 0,1,2… index of the card within its section (because many cards
  per section, decision A).

Editing an answer keeps the same guid → Anki updates the card in place.
Renaming/moving the file keeps the same guid (UID-based) → no orphans.

## 4. Deck naming (revised for D)

- Deck = the folder the note lives in, relative to the vault root.
- Folder tree maps to Anki sub-decks with `::`. Example: a note in
  `vault/Comp Sci/Databases/` → deck `Comp Sci::Databases`.
- `deck_id` is a stable hash of the folder's relative path (must be constant
  across runs so Anki updates instead of duplicating).
- All decks are bundled into the single output `deck.apkg`.
- Moving a note to another folder updates its card's deck on the next rebuild.
  (Caveat: Anki may keep an already-imported note in its original deck on
  re-import; documented in README.)

---

## 5. File / module structure

```
obsidian-anki-pipeline/
├── config.json          # vault path, model, deck root name, intervals, UID options
├── config.py            # load + validate config.json, defaults
├── watcher.py           # ENTRY POINT: watchdog observer, debounce, dispatch
├── processor.py         # orchestration brain
├── markdown_parser.py   # frontmatter + heading/section splitting, heading_path
├── ollama_client.py     # Ollama API wrapper, schema validation, retry-once
├── schema.py            # JSON schema contract + validator
├── store.py             # state.json + cards.json load/save, GUID + deck derivation
├── deck_builder.py      # genanki rebuild (multi-deck), idempotent
├── logging_setup.py     # rotating log file + console
├── run_watcher.bat      # Windows launcher (Task Scheduler / double-click)
├── requirements.txt     # watchdog, requests, genanki, jsonschema, python-frontmatter
├── README.md
├── state.json           # generated: per-note, per-section hashes
├── cards.json           # generated: source of truth, keyed by guid
├── deck.apkg            # generated: rebuilt each change (contains all folder decks)
└── logs/
    ├── pipeline.log
    └── flagged_for_deletion.txt   # soft-delete manual cleanup list
```

## 6. Data flow (file save → deck.apkg)

```
Obsidian saves note.md
  -> watchdog event (created / modified / moved / deleted)
  -> watcher.py debounces per-path (default 1.5s), then calls processor
  -> processor:
       deleted -> soft-delete that note's cards -> rebuild
       moved   -> keep guids (UID-based); update deck from new folder -> rebuild
       created/modified:
         parse frontmatter (get/insert UID) + split into sections
         for each section: hash it; compare to state.json
            unchanged -> skip
            changed/new -> ollama_client.generate(section)  [format=json + schema]
                           malformed -> retry once -> still bad -> log + skip
                           valid -> upsert cards (guid) into cards.json
         sections that vanished -> soft-delete their cards
         save cards.json + state.json
  -> deck_builder.rebuild(): group cards by deck (folder), build one .apkg
  -> deck.apkg on disk
  -> user re-imports into Anki with "update existing notes" ON
```

The whole per-file process is wrapped in try/except: if Ollama is down or a file
errors, it logs loudly and the watcher keeps running.

## 7. JSON schema contract from Ollama

Model must return ONLY this object (empty `cards` array is valid — a section may
yield no cards):

```json
{
  "cards": [
    { "question": "…", "answer": "…", "tags": ["…"] }
  ]
}
```

Validated with `jsonschema` before anything is written:

```json
{
  "type": "object",
  "required": ["cards"],
  "additionalProperties": false,
  "properties": {
    "cards": {
      "type": "array", "minItems": 0, "maxItems": 20,
      "items": {
        "type": "object",
        "required": ["question", "answer"],
        "additionalProperties": false,
        "properties": {
          "question": { "type": "string", "minLength": 3, "maxLength": 500 },
          "answer":   { "type": "string", "minLength": 1, "maxLength": 2000 },
          "tags": { "type": "array", "maxItems": 10,
                    "items": { "type": "string", "maxLength": 40 } }
        }
      }
    }
  }
}
```

Same schema is passed to Ollama's `format` parameter to constrain generation, AND
used to validate the reply. Fail → retry once → log + skip. Never write unchecked.

## 8. state.json / cards.json shapes

`state.json` (per-note, per-section for decision G):
```json
{ "7f3a-uid": { "path": "Comp Sci/Databases/db.md",
                "sections": { "Databases::CAP theorem": "sha256…" } } }
```

`cards.json` (source of truth, keyed by guid):
```json
{ "b1f3…": { "guid": "b1f3…", "question": "…", "answer": "…", "tags": ["…"],
             "note_uid": "7f3a-uid", "heading_path": "Databases::CAP theorem",
             "ordinal": 0, "deck": "Comp Sci::Databases",
             "content_hash": "…", "status": "active", "updated_at": "…" } }
```

## 9. Edge cases

- **Ollama unreachable** → caught, logged, watcher keeps running; that file
  retried on its next save.
- **Malformed/partial JSON** → retry once, then log + skip; never stored.
- **Note deleted** → its cards soft-deleted, listed in flagged_for_deletion.txt.
- **Note renamed/moved** → UID keeps identity; deck updated from new folder.
- **Large section over token budget** → sub-split by paragraph before sending.
- **Note with no headings** → whole body handled as one `(intro)` section.
- **We write a UID back into a file** → we update state hash for our own write so
  it does not cause an infinite re-process loop.

---

## 10. UID handling — RESOLVED

**Auto-insert (chosen).** When a note has no `uid:` in its frontmatter, the tool
adds one hidden `uid:` line to that note's frontmatter. Nothing else in the note
is touched. This makes rename/move fully safe (cards are never orphaned).

Implementation notes:
- If a note has no frontmatter block at all, create a minimal one (`---\nuid: …\n---`)
  at the top, preserving the rest of the file byte-for-byte.
- After writing the UID back, immediately record the new file/section hashes in
  state.json so our own write does not trigger a redundant re-process loop.
- UID value = a fresh random id (uuid4) generated once per note.
- A `write_uid_to_vault` flag stays in config.json (default true) so the user can
  turn it off later without code changes.

**All decisions are now locked. Ready to implement.**
