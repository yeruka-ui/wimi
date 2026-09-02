"""
Minimal Tkinter GUI for the Obsidian→Anki pipeline.

- Lists top-level folders in the vault.
- Lets you check which folders to process and persists the choice to config.json.
- Start/Stop buttons run watcher.py in a subprocess.
- Tails logs/pipeline.log so you can see what's happening.
"""
import json
import os
import subprocess
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
    "ollama_url": "http://localhost:11434",
    "ollama_model": "llama3.2:3b",
    "ollama_timeout_seconds": 120,
    "debounce_seconds": 1.5,
    "write_uid_to_vault": True,
    "max_section_chars": 6000,
    "include_dirs": [],
    "auto_start_watcher": True,
    "ignore_dirs": [".obsidian", ".trash", ".git", "node_modules"],
}


def _exe_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _find_config():
    """Search exe dir, then parent, then cwd for config.json. Returns first match or None."""
    candidates = [
        _exe_dir() / "config.json",
        _exe_dir().parent / "config.json",
        Path.cwd() / "config.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _app_dir():
    """Directory where config.json (and thus logs/state/cards/deck) lives."""
    found = _find_config()
    if found:
        return found.parent
    return _exe_dir()


HERE = _app_dir()
CONFIG_PATH = HERE / "config.json"
LOG_PATH = HERE / "logs" / "pipeline.log"


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def list_top_level_folders(vault_path, ignore_dirs):
    vault = Path(vault_path)
    if not vault.exists():
        return []
    ignore = {d.lower() for d in ignore_dirs}
    out = []
    for p in sorted(vault.iterdir()):
        if p.is_dir() and p.name.lower() not in ignore and not p.name.startswith("."):
            out.append(p.name)
    return out


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Wimi — Obsidian → Anki")
        self.root.geometry("780x560")

        self.cfg = self._load_or_bootstrap_config()
        if self.cfg is None:
            root.destroy()
            return

        self.proc = None
        self.checkbox_vars = {}

        self._build_ui()
        self._refresh_folders()
        self._tail_thread = threading.Thread(target=self._tail_log, daemon=True)
        self._tail_thread.start()

        # Auto-stop watcher when the window closes.
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Auto-start watcher on launch unless disabled in config.
        if self.cfg.get("auto_start_watcher", True):
            self.root.after(500, self._ensure_ollama_then_start)

    def _ensure_ollama_then_start(self):
        # Best-effort: launch the Ollama tray app if the server isn't reachable.
        if not self._ollama_up():
            self._try_launch_ollama()
            # Give it a few seconds to come up; retry once.
            self.root.after(4000, self._start)
            return
        self._start()

    def _ollama_up(self):
        try:
            import urllib.request
            url = self.cfg.get("ollama_url", "http://localhost:11434").rstrip("/") + "/api/tags"
            with urllib.request.urlopen(url, timeout=1.5) as r:
                return r.status == 200
        except Exception:
            return False

    def _try_launch_ollama(self):
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        ]
        for exe in candidates:
            if exe.exists():
                try:
                    subprocess.Popen([str(exe)], creationflags=subprocess.DETACHED_PROCESS if os.name == "nt" else 0)
                    return True
                except Exception:
                    continue
        return False

    def _on_close(self):
        try:
            if self.proc and self.proc.poll() is None:
                self._stop()
        finally:
            self.root.destroy()

    # --- config load / bootstrap -------------------------------------------

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

        # No config anywhere — offer to create one.
        choice = messagebox.askyesno(
            "First-run setup",
            f"No config.json found next to the app.\n\n"
            f"Create one at:\n{_exe_dir() / 'config.json'}\n\nProceed?",
        )
        if not choice:
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

    # --- UI construction ---------------------------------------------------

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Vault:").pack(side="left")
        self.vault_var = tk.StringVar(value=self.cfg.get("vault_path", ""))
        ttk.Entry(top, textvariable=self.vault_var, width=60).pack(side="left", padx=6)
        ttk.Button(top, text="Refresh folders", command=self._refresh_folders).pack(side="left")

        body = ttk.Frame(self.root, padding=8)
        body.pack(fill="both", expand=True)

        left = ttk.LabelFrame(body, text="Folders to process (unchecked = ignored)", padding=6)
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
        ttk.Button(right, text="Select all", command=self._select_all).pack(fill="x", pady=2)
        ttk.Button(right, text="Clear all", command=self._clear_all).pack(fill="x", pady=2)
        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=6)
        ttk.Button(right, text="Save selection", command=self._save_selection).pack(fill="x", pady=2)
        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=6)
        self.start_btn = ttk.Button(right, text="Start watcher", command=self._start)
        self.start_btn.pack(fill="x", pady=2)
        self.stop_btn = ttk.Button(right, text="Stop watcher", command=self._stop, state="disabled")
        self.stop_btn.pack(fill="x", pady=2)
        ttk.Separator(right, orient="horizontal").pack(fill="x", pady=6)
        self.status_var = tk.StringVar(value="Stopped")
        ttk.Label(right, textvariable=self.status_var).pack(fill="x")

        log_frame = ttk.LabelFrame(self.root, text="Log (tail)", padding=6)
        log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log_text = tk.Text(log_frame, height=12, wrap="none", state="disabled")
        self.log_text.pack(fill="both", expand=True, side="left")
        yscroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        yscroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=yscroll.set)

    # --- folder list -------------------------------------------------------

    def _refresh_folders(self):
        for w in self.folder_inner.winfo_children():
            w.destroy()
        self.checkbox_vars.clear()

        vault = self.vault_var.get().strip()
        self.cfg["vault_path"] = vault  # keep in-memory copy in sync
        folders = list_top_level_folders(vault, self.cfg.get("ignore_dirs", []))
        selected = {d.lower() for d in self.cfg.get("include_dirs", [])}

        if not folders:
            ttk.Label(self.folder_inner, text="(no folders found)").pack(anchor="w")
            return

        # If include_dirs is empty, treat all as selected visually.
        default_all = not selected
        for name in folders:
            var = tk.BooleanVar(value=(default_all or name.lower() in selected))
            cb = ttk.Checkbutton(self.folder_inner, text=name, variable=var)
            cb.pack(anchor="w", pady=1)
            self.checkbox_vars[name] = var

    def _select_all(self):
        for v in self.checkbox_vars.values():
            v.set(True)

    def _clear_all(self):
        for v in self.checkbox_vars.values():
            v.set(False)

    def _save_selection(self, silent=False):
        checked = [n for n, v in self.checkbox_vars.items() if v.get()]
        # If everything is checked, store empty list = "everything" (matches watcher semantics).
        if len(checked) == len(self.checkbox_vars) and len(checked) > 0:
            self.cfg["include_dirs"] = []
        else:
            self.cfg["include_dirs"] = checked
        self.cfg["vault_path"] = self.vault_var.get().strip()
        save_config(self.cfg)
        if not silent:
            messagebox.showinfo("Saved", f"include_dirs = {self.cfg['include_dirs']}")

    # --- watcher control ---------------------------------------------------

    def _start(self):
        if self.proc and self.proc.poll() is None:
            return
        # Save first so the watcher picks up the current selection.
        self._save_selection(silent=True)
        try:
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--watcher"]
            else:
                cmd = [sys.executable, str(HERE / "watcher.py")]
            self.proc = subprocess.Popen(
                cmd,
                cwd=str(HERE),
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
        except Exception as e:
            messagebox.showerror("Start failed", str(e))
            return
        self.status_var.set(f"Running (PID {self.proc.pid})")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")

    def _stop(self):
        if not self.proc or self.proc.poll() is not None:
            self._on_stopped()
            return
        try:
            if os.name == "nt":
                self.proc.send_signal(subprocess.signal.CTRL_BREAK_EVENT)
            else:
                self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        self._on_stopped()

    def _on_stopped(self):
        self.status_var.set("Stopped")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    # --- log tail ----------------------------------------------------------

    def _tail_log(self):
        last_size = 0
        while True:
            try:
                if LOG_PATH.exists():
                    size = LOG_PATH.stat().st_size
                    if size < last_size:  # rotated
                        last_size = 0
                    if size > last_size:
                        with LOG_PATH.open("r", encoding="utf-8", errors="replace") as f:
                            f.seek(last_size)
                            chunk = f.read()
                        last_size = size
                        if chunk:
                            self.root.after(0, self._append_log, chunk)
                # detect subprocess exit
                if self.proc and self.proc.poll() is not None:
                    self.root.after(0, self._on_stopped)
            except Exception:
                pass
            time.sleep(0.7)

    def _append_log(self, chunk):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", chunk)
        # cap the buffer so it doesn't grow unbounded
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 2000:
            self.log_text.delete("1.0", f"{line_count - 1500}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main():
    # Dual-mode entry point: --watcher runs the watcher process, otherwise the GUI.
    if "--watcher" in sys.argv[1:]:
        import watcher  # local import so PyInstaller sees the dep
        watcher.main()
        return
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
