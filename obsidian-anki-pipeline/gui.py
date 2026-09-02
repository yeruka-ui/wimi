"""
Tkinter GUI for the Obsidian→Anki pipeline (on-demand mode).

Workflow: launch → pick a folder from the vault tree → Generate & export →
Groq processes every .md under that folder (reusing cached hashes) → subset
.apkg is written straight to the user-chosen path.
"""
import json
import logging
import os
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


DEFAULT_CONFIG = {
    "vault_path": "",
    "output_dir": "./",
    "deck_root_name": "Obsidian",
    "write_uid_to_vault": True,
    "max_section_chars": 6000,
    "llm_backend": "groq",
    "groq_model": "llama-3.1-8b-instant",
    "groq_timeout_seconds": 60,
    "ignore_dirs": [".obsidian", ".trash", ".git", "node_modules"],
}


def _exe_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _find_config():
    for c in (_exe_dir() / "config.json",
              _exe_dir().parent / "config.json",
              Path.cwd() / "config.json"):
        if c.exists():
            return c
    return None


def _app_dir():
    found = _find_config()
    return found.parent if found else _exe_dir()


HERE = _app_dir()
CONFIG_PATH = HERE / "config.json"
LOG_PATH = HERE / "logs" / "pipeline.log"


def save_config(cfg):
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def list_all_folders(vault_path, ignore_dirs):
    """Recursively list every folder under the vault as (rel_posix_path, depth)."""
    vault = Path(vault_path)
    if not vault.exists():
        return []
    ignore = {d.lower() for d in ignore_dirs}

    def walk(dir_path, depth):
        entries = []
        try:
            children = sorted(dir_path.iterdir(), key=lambda x: x.name.lower())
        except OSError:
            return entries
        for p in children:
            if not p.is_dir():
                continue
            if p.name.startswith(".") or p.name.lower() in ignore:
                continue
            rel = p.relative_to(vault).as_posix()
            entries.append((rel, depth))
            entries.extend(walk(p, depth + 1))
        return entries

    return walk(vault, 0)


