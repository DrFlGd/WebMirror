"""Tkinter GUI for saving, browsing, and locally hosting mirrored websites."""
from __future__ import annotations

from queue import Empty, Queue
from threading import Thread
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
import webbrowser

from .core import MirrorEngine, MirrorLibrary, MirrorRecord, MirrorServer, normalize_url


class WebMirrorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("WebMirror — Offline website library")
        root.geometry("850x650")
        root.minsize(670, 540)
        self.library = MirrorLibrary()
        self.server = MirrorServer(self.library)
        self.queue: Queue[tuple[str, object]] = Queue()
        self.records: list[MirrorRecord] = []
        self.active_key: str | None = None
        self.busy = False
        self.url = tk.StringVar()
        self.port = tk.StringVar(value="8000")
        self.full_assets = tk.BooleanVar(value=False)
        self.dynamic_assets = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Ready to mirror a website.")
        self.site_details = tk.StringVar(value="Choose a saved website.")
        self.host_status = tk.StringVar(value="Local server stopped.")

        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Heading.TLabel", font=("TkDefaultFont", 19, "bold"))
        style.configure("Note.TLabel", foreground="#556477")
        style.configure("Action.TButton", padding=(10, 7))

        panel = ttk.Frame(root, padding=18)
        panel.pack(fill="both", expand=True)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)
        panel.rowconfigure(4, weight=1)

        heading = ttk.Frame(panel)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        ttk.Label(heading, text="WebMirror", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(heading, text="Save public websites and serve your offline copies.",
                  style="Note.TLabel").pack(anchor="w")

        download_box = ttk.LabelFrame(panel, text="1 · Mirror a website", padding=12)
        download_box.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        download_box.columnconfigure(0, weight=1)
        self.url_entry = ttk.Entry(download_box, textvariable=self.url)
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 9))
        self.url_entry.bind("<Return>", lambda _: self.start_mirror())
        self.mirror_button = ttk.Button(download_box, text="Mirror website",
                                        command=self.start_mirror, style="Action.TButton")
        self.mirror_button.grid(row=0, column=1)
        ttk.Label(download_box, text="Example: https://ianbauters.github.io/terrain-press/",
                  style="Note.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.full_checkbox = ttk.Checkbutton(
            download_box,
            text="GitHub Pages: include all published repository files (may be a large download)",
            variable=self.full_assets,
        )
        self.full_checkbox.grid(row=2, column=0, columnspan=2, sticky="w", pady=(7, 0))
        self.dynamic_checkbox = ttk.Checkbutton(
            download_box,
            text="Capture JavaScript-loaded assets with Chrome or Edge (recommended for web apps)",
            variable=self.dynamic_assets,
        )
        self.dynamic_checkbox.grid(row=3, column=0, columnspan=2, sticky="w", pady=(7, 0))

        library_box = ttk.LabelFrame(panel, text="2 · Saved websites", padding=12)
        library_box.grid(row=2, column=0, sticky="nsew", pady=(0, 12))
        library_box.columnconfigure(0, weight=1)
        library_box.rowconfigure(0, weight=1)
        self.sites = tk.Listbox(library_box, height=7, exportselection=False, activestyle="none")
        self.sites.grid(row=0, column=0, sticky="nsew")
        self.sites.bind("<<ListboxSelect>>", self.selection_changed)
        scrollbar = ttk.Scrollbar(library_box, orient="vertical", command=self.sites.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.sites.configure(yscrollcommand=scrollbar.set)
        ttk.Label(library_box, textvariable=self.site_details, wraplength=720,
                  style="Note.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=8)
        actions = ttk.Frame(library_box)
        actions.grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(actions, text="Port").pack(side="left", padx=(0, 5))
        ttk.Entry(actions, width=7, textvariable=self.port).pack(side="left", padx=(0, 10))
        self.host_button = ttk.Button(actions, text="Host selected", command=self.host_selected)
        self.host_button.pack(side="left", padx=(0, 6))
        self.open_button = ttk.Button(actions, text="Open in browser", command=self.open_host)
        self.open_button.pack(side="left", padx=(0, 6))
        self.stop_button = ttk.Button(actions, text="Stop", command=self.stop_host)
        self.stop_button.pack(side="left", padx=(0, 6))
        self.delete_button = ttk.Button(actions, text="Delete", command=self.delete_selected)
        self.delete_button.pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="Refresh", command=self.refresh).pack(side="left")

        info = ttk.Frame(panel)
        info.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(info, textvariable=self.host_status).pack(anchor="w")
        ttk.Label(info, textvariable=self.status, style="Note.TLabel",
                  wraplength=780).pack(anchor="w")

        activity = ttk.LabelFrame(panel, text="3 · Activity", padding=8)
        activity.grid(row=4, column=0, sticky="nsew")
        activity.columnconfigure(0, weight=1)
        activity.rowconfigure(0, weight=1)
        self.log = scrolledtext.ScrolledText(activity, height=6, state="disabled", wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew")

        self.refresh()
        self._controls()
        root.after(100, self._poll)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.url_entry.focus_set()

    def _write_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 500:
            self.log.delete("1.0", "100.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def selected(self) -> MirrorRecord | None:
        indices = self.sites.curselection()
        return self.records[indices[0]] if indices and indices[0] < len(self.records) else None

    def refresh(self, select_key: str | None = None) -> None:
        previous = self.selected()
        key = select_key or (previous.key if previous else None)
        self.records = self.library.list()
        self.sites.delete(0, "end")
        for i, item in enumerate(self.records):
            self.sites.insert("end", f"{item.title}   •   {item.saved_at[:10]}")
            if item.key == key:
                self.sites.selection_set(i)
                self.sites.see(i)
        self.selection_changed()

    def selection_changed(self, _event: object = None) -> None:
        item = self.selected()
        self.site_details.set(
            f"{item.url} · {item.files} files · {item.pages} pages · {item.failures} failed/skipped"
            if item else "Select a mirrored website above."
        )
        self._controls()

    def _controls(self) -> None:
        self.mirror_button.configure(state="disabled" if self.busy else "normal")
        self.url_entry.configure(state="disabled" if self.busy else "normal")
        self.full_checkbox.configure(state="disabled" if self.busy else "normal")
        self.dynamic_checkbox.configure(state="disabled" if self.busy else "normal")
        can_select = self.selected() is not None and not self.busy
        self.host_button.configure(state="normal" if can_select else "disabled")
        self.delete_button.configure(state="normal" if can_select else "disabled")
        self.open_button.configure(state="normal" if self.server.url else "disabled")
        self.stop_button.configure(state="normal" if self.server.url else "disabled")

    def start_mirror(self) -> None:
        if self.busy:
            return
        try:
            url = normalize_url(self.url.get())
        except ValueError as exc:
            messagebox.showerror("Invalid URL", str(exc), parent=self.root)
            return
        self.busy = True
        self.status.set("Downloading website...")
        self._write_log(f"Starting: {url}")
        include_pages_files = self.full_assets.get()
        browser_capture = self.dynamic_assets.get()
        self._controls()

        def worker() -> None:
            try:
                result = MirrorEngine(
                    self.library, progress=lambda msg: self.queue.put(("log", msg)),
                    include_pages_files=include_pages_files,
                    browser_capture=browser_capture,
                ).mirror(url)
                self.queue.put(("done", result))
            except Exception as exc:
                self.queue.put(("error", str(exc)))
        Thread(target=worker, daemon=True, name="webmirror-download").start()

    def _poll(self) -> None:
        try:
            while True:
                kind, value = self.queue.get_nowait()
                if kind == "log":
                    self.status.set(str(value))
                    self._write_log(str(value))
                elif kind == "done":
                    self.busy = False
                    self.status.set("Website mirror saved.")
                    self._write_log(f"Finished: {value.url}")
                    self.refresh(select_key=value.key)
                elif kind == "error":
                    self.busy = False
                    self.status.set("Mirror failed; see the activity log.")
                    self._write_log(f"Error: {value}")
                    messagebox.showerror("Mirror failed", str(value), parent=self.root)
                    self._controls()
        except Empty:
            pass
        self.root.after(100, self._poll)

    def host_selected(self) -> None:
        item = self.selected()
        if item is None:
            return
        try:
            url = self.server.start(item, int(self.port.get()))
        except (ValueError, OSError) as exc:
            messagebox.showerror("Server error", str(exc), parent=self.root)
            return
        self.active_key = item.key
        self.host_status.set(f"Hosting {item.title} at {url} (this computer only)")
        self.status.set("Open the site in your browser.")
        self._write_log(f"Hosting: {url}")
        self._controls()

    def open_host(self) -> None:
        if self.server.url:
            webbrowser.open(self.server.url)

    def stop_host(self) -> None:
        if self.server.url:
            self.server.stop()
            self.active_key = None
            self.host_status.set("Local server stopped.")
            self._write_log("Stopped local server.")
            self._controls()

    def delete_selected(self) -> None:
        item = self.selected()
        if item and not self.busy and messagebox.askyesno(
            "Delete mirror", f"Delete all downloaded files for {item.url}?",
            parent=self.root
        ):
            if item.key == self.active_key:
                self.stop_host()
            try:
                self.library.delete(item)
            except OSError as exc:
                messagebox.showerror("Delete failed", str(exc), parent=self.root)
                return
            self._write_log(f"Deleted: {item.url}")
            self.refresh()

    def close(self) -> None:
        self.server.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    WebMirrorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
