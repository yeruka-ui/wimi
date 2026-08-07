# 🧡 Wimi: An Obsidian → Ollama → Anki auto-flashcard pipeline

Watches an Obsidian vault, sends changed sections of notes to a local Ollama
LLM, and rebuilds a single `deck.apkg` (containing one Anki sub-deck per
vault folder) that you re-import into Anki.

See `PLAN.md` in the parent directory for the full design.

## Setup (Windows)

1. Install [Ollama](https://ollama.com/) and pull the model:
   ```
   ollama pull llama3.1:8b
   ```
2. Edit `config.json` and set `vault_path` to your Obsidian vault root.
3. Double-click `run_watcher.bat` (or run it from a terminal). On first run it
   creates a `.venv` and installs dependencies from `requirements.txt`.

## What it produces

- `deck.apkg` — import this file into Anki with **"Update existing notes"** enabled.
- `cards.json` — source of truth for every generated card (keyed by guid).
- `state.json` — per-note, per-section hashes so unchanged sections are skipped.
- `logs/pipeline.log` — rotating log file.
- `logs/flagged_for_deletion.txt` — soft-deleted cards for your manual review.

## Config keys

| key | meaning |
|-----|---------|
| `vault_path` | absolute path to the Obsidian vault |
| `output_dir` | where `deck.apkg`, `state.json`, `cards.json`, `logs/` are written |
| `deck_root_name` | top-level Anki deck name (folder hierarchy nests under it) |
| `ollama_url` | Ollama HTTP endpoint (default `http://localhost:11434`) |
| `ollama_model` | model tag, e.g. `llama3.1:8b` |
| `ollama_timeout_seconds` | per-request timeout |
| `debounce_seconds` | how long to wait after last save before processing |
| `write_uid_to_vault` | if true, inserts a hidden `uid:` in note frontmatter |
| `max_section_chars` | large sections are sub-split by paragraph before sending |
| `ignore_dirs` | folder names skipped during scan/watch (e.g. `.obsidian`) |

## Caveats

- Anki may keep an already-imported note in its original deck even after we
  update the deck field on re-import. If you move notes between folders, you
  may need to use Anki's "Change deck" once for the affected cards.
- The tool writes a hidden `uid:` field into your note frontmatter so cards
  survive rename/move. Turn this off with `write_uid_to_vault: false` if you
  don't want the tool touching your files (identity then falls back to path).
