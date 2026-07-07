"""
Confluence space -> single PDF exporter.

Fetches a root Confluence page and all its descendants (Cloud REST API v2),
stitches them into one HTML document (cover page + table of contents +
content, with internal links rewritten to in-document anchors and images
embedded as data URIs), then prints it to PDF with a headless Chromium-family
browser (Playwright Chromium on servers, Microsoft Edge on Windows desktops).

Used by gui.py; can also be run standalone:
    python exporter.py --selftest          # offline pipeline check, no credentials
    python exporter.py --version 4.0.0     # full export using config.json
"""

import argparse
import base64
import html as html_mod
import json
import mimetypes
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("SIMDOC_CONFIG", SCRIPT_DIR / "config.json"))

DEFAULT_CONFIG = {
    "site": "simnovus.atlassian.net",
    "root_page": "https://simnovus.atlassian.net/wiki/x/QQHPJQ",
    "email": "",
    "api_token": "",
    "output_dir": str(Path.home() / "Documents"),
    "include_drafts": False,
    "keep_html": False,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)  # holds the Confluence API token
    except OSError:
        pass


def tiny_to_page_id(code):
    """Decode a Confluence /wiki/x/<code> tiny link to a numeric page id."""
    s = code.replace("-", "/").replace("_", "+")
    s = s + "A" * (11 - len(s)) + "="
    return struct.unpack("<Q", base64.b64decode(s))[0]


