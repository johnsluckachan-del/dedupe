"""Windows GUI for dedupe using stdlib tkinter."""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, scrolledtext
from pathlib import Path


if sys.stdout is None:
    class _NullStream:
        def write(self, *args, **kwargs):
            return 0

        def flush(self, *args, **kwargs):
            pass

    sys.stdout = _NullStream()

if sys.stderr is None:
    class _NullStream2:
        def write(self, *args, **kwargs):
            return 0

        def flush(self, *args, **kwargs):
            pass

    sys.stderr = _NullStream2()


def get_gui_config_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        appdata = str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "dedupe" / "gui.json"


def load_last_folder() -> str:
    try:
        p = get_gui_config_path()
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            last = data.get("last_folder", "")
            if isinstance(last, str):
                return last
    except Exception:
        pass
    return ""


def save_last_folder(folder: str) -> None:
    try:
        p = get_gui_config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"last_folder": folder}, indent=2), encoding="utf-8")
    except Exception:
        pass


def child_python_executable() -> str:
    exe = sys.executable or "python.exe"
    try:
        p = Path(exe)
        if p.name.lower() == "pythonw.exe":
            candidate = p.with_name("python.exe")
            if candidate.is_file():
                return str(candidate)
            return str(candidate)
    except Exception:
        pass
    return exe


def parse_gui_args(argv: list[str]) -> dict:
    parser = argparse.ArgumentParser(prog="dedupe-gui")
    parser.add_argument("folder", nargs="?", default="", help="Folder to scan")
    parser.add_argument("--smoke", dest="smoke", default=None, help="Folder to scan in smoke mode")
    parser.add_argument("--smoke-out", dest="smoke_out", default=None, help="Output file for smoke stdout")
    parser.add_argument("--dry-run", action="store_true", help="Dry run")
    parser.add_argument("--no-similar", action="store_true", help="No similar")
    parser.add_argument("--permanent", action="store_true", help="Permanent delete")
    args, _unknown = parser.parse_known_args(argv)

    is_smoke = args.smoke is not None
    if is_smoke:
        folder = args.smoke or ""
    else:
        folder = args.folder or ""
    return {
        "smoke": is_smoke,
        "folder": folder,
        "smoke_out": args.smoke_out,
        "dry_run": bool(args.dry_run),
        "no_similar": bool(args.no_similar),
        "permanent": bool(args.permanent),
    }


