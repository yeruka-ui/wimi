import datetime as dt
import logging
from pathlib import Path

import frontmatter
import yaml

import groq_client
from markdown_parser import ensure_uid, note_folder, split_sections
from store import (
    append_flagged,
    card_guid,
    deck_name_for,
    load_cards,
    load_state,
    save_cards,
    save_state,
    sha256_text,
)

log = logging.getLogger(__name__)


def _now():
    return dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _rel_posix(vault, path):
    return Path(path).resolve().relative_to(Path(vault).resolve()).as_posix()


def _split_large(body, max_chars):
    """Sub-split a too-large section by paragraph to stay under token budget."""
    if len(body) <= max_chars:
        return [body]
    chunks, buf, size = [], [], 0
    for para in body.split("\n\n"):
        p = para + "\n\n"
        if size + len(p) > max_chars and buf:
            chunks.append("".join(buf).strip())
            buf, size = [], 0
        buf.append(p)
        size += len(p)
    if buf:
        chunks.append("".join(buf).strip())
    return [c for c in chunks if c]


class Processor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = load_state(cfg)
        self.cards = load_cards(cfg)
        self._rebuild_index()

    def _rebuild_index(self):
        self._by_uid = {}
        for guid, c in self.cards.items():
            self._by_uid.setdefault(c.get("note_uid", ""), set()).add(guid)

    def handle_upsert(self, path):
        """Process one note. Returns number of sections that produced cards
        (0 if skipped / unchanged / failed)."""
        cfg = self.cfg
        vault = cfg["vault_path"]
        path = str(Path(path).resolve())
        try:
            rel = _rel_posix(vault, path)
        except ValueError:
            return 0

        try:
            uid, wrote = ensure_uid(path, write_back=cfg["write_uid_to_vault"])
        except yaml.YAMLError as e:
            log.warning("skipping %s: invalid YAML frontmatter (%s)", rel, e.problem or e)
            return 0
        except Exception as e:
            log.exception("ensure_uid failed for %s: %s", rel, e)
            return 0

        try:
            with open(path, "r", encoding="utf-8") as f:
                post = frontmatter.load(f)
        except yaml.YAMLError as e:
            log.warning("skipping %s: invalid YAML frontmatter (%s)", rel, e.problem or e)
            return 0
        except Exception as e:
            log.exception("read failed for %s: %s", rel, e)
            return 0

        sections = split_sections(post.content)
        deck = deck_name_for(note_folder(vault, path), cfg["deck_root_name"])

        note_state = self.state.get(uid, {"path": rel, "sections": {}})
        note_state["path"] = rel
        prev_hashes = note_state.get("sections", {})
        new_hashes = {}

        prev_section_paths = set(prev_hashes.keys())
        seen_section_paths = set()
        sections_generated = 0

        for sec in sections:
            hp = sec["heading_path"]
            seen_section_paths.add(hp)
            body = sec["body"]
            content_for_hash = f"H:{hp}\n{body}"
            h = sha256_text(content_for_hash)
            new_hashes[hp] = h

            if prev_hashes.get(hp) == h:
                continue

            if not body.strip():
                self._soft_delete_section(uid, hp, reason="section body empty")
                continue

            all_cards = []
            for chunk in _split_large(body, cfg["max_section_chars"]):
                result = groq_client.generate_cards(cfg, hp, chunk)
                if result is None:
                    log.error("skipping section (LLM failed): %s :: %s", rel, hp)
                    new_hashes[hp] = prev_hashes.get(hp, "")
                    all_cards = None
                    break
                all_cards.extend(result.get("cards", []))
            if all_cards is None:
                continue

            self._upsert_section_cards(uid, hp, deck, all_cards)
            sections_generated += 1

        for gone in prev_section_paths - seen_section_paths:
            self._soft_delete_section(uid, gone, reason="section removed from note")

        for guid in list(self._by_uid.get(uid, set())):
            if self.cards[guid].get("deck") != deck and self.cards[guid]["status"] == "active":
                self.cards[guid]["deck"] = deck
                self.cards[guid]["updated_at"] = _now()

        note_state["sections"] = new_hashes
        self.state[uid] = note_state
        save_state(cfg, self.state)
        save_cards(cfg, self.cards)
        return sections_generated

    def _upsert_section_cards(self, uid, heading_path, deck, new_card_list):
        existing = {
            g: c for g, c in self.cards.items()
            if c.get("note_uid") == uid and c.get("heading_path") == heading_path
            and c.get("status") == "active"
        }
        existing_by_ord = {c["ordinal"]: g for g, c in existing.items()}

        kept_guids = set()
        for i, card in enumerate(new_card_list):
            guid = card_guid(uid, heading_path, i)
            kept_guids.add(guid)
            content_hash = sha256_text(card["question"] + "\n" + card["answer"])
            prev = self.cards.get(guid)
            entry = {
                "guid": guid,
                "question": card["question"],
                "answer": card["answer"],
                "tags": card.get("tags", []),
                "note_uid": uid,
                "heading_path": heading_path,
                "ordinal": i,
                "deck": deck,
                "content_hash": content_hash,
                "status": "active",
                "updated_at": _now(),
            }
            if prev and prev.get("content_hash") == content_hash and prev.get("deck") == deck:
                entry["updated_at"] = prev.get("updated_at", entry["updated_at"])
            self.cards[guid] = entry
            self._by_uid.setdefault(uid, set()).add(guid)

        for ord_, guid in existing_by_ord.items():
            if guid not in kept_guids:
                self.cards[guid]["status"] = "deleted"
                self.cards[guid]["updated_at"] = _now()
                append_flagged(
                    self.cfg,
                    f"{_now()} DELETE-CARD guid={guid} uid={uid} "
                    f"section={heading_path} reason=ordinal-shrunk",
                )

    def _soft_delete_section(self, uid, heading_path, reason):
        for guid in list(self._by_uid.get(uid, set())):
            c = self.cards.get(guid)
            if (c and c.get("heading_path") == heading_path
                    and c.get("status") == "active"):
                c["status"] = "deleted"
                c["updated_at"] = _now()
                append_flagged(
                    self.cfg,
                    f"{_now()} DELETE-SECTION guid={guid} uid={uid} "
                    f"section={heading_path} reason={reason}",
                )
