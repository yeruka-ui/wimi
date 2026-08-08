import hashlib
import json
import os
from pathlib import Path


def _load_json(path, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json_atomic(path, obj):
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def load_state(cfg):
    return _load_json(cfg["_state_path"], {})


def save_state(cfg, state):
    _save_json_atomic(cfg["_state_path"], state)


def load_cards(cfg):
    return _load_json(cfg["_cards_path"], {})


def save_cards(cfg, cards):
    _save_json_atomic(cfg["_cards_path"], cards)


def append_flagged(cfg, line):
    with open(cfg["_flagged_path"], "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def card_guid(note_uid, heading_path, ordinal):
    """Stable guid used both as our internal key AND as Anki's note guid."""
    raw = f"{note_uid}::{heading_path}::{ordinal}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def deck_name_for(folder_rel_posix, deck_root_name):
    """
    Convert 'Comp Sci/Databases' -> 'Obsidian::Comp Sci::Databases'.
    Empty folder (note directly in vault root) -> 'Obsidian'.
    """
    parts = [p for p in folder_rel_posix.split("/") if p and p != "."]
    return "::".join([deck_root_name] + parts) if parts else deck_root_name


def deck_id_for(deck_name):
    """Stable numeric deck id derived from deck name."""
    h = hashlib.sha256(deck_name.encode("utf-8")).digest()
    # Anki requires signed 63-bit-ish int; use 8 bytes and mask.
    return int.from_bytes(h[:8], "big") & 0x7FFFFFFFFFFFFFFF


def model_id():
    """Stable numeric model id for our single note type."""
    return int.from_bytes(
        hashlib.sha256(b"obsidian-anki-pipeline::basic-v1").digest()[:8], "big"
    ) & 0x7FFFFFFFFFFFFFFF
