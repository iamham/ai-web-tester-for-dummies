"""
Local web UI for the AI Web Tester.

For non-technical users: double-click the launcher (or run `python app.py`). A
page opens in your browser where you can create test cases in plain English, run
them, watch progress, and view the screenshot report — no terminal needed.

Runs only on 127.0.0.1 (your machine). Your API key is stored locally in .env and
is never shown back in the UI or exposed on the network.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import uuid
import webbrowser
from pathlib import Path

from dotenv import set_key
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from runner import (
    TestCase,
    config_status,
    delete_test,
    load_config,
    load_test,
    load_tests,
    run_suite,
    save_test,
    valid_name,
    with_headless,
)

ENV_PATH = str(Path(__file__).parent / ".env")

# In-memory state for the single active run (one at a time).
RUN: dict | None = None
_RUN_TASK: asyncio.Task | None = None

# Maps UI setting field -> .env variable name.
SETTING_KEYS = {
    "google_api_key": "GOOGLE_API_KEY",   # write-only (never echoed back)
    "login_url": "LOGIN_URL",             # write-only (may contain a token)
    "llm_model": "LLM_MODEL",
    "base_url": "BASE_URL",
    "context_hint": "CONTEXT_HINT",
    "viewport_width": "VIEWPORT_WIDTH",
    "viewport_height": "VIEWPORT_HEIGHT",
    "device_scale": "DEVICE_SCALE",
    "user_agent": "USER_AGENT",
}


# --------------------------------------------------------------------------- #
# Test-case API
# --------------------------------------------------------------------------- #

def _tc_to_dict(tc: TestCase) -> dict:
    return {
        "name": tc.name, "title": tc.display_title(), "description": tc.description,
        "enabled": tc.enabled, "requires_login": tc.requires_login,
        "start_url": tc.start_url, "max_steps": tc.max_steps,
    }


async def api_list_tests(request: Request):
    return JSONResponse([_tc_to_dict(tc) for tc in load_tests()])


def _tc_from_payload(data: dict) -> TestCase:
    return TestCase(
        name=(data.get("name") or "").strip(),
        title=(data.get("title") or "").strip(),
        description=(data.get("description") or "").strip(),
        enabled=bool(data.get("enabled", True)),
        requires_login=bool(data.get("requires_login", False)),
        start_url=(data.get("start_url") or "").strip(),
        max_steps=int(data.get("max_steps", 20)),
    )


async def api_create_test(request: Request):
    data = await request.json()
    tc = _tc_from_payload(data)
    if not valid_name(tc.name):
        return JSONResponse({"error": "Invalid name. Use lowercase letters, numbers, '_' or '-'."}, status_code=400)
    if (Path(__file__).parent / "tests" / f"{tc.name}.yaml").exists():
        return JSONResponse({"error": f"A test named '{tc.name}' already exists."}, status_code=409)
    save_test(tc)
    return JSONResponse(_tc_to_dict(tc), status_code=201)


async def api_update_test(request: Request):
    name = request.path_params["name"]
    data = await request.json()
    data["name"] = name  # name is fixed once created
    tc = _tc_from_payload(data)
    try:
        load_test(name)
    except FileNotFoundError:
        return JSONResponse({"error": "Test not found."}, status_code=404)
    save_test(tc)
    return JSONResponse(_tc_to_dict(tc))


async def api_delete_test(request: Request):
    delete_test(request.path_params["name"])
    return PlainTextResponse("", status_code=204)


# --------------------------------------------------------------------------- #
# Run API
# --------------------------------------------------------------------------- #

async def _execute(names: list[str], headless: bool):
    global RUN
    cfg = with_headless(load_config(), headless)

    def on_event(ev: dict):
        if RUN is None:
            return
        i = ev["index"]
        if ev["type"] == "start":
            RUN["current_index"] = i
            RUN["tests"][i]["status"] = "running"
        elif ev["type"] == "step":
            RUN["tests"][i]["step"] = ev["step"]
        elif ev["type"] == "end":
            RUN["tests"][i]["status"] = "pass" if ev["passed"] else "fail"
            RUN["tests"][i]["summary"] = ev["summary"]

    try:
        await run_suite(names, cfg, on_event=on_event)
        if RUN is not None:
            RUN["status"] = "done"
            RUN["report_ready"] = True
    except Exception as exc:  # never leave the run wedged in "running"
        if RUN is not None:
            RUN["status"] = "error"
            RUN["error"] = str(exc)


async def api_run(request: Request):
    global RUN, _RUN_TASK
    if RUN is not None and RUN["status"] == "running":
        return JSONResponse({"error": "A run is already in progress."}, status_code=409)

    cfg = load_config()
    if not config_status(cfg)["has_api_key"]:
        return JSONResponse({"error": "No Gemini API key set. Add it in Settings."}, status_code=400)

    data = await request.json()
    headless = bool(data.get("headless", True))
    requested = data.get("names")

    cases = load_tests()
    by_name = {c.name: c for c in cases}
    if requested:
        selected = [by_name[n] for n in requested if n in by_name]
    else:
        selected = [c for c in cases if c.enabled]
    if not selected:
        return JSONResponse({"error": "No tests selected."}, status_code=400)

    run_id = uuid.uuid4().hex[:8]
    RUN = {
        "id": run_id, "status": "running", "headless": headless,
        "current_index": -1, "report_ready": False, "error": None,
        "tests": [
            {"name": c.display_title(), "status": "pending", "step": 0,
             "max_steps": c.max_steps, "summary": ""}
            for c in selected
        ],
    }
    _RUN_TASK = asyncio.create_task(_execute([c.name for c in selected], headless))
    return JSONResponse({"run_id": run_id}, status_code=202)


async def api_run_status(request: Request):
    if RUN is None:
        return JSONResponse({"status": "idle"})
    return JSONResponse(RUN)


async def serve_report(request: Request):
    report = Path(load_config().report_path)
    if not report.is_file():
        return HTMLResponse("<p style='font-family:sans-serif'>No report yet — run a test first.</p>", status_code=404)
    return FileResponse(report, media_type="text/html")


# --------------------------------------------------------------------------- #
# Settings API
# --------------------------------------------------------------------------- #

async def api_get_settings(request: Request):
    cfg = load_config()
    st = config_status(cfg)
    return JSONResponse({
        "has_api_key": st["has_api_key"],
        "has_login_url": st["has_login_url"],
        "llm_model": cfg.llm_model,
        "base_url": cfg.base_url,
        "context_hint": cfg.context_hint,
        "viewport_width": cfg.viewport_width,
        "viewport_height": cfg.viewport_height,
        "device_scale": cfg.device_scale,
        "user_agent": cfg.user_agent or "",
    })


async def api_set_settings(request: Request):
    data = await request.json()
    Path(ENV_PATH).touch(exist_ok=True)
    # Secret/write-only fields: only update when a non-empty value is provided.
    write_only = {"google_api_key", "login_url"}
    for field, env_var in SETTING_KEYS.items():
        if field not in data:
            continue
        value = str(data[field]).strip()
        if field in write_only and value == "":
            continue
        set_key(ENV_PATH, env_var, value)
    return JSONResponse({"ok": True, **config_status(load_config())})


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

async def index(request: Request):
    if not config_status(load_config())["has_api_key"]:
        return HTMLResponse(KEY_HTML)
    return HTMLResponse(INDEX_HTML)


KEY_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Setup — AI Web Tester</title>
<style>
  body{font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;background:#0f1115;color:#e6e8ec;
       display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
  .card{background:#171a21;border:1px solid #262b36;border-radius:12px;padding:32px;max-width:460px}
  h1{font-size:20px;margin:0 0 8px} p{color:#9aa3b2}
  a{color:#5b9dff} input{width:100%;padding:10px;margin:12px 0;border-radius:8px;
       border:1px solid #262b36;background:#11141a;color:#e6e8ec;font-size:14px;box-sizing:border-box}
  button{background:#5b9dff;color:#06101f;border:0;border-radius:8px;padding:10px 18px;
       font-weight:700;cursor:pointer;font-size:14px}
  .err{color:#ff5c5c;min-height:18px}
</style></head><body>
<div class="card">
  <h1>👋 Welcome — one-time setup</h1>
  <p>To run tests, enter your own <b>Google Gemini API key</b>. It's stored only on
     this computer (in a local file) and is never shared.</p>
  <p>Get a free key at <a href="https://aistudio.google.com/apikey" target="_blank">aistudio.google.com/apikey</a>.</p>
  <input id="key" type="password" placeholder="Paste your Gemini API key here" autocomplete="off">
  <div class="err" id="err"></div>
  <button onclick="save()">Save and continue</button>
</div>
<script>
async function save(){
  const key=document.getElementById('key').value.trim();
  const err=document.getElementById('err'); err.textContent='';
  if(!key){err.textContent='Please paste a key.';return;}
  const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({google_api_key:key})});
  const d=await r.json();
  if(d.has_api_key){location.reload();}else{err.textContent='Could not save the key.';}
}
document.getElementById('key').addEventListener('keydown',e=>{if(e.key==='Enter')save();});
</script></body></html>"""


