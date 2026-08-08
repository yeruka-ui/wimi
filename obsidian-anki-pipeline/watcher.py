import logging
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from config import load_config
from logging_setup import setup_logging
from processor import Processor

log = logging.getLogger("watcher")


def _is_markdown(path):
    return str(path).lower().endswith(".md")


def _in_ignored_dir(path, ignore_dirs):
    parts = set(Path(path).parts)
    return any(d in parts for d in ignore_dirs)


def _in_included_dir(path, vault, include_dirs):
    """Empty include_dirs = include everything. Otherwise the file's
    vault-relative top-level folder must match (case-insensitive)."""
    if not include_dirs:
        return True
    try:
        rel = Path(path).resolve().relative_to(Path(vault).resolve())
    except ValueError:
        return False
    if not rel.parts or len(rel.parts) < 2:
        # File sits directly in the vault root — not under any folder.
        return False
    top = rel.parts[0].lower()
    return any(top == d.lower() for d in include_dirs)


class _Debouncer:
    """Coalesces bursty events per-path into a single delayed callback."""

    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self._timers = {}
        self._lock = threading.Lock()

    def schedule(self, key, *args):
        with self._lock:
            t = self._timers.get(key)
            if t:
                t.cancel()
            timer = threading.Timer(self.delay, self._fire, args=(key, args))
            self._timers[key] = timer
            timer.daemon = True
            timer.start()

    def _fire(self, key, args):
        with self._lock:
            self._timers.pop(key, None)
        try:
            self.callback(*args)
        except Exception:
            log.exception("callback failed for %s", key)


class Handler(FileSystemEventHandler):
    def __init__(self, cfg, processor):
        self.cfg = cfg
        self.proc = processor
        self.debouncer = _Debouncer(cfg["debounce_seconds"], self._dispatch)

    def _dispatch(self, kind, *paths):
        if kind == "upsert":
            self.proc.handle_upsert(paths[0])
        elif kind == "delete":
            self.proc.handle_delete(paths[0])
        elif kind == "move":
            self.proc.handle_move(paths[0], paths[1])

    def _accept(self, path):
        return (
            _is_markdown(path)
            and not _in_ignored_dir(path, self.cfg["ignore_dirs"])
            and _in_included_dir(path, self.cfg["vault_path"], self.cfg.get("include_dirs", []))
        )

    def on_created(self, event):
        if event.is_directory or not self._accept(event.src_path):
            return
        self.debouncer.schedule(("u", event.src_path), "upsert", event.src_path)

    def on_modified(self, event):
        if event.is_directory or not self._accept(event.src_path):
            return
        self.debouncer.schedule(("u", event.src_path), "upsert", event.src_path)

    def on_deleted(self, event):
        if event.is_directory or not _is_markdown(event.src_path):
            return
        self.debouncer.schedule(("d", event.src_path), "delete", event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        # If destination is a markdown file within the vault, treat as move.
        if _is_markdown(event.dest_path) and self._accept(event.dest_path):
            self.debouncer.schedule(
                ("m", event.src_path), "move", event.src_path, event.dest_path
            )
        elif _is_markdown(event.src_path):
            self.debouncer.schedule(("d", event.src_path), "delete", event.src_path)


def initial_scan(cfg, processor):
    """Walk the vault once at startup so new notes are processed even without an event."""
    vault = Path(cfg["vault_path"])
    log.info("Initial scan of %s", vault)
    count = 0
    for p in vault.rglob("*.md"):
        if _in_ignored_dir(p, cfg["ignore_dirs"]):
            continue
        if not _in_included_dir(p, cfg["vault_path"], cfg.get("include_dirs", [])):
            continue
        try:
            processor.handle_upsert(str(p))
            count += 1
        except Exception:
            log.exception("initial scan failed for %s", p)
    log.info("Initial scan done (%d files)", count)


def main():
    cfg = load_config("config.json")
    setup_logging(cfg["_log_dir"])
    log.info("Starting watcher on vault: %s", cfg["vault_path"])
    log.info("Model: %s @ %s", cfg["ollama_model"], cfg["ollama_url"])

    processor = Processor(cfg)
    initial_scan(cfg, processor)

    handler = Handler(cfg, processor)
    observer = Observer()
    observer.schedule(handler, cfg["vault_path"], recursive=True)
    observer.start()
    log.info("Watching for changes. Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