def parse_root_page(text, site):
    """Accept a bare page id, a full page URL, or a tiny /wiki/x/ URL."""
    text = text.strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    m = re.search(r"/pages/(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"[?&]pageId=(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"/wiki/x/([A-Za-z0-9_-]+)", text)
    if m:
        return tiny_to_page_id(m.group(1))
    raise ValueError(f"Cannot extract a page id from: {text!r}")


@dataclass
class PageNode:
    id: str
    title: str
    depth: int
    status: str = "current"
    html: str = ""
    children: list = field(default_factory=list)


class ConfluenceExporter:
    def __init__(self, site, email, api_token, root_page, version,
                 output_dir, include_drafts=False, keep_html=False, log=print):
        self.site = site.strip().rstrip("/").replace("https://", "").replace("http://", "")
        self.base = f"https://{self.site}"
        self.version = version.strip()
        self.output_dir = Path(output_dir)
        self.include_drafts = include_drafts
        self.keep_html = keep_html
        self.log = log
        self.root_id = str(parse_root_page(root_page, self.site))
        self.session = requests.Session()
        self.session.auth = (email.strip(), api_token.strip())
        self.session.headers["Accept"] = "application/json"
        self._img_cache = {}
        self._att_links = {}
        self.page_ids = set()

    # ---------- REST fetching ----------

    def _get(self, url, **kw):
        r = self.session.get(url, timeout=60, **kw)
        if r.status_code == 401 or r.status_code == 403:
            raise RuntimeError(
                f"Confluence rejected the credentials (HTTP {r.status_code}). "
                "Check the email and API token.")
        r.raise_for_status()
        return r

    def fetch_page(self, page_id):
        url = f"{self.base}/wiki/api/v2/pages/{page_id}"
        data = self._get(url, params={"body-format": "export_view"}).json()
        return data

    def fetch_children(self, page_id):
        out = []
        url = f"{self.base}/wiki/api/v2/pages/{page_id}/children"
        params = {"limit": 250}
        while True:
            data = self._get(url, params=params).json()
            out.extend(data.get("results", []))
            nxt = (data.get("_links") or {}).get("next")
            if not nxt:
                break
            url = self.base + nxt
            params = None
        out.sort(key=lambda c: c.get("childPosition") or 0)
        return out

    def build_tree(self):
        root_data = self.fetch_page(self.root_id)
        root = PageNode(id=str(root_data["id"]), title=root_data["title"], depth=0,
                        status=root_data.get("status", "current"),
                        html=root_data.get("body", {}).get("export_view", {}).get("value", ""))
        self.log(f"Root page: {root.title}")
        flat = [root]
        self.page_ids.add(root.id)

        def walk(node):
            for child in self.fetch_children(node.id):
                status = child.get("status", "current")
                if status != "current" and not self.include_drafts:
                    self.log(f"  skipping ({status}): {child.get('title')}")
                    continue
                cnode = PageNode(id=str(child["id"]), title=child.get("title", "(untitled)"),
                                 depth=node.depth + 1, status=status)
                node.children.append(cnode)
                flat.append(cnode)
                self.page_ids.add(cnode.id)
                walk(cnode)

        walk(root)
        self.log(f"Found {len(flat)} pages to export.")
        # fetch bodies
        for i, node in enumerate(flat):
            if not node.html:
                self.log(f"  [{i + 1}/{len(flat)}] fetching: {node.title}")
                data = self.fetch_page(node.id)
                node.html = data.get("body", {}).get("export_view", {}).get("value", "")
        return root, flat

    # ---------- HTML processing ----------

    def _abs_url(self, url):
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return self.base + url
        return url

    def _page_id_from_href(self, href):
        m = re.search(r"/pages/(\d+)", href)
        if m:
            return m.group(1)
        m = re.search(r"[?&]pageId=(\d+)", href)
        if m:
            return m.group(1)
        m = re.search(r"/wiki/x/([A-Za-z0-9_-]+)", href)
        if m:
            try:
                return str(tiny_to_page_id(m.group(1)))
            except Exception:
                return None
        return None

    def _rewrite_href(self, href):
        href = html_mod.unescape(href)
        if href.startswith(("#", "mailto:", "data:")):
            return href
        pid = self._page_id_from_href(href)
        if pid and pid in self.page_ids:
            return f"#p{pid}"
        return self._abs_url(href)

    def _attachment_link(self, page_id, filename):
        """Resolve an attachment filename to its REST downloadLink.

        Confluence Cloud returns 401 for API-token auth on the raw
        /wiki/download/attachments/... URLs that export_view embeds, but the
        /rest/api/content/.../attachment/.../download link from the v2
        attachments API serves the binary fine.
        """
        if page_id not in self._att_links:
            links = {}
            try:
                url = f"{self.base}/wiki/api/v2/pages/{page_id}/attachments"
                params = {"limit": 250}
                while True:
                    data = self._get(url, params=params).json()
                    for att in data.get("results", []):
                        if att.get("downloadLink"):
                            links[att.get("title") or ""] = att["downloadLink"]
                    nxt = (data.get("_links") or {}).get("next")
                    if not nxt:
                        break
                    url = self.base + nxt
                    params = None
            except Exception as e:
                self.log(f"    attachment list failed for page {page_id}: {e}")
            self._att_links[page_id] = links
        dl = self._att_links[page_id].get(filename)
        if not dl:
            return None
        return dl if dl.startswith("/wiki") else "/wiki" + dl

    def _fetch_image(self, url):
        url = html_mod.unescape(url)
        if url.startswith("data:"):
            return url
        full = self._abs_url(url)
        if full in self._img_cache:
            return self._img_cache[full]
        # attachment/thumbnail URLs must go through the REST download link
        m = re.search(r"/wiki/download/(?:attachments|thumbnails)/(\d+)/([^?#]+)", full)
        if m and full.startswith(self.base):
            dl = self._attachment_link(m.group(1), urllib.parse.unquote(m.group(2)))
            if dl:
                fetch_url = self.base + dl
            else:
                fetch_url = full
        else:
            fetch_url = full
        try:
            # only send our basic-auth credentials to the Confluence site itself
            if fetch_url.startswith(self.base):
                r = self.session.get(fetch_url, timeout=60)
            else:
                r = requests.get(fetch_url, timeout=60)
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "").split(";")[0].strip()
            if not ctype or "html" in ctype:
                ctype = mimetypes.guess_type(full.split("?")[0])[0] or "image/png"
            data_uri = f"data:{ctype};base64,{base64.b64encode(r.content).decode()}"
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", "")
            self.log(f"    image failed ({e.__class__.__name__} {status}): {full[:100]}")
            data_uri = full  # leave the original URL in place
        self._img_cache[full] = data_uri
        return data_uri

    def process_body(self, html):
        html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S | re.I)
        html = re.sub(r'\s(?:srcset|data-image-src|loading)="[^"]*"', "", html)
        html = re.sub(r'href="([^"]*)"', lambda m: f'href="{self._rewrite_href(m.group(1))}"', html)
        html = re.sub(r'src="([^"]*)"', lambda m: f'src="{self._fetch_image(m.group(1))}"', html)
        return html

    # ---------- document assembly ----------

    CSS = """
    @page { size: A4; margin: 18mm 15mm; }
    body { font-family: "Segoe UI", Arial, sans-serif; font-size: 10.5pt;
           color: #172b4d; line-height: 1.5; }
    a { color: #0052cc; text-decoration: none; }
    h1, h2, h3, h4 { color: #172b4d; line-height: 1.25; }
    .cover { text-align: center; padding-top: 220px; page-break-after: always; }
    .cover h1 { font-size: 30pt; margin-bottom: 8px; }
    .cover .version { font-size: 16pt; color: #0052cc; margin: 18px 0 6px; }
    .cover .meta { color: #6b778c; font-size: 11pt; }
    .toc { page-break-after: always; }
    .toc h1 { border-bottom: 2px solid #0052cc; padding-bottom: 6px; }
    .toc ol { list-style: none; padding-left: 0; }
    .toc li { margin: 3px 0; }
    .toc .d1 { padding-left: 0; font-weight: 600; }
    .toc .d2 { padding-left: 18px; }
    .toc .d3 { padding-left: 36px; }
    .toc .d4 { padding-left: 54px; }
    .toc .d5 { padding-left: 72px; }
    .page-section { page-break-before: always; }
    .page-section > h1.pg-title { border-bottom: 2px solid #dfe1e6;
        padding-bottom: 6px; font-size: 18pt; }
    img { max-width: 100%; height: auto; }
    table { border-collapse: collapse; margin: 8px 0; max-width: 100%; }
    th, td { border: 1px solid #c1c7d0; padding: 4px 8px; vertical-align: top; }
    th { background: #f4f5f7; }
    pre, code { font-family: Consolas, monospace; font-size: 9.5pt;
        background: #f4f5f7; }
    pre { border: 1px solid #dfe1e6; border-radius: 3px; padding: 8px;
        white-space: pre-wrap; word-wrap: break-word; }
    .confluence-information-macro { border: 1px solid #dfe1e6; border-left: 4px solid #0052cc;
        border-radius: 3px; padding: 8px 12px; margin: 10px 0; background: #f7f9fc; }
    .confluence-information-macro-warning { border-left-color: #de350b; }
    .confluence-information-macro-note { border-left-color: #ffab00; }
    .confluence-information-macro-tip { border-left-color: #36b37e; }
    .confluence-information-macro-icon { display: none; }
    .expand-container { border: 1px solid #dfe1e6; border-radius: 3px;
        padding: 6px 10px; margin: 8px 0; }
    """

    def _toc_html(self, root):
        items = []

        def add(node):
            for c in node.children:
                d = min(c.depth, 5)
                items.append(
                    f'<li class="d{d}"><a href="#p{c.id}">{html_mod.escape(c.title)}</a></li>')
                add(c)

        add(root)
        return "<div class='toc'><h1>Table of Contents</h1><ol>" + "".join(items) + "</ol></div>"

    def assemble(self, root, flat):
        title = html_mod.escape(root.title)
        version = html_mod.escape(self.version)
        date = time.strftime("%d %B %Y")
        parts = [
            f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<title>{title} – {version}</title><style>{self.CSS}</style></head><body>",
            f"<div class='cover'><h1>{title}</h1>"
            f"<div class='version'>Version {version}</div>"
            f"<div class='meta'>Generated on {date}</div></div>",
            self._toc_html(root),
        ]
        for i, node in enumerate(flat):
            self.log(f"  processing content [{i + 1}/{len(flat)}]: {node.title}")
            body = self.process_body(node.html)
            parts.append(
                f"<div class='page-section' id='p{node.id}'>"
                f"<h1 class='pg-title'>{html_mod.escape(node.title)}</h1>{body}</div>")
        parts.append("</body></html>")
        return "".join(parts)

    # ---------- PDF ----------

    @staticmethod
    def find_browser():
        """Locate a Chromium-family browser for --print-to-pdf rendering."""
        import shutil
        candidates = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ] if os.name == "nt" else [
            "/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium",
        ]
        for p in candidates:
            if os.path.exists(p):
                return p
        for name in ("msedge", "google-chrome", "chromium-browser", "chromium"):
            p = shutil.which(name)
            if p:
                return p
        return None

    def html_to_pdf(self, html_path, pdf_path):
        # Preferred on servers: Playwright's bundled Chromium (installed by
        # scripts/install.sh). Falls back to a system Edge/Chrome/Chromium.
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            sync_playwright = None
        if sync_playwright:
            with sync_playwright() as p:
                browser = p.chromium.launch(args=["--no-sandbox"])
                page = browser.new_page()
                page.goto(Path(html_path).resolve().as_uri(), wait_until="load")
                page.pdf(path=str(pdf_path), prefer_css_page_size=True,
                         print_background=True)
                browser.close()
            return
        browser = self.find_browser()
        if not browser:
            raise RuntimeError(
                "No PDF renderer found: install playwright (pip install playwright && "
                "playwright install chromium) or Microsoft Edge / Google Chrome.")
        with tempfile.TemporaryDirectory(prefix="simdoc_profile_") as profile:
            cmd = [
                browser, "--headless", "--disable-gpu", "--no-first-run",
                f"--user-data-dir={profile}", "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}", Path(html_path).resolve().as_uri(),
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if not Path(pdf_path).exists():
                raise RuntimeError(f"Browser did not produce a PDF.\n{res.stderr[-2000:]}")

    # ---------- entry point ----------

    def run(self):
        if not self.version:
            raise ValueError("Version is required.")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        root, flat = self.build_tree()
        doc = self.assemble(root, flat)

        safe_title = re.sub(r"[^\w\- ]", "", root.title).strip().replace(" ", "_")
        safe_ver = re.sub(r"[^\w.\-]", "_", self.version)
        pdf_path = self.output_dir / f"{safe_title}_v{safe_ver}.pdf"
        html_path = self.output_dir / f"{safe_title}_v{safe_ver}.html"

        html_path.write_text(doc, encoding="utf-8")
        self.log("Rendering PDF with headless Chromium ...")
        self.html_to_pdf(html_path, pdf_path)
        if not self.keep_html:
            html_path.unlink(missing_ok=True)
        size_mb = pdf_path.stat().st_size / (1024 * 1024)
        self.log(f"Done: {pdf_path}  ({size_mb:.1f} MB)")
        return pdf_path


# ---------- offline selftest ----------

def selftest():
    """Build a tiny fake document and verify Edge produces a PDF with links."""
    out = Path(tempfile.mkdtemp(prefix="hbpdf_selftest_"))
    exp = ConfluenceExporter("example.atlassian.net", "x@example.com", "token",
                             "123", "0.0-selftest", out, log=print)
    root = PageNode(id="1", title="Selftest Handbook", depth=0,
                    html="<p>Root page. See <a href='#p2'>chapter</a> and "
                         "<a href='https://example.com'>external</a>.</p>")
    child = PageNode(id="2", title="Chapter One", depth=1,
                     html="<p>Hello <b>world</b>.</p><table><tr><th>k</th><td>v</td></tr></table>")
    root.children = [child]
    exp.page_ids = {"1", "2"}
    doc = exp.assemble(root, [root, child])
    html_path = out / "selftest.html"
    pdf_path = out / "selftest.pdf"
    html_path.write_text(doc, encoding="utf-8")
    exp.html_to_pdf(html_path, pdf_path)
    raw = pdf_path.read_bytes()
    n_uri = raw.count(b"/URI")
    n_link = raw.count(b"/Link")
    print(f"PDF created: {pdf_path} ({len(raw)} bytes)")
    print(f"link annotations: /Link x{n_link}, /URI x{n_uri}")
    ok = pdf_path.stat().st_size > 1000 and n_link >= 1
    print("SELFTEST", "PASS" if ok else "FAIL (links may not be preserved)")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="Export a Confluence space to a single PDF")
    ap.add_argument("--selftest", action="store_true", help="offline pipeline check")
    ap.add_argument("--version", help="version string for the cover page / filename")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    cfg = load_config()
    if not args.version:
        ap.error("--version is required (or use the GUI)")
    exp = ConfluenceExporter(cfg["site"], cfg["email"], cfg["api_token"],
                             cfg["root_page"], args.version, cfg["output_dir"],
                             include_drafts=cfg.get("include_drafts", False),
                             keep_html=cfg.get("keep_html", False))
    exp.run()


if __name__ == "__main__":
    main()
