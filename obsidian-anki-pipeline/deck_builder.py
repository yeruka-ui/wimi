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


def rebuild(cfg, cards):
    """Build one .apkg containing one deck per folder. Only active cards."""
    by_deck = defaultdict(list)
    for guid, c in cards.items():
        if c.get("status") != "active":
            continue
        by_deck[c["deck"]].append(c)

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

    if not decks:
        # genanki refuses empty package; make an empty placeholder deck.
        decks.append(genanki.Deck(deck_id_for(cfg["deck_root_name"]),
                                  cfg["deck_root_name"]))

    pkg = genanki.Package(decks)
    pkg.write_to_file(cfg["_deck_path"])
    log.info("Wrote %s (%d decks, %d active cards)",
             cfg["_deck_path"], len(decks), sum(len(v) for v in by_deck.values()))


def _sanitize_tag(t):
    # Anki tags cannot contain spaces.
    return "_".join(str(t).split())
