"""
Local web UI for the AI Web Tester.

For non-technical users: double-click the launcher (or run `python app.py`). A
page opens in your browser where you can create test cases in plain English, run
them, watch progress, and view the screenshot report — no terminal needed.

The UI is bilingual (ไทย / English) with a switcher; it defaults to Thai.

Runs only on 127.0.0.1 (your machine). Your API key is stored locally in .env and
is never shown back in the UI or exposed on the network.
"""

from __future__ import annotations

import asyncio
import json
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
    is_stop_requested,
    load_config,
    load_test,
    load_tests,
    request_stop,
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

async def _execute(names: list[str], headless: bool, lang: str):
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
        await run_suite(names, cfg, on_event=on_event, lang=lang)
        if RUN is not None:
            RUN["status"] = "stopped" if is_stop_requested() else "done"
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
    lang = data.get("lang") or "th"
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
    _RUN_TASK = asyncio.create_task(_execute([c.name for c in selected], headless, lang))
    return JSONResponse({"run_id": run_id}, status_code=202)


async def api_run_status(request: Request):
    if RUN is None:
        return JSONResponse({"status": "idle"})
    return JSONResponse(RUN)


async def api_stop(request: Request):
    if RUN is not None and RUN["status"] == "running":
        RUN["status"] = "stopping"
        request_stop()
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": "No run in progress."}, status_code=409)


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
# Translations (injected into both pages as JSON)
# --------------------------------------------------------------------------- #