def _slugify(name):
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name.replace("::", "_")).strip("._-")
    return slug or "deck"


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Wimi — Obsidian → Anki")
        self.root.geometry("780x560")

        self.cfg = self._load_or_bootstrap_config()
        if self.cfg is None:
            root.destroy()
            return

        self.selected_folder = tk.StringVar(value="")  # "" means whole vault
        self._busy = False

        self._build_ui()
        self._refresh_folders()
        self._tail_thread = threading.Thread(target=self._tail_log, daemon=True)
        self._tail_thread.start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        self.root.destroy()

    def _load_or_bootstrap_config(self):
        global HERE, CONFIG_PATH, LOG_PATH
        found = _find_config()
        if found:
            HERE = found.parent
            CONFIG_PATH = found
            LOG_PATH = HERE / "logs" / "pipeline.log"
            try:
                with found.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                messagebox.showerror("Config error", f"Could not read {found}:\n{e}")
                return None

        if not messagebox.askyesno(
            "First-run setup",
            f"No config.json found next to the app.\n\n"
            f"Create one at:\n{_exe_dir() / 'config.json'}\n\nProceed?",
        ):
            return None
        vault = filedialog.askdirectory(title="Select your Obsidian vault folder")
        if not vault:
            messagebox.showinfo("Cancelled", "No vault selected — cannot start.")
            return None
        new_cfg = dict(DEFAULT_CONFIG)
        new_cfg["vault_path"] = vault
        target = _exe_dir() / "config.json"
        try:
            with target.open("w", encoding="utf-8") as f:
                json.dump(new_cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror("Write failed", str(e))
            return None
        HERE = target.parent
        CONFIG_PATH = target
        LOG_PATH = HERE / "logs" / "pipeline.log"
        return new_cfg

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Vault:").pack(side="left")
        self.vault_var = tk.StringVar(value=self.cfg.get("vault_path", ""))
        ttk.Entry(top, textvariable=self.vault_var, width=60).pack(side="left", padx=6)
        ttk.Button(top, text="Refresh", command=self._refresh_folders).pack(side="left")

        body = ttk.Frame(self.root, padding=8)
        body.pack(fill="both", expand=True)

        left = ttk.LabelFrame(body, text="Pick one folder (or leave blank for the whole vault)", padding=6)
        left.pack(side="left", fill="both", expand=True)

        self.folder_canvas = tk.Canvas(left, borderwidth=0, highlightthickness=0)
        self.folder_scroll = ttk.Scrollbar(left, orient="vertical", command=self.folder_canvas.yview)
        self.folder_inner = ttk.Frame(self.folder_canvas)
        self.folder_inner.bind(
            "<Configure>",
            lambda e: self.folder_canvas.configure(scrollregion=self.folder_canvas.bbox("all")),
        )
        self.folder_canvas.create_window((0, 0), window=self.folder_inner, anchor="nw")
        self.folder_canvas.configure(yscrollcommand=self.folder_scroll.set)
        self.folder_canvas.pack(side="left", fill="both", expand=True)
        self.folder_scroll.pack(side="right", fill="y")

        right = ttk.Frame(body, padding=(8, 0))
        right.pack(side="left", fill="y")
        self.gen_btn = ttk.Button(right, text="Generate && export…", command=self._generate_and_export)
        self.gen_btn.pack(fill="x", pady=2)
        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=6)
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(right, textvariable=self.status_var, wraplength=180).pack(fill="x")

        log_frame = ttk.LabelFrame(self.root, text="Log", padding=6)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, height=12, wrap="none", state="disabled")
        self.log_text.pack(fill="both", expand=True, side="left")
        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        yscroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=yscroll.set)

    def _refresh_folders(self):
        for w in self.folder_inner.winfo_children():
            w.destroy()

        vault = self.vault_var.get().strip()
        self.cfg["vault_path"] = vault
        folders = list_all_folders(vault, self.cfg.get("ignore_dirs", []))

        ttk.Radiobutton(
            self.folder_inner,
            text="(whole vault)",
            variable=self.selected_folder,
            value="",
        ).pack(anchor="w", pady=1)

        if not folders:
            ttk.Label(self.folder_inner, text="(no folders found)").pack(anchor="w")
            return

        for rel, depth in folders:
            label = ("    " * depth) + rel.rsplit("/", 1)[-1]
            ttk.Radiobutton(
                self.folder_inner,
                text=label,
                variable=self.selected_folder,
                value=rel,
            ).pack(anchor="w", pady=1)

    def _generate_and_export(self):
        if self._busy:
            return
        rel_folder = self.selected_folder.get().strip()
        vault = Path(self.cfg["vault_path"]).resolve()
        if not vault.is_dir():
            messagebox.showerror("Vault missing", f"Not a directory:\n{vault}")
            return
        scope_dir = vault / rel_folder if rel_folder else vault
        if not scope_dir.is_dir():
            messagebox.showerror("Folder missing", f"Not a directory:\n{scope_dir}")
            return

        md_files = self._collect_md_files(scope_dir)
        if not md_files:
            messagebox.showwarning("No notes", f"No .md files found under:\n{scope_dir}")
            return

        default_slug = _slugify(rel_folder or self.cfg.get("deck_root_name", "Obsidian"))
        target = filedialog.asksaveasfilename(
            title=f"Save deck: {rel_folder or '(whole vault)'}",
            defaultextension=".apkg",
            initialfile=f"{default_slug}.apkg",
            filetypes=[("Anki deck", "*.apkg"), ("All files", "*.*")],
        )
        if not target:
            return

        self._set_busy(True, f"Generating {len(md_files)} note(s)…")
        t = threading.Thread(
            target=self._run_job,
            args=(md_files, rel_folder, target),
            daemon=True,
        )
        t.start()

    def _collect_md_files(self, scope_dir):
        ignore = {d.lower() for d in self.cfg.get("ignore_dirs", [])}
        out = []
        for root, dirs, files in os.walk(scope_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in ignore]
            for name in files:
                if name.lower().endswith(".md"):
                    out.append(str(Path(root) / name))
        return sorted(out)

    def _run_job(self, md_files, rel_folder, target):
        try:
            import config as config_mod
            import logging_setup
            import processor as processor_mod
            import deck_builder

            os.chdir(str(HERE))
            cfg = config_mod.load_config(str(CONFIG_PATH))
            logging_setup.setup_logging(cfg["_log_dir"])
            log = logging.getLogger("gui.job")
            log.info("Generate & export: %d files under %s -> %s",
                     len(md_files), rel_folder or "(whole vault)", target)

            proc = processor_mod.Processor(cfg)
            generated = 0
            for i, path in enumerate(md_files, 1):
                self._post_status(f"[{i}/{len(md_files)}] {Path(path).name}")
                try:
                    generated += proc.handle_upsert(path)
                except Exception:
                    log.exception("upsert failed: %s", path)

            deck_root = cfg.get("deck_root_name", "Obsidian")
            if rel_folder:
                parts = [p for p in rel_folder.split("/") if p]
                deck_prefix = "::".join([deck_root] + parts)
            else:
                deck_prefix = deck_root

            n_decks, n_cards = deck_builder.build_subset(proc.cards, deck_prefix, target)
            log.info("Exported %d cards across %d sub-decks", n_cards, n_decks)
            self._post_done(True,
                            f"Done. {n_cards} cards in {n_decks} sub-deck(s).\nSaved to:\n{target}")
        except ValueError as e:
            self._post_done(False, f"Nothing to export.\n\n{e}")
        except Exception as e:
            logging.getLogger("gui.job").exception("job failed")
            self._post_done(False, f"Failed:\n{e}")

    def _post_status(self, text):
        self.root.after(0, self.status_var.set, text)

    def _post_done(self, ok, msg):
        def finish():
            self._set_busy(False, "Idle")
            (messagebox.showinfo if ok else messagebox.showwarning)(
                "Export" if ok else "Export failed", msg
            )
        self.root.after(0, finish)

    def _set_busy(self, busy, status_text):
        self._busy = busy
        self.status_var.set(status_text)
        self.gen_btn.configure(state="disabled" if busy else "normal")

    def _tail_log(self):
        last_size = 0
        while True:
            try:
                if LOG_PATH.exists():
                    size = LOG_PATH.stat().st_size
                    if size < last_size:
                        last_size = 0
                    if size > last_size:
                        with LOG_PATH.open("r", encoding="utf-8", errors="replace") as f:
                            f.seek(last_size)
                            chunk = f.read()
                        last_size = size
                        if chunk:
                            self.root.after(0, self._append_log, chunk)
            except Exception:
                pass
            time.sleep(0.7)

    def _append_log(self, chunk):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", chunk)
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 2000:
            self.log_text.delete("1.0", f"{line_count - 1500}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


def _write_crashlog(exc):
    import traceback
    try:
        path = _app_dir() / "crash.log"
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n----- {time.strftime('%Y-%m-%d %H:%M:%S')} -----\n")
            f.write("".join(traceback.format_exception(exc)))
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        _write_crashlog(e)
        raise
