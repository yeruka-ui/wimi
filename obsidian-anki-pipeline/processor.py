import datetime as dt
import logging
from pathlib import Path

import frontmatter

import deck_builder
import ollama_client
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
        # index: note_uid -> set of guids currently stored active
        self._rebuild_index()

    def _rebuild_index(self):
        self._by_uid = {}
        for guid, c in self.cards.items():
            self._by_uid.setdefault(c.get("note_uid", ""), set()).add(guid)

    # ---- public entry points --------------------------------------------------

    def handle_upsert(self, path):
        cfg = self.cfg
        vault = cfg["vault_path"]
        path = str(Path(path).resolve())
        try:
            rel = _rel_posix(vault, path)
        except ValueError:
            return  # outside vault; ignore

        try:
            uid, wrote = ensure_uid(path, write_back=cfg["write_uid_to_vault"])
        except Exception as e:
            log.exception("ensure_uid failed for %s: %s", rel, e)
            return

        try:
            with open(path, "r", encoding="utf-8") as f:
                post = frontmatter.load(f)
        except Exception as e:
            log.exception("read failed for %s: %s", rel, e)
            return

        sections = split_sections(post.content)
        deck = deck_name_for(note_folder(vault, path), cfg["deck_root_name"])

        note_state = self.state.get(uid, {"path": rel, "sections": {}})
        note_state["path"] = rel
        prev_hashes = note_state.get("sections", {})
        new_hashes = {}

        # Which section paths existed before? We use this to soft-delete vanished ones.
        prev_section_paths = set(prev_hashes.keys())
        seen_section_paths = set()

        for sec in sections:
            hp = sec["heading_path"]
            seen_section_paths.add(hp)
            body = sec["body"]
            content_for_hash = f"H:{hp}\n{body}"
            h = sha256_text(content_for_hash)
            new_hashes[hp] = h

            if prev_hashes.get(hp) == h:
                continue  # unchanged

            if not body.strip():
                # Empty section: drop any existing cards for it (soft-delete).
                self._soft_delete_section(uid, hp, reason="section body empty")
                continue

            # Generate cards (may need to sub-split large sections)
            all_cards = []
            for chunk in _split_large(body, cfg["max_section_chars"]):
                result = ollama_client.generate_cards(cfg, hp, chunk)
                if result is None:
                    log.error("skipping section (LLM failed): %s :: %s", rel, hp)
                    # Do NOT update hash — retry on next save.
                    new_hashes[hp] = prev_hashes.get(hp, "")
                    all_cards = None
                    break
                all_cards.extend(result.get("cards", []))
            if all_cards is None:
                continue

            self._upsert_section_cards(uid, hp, deck, all_cards)

        # Soft-delete sections that vanished from the note.
        for gone in prev_section_paths - seen_section_paths:
            self._soft_delete_section(uid, gone, reason="section removed from note")

        # If deck changed for this note (folder moved), update deck on all its cards.
        for guid in list(self._by_uid.get(uid, set())):
            if self.cards[guid].get("deck") != deck and self.cards[guid]["status"] == "active":
                self.cards[guid]["deck"] = deck
                self.cards[guid]["updated_at"] = _now()

        note_state["sections"] = new_hashes
        self.state[uid] = note_state
        save_state(cfg, self.state)
        save_cards(cfg, self.cards)

        # If we wrote a UID back, our own hash reflects post-write content already
        # (we re-read after ensure_uid), so no infinite loop.
        deck_builder.rebuild(cfg, self.cards)

    def handle_delete(self, path):
        cfg = self.cfg
        try:
            rel = _rel_posix(cfg["vault_path"], path)
        except Exception:
            rel = str(path)
        # Find note_uid(s) whose state.path matches this file.
        victims = [uid for uid, ns in self.state.items() if ns.get("path") == rel]
        for uid in victims:
            for guid in list(self._by_uid.get(uid, set())):
                c = self.cards.get(guid)
                if c and c.get("status") == "active":
                    c["status"] = "deleted"
                    c["updated_at"] = _now()
                    append_flagged(
                        cfg,
                        f"{_now()} DELETE-NOTE guid={guid} uid={uid} path={rel}"
                        f" q={c.get('question','')[:80]!r}",
                    )
            self.state.pop(uid, None)
        save_state(cfg, self.state)
        save_cards(cfg, self.cards)
        deck_builder.rebuild(cfg, self.cards)

    def handle_move(self, src_path, dest_path):
        # UID-based identity means the next upsert on dest will fix state.path and deck.
        # But if UID lookup by state.path is used for delete-detection, refresh path here.
        cfg = self.cfg
        try:
            src_rel = _rel_posix(cfg["vault_path"], src_path)
        except Exception:
            src_rel = str(src_path)
        for uid, ns in self.state.items():
            if ns.get("path") == src_rel:
                try:
                    ns["path"] = _rel_posix(cfg["vault_path"], dest_path)
                except Exception:
                    pass
        save_state(cfg, self.state)
        # Trigger a full re-process to update deck on cards.
        try:
            self.handle_upsert(dest_path)
        except Exception as e:
            log.exception("handle_move->upsert failed: %s", e)

    # ---- internal -------------------------------------------------------------

    def _upsert_section_cards(self, uid, heading_path, deck, new_card_list):
        """Replace the set of cards for this (uid, heading_path) in place by ordinal."""
        # Existing cards for this section
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
                # No change; keep prev updated_at.
                entry["updated_at"] = prev.get("updated_at", entry["updated_at"])
            self.cards[guid] = entry
            self._by_uid.setdefault(uid, set()).add(guid)

        # Soft-delete cards that used to exist for this section but no longer do
        # (i.e., ordinal past the new length).
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