I18N = {
    "en": {
        "doc_title": "AI Web Tester",
        "setup_doc_title": "Setup — AI Web Tester",
        "settings": "⚙ Settings",
        "run_selected": "▶ Run selected",
        "run_all": "▶ Run all",
        "stop": "■ Stop",
        "stopping": "Stopping…",
        "head_stopped": "🛑 Stopped",
        "show_browser": "Show browser window",
        "new_test": "＋ New test",
        "report": "Report",
        "open_new_tab": "Open in new tab ↗",
        "no_tests": 'No tests yet. Click "＋ New test".',
        "enabled": "enabled",
        "disabled": "disabled",
        "run": "▶ Run",
        "edit": "Edit",
        "new_test_title": "New test",
        "edit_prefix": "Edit: ",
        "name_label": "Name (id — lowercase, no spaces)",
        "name_ph": "e.g. signup_form",
        "title_label": "Title (shown in the list)",
        "title_ph": "e.g. Sign-up form appears",
        "desc_label": "What should the test do? (plain language)",
        "desc_ph": "Click the 'Sign up' button and confirm a form with email and password fields appears.",
        "desc_hint": "You don't need to mention opening the site — it navigates there automatically.",
        "start_label": "Start URL (optional — overrides the default site URL)",
        "start_ph": "leave blank to use the site's Base URL",
        "incl_runall": 'Include in "Run all"',
        "start_login": "Start at Login URL first",
        "max_steps": "Max steps",
        "cancel": "Cancel",
        "save": "Save",
        "err_desc": "Please describe what the test should do.",
        "err_save": "Could not save.",
        "confirm_delete": 'Delete test "{name}"?',
        "settings_title": "Settings",
        "s_key_label": "Gemini API key",
        "set_yes": "(set ✓)",
        "set_no": "(not set)",
        "keep_current": "Leave blank to keep current",
        "base_label": "Base URL (the site you want to test)",
        "base_ph": "https://your-site.com",
        "login_label": "Login URL (optional — for tests with \"Start at Login URL\")",
        "model_label": "Model",
        "hint_label": "Context hint (optional — extra instructions for every test)",
        "hint_ph": "e.g. This is a mobile site. Dismiss any cookie banner first.",
        "vw_label": "Viewport width",
        "vh_label": "Viewport height",
        "ds_label": "Device scale",
        "ua_label": "User agent (optional)",
        "ua_ph": "browser default",
        "close": "Close",
        "about": "ℹ️ About",
        "about_title": "About this tool",
        "about_made": "Created with ❤️ by Sarun Peetasai",
        "about_cause": "AI Web Tester is free and open source. It was built to make web testing accessible to everyone — including non-developers and teams who prefer working in Thai — so anyone can check that a website works just by describing it in plain words.",
        "about_contribute": "Contributions are very welcome — ⭐ star the repo, report a problem, or open a pull request on GitHub. Every bit helps the project grow.",
        "about_repo": "⭐ Source & contribute",
        "about_profile": "👤 GitHub",
        "about_site": "🌐 sarunp.com",
        "waiting": "Waiting",
        "running_step": "Running… step {step}/{max}",
        "passed_word": "Passed",
        "failed_word": "Failed",
        "head_running": "⏳ Running tests…",
        "head_done": "✅ Done",
        "head_error": "⚠️ Error",
        "alert_select": "Select at least one test.",
        "alert_busy": "A run is already in progress.",
        "alert_run_err": "Run error: ",
        "welcome": "👋 Welcome — one-time setup",
        "welcome_p1": "To run tests, enter your own <b>Google Gemini API key</b>. It's stored only on this computer and is never shared.",
        "welcome_p2": 'Get a free key at <a href="https://aistudio.google.com/apikey" target="_blank">aistudio.google.com/apikey</a>.',
        "key_ph": "Paste your Gemini API key here",
        "save_continue": "Save and continue",
        "err_paste": "Please paste a key.",
        "err_savekey": "Could not save the key.",
    },
    "th": {
        "doc_title": "AI Web Tester",
        "setup_doc_title": "ตั้งค่า — AI Web Tester",
        "settings": "⚙ ตั้งค่า",
        "run_selected": "▶ รันที่เลือก",
        "run_all": "▶ รันทั้งหมด",
        "stop": "■ หยุด",
        "stopping": "กำลังหยุด…",
        "head_stopped": "🛑 หยุดแล้ว",
        "show_browser": "แสดงหน้าต่างเบราว์เซอร์",
        "new_test": "＋ สร้างเทสต์ใหม่",
        "report": "รายงานผล",
        "open_new_tab": "เปิดในแท็บใหม่ ↗",
        "no_tests": 'ยังไม่มีเทสต์ คลิก "＋ สร้างเทสต์ใหม่"',
        "enabled": "เปิดใช้งาน",
        "disabled": "ปิดใช้งาน",
        "run": "▶ รัน",
        "edit": "แก้ไข",
        "new_test_title": "สร้างเทสต์ใหม่",
        "edit_prefix": "แก้ไข: ",
        "name_label": "ชื่อ (id — ตัวพิมพ์เล็ก ห้ามเว้นวรรค)",
        "name_ph": "เช่น signup_form",
        "title_label": "ชื่อที่แสดง (แสดงในรายการ)",
        "title_ph": "เช่น ฟอร์มสมัครสมาชิกปรากฏขึ้น",
        "desc_label": "ต้องการให้เทสต์ทำอะไร? (อธิบายเป็นภาษาธรรมดา)",
        "desc_ph": "คลิกปุ่ม 'สมัครสมาชิก' และตรวจสอบว่ามีฟอร์มที่มีช่องอีเมลและรหัสผ่านปรากฏขึ้น",
        "desc_hint": "ไม่ต้องระบุการเปิดเว็บไซต์ — ระบบจะเปิดให้อัตโนมัติ",
        "start_label": "Start URL (ไม่บังคับ — ใช้แทน URL เริ่มต้นของเว็บไซต์)",
        "start_ph": "เว้นว่างไว้เพื่อใช้ Base URL ของเว็บไซต์",
        "incl_runall": 'รวมใน "รันทั้งหมด"',
        "start_login": "เริ่มที่ Login URL ก่อน",
        "max_steps": "จำนวนขั้นตอนสูงสุด",
        "cancel": "ยกเลิก",
        "save": "บันทึก",
        "err_desc": "กรุณาอธิบายว่าต้องการให้เทสต์ทำอะไร",
        "err_save": "ไม่สามารถบันทึกได้",
        "confirm_delete": 'ลบเทสต์ "{name}" ใช่หรือไม่?',
        "settings_title": "ตั้งค่า",
        "s_key_label": "Gemini API key",
        "set_yes": "(ตั้งค่าแล้ว ✓)",
        "set_no": "(ยังไม่ได้ตั้งค่า)",
        "keep_current": "เว้นว่างไว้เพื่อใช้ค่าเดิม",
        "base_label": "Base URL (เว็บไซต์ที่ต้องการทดสอบ)",
        "base_ph": "https://your-site.com",
        "login_label": "Login URL (ไม่บังคับ — สำหรับเทสต์ที่ตั้ง \"เริ่มที่ Login URL\")",
        "model_label": "โมเดล",
        "hint_label": "คำแนะนำเพิ่มเติม (ไม่บังคับ — คำสั่งที่ใช้กับทุกเทสต์)",
        "hint_ph": "เช่น นี่คือเว็บไซต์สำหรับมือถือ ปิดแบนเนอร์คุกกี้ก่อน",
        "vw_label": "ความกว้างหน้าจอ",
        "vh_label": "ความสูงหน้าจอ",
        "ds_label": "อัตราส่วนหน้าจอ (Device scale)",
        "ua_label": "User agent (ไม่บังคับ)",
        "ua_ph": "ค่าเริ่มต้นของเบราว์เซอร์",
        "close": "ปิด",
        "about": "ℹ️ เกี่ยวกับ",
        "about_title": "เกี่ยวกับเครื่องมือนี้",
        "about_made": "สร้างด้วย ❤️ โดย Sarun Peetasai",
        "about_cause": "AI Web Tester เป็นซอฟต์แวร์ฟรีและโอเพนซอร์ส สร้างขึ้นเพื่อให้การทดสอบเว็บไซต์เป็นเรื่องง่ายสำหรับทุกคน — รวมถึงผู้ที่ไม่ใช่นักพัฒนาและทีมที่ถนัดภาษาไทย — เพียงอธิบายเป็นคำพูดธรรมดาก็ตรวจสอบเว็บไซต์ได้",
        "about_contribute": "ยินดีรับการร่วมพัฒนาอย่างยิ่ง — ⭐ กดดาวให้โปรเจกต์ แจ้งปัญหา หรือส่ง pull request บน GitHub ทุกการช่วยเหลือมีความหมายต่อโปรเจกต์",
        "about_repo": "⭐ ซอร์สโค้ดและร่วมพัฒนา",
        "about_profile": "👤 GitHub",
        "about_site": "🌐 sarunp.com",
        "waiting": "รอ",
        "running_step": "กำลังรัน… ขั้นตอนที่ {step}/{max}",
        "passed_word": "ผ่าน",
        "failed_word": "ไม่ผ่าน",
        "head_running": "⏳ กำลังรันการทดสอบ…",
        "head_done": "✅ เสร็จสิ้น",
        "head_error": "⚠️ เกิดข้อผิดพลาด",
        "alert_select": "กรุณาเลือกอย่างน้อยหนึ่งเทสต์",
        "alert_busy": "มีการรันอยู่แล้ว กรุณารอให้เสร็จก่อน",
        "alert_run_err": "เกิดข้อผิดพลาดในการรัน: ",
        "welcome": "👋 ยินดีต้อนรับ — ตั้งค่าครั้งแรก",
        "welcome_p1": "หากต้องการรันการทดสอบ กรุณาใส่ <b>Google Gemini API key</b> ของคุณเอง คีย์นี้จะถูกเก็บไว้ในเครื่องนี้เท่านั้นและจะไม่ถูกแชร์",
        "welcome_p2": 'รับคีย์ฟรีได้ที่ <a href="https://aistudio.google.com/apikey" target="_blank">aistudio.google.com/apikey</a>',
        "key_ph": "วาง Gemini API key ของคุณที่นี่",
        "save_continue": "บันทึกและดำเนินการต่อ",
        "err_paste": "กรุณาวางคีย์ก่อน",
        "err_savekey": "ไม่สามารถบันทึกคีย์ได้",
    },
}

