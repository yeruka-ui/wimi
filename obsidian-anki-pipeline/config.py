import json
import os
from pathlib import Path

DEFAULTS = {
    "vault_path": "",
    "output_dir": "./",
    "deck_root_name": "Obsidian",
    "ollama_url": "http://localhost:11434",
    "ollama_model": "llama3.1:8b",
    "ollama_timeout_seconds": 120,
    "debounce_seconds": 1.5,
    "write_uid_to_vault": True,
    "max_section_chars": 6000,
    "ignore_dirs": [".obsidian", ".trash", ".git", "node_modules"],
}


def load_config(path="config.json"):
    cfg = dict(DEFAULTS)
    p = Path(path)
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    vault = cfg.get("vault_path", "")
    if not vault or not Path(vault).is_dir():
        raise SystemExit(
            f"config.json: vault_path is missing or not a directory: {vault!r}"
        )
    out = Path(cfg["output_dir"]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    cfg["output_dir"] = str(out)
    cfg["_state_path"] = str(out / "state.json")
    cfg["_cards_path"] = str(out / "cards.json")
    cfg["_deck_path"] = str(out / "deck.apkg")
    cfg["_log_dir"] = str(out / "logs")
    os.makedirs(cfg["_log_dir"], exist_ok=True)
    cfg["_flagged_path"] = str(Path(cfg["_log_dir"]) / "flagged_for_deletion.txt")
    return cfg
