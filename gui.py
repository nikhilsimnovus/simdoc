"""
One-click GUI for exporting the Simnovator User Handbook Confluence space
to a single PDF. Enter the release version, click "Generate PDF".

Run with:  pythonw gui.py   (or double-click "Handbook PDF.bat")
"""

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from exporter import ConfluenceExporter, load_config, save_config


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Handbook PDF Exporter")
        self.resizable(True, True)
        self.minsize(560, 480)
        self.cfg = load_config()
        self.logq = queue.Queue()
        self.worker = None
        self._build()
        self.after(150, self._drain_log)

    def _build(self):
        pad = {"padx": 8, "pady": 3}
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=10, pady=10)
        frm.columnconfigure(1, weight=1)

        def row(r, label, value, show=None):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", **pad)
            var = tk.StringVar(value=value)
            ent = ttk.Entry(frm, textvariable=var, show=show)
            ent.grid(row=r, column=1, columnspan=2, sticky="ew", **pad)
            return var

        self.v_site = row(0, "Confluence site", self.cfg["site"])
        self.v_root = row(1, "Root page (URL/id)", self.cfg["root_page"])
        self.v_email = row(2, "Atlassian email", self.cfg["email"])
        self.v_token = row(3, "API token", self.cfg["api_token"], show="*")

        ttk.Label(frm, text="Output folder").grid(row=4, column=0, sticky="w", **pad)
        self.v_out = tk.StringVar(value=self.cfg["output_dir"])
        ttk.Entry(frm, textvariable=self.v_out).grid(row=4, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="...", width=3, command=self._pick_dir).grid(row=4, column=2, **pad)

        ttk.Separator(frm).grid(row=5, column=0, columnspan=3, sticky="ew", pady=6)

        ttk.Label(frm, text="Version", font=("Segoe UI", 10, "bold")).grid(
            row=6, column=0, sticky="w", **pad)
        self.v_version = tk.StringVar()
        ent = ttk.Entry(frm, textvariable=self.v_version, font=("Segoe UI", 10))
        ent.grid(row=6, column=1, columnspan=2, sticky="ew", **pad)
        ent.focus_set()

        self.v_drafts = tk.BooleanVar(value=bool(self.cfg.get("include_drafts")))
        ttk.Checkbutton(frm, text="Include draft pages", variable=self.v_drafts).grid(
            row=7, column=1, sticky="w", **pad)

        self.btn = ttk.Button(frm, text="Generate PDF", command=self._start)
        self.btn.grid(row=8, column=0, columnspan=3, sticky="ew", padx=8, pady=8)

        self.prog = ttk.Progressbar(frm, mode="indeterminate")
        self.prog.grid(row=9, column=0, columnspan=3, sticky="ew", padx=8)

        self.log = tk.Text(frm, height=12, state="disabled", font=("Consolas", 9))
        self.log.grid(row=10, column=0, columnspan=3, sticky="nsew", padx=8, pady=8)
        frm.rowconfigure(10, weight=1)

    def _pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.v_out.get())
        if d:
            self.v_out.set(d)

    def _logline(self, msg):
        self.logq.put(str(msg))

    def _drain_log(self):
        try:
            while True:
                msg = self.logq.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", msg + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._drain_log)

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        version = self.v_version.get().strip()
        if not version:
            messagebox.showwarning("Version required",
                                   "Enter the release version to stamp on the PDF.")
            return
        if not self.v_email.get().strip() or not self.v_token.get().strip():
            messagebox.showwarning(
                "Credentials required",
                "Enter your Atlassian email and an API token.\n"
                "Create a token at id.atlassian.com > Security > API tokens.")
            return
        self.cfg.update({
            "site": self.v_site.get().strip(),
            "root_page": self.v_root.get().strip(),
            "email": self.v_email.get().strip(),
            "api_token": self.v_token.get().strip(),
            "output_dir": self.v_out.get().strip(),
            "include_drafts": bool(self.v_drafts.get()),
        })
        save_config(self.cfg)
        self.btn.configure(state="disabled")
        self.prog.start(12)
        self.worker = threading.Thread(target=self._run, args=(version,), daemon=True)
        self.worker.start()

    def _run(self, version):
        try:
            exp = ConfluenceExporter(
                self.cfg["site"], self.cfg["email"], self.cfg["api_token"],
                self.cfg["root_page"], version, self.cfg["output_dir"],
                include_drafts=self.cfg["include_drafts"],
                keep_html=self.cfg.get("keep_html", False),
                log=self._logline)
            pdf = exp.run()
            self.after(0, lambda: self._done(pdf))
        except Exception as e:
            self._logline(f"ERROR: {e}")
            self.after(0, lambda: self._done(None, str(e)))

    def _done(self, pdf, err=None):
        self.prog.stop()
        self.btn.configure(state="normal")
        if pdf:
            import os
            if messagebox.askyesno("Done", f"PDF created:\n{pdf}\n\nOpen it now?"):
                os.startfile(pdf)
        elif err:
            messagebox.showerror("Export failed", err)


if __name__ == "__main__":
    App().mainloop()
