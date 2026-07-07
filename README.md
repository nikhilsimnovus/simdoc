# SimDoc — Confluence Handbook → single PDF

One-click export of the **Simnovator User Handbook** Confluence space
(https://simnovus.atlassian.net/wiki/x/QQHPJQ) into a **single PDF** — cover
page with a version stamp, clickable table of contents, all pages in tree
order, images embedded, and links preserved (links between handbook pages
become in-document jumps; everything else stays a normal hyperlink).

Two front-ends over the same engine ([exporter.py](exporter.py)):

| | |
|---|---|
| **Web UI** ([ui/app.py](ui/app.py)) | Flask service on port **7000** — version field, Generate button, live log, PDF download list, Settings, and an **Update** button that pulls the latest build from this repo and reinstalls itself (oneclick-style). |
| **Desktop GUI** ([gui.py](gui.py)) | Windows tkinter app — double-click `Handbook PDF.bat`. |

## Server install (Ubuntu/Debian or RHEL family)

```bash
curl -fsSL https://github.com/nikhilsimnovus/simdoc/archive/refs/heads/main.tar.gz | tar xz
cd simdoc-main
sudo bash scripts/install.sh          # SIMDOC_PORT=7000 by default
```

The installer is idempotent. It creates the `simdoc` service user, installs
to `/opt/simdoc`, builds a venv (Flask + requests + Playwright Chromium for
PDF rendering), starts the `simdoc` systemd service, and plants the
self-update helper (`/usr/local/sbin/simdoc-update` + a narrow sudoers
entry) that backs the UI's Update button.

Then open `http://<host>:7000/`, expand **Settings**, enter your Atlassian
email + API token (create one at id.atlassian.com › Security › API tokens),
type the release version, and click **Generate PDF**.

- Config: `/var/lib/simdoc/config.json` (server-side only, mode 600)
- PDFs: `/var/lib/simdoc/pdfs/`
- Logs: `journalctl -u simdoc -f`

## Update flow

Click **Update** in the topbar → the service downloads
`main.tar.gz` from this repo and re-runs `scripts/install.sh`, which
restarts the service. **Bump `VERSION` in `ui/app.py` on every push** so the
new number in the topbar confirms the update applied.

## Windows desktop use

Python 3.12 + `pip install requests`; Microsoft Edge renders the PDF.
Run `Handbook PDF.bat` or `python gui.py`. Credentials are saved to a local
`config.json` next to the scripts.

## CLI

```bash
python exporter.py --version 4.0.0     # uses config.json
python exporter.py --selftest          # offline pipeline check, no credentials
```

## Notes

- The root page is configurable (page URL, tiny `/wiki/x/` link, or numeric
  id), so the tool can export any Confluence space, not just the handbook.
- Draft pages are skipped by default (checkbox to include).
- `keep_html: true` in config.json keeps the intermediate HTML for debugging.