_I18N_JS = "const I18N = " + json.dumps(I18N, ensure_ascii=False) + ";"

_LANG_BOOT = """
let LANG = localStorage.getItem('lang') || 'th';
function t(k){ return (I18N[LANG] && I18N[LANG][k]) || I18N.en[k] || k; }
function applyI18n(){
  document.documentElement.lang = LANG;
  document.querySelectorAll('[data-i18n]').forEach(el=>{ el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll('[data-i18n-ph]').forEach(el=>{ el.placeholder = t(el.dataset.i18nPh); });
  document.querySelectorAll('[data-i18n-html]').forEach(el=>{ el.innerHTML = t(el.dataset.i18nHtml); });
  document.querySelectorAll('.langsw button').forEach(b=>b.classList.toggle('active', b.dataset.lang===LANG));
}
"""

_LANG_SWITCH_HTML = """
<div class="langsw">
  <button data-lang="th" onclick="setLang('th')">ไทย</button>
  <button data-lang="en" onclick="setLang('en')">EN</button>
</div>"""


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

async def index(request: Request):
    if not config_status(load_config())["has_api_key"]:
        return HTMLResponse(KEY_HTML)
    return HTMLResponse(INDEX_HTML)


KEY_HTML = ("""<!doctype html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title data-i18n="setup_doc_title">Setup — AI Web Tester</title>
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
  .langsw{display:flex;gap:4px;justify-content:flex-end;margin-bottom:8px}
  .langsw button{background:transparent;border:1px solid #262b36;color:#e6e8ec;padding:4px 10px;font-size:12px;font-weight:400}
  .langsw button.active{background:#5b9dff;color:#06101f;border:0;font-weight:700}
</style></head><body>
<div class="card">
  """ + _LANG_SWITCH_HTML + """
  <h1 data-i18n="welcome">Welcome</h1>
  <p data-i18n-html="welcome_p1"></p>
  <p data-i18n-html="welcome_p2"></p>
  <input id="key" type="password" data-i18n-ph="key_ph" autocomplete="off">
  <div class="err" id="err"></div>
  <button data-i18n="save_continue" onclick="save()">Save and continue</button>
</div>
<script>
/*__I18N__*/
""" + _LANG_BOOT + """
function setLang(l){ LANG=l; localStorage.setItem('lang',l); applyI18n(); }
async function save(){
  const key=document.getElementById('key').value.trim();
  const err=document.getElementById('err'); err.textContent='';
  if(!key){err.textContent=t('err_paste');return;}
  const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({google_api_key:key})});
  const d=await r.json();
  if(d.has_api_key){location.reload();}else{err.textContent=t('err_savekey');}
}
document.getElementById('key').addEventListener('keydown',e=>{if(e.key==='Enter')save();});
applyI18n();
</script></body></html>""").replace("/*__I18N__*/", _I18N_JS)


