#!/usr/bin/env python3
"""SimDoc UI — thin Flask wrapper around exporter.py.

Single-page UI:
  - GET  /                  -> version form + live log + PDF list + settings
  - POST /run               -> kick off an export (one at a time)
  - GET  /jobs/<id>/stream  -> SSE stream of exporter log lines
  - GET  /api/pdfs          -> JSON list of generated PDFs
  - GET  /pdfs/<name>       -> download a PDF
  - GET/POST /api/settings  -> Confluence connection settings (config.json)
  - POST /api/update        -> self-update from GitHub (oneclick-style)

Deployed by scripts/install.sh as the `simdoc` systemd service on port 7000.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, render_template_string,
                   request, send_file)

# SimDoc UI version. Bump on every push to the simdoc repo so users can
# confirm the Update button actually applied — the new number shows up in
# the topbar after the page reloads.
VERSION = "1.0.1"

UI_DIR = Path(__file__).resolve().parent
# exporter.py lives next to app.py when installed (/opt/simdoc), one level
# up in the git checkout.
for cand in (UI_DIR, UI_DIR.parent):
    if (cand / "exporter.py").exists():
        sys.path.insert(0, str(cand))
        break
from exporter import ConfluenceExporter, load_config, save_config  # noqa: E402

PORT = int(os.environ.get("SIMDOC_PORT", "7000"))
OUTPUT_DIR = Path(os.environ.get("SIMDOC_OUTPUT", "/var/lib/simdoc/pdfs"))
UPDATE_REPO_URL = os.environ.get(
    "SIMDOC_UPDATE_TARBALL",
    "https://github.com/nikhilsimnovus/simdoc/archive/refs/heads/main.tar.gz",
)

app = Flask(__name__)
JOBS: dict = {}
LOCK = threading.Lock()


@app.after_request
def _no_cache_html(resp):
    # Inline HTML+JS ships in this one response; stale cache after an
    # Update would run old JS against the new backend.
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if ctype.startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


@app.route("/static/logo.svg")
def static_logo():
    p = UI_DIR / "logo_light.svg"
    if not p.exists():
        p = UI_DIR / "logo_dark.svg"
    if not p.exists():
        abort(404)
    return send_file(str(p), mimetype="image/svg+xml")


@app.route("/favicon.png")
def static_favicon():
    p = UI_DIR / "favicon.png"
    if not p.exists():
        abort(404)
    return send_file(str(p), mimetype="image/png")


# ---------------------------------------------------------------------------
# Settings (Confluence connection). The API token never leaves the server:
# GET reports only whether one is stored; POST with an empty token keeps it.
# ---------------------------------------------------------------------------

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    cfg = load_config()
    return jsonify({
        "site": cfg.get("site", ""),
        "root_page": cfg.get("root_page", ""),
        "email": cfg.get("email", ""),
        "has_token": bool(cfg.get("api_token")),
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    data = request.get_json(force=True, silent=True) or {}
    cfg = load_config()
    for key in ("site", "root_page", "email"):
        if key in data:
            cfg[key] = str(data[key]).strip()
    if data.get("api_token"):
        cfg["api_token"] = str(data["api_token"]).strip()
    save_config(cfg)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Export jobs — one at a time, log streamed over SSE.
# ---------------------------------------------------------------------------

def _run_job(job, version, include_drafts):
    log = job["log"]

    def emit(msg):
        log.append(str(msg))

    try:
        cfg = load_config()
        exp = ConfluenceExporter(
            cfg["site"], cfg["email"], cfg["api_token"], cfg["root_page"],
            version, OUTPUT_DIR, include_drafts=include_drafts,
            keep_html=cfg.get("keep_html", False), log=emit)
        pdf = exp.run()
        job["pdf"] = pdf.name
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        emit(f"ERROR: {exc}")
        job["status"] = "failed"
    finally:
        log.append(None)  # sentinel: stream over


@app.route("/run", methods=["POST"])
def run():
    data = request.get_json(force=True, silent=True) or {}
    version = str(data.get("version", "")).strip()
    if not version:
        return jsonify({"ok": False, "error": "Version is required."}), 400
    cfg = load_config()
    if not cfg.get("email") or not cfg.get("api_token"):
        return jsonify({"ok": False,
                        "error": "Confluence email/API token not set — open Settings."}), 400
    with LOCK:
        if any(j["status"] == "running" for j in JOBS.values()):
            return jsonify({"ok": False, "error": "An export is already running."}), 409
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "status": "running", "log": deque(),
               "pdf": None, "started": time.time()}
        JOBS[job_id] = job
    t = threading.Thread(target=_run_job,
                         args=(job, version, bool(data.get("include_drafts"))),
                         daemon=True)
    t.start()
    return jsonify({"ok": True, "job": job_id})


@app.route("/jobs/<job_id>/stream")
def stream(job_id):
    job = JOBS.get(job_id)
    if not job:
        abort(404)

    def gen():
        idx = 0
        while True:
            log = job["log"]
            while idx < len(log):
                line = log[idx]
                idx += 1
                if line is None:
                    yield ("event: done\ndata: " +
                           json.dumps({"status": job["status"], "pdf": job["pdf"]}) +
                           "\n\n")
                    return
                yield "data: " + json.dumps(line) + "\n\n"
            time.sleep(0.4)

    return Response(gen(), mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Generated PDFs
# ---------------------------------------------------------------------------

@app.route("/api/pdfs")
def api_pdfs():
    out = []
    if OUTPUT_DIR.is_dir():
        for p in OUTPUT_DIR.glob("*.pdf"):
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify(out)


@app.route("/pdfs/<path:name>")
def download_pdf(name):
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".pdf"):
        abort(400)
    p = OUTPUT_DIR / name
    if not p.is_file():
        abort(404)
    return send_file(str(p), as_attachment=True)


@app.route("/api/pdfs/<path:name>", methods=["DELETE"])
def delete_pdf(name):
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".pdf"):
        abort(400)
    p = OUTPUT_DIR / name
    if p.is_file():
        p.unlink()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Self-update from GitHub (same layout as oneclick): the Update button hits
# /api/update which runs /usr/local/sbin/simdoc-update via sudo -n. That
# wrapper (planted by install.sh, with a matching narrow sudoers entry)
# downloads main.tar.gz from the simdoc repo and re-runs scripts/install.sh.
# systemctl restart happens INSIDE install.sh, so this response may cut off
# mid-stream — the client treats that as expected and reloads shortly after.
# ---------------------------------------------------------------------------

@app.route("/api/update", methods=["POST"])
def api_update():
    updater = "/usr/local/sbin/simdoc-update"
    if not Path(updater).exists():
        return jsonify({
            "ok": False,
            "log": (f"[update] {updater} missing — run scripts/install.sh once "
                    f"locally to plant the wrapper + sudoers entry."),
        }), 500
    try:
        rc = subprocess.run(
            ["sudo", "-n", updater],
            capture_output=True, text=True, timeout=600,
            env={**os.environ, "SIMDOC_UPDATE_TARBALL": UPDATE_REPO_URL},
        )
        out = (rc.stdout or "")[-4000:]
        if rc.stderr:
            out += "\n--- stderr ---\n" + rc.stderr[-2000:]
        if rc.returncode != 0:
            return jsonify({"ok": False, "log": out + f"\n[update] exited {rc.returncode}"}), 500
        return jsonify({"ok": True, "log": out + "\n[update] done"})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "log": "[update] timed out after 600s"}), 504
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "log": f"[update] FAILED: {exc}"}), 500


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SimDoc — Handbook PDF</title>
<link rel="icon" href="/favicon.png">
<style>
:root{--navy:#0b1f3a;--navy2:#132c4f;--orange:#f97316;--ink:#0f172a;--mut:#64748b;
--line:#e2e8f0;--bg:#f1f5f9;--ok:#16a34a;--bad:#dc2626}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 ui-sans-serif,system-ui,"Segoe UI",sans-serif}
.topbar{background:var(--navy);border-bottom:3px solid var(--orange);color:#fff;
display:flex;align-items:center;gap:10px;padding:10px 18px}
.topbar img{height:26px}
.topbar .title{font-size:17px;font-weight:600;letter-spacing:.3px}
.brand-sub{color:#94a3b8;font-size:12px;margin-top:3px}
.version-pill{background:rgba(249,115,22,.2);border:1px solid rgba(249,115,22,.6);
color:#fed7aa;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:6px}
.spacer{flex:1}
.update-btn{display:inline-flex;align-items:center;gap:5px;background:rgba(34,197,94,.18);
border:1px solid rgba(34,197,94,.5);color:#dcfce7;font:500 11.5px ui-sans-serif,sans-serif;
padding:4px 10px;border-radius:6px;cursor:pointer;transition:background .15s}
.update-btn:hover{background:rgba(34,197,94,.32);color:#fff}
.update-btn:disabled{cursor:wait;opacity:.7}
.update-btn.spin .update-icon{animation:uspin .9s linear infinite;display:inline-block}
@keyframes uspin{to{transform:rotate(360deg)}}
.wrap{max-width:860px;margin:22px auto;padding:0 16px}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;
padding:18px 20px;margin-bottom:18px;box-shadow:0 1px 2px rgba(15,23,42,.05)}
.card h2{margin:0 0 12px;font-size:15px;color:var(--navy)}
label{display:block;font-size:12px;color:var(--mut);margin:10px 0 3px}
input[type=text],input[type=password]{width:100%;padding:8px 10px;border:1px solid var(--line);
border-radius:6px;font:inherit}
input:focus{outline:2px solid rgba(249,115,22,.35);border-color:var(--orange)}
.row{display:flex;gap:14px;align-items:end}
.row>div{flex:1}
.gen-btn{background:var(--orange);color:#fff;border:0;border-radius:8px;
font:600 15px ui-sans-serif,sans-serif;padding:11px 26px;cursor:pointer;white-space:nowrap}
.gen-btn:hover{filter:brightness(1.08)}
.gen-btn:disabled{background:#cbd5e1;cursor:wait}
.chk{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--mut);margin-top:10px}
#log{background:#0b1220;color:#cbd5e1;font:12px/1.55 Consolas,ui-monospace,monospace;
border-radius:8px;padding:12px 14px;height:260px;overflow-y:auto;white-space:pre-wrap;
display:none;margin-top:14px}
#log .err{color:#fca5a5}
.status{font-size:13px;margin-top:10px}
.status.ok{color:var(--ok)} .status.bad{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500;font-size:12px}
td a{color:var(--navy);font-weight:600;text-decoration:none}
td a:hover{color:var(--orange)}
.del{color:var(--bad);cursor:pointer;font-size:12px;background:none;border:0}
details summary{cursor:pointer;color:var(--navy);font-weight:600;font-size:14px}
.hint{font-size:12px;color:var(--mut)}
.save-btn{background:var(--navy);color:#fff;border:0;border-radius:6px;
padding:8px 18px;font:500 13px ui-sans-serif,sans-serif;cursor:pointer;margin-top:12px}
</style>
</head>
<body>
<div class="topbar">
  <img src="/static/logo.svg" alt="Simnovus">
  <span class="title">SimDoc</span><span class="version-pill"
    title="SimDoc UI version — bumps when you click Update and pick up a new build">v{{ version }}</span>
  <span class="brand-sub">handbook&nbsp;→&nbsp;PDF</span>
  <span class="spacer"></span>
  <button type="button" class="update-btn" id="update-btn" onclick="runUpdate()"
    title="Pull the latest SimDoc build from GitHub and restart">
    <span class="update-icon" id="update-icon">⤓</span>
    <span id="update-label">Update</span>
  </button>
</div>

<div class="wrap">
  <div class="card">
    <h2>Generate Handbook PDF</h2>
    <div class="row">
      <div>
        <label for="ver">Release version (stamped on the cover page + filename)</label>
        <input type="text" id="ver" placeholder="e.g. 4.0.0" autocomplete="off">
      </div>
      <button class="gen-btn" id="gen" onclick="runExport()">Generate&nbsp;PDF</button>
    </div>
    <label class="chk"><input type="checkbox" id="drafts"> Include draft pages</label>
    <div class="status" id="status"></div>
    <div id="log"></div>
  </div>

  <div class="card">
    <h2>Generated PDFs</h2>
    <table id="pdfs"><thead>
      <tr><th>File</th><th>Size</th><th>Created</th><th></th></tr>
    </thead><tbody></tbody></table>
    <div class="hint" id="nopdfs" style="display:none">No PDFs yet.</div>
  </div>

  <div class="card">
    <details id="settings-box">
      <summary>Settings — Confluence connection</summary>
      <label for="s-site">Confluence site</label>
      <input type="text" id="s-site">
      <label for="s-root">Root page (URL, tiny link, or page id)</label>
      <input type="text" id="s-root">
      <label for="s-email">Atlassian email</label>
      <input type="text" id="s-email">
      <label for="s-token">API token <span class="hint" id="tok-hint"></span></label>
      <input type="password" id="s-token" placeholder="paste a new token to replace"
        autocomplete="new-password">
      <div class="hint">Create a token at id.atlassian.com &rsaquo; Security &rsaquo; API tokens.
        Stored server-side in config.json (never sent back to the browser).</div>
      <button class="save-btn" onclick="saveSettings()">Save settings</button>
      <span class="status" id="s-status"></span>
    </details>
  </div>
</div>

<script>
function fmtSize(b){return b>1048576?(b/1048576).toFixed(1)+' MB':(b/1024).toFixed(0)+' KB';}
function fmtTime(t){return new Date(t*1000).toLocaleString();}

async function loadPdfs(){
  const r = await fetch('/api/pdfs'); const list = await r.json();
  const tb = document.querySelector('#pdfs tbody'); tb.innerHTML = '';
  document.getElementById('nopdfs').style.display = list.length ? 'none' : 'block';
  for (const p of list){
    const tr = document.createElement('tr');
    tr.innerHTML = `<td><a href="/pdfs/${encodeURIComponent(p.name)}">${p.name}</a></td>
      <td>${fmtSize(p.size)}</td><td>${fmtTime(p.mtime)}</td>
      <td><button class="del" onclick="delPdf('${p.name}')">delete</button></td>`;
    tb.appendChild(tr);
  }
}
async function delPdf(name){
  if (!confirm('Delete ' + name + '?')) return;
  await fetch('/api/pdfs/' + encodeURIComponent(name), {method:'DELETE'});
  loadPdfs();
}

async function loadSettings(){
  const r = await fetch('/api/settings'); const s = await r.json();
  document.getElementById('s-site').value = s.site || '';
  document.getElementById('s-root').value = s.root_page || '';
  document.getElementById('s-email').value = s.email || '';
  document.getElementById('tok-hint').textContent =
    s.has_token ? '(a token is stored)' : '(no token stored yet)';
  if (!s.has_token || !s.email) document.getElementById('settings-box').open = true;
}
async function saveSettings(){
  const body = {
    site: document.getElementById('s-site').value,
    root_page: document.getElementById('s-root').value,
    email: document.getElementById('s-email').value,
    api_token: document.getElementById('s-token').value,
  };
  const r = await fetch('/api/settings', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const el = document.getElementById('s-status');
  el.textContent = r.ok ? 'Saved.' : 'Save failed';
  el.className = 'status ' + (r.ok ? 'ok' : 'bad');
  document.getElementById('s-token').value = '';
  loadSettings();
  setTimeout(()=>{el.textContent='';}, 3000);
}

function logLine(msg, err){
  const log = document.getElementById('log');
  log.style.display = 'block';
  const span = document.createElement('span');
  if (err) span.className = 'err';
  span.textContent = msg + '\n';
  log.appendChild(span);
  log.scrollTop = log.scrollHeight;
}

async function runExport(){
  const ver = document.getElementById('ver').value.trim();
  const status = document.getElementById('status');
  if (!ver){ status.textContent = 'Enter the release version first.';
    status.className = 'status bad'; return; }
  const btn = document.getElementById('gen');
  btn.disabled = true; status.textContent = 'Starting…'; status.className = 'status';
  document.getElementById('log').innerHTML = '';
  const r = await fetch('/run', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({version: ver,
      include_drafts: document.getElementById('drafts').checked})});
  const j = await r.json();
  if (!j.ok){ status.textContent = j.error; status.className = 'status bad';
    btn.disabled = false; return; }
  status.textContent = 'Export running…';
  const es = new EventSource('/jobs/' + j.job + '/stream');
  es.onmessage = (e) => logLine(JSON.parse(e.data));
  es.addEventListener('done', (e) => {
    es.close(); btn.disabled = false;
    const d = JSON.parse(e.data);
    if (d.status === 'done'){
      status.textContent = 'Done — ' + d.pdf;
      status.className = 'status ok';
    } else {
      status.textContent = 'Export failed — see log.';
      status.className = 'status bad';
    }
    loadPdfs();
  });
  es.onerror = () => { es.close(); btn.disabled = false; };
}

// ---- Self-update from GitHub (mirrors oneclick) ----
async function runUpdate(){
  const btn = document.getElementById('update-btn');
  const lbl = document.getElementById('update-label');
  btn.disabled = true; btn.classList.add('spin'); lbl.textContent = 'Updating…';
  logLine('[update] pulling latest build from GitHub …');
  try {
    const r = await fetch('/api/update', {method:'POST', cache:'no-store'});
    const j = await r.json();
    (j.log || '').split('\n').forEach(l => l && logLine(l, !j.ok));
    if (j.ok){ lbl.textContent = 'Restarting…';
      setTimeout(()=>location.reload(), 4000); return; }
    lbl.textContent = 'Update';
  } catch (_) {
    // service restarted under us mid-response — expected; reload picks up the new build
    logLine('[update] service restarting — reloading page …');
    setTimeout(()=>location.reload(), 5000); return;
  }
  btn.disabled = false; btn.classList.remove('spin');
}

loadSettings(); loadPdfs();
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(PAGE, version=VERSION)


if __name__ == "__main__":
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