class DedupeGUI:
    def __init__(self, root: tk.Tk, params: dict):
        self.root = root
        self.params = params
        self.is_smoke = bool(params["smoke"])
        self.smoke_out = params["smoke_out"]

        self.proc: subprocess.Popen | None = None
        self.msg_queue: queue.Queue = queue.Queue()
        self.captured_stdout: list[str] = []
        self.captured_stderr: list[str] = []
        self.stdout_eof = False
        self.stderr_eof = False
        self._smoke_done = False

        self.root.title("Dedupe")
        self.root.geometry("680x500")
        self.root.minsize(550, 400)

        main_frame = tk.Frame(root, padx=10, pady=10)
        main_frame.pack(fill=tk.BOTH, expand=True)

        folder_frame = tk.Frame(main_frame)
        folder_frame.pack(fill=tk.X, pady=(0, 10))

        folder_label = tk.Label(folder_frame, text="Folder:")
        folder_label.pack(side=tk.LEFT, padx=(0, 5))

        initial_folder = params["folder"]
        if not initial_folder:
            initial_folder = load_last_folder()

        self.folder_var = tk.StringVar(value=initial_folder)
        self.folder_entry = tk.Entry(folder_frame, textvariable=self.folder_var)
        self.folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        self.browse_btn = tk.Button(folder_frame, text="Browse...", command=self.browse_folder)
        self.browse_btn.pack(side=tk.LEFT)

        options_frame = tk.Frame(main_frame)
        options_frame.pack(fill=tk.X, pady=(0, 10))

        self.dry_run_var = tk.BooleanVar(value=bool(params["dry_run"]))
        self.dry_run_cb = tk.Checkbutton(
            options_frame,
            text="Preview only (change nothing)",
            variable=self.dry_run_var,
        )
        self.dry_run_cb.pack(anchor=tk.W)

        self.similar_var = tk.BooleanVar(value=not bool(params["no_similar"]))
        self.similar_cb = tk.Checkbutton(
            options_frame,
            text="Also find re-encoded / resized copies (slower)",
            variable=self.similar_var,
        )
        self.similar_cb.pack(anchor=tk.W)

        self.permanent_var = tk.BooleanVar(value=bool(params["permanent"]))
        self.permanent_cb = tk.Checkbutton(
            options_frame,
            text="Delete permanently (no trash)",
            variable=self.permanent_var,
        )
        self.permanent_cb.pack(anchor=tk.W)

        actions_frame = tk.Frame(main_frame)
        actions_frame.pack(fill=tk.X, pady=(0, 10))

        self.start_btn = tk.Button(actions_frame, text="Start", width=10, command=self.start_scan)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.stop_btn = tk.Button(actions_frame, text="Stop", width=10, state=tk.DISABLED, command=self.stop_scan)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 15))

        self.open_folder_btn = tk.Button(actions_frame, text="Open folder", command=self.open_folder)
        self.open_folder_btn.pack(side=tk.LEFT, padx=(0, 5))

        self.open_trash_btn = tk.Button(actions_frame, text="Open trash folder", command=self.open_trash_folder)
        self.open_trash_btn.pack(side=tk.LEFT)

        status_frame = tk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=(0, 5))

        self.status_var = tk.StringVar(value="Ready")
        self.status_label = tk.Label(status_frame, textvariable=self.status_var, anchor="w")
        self.status_label.pack(fill=tk.X)

        self.text_area = scrolledtext.ScrolledText(main_frame, wrap=tk.WORD, state=tk.DISABLED)
        self.text_area.pack(fill=tk.BOTH, expand=True)

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        if self.is_smoke:
            self.root.withdraw()
            self.root.after(10, self.start_scan)

    def append_text(self, text: str) -> None:
        self.text_area.config(state=tk.NORMAL)
        self.text_area.insert(tk.END, text)
        self.text_area.see(tk.END)
        self.text_area.config(state=tk.DISABLED)

    def browse_folder(self) -> None:
        initial = self.folder_var.get().strip() or None
        chosen = filedialog.askdirectory(initialdir=initial)
        if chosen:
            self.folder_var.set(chosen)
            save_last_folder(chosen)

    def open_folder(self) -> None:
        folder = self.folder_var.get().strip()
        if not folder:
            self.append_text("Error: no folder specified\n")
            return
        p = Path(folder)
        if not p.is_dir():
            self.append_text(f"Error: path '{folder}' is not a directory\n")
            return
        if sys.platform == "win32":
            os.startfile(str(p))
        else:
            subprocess.Popen(["xdg-open", str(p)])

    def open_trash_folder(self) -> None:
        folder = self.folder_var.get().strip()
        if not folder:
            self.append_text("Error: no folder specified\n")
            return
        p = Path(folder) / "_dedupe_trash"
        if not p.is_dir():
            self.append_text(f"Trash folder does not exist: '{p}'\n")
            return
        if sys.platform == "win32":
            os.startfile(str(p))
        else:
            subprocess.Popen(["xdg-open", str(p)])

    def set_running_state(self, running: bool) -> None:
        if running:
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            self.browse_btn.config(state=tk.DISABLED)
            self.folder_entry.config(state=tk.DISABLED)
            self.dry_run_cb.config(state=tk.DISABLED)
            self.similar_cb.config(state=tk.DISABLED)
            self.permanent_cb.config(state=tk.DISABLED)
        else:
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.browse_btn.config(state=tk.NORMAL)
            self.folder_entry.config(state=tk.NORMAL)
            self.dry_run_cb.config(state=tk.NORMAL)
            self.similar_cb.config(state=tk.NORMAL)
            self.permanent_cb.config(state=tk.NORMAL)

    def start_scan(self) -> None:
        folder = self.folder_var.get().strip()
        if not folder:
            self.append_text("Error: no folder specified\n")
            if self.is_smoke:
                self.finish_smoke()
            return
        folder_path = Path(folder)
        if not folder_path.exists():
            self.append_text(f"Error: path '{folder}' does not exist\n")
            if self.is_smoke:
                self.finish_smoke()
            return
        if not folder_path.is_dir():
            self.append_text(f"Error: path '{folder}' is not a directory\n")
            if self.is_smoke:
                self.finish_smoke()
            return

        save_last_folder(str(folder_path))

        cmd = [child_python_executable(), "-m", "dedupe", str(folder_path)]
        if self.dry_run_var.get():
            cmd.append("--dry-run")
        if not self.similar_var.get():
            cmd.append("--no-similar")
        if self.permanent_var.get():
            cmd.append("--permanent")

        self.text_area.config(state=tk.NORMAL)
        self.text_area.delete("1.0", tk.END)
        self.text_area.config(state=tk.DISABLED)

        self.captured_stdout.clear()
        self.captured_stderr.clear()
        self.stdout_eof = False
        self.stderr_eof = False
        self.status_var.set("Starting...")
        self.set_running_state(True)

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        env = os.environ.copy()
        package_parent = str(Path(__file__).resolve().parent.parent)
        existing = env.get("PYTHONPATH", "")
        if existing:
            if package_parent not in existing.split(os.pathsep):
                env["PYTHONPATH"] = package_parent + os.pathsep + existing
        else:
            env["PYTHONPATH"] = package_parent

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
                env=env,
            )
        except Exception as exc:
            self.append_text(f"Error starting process: {exc}\n")
            self.status_var.set("Failed to start")
            self.set_running_state(False)
            if self.is_smoke:
                self.finish_smoke()
            return

        def read_stream(stream, name, q):
            try:
                for line in iter(stream.readline, ""):
                    q.put((name, line))
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass
                q.put((f"{name}_eof", None))

        t_out = threading.Thread(target=read_stream, args=(self.proc.stdout, "stdout", self.msg_queue), daemon=True)
        t_err = threading.Thread(target=read_stream, args=(self.proc.stderr, "stderr", self.msg_queue), daemon=True)
        t_out.start()
        t_err.start()
        self.root.after(50, self.poll_queue)

    def stop_scan(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass
            self.status_var.set("Stopped")
            self.append_text("Scan stopped by user.\n")

    def poll_queue(self) -> None:
        try:
            while True:
                msg_type, data = self.msg_queue.get_nowait()
                if msg_type == "stdout":
                    self.captured_stdout.append(data)
                    self.append_text(data)
                elif msg_type == "stderr":
                    if data:
                        self.captured_stderr.append(data)
                    s = data.strip() if data else ""
                    if s:
                        self.status_var.set(s)
                        if s.startswith("Done in") or s.startswith("Error:"):
                            self.append_text(s + "\n")
                elif msg_type == "stdout_eof":
                    self.stdout_eof = True
                elif msg_type == "stderr_eof":
                    self.stderr_eof = True
        except queue.Empty:
            pass

        if self.proc is not None and self.proc.poll() is not None and self.stdout_eof and self.stderr_eof:
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
            self.proc = None
            self.set_running_state(False)
            if self.is_smoke:
                self.finish_smoke()
            return

        # If process ended but EOF not yet seen, keep polling briefly.
        self.root.after(50, self.poll_queue)

    def finish_smoke(self) -> None:
        if self._smoke_done:
            return
        self._smoke_done = True
        if self.smoke_out:
            try:
                out_p = Path(self.smoke_out)
                out_p.parent.mkdir(parents=True, exist_ok=True)
                out_p.write_text("".join(self.captured_stdout), encoding="utf-8")
            except Exception:
                pass
        try:
            self.root.quit()
        except Exception:
            pass

    def on_closing(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    params = parse_gui_args(argv)
    root = tk.Tk()
    _app = DedupeGUI(root, params)
    root.mainloop()
    if params["smoke"]:
        try:
            root.destroy()
        except Exception:
            pass
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
