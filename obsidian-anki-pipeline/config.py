import json
import os
from pathlib import Path

DEFAULTS = {
    "vault_path": "",
    "output_dir": "./",
    "deck_root_name": "Obsidian",
    "write_uid_to_vault": True,
    "max_section_chars": 6000,
    "ignore_dirs": [".obsidian", ".trash", ".git", "node_modules"],
    "llm_backend": "groq",
    "groq_model": "llama-3.1-8b-instant",
    "groq_timeout_seconds": 60,
}


def load_config(path="config.json"):
    cfg = dict(DEFAULTS)
    p = Path(path)
    config_dir = p.resolve().parent if p.exists() else Path.cwd()
    if p.exists():
        with p.open("r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    vault = cfg.get("vault_path", "")
    if not vault or not Path(vault).is_dir():
        raise SystemExit(
            f"config.json: vault_path is missing or not a directory: {vault!r}"
        )
    # Resolve output_dir relative to the config file, not the process cwd,
    # so state/cards/logs land next to config.json regardless of where the
    # app was launched from.
    out_raw = Path(cfg["output_dir"])
    out = out_raw if out_raw.is_absolute() else (config_dir / out_raw)
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    cfg["output_dir"] = str(out)
    cfg["_state_path"] = str(out / "state.json")
    cfg["_cards_path"] = str(out / "cards.json")
    cfg["_log_dir"] = str(out / "logs")
    os.makedirs(cfg["_log_dir"], exist_ok=True)
    cfg["_flagged_path"] = str(Path(cfg["_log_dir"]) / "flagged_for_deletion.txt")
    return cfg