INDEX_HTML = ("""<!doctype html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title data-i18n="doc_title">AI Web Tester</title>
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
button.danger{background:var(--fail);color:#fff;border:0;font-weight:700}
button.ghost{background:transparent}
button:disabled{opacity:.5;cursor:not-allowed}
.langsw{display:flex;gap:4px}
.langsw button{padding:5px 10px;font-size:12px}
.langsw button.active{background:var(--accent);color:#06101f;border:0;font-weight:700}
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
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.6);display:none;align-items:center;justify-content:center;padding:20px;z-index:20}
.overlay.show{display:flex}
.modal{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;width:560px;max-width:100%;max-height:90vh;overflow:auto}
.modal h2{margin:0 0 12px;font-size:16px}
label{display:block;margin:10px 0 4px;color:var(--mut);font-size:12px}
input[type=text],input[type=number],input[type=password],textarea,select{width:100%;padding:9px;border-radius:8px;
  border:1px solid var(--line);background:#11141a;color:var(--txt);font-size:14px;font-family:inherit}
textarea{min-height:120px;resize:vertical}
.checks{display:flex;gap:16px;flex-wrap:wrap;margin-top:10px}
.checks label{display:flex;gap:6px;align-items:center;color:var(--txt);font-size:13px;margin:0}
.modal-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
.err{color:var(--fail);min-height:18px;font-size:13px}
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
.links{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}
.links a.linkbtn{text-decoration:none;color:var(--txt);background:#11141a;border:1px solid var(--line);border-radius:8px;padding:9px 14px;font-size:13px}
.links a.linkbtn:hover{border-color:var(--accent);color:var(--accent)}
.about-cause{color:var(--mut);line-height:1.6}
</style></head><body>
<header>
  <h1>🌐 AI Web Tester</h1>
  """ + _LANG_SWITCH_HTML + """
  <button class="ghost" data-i18n="about" onclick="openAbout()">About</button>
  <button class="ghost" data-i18n="settings" onclick="openSettings()">Settings</button>
</header>
<main>
  <div class="toolbar">
    <button class="primary" id="runSelBtn" data-i18n="run_selected" onclick="run('selected')">Run selected</button>
    <button id="runAllBtn" data-i18n="run_all" onclick="run('all')">Run all</button>
    <button class="danger" id="stopBtn" data-i18n="stop" style="display:none" onclick="stopRun()">Stop</button>
    <label class="checks" style="margin:0"><input type="checkbox" id="showBrowser"> <span data-i18n="show_browser">Show browser window</span></label>
    <span class="spacer"></span>
    <button data-i18n="new_test" onclick="openNew()">New test</button>
  </div>

  <div id="progress"></div>
  <div id="reportWrap" style="display:none">
    <div class="toolbar"><b data-i18n="report">Report</b><span class="spacer"></span>
      <a href="/report" target="_blank"><button class="ghost" data-i18n="open_new_tab">Open in new tab</button></a></div>
    <iframe id="report" src="about:blank"></iframe>
  </div>

  <div id="list"></div>
</main>

<div class="overlay" id="editOverlay">
  <div class="modal">
    <h2 id="editTitle">New test</h2>
    <label data-i18n="name_label">Name</label>
    <input type="text" id="f_name" data-i18n-ph="name_ph">
    <label data-i18n="title_label">Title</label>
    <input type="text" id="f_title" data-i18n-ph="title_ph">
    <label data-i18n="desc_label">What should the test do?</label>
    <textarea id="f_desc" data-i18n-ph="desc_ph"></textarea>
    <div class="hint" data-i18n="desc_hint"></div>
    <label data-i18n="start_label">Start URL</label>
    <input type="text" id="f_start" data-i18n-ph="start_ph">
    <div class="checks">
      <label><input type="checkbox" id="f_enabled" checked> <span data-i18n="incl_runall">Include in Run all</span></label>
      <label><input type="checkbox" id="f_login"> <span data-i18n="start_login">Start at Login URL first</span></label>
    </div>
    <label data-i18n="max_steps">Max steps</label>
    <input type="number" id="f_steps" value="20" min="1" max="100" style="width:120px">
    <div class="err" id="editErr"></div>
    <div class="modal-actions">
      <button data-i18n="cancel" onclick="closeModal('editOverlay')">Cancel</button>
      <button class="primary" id="saveBtn" data-i18n="save" onclick="saveTest()">Save</button>
    </div>
  </div>
</div>

<div class="overlay" id="setOverlay">
  <div class="modal">
    <h2 data-i18n="settings_title">Settings</h2>
    <label><span data-i18n="s_key_label">Gemini API key</span> <span class="muted" id="keyState"></span></label>
    <input type="password" id="s_key" data-i18n-ph="keep_current" autocomplete="off">
    <label data-i18n="base_label">Base URL</label>
    <input type="text" id="s_base" data-i18n-ph="base_ph">
    <label><span data-i18n="login_label">Login URL</span> <span class="muted" id="loginState"></span></label>
    <input type="password" id="s_login" data-i18n-ph="keep_current">
    <label data-i18n="model_label">Model</label>
    <select id="s_model"><option>gemini-2.5-flash</option><option>gemini-2.5-pro</option></select>
    <label data-i18n="hint_label">Context hint</label>
    <textarea id="s_hint" style="min-height:60px" data-i18n-ph="hint_ph"></textarea>
    <div class="two">
      <div><label data-i18n="vw_label">Viewport width</label><input type="number" id="s_vw"></div>
      <div><label data-i18n="vh_label">Viewport height</label><input type="number" id="s_vh"></div>
    </div>
    <div class="two">
      <div><label data-i18n="ds_label">Device scale</label><input type="number" id="s_ds" step="0.5"></div>
      <div><label data-i18n="ua_label">User agent</label><input type="text" id="s_ua" data-i18n-ph="ua_ph"></div>
    </div>
    <div class="err" id="setErr"></div>
    <div class="modal-actions">
      <button data-i18n="close" onclick="closeModal('setOverlay')">Close</button>
      <button class="primary" data-i18n="save" onclick="saveSettings()">Save</button>
    </div>
  </div>
</div>

<div class="overlay" id="aboutOverlay">
  <div class="modal">
    <h2 data-i18n="about_title">About this tool</h2>
    <p style="font-size:16px;margin:4px 0 14px" data-i18n="about_made">Created with ❤️ by Sarun Peetasai</p>
    <p class="about-cause" data-i18n="about_cause"></p>
    <p class="about-cause" data-i18n="about_contribute"></p>
    <div class="links">
      <a class="linkbtn" href="https://github.com/iamham/ai-web-tester-for-dummies" target="_blank" rel="noopener" data-i18n="about_repo">Source &amp; contribute</a>
      <a class="linkbtn" href="https://github.com/iamham" target="_blank" rel="noopener" data-i18n="about_profile">GitHub</a>
      <a class="linkbtn" href="https://sarunp.com" target="_blank" rel="noopener" data-i18n="about_site">sarunp.com</a>
    </div>
    <div class="modal-actions">
      <button data-i18n="close" onclick="closeModal('aboutOverlay')">Close</button>
    </div>
  </div>
</div>

<script>
/*__I18N__*/
""" + _LANG_BOOT + """
let TESTS=[], POLL=null, EDIT_NAME=null, LAST_STATUS=null;

function setLang(l){ LANG=l; localStorage.setItem('lang',l); applyI18n(); loadTests();
  if(LAST_STATUS) renderProgress(LAST_STATUS); }

async function loadTests(){
  TESTS = await (await fetch('/api/tests')).json();
  const list=document.getElementById('list');
  if(!TESTS.length){list.innerHTML='<p class="muted">'+esc(t('no_tests'))+'</p>';return;}
  list.innerHTML = TESTS.map(t_=>`
    <div class="test">
      <input type="checkbox" class="sel" data-name="${t_.name}" ${t_.enabled?'checked':''}>
      <div class="grow">
        <div class="title">${esc(t_.title)} <span class="pill">${t_.enabled?t('enabled'):t('disabled')}</span></div>
        <div class="desc">${esc(t_.description)}</div>
      </div>
      <div class="row-actions">
        <button onclick="run('one','${t_.name}')">${t('run')}</button>
        <button class="ghost" onclick="openEdit('${t_.name}')">${t('edit')}</button>
        <button class="ghost" onclick="delTest('${t_.name}')">🗑</button>
      </div>
    </div>`).join('');
}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function selectedNames(){return [...document.querySelectorAll('.sel:checked')].map(c=>c.dataset.name);}

async function run(mode,one){
  let names=null;
  if(mode==='one') names=[one];
  else if(mode==='selected'){names=selectedNames(); if(!names.length){alert(t('alert_select'));return;}}
  const headless=!document.getElementById('showBrowser').checked;
  const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({names,headless,lang:LANG})});
  if(r.status===409){alert(t('alert_busy'));return;}
  const d=await r.json();
  if(d.error){alert(d.error);return;}
  document.getElementById('reportWrap').style.display='none';
  setRunning(true);
  startPolling();
}

async function stopRun(){
  const b=document.getElementById('stopBtn');
  b.disabled=true; b.textContent=t('stopping');
  await fetch('/api/run/stop',{method:'POST'});
}

function setRunning(running){
  const stop=document.getElementById('stopBtn');
  if(running){ stop.style.display=''; stop.disabled=false; stop.textContent=t('stop'); }
  else { stop.style.display='none'; }
  document.getElementById('runSelBtn').disabled=running;
  document.getElementById('runAllBtn').disabled=running;
}

function startPolling(){
  document.getElementById('progress').classList.add('show');
  if(POLL) clearInterval(POLL);
  POLL=setInterval(pollStatus,1200); pollStatus();
}
async function pollStatus(){
  const s=await (await fetch('/api/run/status')).json();
  if(s.status==='idle'){ setRunning(false); return; }
  LAST_STATUS=s; renderProgress(s);
  if(s.status==='running'||s.status==='stopping'){ setRunning(true); if(!POLL) startPolling(); return; }
  // terminal: done | stopped | error
  if(POLL){ clearInterval(POLL); POLL=null; }
  setRunning(false);
  if(s.report_ready){
    document.getElementById('reportWrap').style.display='block';
    document.getElementById('report').src='/report?ts='+Date.now();
  }
  if(s.status==='error') alert(t('alert_run_err')+(s.error||''));
}
function renderProgress(s){
  const rows=s.tests.map(x=>{
    const pct=x.max_steps? Math.min(100, Math.round(100*x.step/x.max_steps)):0;
    const label={pending:t('waiting'),
                 running:t('running_step').replace('{step}',x.step).replace('{max}',x.max_steps),
                 pass:'✅ '+esc(x.summary||t('passed_word')),
                 fail:'❌ '+esc(x.summary||t('failed_word'))}[x.status];
    return `<div class="prow"><span class="dot ${x.status}"></span>
       <b style="min-width:160px">${esc(x.name)}</b>
       <div class="bar"><i style="width:${x.status==='pass'||x.status==='fail'?100:pct}%"></i></div>
       <span class="muted">${label}</span></div>`;
  }).join('');
  const head = s.status==='running'? t('head_running')
             : s.status==='stopping'? t('stopping')
             : s.status==='done'? t('head_done')
             : s.status==='stopped'? t('head_stopped')
             : t('head_error');
  document.getElementById('progress').innerHTML='<b>'+head+'</b>'+rows;
}

function openNew(){
  EDIT_NAME=null;
  document.getElementById('editTitle').textContent=t('new_test_title');
  document.getElementById('f_name').disabled=false;
  set('f_name','');set('f_title','');set('f_desc','');set('f_start','');setNum('f_steps',20);
  check('f_enabled',true);check('f_login',false);
  document.getElementById('editErr').textContent='';
  show('editOverlay');
}
function openEdit(name){
  const x=TESTS.find(z=>z.name===name); if(!x)return;
  EDIT_NAME=name;
  document.getElementById('editTitle').textContent=t('edit_prefix')+x.title;
  document.getElementById('f_name').disabled=true;
  set('f_name',x.name);set('f_title',x.title);set('f_desc',x.description);set('f_start',x.start_url);setNum('f_steps',x.max_steps);
  check('f_enabled',x.enabled);check('f_login',x.requires_login);
  document.getElementById('editErr').textContent='';
  show('editOverlay');
}
async function saveTest(){
  const payload={name:val('f_name'),title:val('f_title'),description:val('f_desc'),
    start_url:val('f_start'),enabled:chk('f_enabled'),requires_login:chk('f_login'),
    max_steps:parseInt(val('f_steps')||'20',10)};
  const err=document.getElementById('editErr'); err.textContent='';
  if(!payload.description.trim()){err.textContent=t('err_desc');return;}
  let r;
  if(EDIT_NAME){r=await fetch('/api/tests/'+EDIT_NAME,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});}
  else{r=await fetch('/api/tests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});}
  const d=await r.json();
  if(!r.ok){err.textContent=d.error||t('err_save');return;}
  closeModal('editOverlay'); loadTests();
}
async function delTest(name){
  if(!confirm(t('confirm_delete').replace('{name}',name)))return;
  await fetch('/api/tests/'+name,{method:'DELETE'}); loadTests();
}

async function openSettings(){
  const s=await (await fetch('/api/settings')).json();
  document.getElementById('keyState').textContent=s.has_api_key?t('set_yes'):t('set_no');
  document.getElementById('loginState').textContent=s.has_login_url?t('set_yes'):'';
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
  if(!r.ok){document.getElementById('setErr').textContent=t('err_save');return;}
  closeModal('setOverlay');
}

function openAbout(){show('aboutOverlay');}
function show(id){document.getElementById(id).classList.add('show');}
function closeModal(id){document.getElementById(id).classList.remove('show');}
function val(id){return document.getElementById(id).value;}
function set(id,v){document.getElementById(id).value=v||'';}
function setNum(id,v){document.getElementById(id).value=v;}
function chk(id){return document.getElementById(id).checked;}
function check(id,v){document.getElementById(id).checked=!!v;}

applyI18n();
loadTests();
pollStatus();
</script></body></html>""").replace("/*__I18N__*/", _I18N_JS)


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
    Route("/api/run/stop", api_stop, methods=["POST"]),
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