INDEX_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Web Tester</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#262b36;--txt:#e6e8ec;--mut:#9aa3b2;
      --pass:#2ecc71;--fail:#ff5c5c;--accent:#5b9dff;--run:#f5a623}
*{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--txt);
  font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:16px 24px;border-bottom:1px solid var(--line)}
header h1{font-size:18px;margin:0;flex:1}
button{font:inherit;cursor:pointer;border-radius:8px;border:1px solid var(--line);
  background:var(--card);color:var(--txt);padding:8px 14px}
button.primary{background:var(--accent);color:#06101f;border:0;font-weight:700}
button.ghost{background:transparent}
button:disabled{opacity:.5;cursor:not-allowed}
main{padding:20px 24px;max-width:1100px;margin:0 auto}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.toolbar .spacer{flex:1}
.test{display:flex;align-items:center;gap:12px;background:var(--card);border:1px solid var(--line);
  border-radius:10px;padding:12px 14px;margin-bottom:10px}
.test .title{font-weight:600} .test .desc{color:var(--mut);font-size:13px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:520px}
.test .grow{flex:1;min-width:0}
.row-actions{display:flex;gap:6px}
.pill{font-size:11px;padding:2px 8px;border-radius:99px;border:1px solid var(--line)}
.muted{color:var(--mut)}
/* modal */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;padding:20px;z-index:20}
.overlay.show{display:flex}
.modal{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;width:560px;max-width:100%;max-height:90vh;overflow:auto}
.modal h2{margin:0 0 12px;font-size:16px}
label{display:block;margin:10px 0 4px;color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.03em}
input[type=text],input[type=number],input[type=password],textarea,select{width:100%;padding:9px;border-radius:8px;
  border:1px solid var(--line);background:#11141a;color:var(--txt);font-size:14px;font-family:inherit}
textarea{min-height:120px;resize:vertical}
.checks{display:flex;gap:16px;flex-wrap:wrap;margin-top:10px}
.checks label{display:flex;gap:6px;align-items:center;text-transform:none;letter-spacing:0;color:var(--txt);font-size:13px;margin:0}
.modal-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
.err{color:var(--fail);min-height:18px;font-size:13px}
/* progress */
#progress{display:none;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
#progress.show{display:block}
.prow{display:flex;align-items:center;gap:10px;padding:6px 0}
.dot{width:10px;height:10px;border-radius:50%;background:var(--mut)}
.dot.running{background:var(--run);animation:pulse 1s infinite}
.dot.pass{background:var(--pass)} .dot.fail{background:var(--fail)}
@keyframes pulse{50%{opacity:.3}}
.bar{height:6px;background:#11141a;border-radius:99px;overflow:hidden;flex:1;max-width:200px}
.bar>i{display:block;height:100%;background:var(--accent);width:0}
iframe{width:100%;height:78vh;border:1px solid var(--line);border-radius:10px;background:#fff;margin-top:8px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.hint{color:var(--mut);font-size:12px;margin-top:4px}
</style></head><body>
<header>
  <h1>🌐 AI Web Tester</h1>
  <button class="ghost" onclick="openSettings()">⚙ Settings</button>
</header>
<main>
  <div class="toolbar">
    <button class="primary" id="runSel" onclick="run('selected')">▶ Run selected</button>
    <button onclick="run('all')">▶ Run all</button>
    <label class="checks" style="margin:0"><input type="checkbox" id="showBrowser"> Show browser window</label>
    <span class="spacer"></span>
    <button onclick="openNew()">＋ New test</button>
  </div>

  <div id="progress"></div>
  <div id="reportWrap" style="display:none">
    <div class="toolbar"><b>Report</b><span class="spacer"></span>
      <a href="/report" target="_blank"><button class="ghost">Open in new tab ↗</button></a></div>
    <iframe id="report" src="about:blank"></iframe>
  </div>

  <div id="list"></div>
</main>

<!-- Test editor modal -->
<div class="overlay" id="editOverlay">
  <div class="modal">
    <h2 id="editTitle">New test</h2>
    <label>Name (id — lowercase, no spaces)</label>
    <input type="text" id="f_name" placeholder="e.g. signup_form">
    <label>Title (shown in the list)</label>
    <input type="text" id="f_title" placeholder="e.g. Sign-up form appears">
    <label>What should the test do? (plain English)</label>
    <textarea id="f_desc" placeholder="Click the 'Sign up' button and confirm a form with email and password fields appears."></textarea>
    <div class="hint">You don't need to mention opening the site — it navigates there automatically.</div>
    <label>Start URL (optional — overrides the default site URL)</label>
    <input type="text" id="f_start" placeholder="leave blank to use the site's Base URL">
    <div class="checks">
      <label><input type="checkbox" id="f_enabled" checked> Include in "Run all"</label>
      <label><input type="checkbox" id="f_login"> Start at Login URL first</label>
    </div>
    <label>Max steps</label>
    <input type="number" id="f_steps" value="20" min="1" max="100" style="width:120px">
    <div class="err" id="editErr"></div>
    <div class="modal-actions">
      <button onclick="closeModal('editOverlay')">Cancel</button>
      <button class="primary" id="saveBtn" onclick="saveTest()">Save</button>
    </div>
  </div>
</div>

<!-- Settings modal -->
<div class="overlay" id="setOverlay">
  <div class="modal">
    <h2>Settings</h2>
    <label>Gemini API key <span class="muted" id="keyState"></span></label>
    <input type="password" id="s_key" placeholder="Leave blank to keep current" autocomplete="off">
    <label>Base URL (the site you want to test)</label>
    <input type="text" id="s_base" placeholder="https://your-site.com">
    <label>Login URL <span class="muted" id="loginState"></span> (optional — used by tests with "Start at Login URL")</label>
    <input type="password" id="s_login" placeholder="Leave blank to keep current">
    <label>Model</label>
    <select id="s_model">
      <option>gemini-2.5-flash</option>
      <option>gemini-2.5-pro</option>
    </select>
    <label>Context hint (optional — extra standing instructions for every test)</label>
    <textarea id="s_hint" style="min-height:60px" placeholder="e.g. This is a mobile site. Dismiss any cookie banner first."></textarea>
    <div class="two">
      <div><label>Viewport width</label><input type="number" id="s_vw"></div>
      <div><label>Viewport height</label><input type="number" id="s_vh"></div>
    </div>
    <div class="two">
      <div><label>Device scale</label><input type="number" id="s_ds" step="0.5"></div>
      <div><label>User agent (optional)</label><input type="text" id="s_ua" placeholder="browser default"></div>
    </div>
    <div class="err" id="setErr"></div>
    <div class="modal-actions">
      <button onclick="closeModal('setOverlay')">Close</button>
      <button class="primary" onclick="saveSettings()">Save</button>
    </div>
  </div>
</div>

<script>
let TESTS=[], POLL=null, EDIT_NAME=null;

async function loadTests(){
  TESTS = await (await fetch('/api/tests')).json();
  const list=document.getElementById('list');
  if(!TESTS.length){list.innerHTML='<p class="muted">No tests yet. Click “＋ New test”.</p>';return;}
  list.innerHTML = TESTS.map(t=>`
    <div class="test">
      <input type="checkbox" class="sel" data-name="${t.name}" ${t.enabled?'checked':''}>
      <div class="grow">
        <div class="title">${esc(t.title)} <span class="pill">${t.enabled?'enabled':'disabled'}</span></div>
        <div class="desc">${esc(t.description)}</div>
      </div>
      <div class="row-actions">
        <button onclick="run('one','${t.name}')">▶ Run</button>
        <button class="ghost" onclick="openEdit('${t.name}')">Edit</button>
        <button class="ghost" onclick="delTest('${t.name}')">🗑</button>
      </div>
    </div>`).join('');
}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function selectedNames(){return [...document.querySelectorAll('.sel:checked')].map(c=>c.dataset.name);}

async function run(mode,one){
  let names=null;
  if(mode==='one') names=[one];
  else if(mode==='selected'){names=selectedNames(); if(!names.length){alert('Select at least one test.');return;}}
  const headless=!document.getElementById('showBrowser').checked;
  const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({names,headless})});
  if(r.status===409){alert('A run is already in progress.');return;}
  const d=await r.json();
  if(d.error){alert(d.error);return;}
  document.getElementById('reportWrap').style.display='none';
  startPolling();
}

function startPolling(){
  const p=document.getElementById('progress'); p.classList.add('show');
  if(POLL) clearInterval(POLL);
  POLL=setInterval(pollStatus,1200); pollStatus();
}
async function pollStatus(){
  const s=await (await fetch('/api/run/status')).json();
  if(s.status==='idle'){return;}
  renderProgress(s);
  if(s.status==='done'||s.status==='error'){
    clearInterval(POLL); POLL=null;
    if(s.report_ready){
      const w=document.getElementById('reportWrap'); w.style.display='block';
      document.getElementById('report').src='/report?ts='+Date.now();
    }
    if(s.status==='error') alert('Run error: '+(s.error||'unknown'));
  }
}
function renderProgress(s){
  const rows=s.tests.map(t=>{
    const pct=t.max_steps? Math.min(100, Math.round(100*t.step/t.max_steps)):0;
    const label={pending:'Waiting',running:'Running… step '+t.step+'/'+t.max_steps,
                 pass:'✅ '+esc(t.summary||'Passed'),fail:'❌ '+esc(t.summary||'Failed')}[t.status];
    return `<div class="prow"><span class="dot ${t.status}"></span>
       <b style="min-width:160px">${esc(t.name)}</b>
       <div class="bar"><i style="width:${t.status==='pass'||t.status==='fail'?100:pct}%"></i></div>
       <span class="muted">${label}</span></div>`;
  }).join('');
  const head = s.status==='running'? '⏳ Running tests…' :
               s.status==='done'? '✅ Done' : '⚠️ Error';
  document.getElementById('progress').innerHTML='<b>'+head+'</b>'+rows;
}

/* ---- test editor ---- */
function openNew(){
  EDIT_NAME=null;
  document.getElementById('editTitle').textContent='New test';
  document.getElementById('f_name').disabled=false;
  set('f_name','');set('f_title','');set('f_desc','');set('f_start','');setNum('f_steps',20);
  check('f_enabled',true);check('f_login',false);
  document.getElementById('editErr').textContent='';
  show('editOverlay');
}
function openEdit(name){
  const t=TESTS.find(x=>x.name===name); if(!t)return;
  EDIT_NAME=name;
  document.getElementById('editTitle').textContent='Edit: '+t.title;
  document.getElementById('f_name').disabled=true;
  set('f_name',t.name);set('f_title',t.title);set('f_desc',t.description);set('f_start',t.start_url);setNum('f_steps',t.max_steps);
  check('f_enabled',t.enabled);check('f_login',t.requires_login);
  document.getElementById('editErr').textContent='';
  show('editOverlay');
}
async function saveTest(){
  const payload={name:val('f_name'),title:val('f_title'),description:val('f_desc'),
    start_url:val('f_start'),enabled:chk('f_enabled'),requires_login:chk('f_login'),
    max_steps:parseInt(val('f_steps')||'20',10)};
  const err=document.getElementById('editErr'); err.textContent='';
  if(!payload.description.trim()){err.textContent='Please describe what the test should do.';return;}
  let r;
  if(EDIT_NAME){r=await fetch('/api/tests/'+EDIT_NAME,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});}
  else{r=await fetch('/api/tests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});}
  const d=await r.json();
  if(!r.ok){err.textContent=d.error||'Could not save.';return;}
  closeModal('editOverlay'); loadTests();
}
async function delTest(name){
  if(!confirm('Delete test "'+name+'"?'))return;
  await fetch('/api/tests/'+name,{method:'DELETE'}); loadTests();
}

/* ---- settings ---- */
async function openSettings(){
  const s=await (await fetch('/api/settings')).json();
  document.getElementById('keyState').textContent=s.has_api_key?'(set ✓)':'(not set)';
  document.getElementById('loginState').textContent=s.has_login_url?'(set ✓)':'';
  set('s_key','');set('s_login','');
  set('s_base',s.base_url);set('s_hint',s.context_hint);set('s_ua',s.user_agent);
  document.getElementById('s_model').value=s.llm_model;
  setNum('s_vw',s.viewport_width);setNum('s_vh',s.viewport_height);setNum('s_ds',s.device_scale);
  document.getElementById('setErr').textContent='';
  show('setOverlay');
}
async function saveSettings(){
  const payload={llm_model:val('s_model'),base_url:val('s_base'),context_hint:val('s_hint'),
    user_agent:val('s_ua'),viewport_width:val('s_vw'),viewport_height:val('s_vh'),device_scale:val('s_ds')};
  if(val('s_key').trim()) payload.google_api_key=val('s_key').trim();
  if(val('s_login').trim()) payload.login_url=val('s_login').trim();
  const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  if(!r.ok){document.getElementById('setErr').textContent='Could not save.';return;}
  closeModal('setOverlay');
}

/* helpers */
function show(id){document.getElementById(id).classList.add('show');}
function closeModal(id){document.getElementById(id).classList.remove('show');}
function val(id){return document.getElementById(id).value;}
function set(id,v){document.getElementById(id).value=v||'';}
function setNum(id,v){document.getElementById(id).value=v;}
function chk(id){return document.getElementById(id).checked;}
function check(id,v){document.getElementById(id).checked=!!v;}

loadTests();
pollStatus();
</script></body></html>"""


# --------------------------------------------------------------------------- #
# App / server
# --------------------------------------------------------------------------- #

app = Starlette(routes=[
    Route("/", index),
    Route("/api/tests", api_list_tests, methods=["GET"]),
    Route("/api/tests", api_create_test, methods=["POST"]),
    Route("/api/tests/{name}", api_update_test, methods=["PUT"]),
    Route("/api/tests/{name}", api_delete_test, methods=["DELETE"]),
    Route("/api/run", api_run, methods=["POST"]),
    Route("/api/run/status", api_run_status, methods=["GET"]),
    Route("/report", serve_report, methods=["GET"]),
    Route("/api/settings", api_get_settings, methods=["GET"]),
    Route("/api/settings", api_set_settings, methods=["POST"]),
])


def _find_port(host: str, start: int, end: int) -> int | None:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((host, port)) != 0:  # nothing listening => free
                return port
    return None


def main():
    import uvicorn
    host = "127.0.0.1"
    port = _find_port(host, 8765, 8775)
    if port is None:
        print("❌ No free port between 8765-8775. Close other apps and retry.")
        return
    url = f"http://{host}:{port}/"
    print(f"\n✅ AI Web Tester is running.\n   Open this in your browser:  {url}\n   (Keep this window open. Press Ctrl+C to stop.)\n")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
