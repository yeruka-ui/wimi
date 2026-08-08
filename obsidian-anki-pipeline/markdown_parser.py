import re
import uuid
from pathlib import Path

import frontmatter

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def load_note(path):
    """Return frontmatter.Post loaded from path (handles no-frontmatter files)."""
    text = _read(path)
    return frontmatter.loads(text), text


def ensure_uid(path, write_back=True):
    """
    Ensure the note at `path` has a `uid` in its frontmatter.
    Returns (uid, was_written). If the file had no frontmatter block at all,
    a minimal one is inserted at the top preserving the body byte-for-byte.
    """
    text = _read(path)
    post = frontmatter.loads(text)
    uid = post.metadata.get("uid")
    if uid:
        return str(uid), False
    new_uid = uuid.uuid4().hex
    if not write_back:
        return new_uid, False
    post.metadata["uid"] = new_uid
    # frontmatter.dumps re-serializes; to minimize churn, if the file had no
    # frontmatter block, prepend a minimal one and keep body untouched.
    if not text.lstrip().startswith("---"):
        new_text = f"---\nuid: {new_uid}\n---\n" + text
    else:
        new_text = frontmatter.dumps(post) + ("\n" if not text.endswith("\n") else "")
    _write(path, new_text)
    return new_uid, True


def split_sections(body_text):
    """
    Split markdown body into sections keyed by full heading breadcrumb.
    Text above the first heading becomes an '(intro)' section.
    Returns list of dicts: [{heading_path, level, title, body}], preserving order.
    """
    lines = body_text.splitlines()
    sections = []
    stack = []  # list of (level, title)
    current_title = "(intro)"
    current_level = 0
    current_path = "(intro)"
    buf = []

    def flush():
        text = "\n".join(buf).strip()
        # Always emit a section entry (even empty intro) so we track structure;
        # deck/card generation skips empty-body sections with no title info.
        if text or current_path != "(intro)":
            sections.append({
                "heading_path": current_path,
                "level": current_level,
                "title": current_title,
                "body": text,
            })

    for line in lines:
        m = HEADING_RE.match(line)
        if m:
            flush()
            buf = []
            level = len(m.group(1))
            title = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current_title = title
            current_level = level
            current_path = "::".join(t for _, t in stack)
        else:
            buf.append(line)
    flush()
    return sections


def note_folder(vault_path, note_path):
    """Return posix relative folder of note within vault (e.g. 'Comp Sci/Databases')."""
    rel = Path(note_path).resolve().relative_to(Path(vault_path).resolve()).parent
    return rel.as_posix()
