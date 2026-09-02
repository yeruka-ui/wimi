import logging
from collections import defaultdict

import genanki

from store import deck_id_for, model_id

log = logging.getLogger(__name__)

_MODEL = genanki.Model(
    model_id(),
    "Obsidian Pipeline Basic",
    fields=[{"name": "Question"}, {"name": "Answer"}, {"name": "Source"}],
    templates=[{
        "name": "Card 1",
        "qfmt": "{{Question}}",
        "afmt": '{{FrontSide}}<hr id="answer">{{Answer}}<br><br>'
                '<div style="font-size:11px;color:#888">{{Source}}</div>',
    }],
    css=(
        ".card{font-family:Segoe UI,Arial,sans-serif;font-size:18px;"
        "text-align:left;color:#222;background:#fff;padding:12px}"
    ),
)


def build_subset(cards, deck_prefix, out_path):
    """Write an .apkg containing only the deck matching `deck_prefix` and its
    subdecks. Returns (deck_count, card_count)."""
    by_deck = defaultdict(list)
    for c in cards.values():
        if c.get("status") != "active":
            continue
        name = c["deck"]
        if name == deck_prefix or name.startswith(deck_prefix + "::"):
            by_deck[name].append(c)

    if not by_deck:
        raise ValueError(f"No active cards found for deck '{deck_prefix}'.")

    decks = []
    for deck_name, items in sorted(by_deck.items()):
        deck = genanki.Deck(deck_id_for(deck_name), deck_name)
        for c in items:
            source = f'{c.get("note_uid","")} :: {c.get("heading_path","")}'
            note = genanki.Note(
                model=_MODEL,
                fields=[c["question"], c["answer"], source],
                tags=[_sanitize_tag(t) for t in c.get("tags", []) if t],
                guid=c["guid"],
            )
            deck.add_note(note)
        decks.append(deck)

    genanki.Package(decks).write_to_file(out_path)
    return len(decks), sum(len(v) for v in by_deck.values())


def list_active_decks(cards):
    """Return sorted list of unique deck names with active cards."""
    names = set()
    for c in cards.values():
        if c.get("status") == "active":
            names.add(c["deck"])
    return sorted(names)


def list_deck_tree(cards):
    """Return [(deck_name, depth, direct_cards, total_cards), ...] covering
    every active deck AND its ancestors. Sorted so children follow parents."""
    direct = {}
    for c in cards.values():
        if c.get("status") != "active":
            continue
        direct[c["deck"]] = direct.get(c["deck"], 0) + 1

    all_names = set()
    for name in direct:
        parts = name.split("::")
        for i in range(1, len(parts) + 1):
            all_names.add("::".join(parts[:i]))

    total = {}
    for anc in all_names:
        prefix = anc + "::"
        total[anc] = sum(n for d, n in direct.items()
                         if d == anc or d.startswith(prefix))

    return [
        (name, name.count("::"), direct.get(name, 0), total[name])
        for name in sorted(all_names)
    ]


def filter_cards_by_include_dirs(cards, include_dirs, deck_root_name="Obsidian"):
    """Filter `cards` dict to only include cards whose deck belongs to one of
    the relative folder paths in `include_dirs`. If `include_dirs` is empty,
    returns all cards unchanged."""
    if not include_dirs:
        return cards

    allowed_prefixes = []
    for d in include_dirs:
        entry = str(d).replace("\\", "/").strip("/")
        if not entry:
            continue
        parts = [p for p in entry.split("/") if p and p != "."]
        deck_prefix = "::".join([deck_root_name] + parts)
        allowed_prefixes.append(deck_prefix.lower())

    if not allowed_prefixes:
        return cards

    filtered = {}
    for guid, card in cards.items():
        deck = card.get("deck", "").lower()
        if any(deck == prefix or deck.startswith(prefix + "::") for prefix in allowed_prefixes):
            filtered[guid] = card

    return filtered


def _sanitize_tag(t):
    # Anki tags cannot contain spaces.
    return "_".join(str(t).split())

