"""Tkinter GUI for saving, browsing, and locally hosting mirrored websites."""
from __future__ import annotations

from queue import Empty, Queue
from threading import Event, Thread
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
import webbrowser

from .core import (
    MirrorEngine, MirrorLibrary, MirrorPaused, MirrorRecord, MirrorServer, normalize_url,
    parse_filters,
)


class WebMirrorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("WebMirror — Offline website library")
        root.geometry("850x820")
        root.minsize(670, 680)
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
        self.manual_verification = tk.BooleanVar(value=True)
        self.auto_solve = tk.BooleanVar(value=False)
        self.browserlike = tk.BooleanVar(value=True)
        self.block_ads = tk.BooleanVar(value=True)
        self.polite = tk.BooleanVar(value=True)
        self.obey_robots = tk.BooleanVar(value=False)
        self.update_unchanged = tk.BooleanVar(value=True)
        self.filters = tk.StringVar()
        self.pause_event: Event | None = None
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
        self.pause_button = ttk.Button(download_box, text="Pause", command=self.pause_mirror)
        self.pause_button.grid(row=0, column=2, padx=(6, 0))
        ttk.Label(download_box, text="Example: https://ianbauters.github.io/terrain-press/",
                  style="Note.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.full_checkbox = ttk.Checkbutton(
            download_box,
            text="GitHub Pages: include all published repository files (may be a large download)",
            variable=self.full_assets,
        )
        self.full_checkbox.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.dynamic_checkbox = ttk.Checkbutton(
            download_box,
            text="Capture JavaScript-loaded assets with Chrome or Edge (recommended for web apps)",
            variable=self.dynamic_assets,
        )
        self.dynamic_checkbox.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.verification_checkbox = ttk.Checkbutton(
            download_box,
            text="Let me complete human-verification checks in an opened browser (up to 3 min)",
            variable=self.manual_verification,
        )
        self.verification_checkbox.grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )
        self.auto_solve_checkbox = ttk.Checkbutton(
            download_box,
            text="Solve Cloudflare checks automatically with Scrapling's stealth browser (optional)",
            variable=self.auto_solve,
        )
        self.auto_solve_checkbox.grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        options = ttk.Frame(download_box)
        options.grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.option_checkboxes = []
        for index, (text, variable) in enumerate((
            ("Request pages the way Chrome does", self.browserlike),
            ("Block ads and trackers", self.block_ads),
            ("Slow down when a site is busy", self.polite),
            ("Obey robots.txt", self.obey_robots),
            ("Only re-download files that changed", self.update_unchanged),
        )):
            box = ttk.Checkbutton(options, text=text, variable=variable)
            box.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 24), pady=(0, 4))
            self.option_checkboxes.append(box)
        filter_row = ttk.Frame(download_box)
        filter_row.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(3, 0))
        filter_row.columnconfigure(1, weight=1)
        ttk.Label(filter_row, text="Filters").grid(row=0, column=0, padx=(0, 8))
        self.filter_entry = ttk.Entry(filter_row, textvariable=self.filters)
        self.filter_entry.grid(row=0, column=1, sticky="ew")
        ttk.Label(filter_row, text="Optional, e.g. +*.glb -*/forum/*  (+ adds, - leaves out; the last match wins)",
                  style="Note.TLabel").grid(row=1, column=1, sticky="w", pady=(3, 0))

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
        self.verification_checkbox.configure(state="disabled" if self.busy else "normal")
        self.auto_solve_checkbox.configure(state="disabled" if self.busy else "normal")
        for box in self.option_checkboxes:
            box.configure(state="disabled" if self.busy else "normal")
        self.filter_entry.configure(state="disabled" if self.busy else "normal")
        self.pause_button.configure(state="normal" if self.busy else "disabled")
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
        filters = self.filters.get().strip()
        try:
            parse_filters(filters)
        except ValueError as exc:
            messagebox.showerror("Invalid filter", str(exc), parent=self.root)
            return
        resume = False
        if self.library.is_paused(url):
            answer = messagebox.askyesnocancel(
                "Resume paused mirror",
                "This website has a paused mirror. Resume where it stopped?\n\n"
                "Yes resumes it. No starts over and discards the paused copy.",
                parent=self.root,
            )
            if answer is None:
                return
            resume = answer
            if not resume:
                self.library.discard_paused(url)
        self.busy = True
        pause_event = self.pause_event = Event()
        self.status.set("Downloading website...")
        self._write_log(f"Starting: {url}")
        include_pages_files = self.full_assets.get()
        browser_capture = self.dynamic_assets.get()
        manual_verification = browser_capture and self.manual_verification.get()
        options = dict(
            auto_solve_challenges=browser_capture and self.auto_solve.get(),
            browserlike=self.browserlike.get(), block_ads=self.block_ads.get(),
            polite=self.polite.get(), obey_robots=self.obey_robots.get(),
            update_unchanged=self.update_unchanged.get(), filters=filters or None,
        )
        self._controls()

        def worker() -> None:
            try:
                result = MirrorEngine(
                    self.library, progress=lambda msg: self.queue.put(("log", msg)),
                    include_pages_files=include_pages_files,
                    browser_capture=browser_capture,
                    manual_verification=manual_verification,
                    on_verification_required=lambda message: self.queue.put(
                        ("verification", message)
                    ),
                    pause_event=pause_event,
                    **options,
                ).mirror(url, resume=resume)
                self.queue.put(("done", result))
            except MirrorPaused:
                self.queue.put(("paused", url))
            except Exception as exc:
                self.queue.put(("error", str(exc)))
        Thread(target=worker, daemon=True, name="webmirror-download").start()

    def pause_mirror(self) -> None:
        if self.busy and self.pause_event is not None and not self.pause_event.is_set():
            self.pause_event.set()
            self.status.set("Pausing after the current page...")
            self._write_log("Pausing after the current page...")

    def _poll(self) -> None:
        try:
            while True:
                kind, value = self.queue.get_nowait()
                if kind == "log":
                    self.status.set(str(value))
                    self._write_log(str(value))
                elif kind == "verification":
                    self.status.set("Human verification required — see browser window.")
                    self._write_log(str(value))
                    messagebox.showinfo(
                        "Website verification needed",
                        str(value),
                        parent=self.root,
                    )
                elif kind == "done":
                    self.busy = False
                    self.status.set("Website mirror saved.")
                    self._write_log(f"Finished: {value.url}")
                    self.refresh(select_key=value.key)
                elif kind == "paused":
                    self.busy = False
                    self.status.set("Mirror paused. Mirror the same address again to resume.")
                    self._write_log(f"Paused: {value}")
                    self._controls()
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
        if self.pause_event is not None:
            self.pause_event.set()
        self.server.stop()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    WebMirrorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
