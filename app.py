"""
Local web UI for the AI Web Tester.

For non-technical users: double-click the launcher (or run `python app.py`). A
page opens in your browser with a guided, plain-language interface to create
checks, run them, watch progress, and view the screenshot report — no terminal.

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
        return HTMLResponse("<p style='font-family:sans-serif'>No report yet — run a check first.</p>", status_code=404)
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
        # chrome
        "settings": "⚙ Settings",
        "about": "ℹ️ About",
        "testing_label": "Testing",
        "set_site_nudge": "Set your website",
        # toolbar
        "run_all": "▶ Run all",
        "stop": "■ Stop",
        "stopping": "Stopping…",
        "new_check": "＋ New check",
        "show_browser": "Show browser",
        # how-it-works banner
        "how_title": "How it works",
        "how_1": "Write a check in plain words",
        "how_2": "Press Run",
        "how_3": "Read the report with screenshots",
        "how_dismiss": "Got it",
        # report / results help
        "report": "Report",
        "open_new_tab": "Open in new tab ↗",
        "results_help_title": "What do the results mean?",
        "results_help_body": "<b>✅ Passed</b> — everything you described was true. <b>❌ Failed</b> — something you described didn't happen, or the page showed an error. The report shows step-by-step screenshots of what the AI saw and did — click a row to expand it.",
        # list / cards
        "no_checks_title": "No checks yet",
        "start_example": "Start with a ready-made one:",
        "write_own": "＋ Write your own",
        "run": "▶ Run",
        "edit": "Edit",
        "chip_pass": "passed",
        "chip_fail": "failed",
        "chip_notrun": "not run yet",
        "disabled_badge": "off",
        "confirm_delete": 'Delete check "{name}"?',
        # create / edit modal
        "new_check_title": "New check",
        "edit_prefix": "Edit: ",
        "check_q": "What should we check?",
        "check_q_ph": "e.g. Click 'Sign up' and confirm a form with email and password appears.",
        "desc_hint": "We open your site automatically — just describe the scenario and what counts as pass.",
        "short_name": "Short name",
        "short_name_ph": "e.g. Sign-up form appears",
        "id_auto": "id:",
        "advanced": "Advanced",
        "start_label": "Start URL (override)",
        "start_ph": "leave blank to use your website",
        "start_login": "Start at Login URL first",
        "incl_runall": 'Include in "Run all"',
        "max_steps": "Max steps",
        "cancel": "Cancel",
        "save": "Save",
        "err_desc": "Please describe what to check.",
        "err_save": "Could not save.",
        # settings
        "settings_title": "Settings",
        "grp_site": "Your website",
        "grp_ai": "AI",
        "grp_advanced": "Advanced",
        "base_label": "Website address (Base URL)",
        "base_ph": "https://your-site.com",
        "s_key_label": "Gemini API key",
        "set_yes": "(set ✓)",
        "set_no": "(not set)",
        "keep_current": "Leave blank to keep current",
        "model_label": "AI model",
        "login_label": "Login URL",
        "hint_label": "Context hint",
        "hint_ph": "e.g. This is a mobile site. Dismiss any cookie banner first.",
        "vw_label": "Viewport width",
        "vh_label": "Viewport height",
        "ds_label": "Device scale",
        "ua_label": "User agent",
        "ua_ph": "browser default",
        "close": "Close",
        # tooltips
        "tip_base": "The website the AI will open and test.",
        "tip_key": "Your Google Gemini key. Stored only on this computer.",
        "tip_model": "Flash is fast & cheap; Pro is slower but smarter.",
        "tip_login": "A URL that logs you in. Used by checks set to start at login.",
        "tip_hint": "Extra instructions added to every check (e.g. dismiss a cookie banner).",
        "tip_viewport": "Browser window size in pixels.",
        "tip_scale": "Pixel density. Use 2 for sharper mobile screenshots.",
        "tip_ua": "Advanced: override the browser's user-agent string.",
        "tip_maxsteps": "Safety cap on how many actions the AI may take.",
        "tip_starturl": "Open this exact URL instead of your website address.",
        # progress / statuses
        "waiting": "Waiting",
        "running_step": "Running… step {step}/{max}",
        "passed_word": "Passed",
        "failed_word": "Failed",
        "head_running": "⏳ Running…",
        "head_done": "✅ Done",
        "head_stopped": "🛑 Stopped",
        "head_error": "⚠️ Error",
        "alert_busy": "A run is already in progress.",
        "alert_run_err": "Run error: ",
        # about
        "about_title": "About this tool",
        "about_made": "Created with ❤️ by Sarun Peetasai",
        "about_cause": "AI Web Tester is free and open source. It was built to make web testing accessible to everyone — including non-developers and teams who prefer working in Thai — so anyone can check that a website works just by describing it in plain words.",
        "about_contribute": "Contributions are very welcome — ⭐ star the repo, report a problem, or open a pull request on GitHub. Every bit helps the project grow.",
        "about_repo": "⭐ Source & contribute",
        "about_profile": "👤 GitHub",
        "about_site": "🌐 sarunp.com",
        # wizard
        "wiz_step": "Step {n} of 3",
        "wiz_welcome_title": "👋 Welcome to AI Web Tester",
        "wiz_welcome_body": "Check your website in plain language. Let's set up in 3 quick steps.",
        "wiz_key_title": "Your Gemini API key",
        "wiz_key_body": "We use Google Gemini (the AI) to read and click your site. Paste a free key — it's stored only on this computer.",
        "wiz_key_help": "How do I get a key?",
        "wiz_key_help_body": 'Go to <a href="https://aistudio.google.com/apikey" target="_blank">aistudio.google.com/apikey</a> → sign in with Google → "Create API key" → copy and paste it here.',
        "wiz_url_title": "Your website",
        "wiz_url_body": "Which website should we test?",
        "wiz_url_hint": "You can change this later in Settings.",
        "wiz_url_ph": "https://your-site.com",
        "wiz_next": "Next →",
        "wiz_back": "← Back",
        "wiz_finish": "Finish & start ✓",
        "key_ph": "Paste your Gemini API key here",
        "err_paste": "Please paste a key.",
        "err_savekey": "Could not save the key.",
        # templates
        "tpl_home_title": "Homepage loads",
        "tpl_home_desc": "Confirm the homepage loads with no errors and the main content is visible. Report the page title.",
        "tpl_login_title": "Login works",
        "tpl_login_desc": "Log in and confirm you reach the logged-in area with no error message.",
        "tpl_search_title": "Search works",
        "tpl_search_desc": "Use the search box to search for a common term and confirm results appear. Report how many.",
        "tpl_mobile_title": "Mobile layout",
        "tpl_mobile_desc": "Check the page has no horizontal scrolling, no overlapping elements, and no broken images.",
    },
    "th": {
        "doc_title": "AI Web Tester",
        "setup_doc_title": "ตั้งค่า — AI Web Tester",
        "settings": "⚙ ตั้งค่า",
        "about": "ℹ️ เกี่ยวกับ",
        "testing_label": "กำลังทดสอบ",
        "set_site_nudge": "ตั้งค่าเว็บไซต์ของคุณ",
        "run_all": "▶ รันทั้งหมด",
        "stop": "■ หยุด",
        "stopping": "กำลังหยุด…",
        "new_check": "＋ สร้างการตรวจสอบ",
        "show_browser": "แสดงเบราว์เซอร์",
        "how_title": "วิธีใช้งาน",
        "how_1": "เขียนสิ่งที่ต้องการตรวจสอบเป็นคำพูดธรรมดา",
        "how_2": "กดปุ่มรัน",
        "how_3": "อ่านรายงานพร้อมภาพหน้าจอ",
        "how_dismiss": "เข้าใจแล้ว",
        "report": "รายงานผล",
        "open_new_tab": "เปิดในแท็บใหม่ ↗",
        "results_help_title": "ผลลัพธ์หมายความว่าอย่างไร?",
        "results_help_body": "<b>✅ ผ่าน</b> — ทุกอย่างที่คุณอธิบายเป็นจริง <b>❌ ไม่ผ่าน</b> — มีบางอย่างไม่เกิดขึ้นตามที่อธิบาย หรือหน้าเว็บแสดงข้อผิดพลาด รายงานจะแสดงภาพหน้าจอแต่ละขั้นตอนที่ AI เห็นและทำ — คลิกที่แถวเพื่อดูรายละเอียด",
        "no_checks_title": "ยังไม่มีการตรวจสอบ",
        "start_example": "เริ่มจากตัวอย่างสำเร็จรูป:",
        "write_own": "＋ เขียนเอง",
        "run": "▶ รัน",
        "edit": "แก้ไข",
        "chip_pass": "ผ่าน",
        "chip_fail": "ไม่ผ่าน",
        "chip_notrun": "ยังไม่ได้รัน",
        "disabled_badge": "ปิด",
        "confirm_delete": 'ลบการตรวจสอบ "{name}" ใช่หรือไม่?',
        "new_check_title": "สร้างการตรวจสอบ",
        "edit_prefix": "แก้ไข: ",
        "check_q": "ต้องการตรวจสอบอะไร?",
        "check_q_ph": "เช่น คลิกปุ่ม 'สมัครสมาชิก' และตรวจสอบว่ามีฟอร์มที่มีช่องอีเมลและรหัสผ่านปรากฏขึ้น",
        "desc_hint": "ระบบจะเปิดเว็บไซต์ให้อัตโนมัติ — เพียงอธิบายสถานการณ์และเกณฑ์ที่ถือว่าผ่าน",
        "short_name": "ชื่อย่อ",
        "short_name_ph": "เช่น ฟอร์มสมัครสมาชิกปรากฏขึ้น",
        "id_auto": "id:",
        "advanced": "ตัวเลือกขั้นสูง",
        "start_label": "Start URL (ระบุเอง)",
        "start_ph": "เว้นว่างไว้เพื่อใช้เว็บไซต์ของคุณ",
        "start_login": "เริ่มที่ Login URL ก่อน",
        "incl_runall": 'รวมใน "รันทั้งหมด"',
        "max_steps": "จำนวนขั้นตอนสูงสุด",
        "cancel": "ยกเลิก",
        "save": "บันทึก",
        "err_desc": "กรุณาอธิบายสิ่งที่ต้องการตรวจสอบ",
        "err_save": "ไม่สามารถบันทึกได้",
        "settings_title": "ตั้งค่า",
        "grp_site": "เว็บไซต์ของคุณ",
        "grp_ai": "AI",
        "grp_advanced": "ขั้นสูง",
        "base_label": "ที่อยู่เว็บไซต์ (Base URL)",
        "base_ph": "https://your-site.com",
        "s_key_label": "Gemini API key",
        "set_yes": "(ตั้งค่าแล้ว ✓)",
        "set_no": "(ยังไม่ได้ตั้งค่า)",
        "keep_current": "เว้นว่างไว้เพื่อใช้ค่าเดิม",
        "model_label": "โมเดล AI",
        "login_label": "Login URL",
        "hint_label": "คำแนะนำเพิ่มเติม",
        "hint_ph": "เช่น นี่คือเว็บไซต์สำหรับมือถือ ปิดแบนเนอร์คุกกี้ก่อน",
        "vw_label": "ความกว้างหน้าจอ",
        "vh_label": "ความสูงหน้าจอ",
        "ds_label": "อัตราส่วนหน้าจอ (Device scale)",
        "ua_label": "User agent",
        "ua_ph": "ค่าเริ่มต้นของเบราว์เซอร์",
        "close": "ปิด",
        "tip_base": "เว็บไซต์ที่ AI จะเปิดและทดสอบ",
        "tip_key": "คีย์ Google Gemini ของคุณ เก็บไว้ในเครื่องนี้เท่านั้น",
        "tip_model": "Flash เร็วและประหยัด; Pro ช้ากว่าแต่ฉลาดกว่า",
        "tip_login": "URL ที่ใช้เข้าสู่ระบบ ใช้กับการตรวจสอบที่ตั้งให้เริ่มที่ Login",
        "tip_hint": "คำสั่งเพิ่มเติมที่ใช้กับทุกการตรวจสอบ (เช่น ปิดแบนเนอร์คุกกี้)",
        "tip_viewport": "ขนาดหน้าต่างเบราว์เซอร์ (พิกเซล)",
        "tip_scale": "ความละเอียดพิกเซล ใช้ 2 สำหรับภาพมือถือที่คมขึ้น",
        "tip_ua": "ขั้นสูง: กำหนด user-agent ของเบราว์เซอร์เอง",
        "tip_maxsteps": "จำกัดจำนวนการกระทำสูงสุดที่ AI ทำได้",
        "tip_starturl": "เปิด URL นี้แทนที่อยู่เว็บไซต์ของคุณ",
        "waiting": "รอ",
        "running_step": "กำลังรัน… ขั้นตอนที่ {step}/{max}",
        "passed_word": "ผ่าน",
        "failed_word": "ไม่ผ่าน",
        "head_running": "⏳ กำลังรัน…",
        "head_done": "✅ เสร็จสิ้น",
        "head_stopped": "🛑 หยุดแล้ว",
        "head_error": "⚠️ เกิดข้อผิดพลาด",
        "alert_busy": "มีการรันอยู่แล้ว กรุณารอให้เสร็จก่อน",
        "alert_run_err": "เกิดข้อผิดพลาดในการรัน: ",
        "about_title": "เกี่ยวกับเครื่องมือนี้",
        "about_made": "สร้างด้วย ❤️ โดย Sarun Peetasai",
        "about_cause": "AI Web Tester เป็นซอฟต์แวร์ฟรีและโอเพนซอร์ส สร้างขึ้นเพื่อให้การทดสอบเว็บไซต์เป็นเรื่องง่ายสำหรับทุกคน — รวมถึงผู้ที่ไม่ใช่นักพัฒนาและทีมที่ถนัดภาษาไทย — เพียงอธิบายเป็นคำพูดธรรมดาก็ตรวจสอบเว็บไซต์ได้",
        "about_contribute": "ยินดีรับการร่วมพัฒนาอย่างยิ่ง — ⭐ กดดาวให้โปรเจกต์ แจ้งปัญหา หรือส่ง pull request บน GitHub ทุกการช่วยเหลือมีความหมายต่อโปรเจกต์",
        "about_repo": "⭐ ซอร์สโค้ดและร่วมพัฒนา",
        "about_profile": "👤 GitHub",
        "about_site": "🌐 sarunp.com",
        "wiz_step": "ขั้นตอนที่ {n} จาก 3",
        "wiz_welcome_title": "👋 ยินดีต้อนรับสู่ AI Web Tester",
        "wiz_welcome_body": "ตรวจสอบเว็บไซต์ของคุณด้วยภาษาธรรมดา มาตั้งค่ากันใน 3 ขั้นตอนง่าย ๆ",
        "wiz_key_title": "Gemini API key ของคุณ",
        "wiz_key_body": "เราใช้ Google Gemini (AI) ในการอ่านและคลิกบนเว็บไซต์ของคุณ วางคีย์ฟรี — เก็บไว้ในเครื่องนี้เท่านั้น",
        "wiz_key_help": "ขอคีย์ได้อย่างไร?",
        "wiz_key_help_body": 'ไปที่ <a href="https://aistudio.google.com/apikey" target="_blank">aistudio.google.com/apikey</a> → เข้าสู่ระบบด้วย Google → "Create API key" → คัดลอกมาวางที่นี่',
        "wiz_url_title": "เว็บไซต์ของคุณ",
        "wiz_url_body": "ต้องการทดสอบเว็บไซต์ใด?",
        "wiz_url_hint": "เปลี่ยนภายหลังได้ในหน้า ตั้งค่า",
        "wiz_url_ph": "https://your-site.com",
        "wiz_next": "ถัดไป →",
        "wiz_back": "← ย้อนกลับ",
        "wiz_finish": "เสร็จสิ้นและเริ่ม ✓",
        "key_ph": "วาง Gemini API key ที่นี่",
        "err_paste": "กรุณาวางคีย์ก่อน",
        "err_savekey": "ไม่สามารถบันทึกคีย์ได้",
        "tpl_home_title": "หน้าแรกโหลดได้",
        "tpl_home_desc": "ยืนยันว่าหน้าแรกโหลดได้โดยไม่มีข้อผิดพลาด และเนื้อหาหลักแสดงผล รายงานชื่อหน้าเว็บด้วย",
        "tpl_login_title": "เข้าสู่ระบบได้",
        "tpl_login_desc": "เข้าสู่ระบบและยืนยันว่าเข้าถึงพื้นที่หลังล็อกอินได้โดยไม่มีข้อความแสดงข้อผิดพลาด",
        "tpl_search_title": "ค้นหาได้",
        "tpl_search_desc": "ใช้ช่องค้นหาเพื่อค้นหาคำทั่วไป และยืนยันว่ามีผลลัพธ์แสดงขึ้น รายงานจำนวนผลลัพธ์",
        "tpl_mobile_title": "การแสดงผลบนมือถือ",
        "tpl_mobile_desc": "ตรวจสอบว่าหน้าเว็บไม่มีการเลื่อนแนวนอน ไม่มีองค์ประกอบซ้อนทับ และรูปภาพไม่เสีย",
    },
}

_I18N_JS = "const I18N = " + json.dumps(I18N, ensure_ascii=False) + ";"

# Google Sans is Latin-only, so Noto Sans Thai follows it for Thai glyphs.
_FONT = '"Google Sans","Noto Sans Thai","Sarabun","Leelawadee UI",Tahoma,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif'

_LANG_BOOT = """
let LANG = localStorage.getItem('lang') || 'th';
function t(k){ return (I18N[LANG] && I18N[LANG][k]) || I18N.en[k] || k; }
function applyI18n(){
  document.documentElement.lang = LANG;
  document.querySelectorAll('[data-i18n]').forEach(el=>{ el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll('[data-i18n-ph]').forEach(el=>{ el.placeholder = t(el.dataset.i18nPh); });
  document.querySelectorAll('[data-i18n-html]').forEach(el=>{ el.innerHTML = t(el.dataset.i18nHtml); });
  document.querySelectorAll('[data-i18n-title]').forEach(el=>{ el.title = t(el.dataset.i18nTitle); });
  document.querySelectorAll('.langsw button').forEach(b=>b.classList.toggle('active', b.dataset.lang===LANG));
}
"""

_LANG_SWITCH_HTML = """
<div class="langsw">
  <button data-lang="th" onclick="setLang('th')">ไทย</button>
  <button data-lang="en" onclick="setLang('en')">EN</button>
</div>"""

# Shared light-theme tokens + base styles for both pages.
_BASE_CSS = """
:root{--bg:#f6f7f9;--surface:#fff;--line:#e3e6eb;--txt:#1f2430;--mut:#6b7280;
      --primary:#2563eb;--ink:#fff;--pass:#16a34a;--fail:#dc2626;--run:#d97706}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.6 """ + _FONT + """}
button{font:inherit;cursor:pointer;border-radius:8px;border:1px solid var(--line);background:var(--surface);color:var(--txt);padding:8px 14px}
button.primary{background:var(--primary);color:var(--ink);border:0;font-weight:700}
button.danger{background:var(--fail);color:#fff;border:0;font-weight:700}
button.ghost{background:transparent}
button.sm{padding:6px 10px;font-size:13px}
button:disabled{opacity:.5;cursor:not-allowed}
a{color:var(--primary)}
.langsw{display:flex;gap:4px}
.langsw button{padding:5px 10px;font-size:12px;background:transparent}
.langsw button.active{background:var(--primary);color:var(--ink);border:0;font-weight:700}
input[type=text],input[type=number],input[type=password],textarea,select{width:100%;padding:9px;border-radius:8px;border:1px solid var(--line);background:var(--surface);color:var(--txt);font-size:14px;font-family:inherit}
input:focus,textarea:focus,select:focus{outline:2px solid rgba(37,99,235,.25);border-color:var(--primary)}
textarea{min-height:120px;resize:vertical}
.muted{color:var(--mut)}
.err{color:var(--fail);min-height:18px;font-size:13px}
"""


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

async def index(request: Request):
    if not config_status(load_config())["has_api_key"]:
        return HTMLResponse(WIZARD_HTML)
    return HTMLResponse(INDEX_HTML)


WIZARD_HTML = ("""<!doctype html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;700&family=Noto+Sans+Thai:wght@400;500;700&display=swap" rel="stylesheet">
<title data-i18n="setup_doc_title">Setup — AI Web Tester</title>
<style>""" + _BASE_CSS + """
body{display:flex;min-height:100vh;align-items:center;justify-content:center;padding:20px}
.wiz{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:28px;max-width:480px;width:100%;box-shadow:0 8px 30px rgba(16,24,40,.08)}
.wiz-top{display:flex;align-items:center;margin-bottom:18px}
.dots{display:flex;gap:8px;flex:1}
.dots span{width:26px;height:6px;border-radius:99px;background:var(--line)}
.dots span.on{background:var(--primary)}
h1{font-size:22px;margin:0 0 8px} h2{font-size:19px;margin:0 0 8px}
.wiz p{color:var(--mut);margin:0 0 14px}
.wiz input{margin:6px 0 4px}
.wiz details{margin:8px 0}
.wiz summary{cursor:pointer;color:var(--primary);font-size:13px}
.wiz-actions{display:flex;justify-content:space-between;margin-top:20px;gap:10px}
.hint{color:var(--mut);font-size:12px;margin-top:4px}
</style></head><body>
<div class="wiz">
  <div class="wiz-top">
    <div class="dots"><span data-step="1"></span><span data-step="2"></span><span data-step="3"></span></div>
    """ + _LANG_SWITCH_HTML + """
  </div>

  <div class="wiz-step" data-step="1">
    <h1 data-i18n="wiz_welcome_title">Welcome</h1>
    <p data-i18n="wiz_welcome_body"></p>
    <div class="wiz-actions"><span></span>
      <button class="primary" data-i18n="wiz_next" onclick="nextStep()">Next</button></div>
  </div>

  <div class="wiz-step" data-step="2" style="display:none">
    <h2 data-i18n="wiz_key_title">API key</h2>
    <p data-i18n="wiz_key_body"></p>
    <input id="key" type="password" data-i18n-ph="key_ph" autocomplete="off">
    <details><summary data-i18n="wiz_key_help">How do I get a key?</summary>
      <p class="hint" data-i18n-html="wiz_key_help_body"></p></details>
    <div class="err" id="err2"></div>
    <div class="wiz-actions">
      <button class="ghost" data-i18n="wiz_back" onclick="prevStep()">Back</button>
      <button class="primary" data-i18n="wiz_next" onclick="nextStep()">Next</button></div>
  </div>

  <div class="wiz-step" data-step="3" style="display:none">
    <h2 data-i18n="wiz_url_title">Your website</h2>
    <p data-i18n="wiz_url_body"></p>
    <input id="url" type="text" data-i18n-ph="wiz_url_ph">
    <div class="hint" data-i18n="wiz_url_hint"></div>
    <div class="err" id="err3"></div>
    <div class="wiz-actions">
      <button class="ghost" data-i18n="wiz_back" onclick="prevStep()">Back</button>
      <button class="primary" data-i18n="wiz_finish" onclick="finishWizard()">Finish</button></div>
  </div>
</div>
<script>
/*__I18N__*/
""" + _LANG_BOOT + """
let STEP=1; const WIZ={key:''};
function setLang(l){ LANG=l; localStorage.setItem('lang',l); applyI18n(); }
function gotoStep(n){
  STEP=n;
  document.querySelectorAll('.wiz-step').forEach(el=>{ el.style.display=(+el.dataset.step===n)?'':'none'; });
  document.querySelectorAll('.dots span').forEach(d=>d.classList.toggle('on', +d.dataset.step<=n));
}
function nextStep(){
  if(STEP===2){ const k=document.getElementById('key').value.trim();
    if(!k){ document.getElementById('err2').textContent=t('err_paste'); return; } WIZ.key=k; }
  gotoStep(Math.min(3,STEP+1));
}
function prevStep(){ gotoStep(Math.max(1,STEP-1)); }
async function finishWizard(){
  let url=document.getElementById('url').value.trim();
  if(url && !/^https?:\\/\\//i.test(url)) url='https://'+url;
  const payload={google_api_key:WIZ.key}; if(url) payload.base_url=url;
  const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const d=await r.json();
  if(d.has_api_key){ location.reload(); }
  else { gotoStep(2); document.getElementById('err2').textContent=t('err_savekey'); }
}
document.getElementById('url').addEventListener('keydown',e=>{if(e.key==='Enter')finishWizard();});
applyI18n(); gotoStep(1);
</script></body></html>""").replace("/*__I18N__*/", _I18N_JS)


INDEX_HTML = ("""<!doctype html><html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;700&family=Noto+Sans+Thai:wght@400;500;700&display=swap" rel="stylesheet">
<title data-i18n="doc_title">AI Web Tester</title>
<style>""" + _BASE_CSS + """
header{display:flex;align-items:center;gap:12px;padding:14px 24px;border-bottom:1px solid var(--line);background:var(--surface);position:sticky;top:0;z-index:5}
header h1{font-size:17px;margin:0}
.spacer{flex:1}
.chip-btn{background:var(--bg);border:1px solid var(--line);border-radius:99px;padding:6px 12px;font-size:13px;color:var(--txt);max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip-btn.nudge{background:#fff7ed;border-color:#fed7aa;color:#b45309;font-weight:600}
main{padding:20px 24px;max-width:1000px;margin:0 auto}
.banner{display:flex;align-items:center;gap:16px;background:#eef4ff;border:1px solid #d7e3ff;border-radius:12px;padding:14px 16px;margin-bottom:16px}
.banner .steps{display:flex;gap:18px;flex-wrap:wrap;flex:1}
.banner .st{display:flex;align-items:center;gap:8px;font-size:13px}
.banner .n{display:inline-flex;width:22px;height:22px;border-radius:50%;background:var(--primary);color:#fff;align-items:center;justify-content:center;font-weight:700;font-size:12px}
.banner b{font-size:13px;color:#1e3a8a}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
.toolbar .spacer{flex:1}
.help{margin:0 0 16px;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.help summary{cursor:pointer;font-weight:600}
.help p{margin:8px 0 0}
.card{display:flex;align-items:center;gap:12px;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:10px;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.card-ic{font-size:20px;width:24px;text-align:center}
.card .grow{flex:1;min-width:0}
.card-title{font-weight:600}
.card-desc{color:var(--mut);font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:480px}
.row-actions{display:flex;gap:6px}
.chip{font-size:11px;padding:2px 9px;border-radius:99px;border:1px solid var(--line);white-space:nowrap}
.chip.pass{color:var(--pass);border-color:#bbf7d0;background:#f0fdf4}
.chip.fail{color:var(--fail);border-color:#fecaca;background:#fef2f2}
.chip.off{color:var(--mut)}
.empty{text-align:center;padding:48px 20px;background:var(--surface);border:1px dashed var(--line);border-radius:14px}
.empty-emoji{font-size:40px}
.empty h3{margin:8px 0 4px}
.chips{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin:16px 0}
.tplchip{background:var(--bg);border:1px solid var(--line);border-radius:99px;padding:8px 14px;font-size:13px}
.tplchip:hover{border-color:var(--primary);color:var(--primary)}
.overlay{position:fixed;inset:0;background:rgba(16,24,40,.45);display:none;align-items:center;justify-content:center;padding:20px;z-index:20}
.overlay.show{display:flex}
.modal{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:22px;width:560px;max-width:100%;max-height:90vh;overflow:auto;box-shadow:0 12px 40px rgba(16,24,40,.16)}
.modal h2{margin:0 0 12px;font-size:17px}
label{display:block;margin:12px 0 4px;color:var(--mut);font-size:12px;font-weight:600}
.tip{cursor:help;color:var(--mut);font-size:12px;border:1px solid var(--line);border-radius:50%;width:16px;height:16px;display:inline-flex;align-items:center;justify-content:center}
.idline{color:var(--mut);font-size:12px;margin-top:4px}
.idline code{background:var(--bg);padding:1px 6px;border-radius:4px}
.hint{color:var(--mut);font-size:12px;margin-top:4px}
details.adv{margin-top:14px;border-top:1px solid var(--line);padding-top:8px}
details.adv summary{cursor:pointer;font-weight:600;color:var(--primary)}
.grp{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin:18px 0 2px}
.checks{display:flex;gap:16px;flex-wrap:wrap;margin-top:10px}
.checks label{display:flex;gap:6px;align-items:center;color:var(--txt);font-size:13px;margin:0;font-weight:400}
.modal-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
#progress{display:none;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px}
#progress.show{display:block}
.prow{display:flex;align-items:center;gap:10px;padding:6px 0}
.dot{width:10px;height:10px;border-radius:50%;background:var(--mut)}
.dot.running{background:var(--run);animation:pulse 1s infinite}
.dot.pass{background:var(--pass)} .dot.fail{background:var(--fail)}
@keyframes pulse{50%{opacity:.3}}
.bar{height:6px;background:#eef0f4;border-radius:99px;overflow:hidden;flex:1;max-width:200px}
.bar>i{display:block;height:100%;background:var(--primary);width:0}
iframe{width:100%;height:78vh;border:1px solid var(--line);border-radius:10px;background:#fff;margin-top:8px}
.links{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0}
.links a.linkbtn{text-decoration:none;color:var(--txt);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:9px 14px;font-size:13px}
.links a.linkbtn:hover{border-color:var(--primary);color:var(--primary)}
.about-cause{color:var(--mut)}
</style></head><body>
<header>
  <h1>🌐 AI Web Tester</h1>
  <button class="chip-btn" id="siteChip" onclick="openSettings()"></button>
  <span class="spacer"></span>
  """ + _LANG_SWITCH_HTML + """
  <button class="ghost" data-i18n="about" onclick="openAbout()">About</button>
  <button class="ghost" data-i18n="settings" onclick="openSettings()">Settings</button>
</header>
<main>
  <div class="banner" id="howBanner">
    <b data-i18n="how_title">How it works</b>
    <div class="steps">
      <span class="st"><span class="n">1</span><span data-i18n="how_1"></span></span>
      <span class="st"><span class="n">2</span><span data-i18n="how_2"></span></span>
      <span class="st"><span class="n">3</span><span data-i18n="how_3"></span></span>
    </div>
    <button class="ghost sm" data-i18n="how_dismiss" onclick="dismissBanner()">Got it</button>
  </div>

  <div class="toolbar">
    <button class="primary" id="runAllBtn" data-i18n="run_all" onclick="run('all')">Run all</button>
    <button class="danger" id="stopBtn" data-i18n="stop" style="display:none" onclick="stopRun()">Stop</button>
    <label class="checks" style="margin:0"><input type="checkbox" id="showBrowser"> <span data-i18n="show_browser">Show browser</span></label>
    <span class="spacer"></span>
    <button data-i18n="new_check" onclick="openNew()">New check</button>
  </div>

  <details class="help">
    <summary data-i18n="results_help_title">What do the results mean?</summary>
    <p class="muted" data-i18n-html="results_help_body"></p>
  </details>

  <div id="progress"></div>
  <div id="reportWrap" style="display:none">
    <div class="toolbar"><b data-i18n="report">Report</b><span class="spacer"></span>
      <a href="/report" target="_blank"><button class="ghost sm" data-i18n="open_new_tab">Open in new tab</button></a></div>
    <iframe id="report" src="about:blank"></iframe>
  </div>

  <div id="list"></div>
</main>

<div class="overlay" id="editOverlay">
  <div class="modal">
    <h2 id="editTitle">New check</h2>
    <label data-i18n="check_q">What should we check?</label>
    <textarea id="f_desc" data-i18n-ph="check_q_ph"></textarea>
    <div class="hint" data-i18n="desc_hint"></div>
    <label data-i18n="short_name">Short name</label>
    <input type="text" id="f_title" data-i18n-ph="short_name_ph" oninput="updateIdPreview()">
    <div class="idline"><span data-i18n="id_auto">id:</span> <code id="idPreview"></code></div>
    <details class="adv">
      <summary data-i18n="advanced">Advanced</summary>
      <label data-i18n="start_label">Start URL</label>
      <input type="text" id="f_start" data-i18n-ph="start_ph">
      <div class="checks">
        <label><input type="checkbox" id="f_enabled" checked> <span data-i18n="incl_runall">Include in Run all</span></label>
        <label><input type="checkbox" id="f_login"> <span data-i18n="start_login">Start at Login URL first</span></label>
      </div>
      <label data-i18n="max_steps">Max steps</label>
      <input type="number" id="f_steps" value="20" min="1" max="100" style="width:120px">
    </details>
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
    <div class="grp" data-i18n="grp_site">Your website</div>
    <label><span data-i18n="base_label">Base URL</span> <span class="tip" data-i18n-title="tip_base">i</span></label>
    <input type="text" id="s_base" data-i18n-ph="base_ph">
    <div class="grp" data-i18n="grp_ai">AI</div>
    <label><span data-i18n="s_key_label">Gemini API key</span> <span class="muted" id="keyState"></span> <span class="tip" data-i18n-title="tip_key">i</span></label>
    <input type="password" id="s_key" data-i18n-ph="keep_current" autocomplete="off">
    <label><span data-i18n="model_label">Model</span> <span class="tip" data-i18n-title="tip_model">i</span></label>
    <select id="s_model"><option>gemini-2.5-flash</option><option>gemini-2.5-pro</option></select>
    <details class="adv">
      <summary data-i18n="grp_advanced">Advanced</summary>
      <label><span data-i18n="login_label">Login URL</span> <span class="muted" id="loginState"></span> <span class="tip" data-i18n-title="tip_login">i</span></label>
      <input type="password" id="s_login" data-i18n-ph="keep_current">
      <label><span data-i18n="hint_label">Context hint</span> <span class="tip" data-i18n-title="tip_hint">i</span></label>
      <textarea id="s_hint" style="min-height:60px" data-i18n-ph="hint_ph"></textarea>
      <div class="two">
        <div><label><span data-i18n="vw_label">Viewport width</span> <span class="tip" data-i18n-title="tip_viewport">i</span></label><input type="number" id="s_vw"></div>
        <div><label data-i18n="vh_label">Viewport height</label><input type="number" id="s_vh"></div>
      </div>
      <div class="two">
        <div><label><span data-i18n="ds_label">Device scale</span> <span class="tip" data-i18n-title="tip_scale">i</span></label><input type="number" id="s_ds" step="0.5"></div>
        <div><label><span data-i18n="ua_label">User agent</span> <span class="tip" data-i18n-title="tip_ua">i</span></label><input type="text" id="s_ua" data-i18n-ph="ua_ph"></div>
      </div>
    </details>
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
const TEMPLATES=[
  {id:'homepage_loads', title:'tpl_home_title', desc:'tpl_home_desc', requires_login:false, max_steps:10, start_url:''},
  {id:'login_works',    title:'tpl_login_title',desc:'tpl_login_desc',requires_login:true,  max_steps:18, start_url:''},
  {id:'search_works',   title:'tpl_search_title',desc:'tpl_search_desc',requires_login:false,max_steps:15, start_url:''},
  {id:'mobile_layout',  title:'tpl_mobile_title',desc:'tpl_mobile_desc',requires_login:false,max_steps:12, start_url:''},
];
let TESTS=[], POLL=null, EDIT_NAME=null, LAST_STATUS=null;

function setLang(l){ LANG=l; localStorage.setItem('lang',l); applyI18n(); loadTests(); refreshSiteChip();
  if(LAST_STATUS) renderProgress(LAST_STATUS); }

function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

/* ---------- site chip & banner ---------- */
async function refreshSiteChip(){
  try{
    const s=await (await fetch('/api/settings')).json();
    const chip=document.getElementById('siteChip');
    let host=''; try{ host=new URL(s.base_url).host; }catch(e){}
    const isDefault=!s.base_url || s.base_url==='https://example.com';
    if(isDefault){ chip.textContent='⚠ '+t('set_site_nudge'); chip.classList.add('nudge'); }
    else { chip.textContent=t('testing_label')+' ▸ '+host; chip.classList.remove('nudge'); }
  }catch(e){}
}
function initBanner(){ if(localStorage.getItem('hideHowBanner')) document.getElementById('howBanner').style.display='none'; }
function dismissBanner(){ localStorage.setItem('hideHowBanner','1'); document.getElementById('howBanner').style.display='none'; }

/* ---------- checks list ---------- */
function lastResultFor(title){
  if(!LAST_STATUS||!LAST_STATUS.tests) return null;
  const x=LAST_STATUS.tests.find(z=>z.name===title);
  return x && (x.status==='pass'||x.status==='fail') ? x.status : null;
}
async function loadTests(){
  TESTS = await (await fetch('/api/tests')).json();
  const list=document.getElementById('list');
  if(!TESTS.length){
    list.innerHTML=`<div class="empty"><div class="empty-emoji">🎯</div>
      <h3>${esc(t('no_checks_title'))}</h3>
      <p class="muted">${esc(t('start_example'))}</p>
      <div class="chips">${TEMPLATES.map(tp=>`<button class="tplchip" onclick="createFromTemplate('${tp.id}')">${esc(t(tp.title))}</button>`).join('')}</div>
      <button class="primary" onclick="openNew()">${esc(t('write_own'))}</button></div>`;
    return;
  }
  list.innerHTML = TESTS.map(x=>{
    const res=lastResultFor(x.title);
    const ic = res==='pass'?'✅':res==='fail'?'❌':'⬜';
    const chip = res==='pass'?`<span class="chip pass">${t('chip_pass')}</span>`
               : res==='fail'?`<span class="chip fail">${t('chip_fail')}</span>`
               : `<span class="chip off">${t('chip_notrun')}</span>`;
    const off = x.enabled?'':` <span class="chip off">${t('disabled_badge')}</span>`;
    return `<div class="card">
      <div class="card-ic">${ic}</div>
      <div class="grow">
        <div class="card-title">${esc(x.title)}${off}</div>
        <div class="card-desc">${esc(x.description)}</div>
      </div>
      ${chip}
      <div class="row-actions">
        <button class="primary sm" onclick="run('one','${x.name}')">${t('run')}</button>
        <button class="ghost sm" onclick="openEdit('${x.name}')">${t('edit')}</button>
        <button class="ghost sm" onclick="delTest('${x.name}')">🗑</button>
      </div></div>`;
  }).join('');
}

/* ---------- run / stop / progress ---------- */
async function run(mode,one){
  const names = mode==='one' ? [one] : null;
  const headless=!document.getElementById('showBrowser').checked;
  const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({names,headless,lang:LANG})});
  if(r.status===409){alert(t('alert_busy'));return;}
  const d=await r.json();
  if(d.error){alert(d.error);return;}
  document.getElementById('reportWrap').style.display='none';
  setRunning(true); startPolling();
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
  if(POLL){ clearInterval(POLL); POLL=null; }
  setRunning(false);
  loadTests();  // refresh card status icons/chips
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

/* ---------- create / edit ---------- */
function slugify(s){
  s=(s||'').toLowerCase().trim().normalize('NFD').replace(/[\\u0300-\\u036f]/g,'');
  s=s.replace(/[^a-z0-9_-]+/g,'_').replace(/_+/g,'_').replace(/^[_-]+|[_-]+$/g,'');
  if(!s) s='check';
  return s.slice(0,40);
}
function uniqueSlug(base){
  const names=new Set(TESTS.map(x=>x.name));
  if(!names.has(base)) return base;
  for(let i=2;i<100;i++){ if(!names.has(base+'-'+i)) return base+'-'+i; }
  return base+'-'+(TESTS.length+1);
}
function proposedId(){
  const seed = val('f_title') || (val('f_desc')||'').split(/\\s+/).slice(0,4).join(' ');
  return uniqueSlug(slugify(seed));
}
function updateIdPreview(){
  document.getElementById('idPreview').textContent = EDIT_NAME || proposedId();
}
function openNew(){
  EDIT_NAME=null;
  document.getElementById('editTitle').textContent=t('new_check_title');
  set('f_desc','');set('f_title','');set('f_start','');setNum('f_steps',20);
  check('f_enabled',true);check('f_login',false);
  document.getElementById('editErr').textContent='';
  updateIdPreview();
  show('editOverlay');
  setTimeout(()=>document.getElementById('f_desc').focus(),50);
}
function openEdit(name){
  const x=TESTS.find(z=>z.name===name); if(!x)return;
  EDIT_NAME=name;
  document.getElementById('editTitle').textContent=t('edit_prefix')+x.title;
  set('f_desc',x.description);set('f_title',x.title);set('f_start',x.start_url);setNum('f_steps',x.max_steps);
  check('f_enabled',x.enabled);check('f_login',x.requires_login);
  document.getElementById('editErr').textContent='';
  updateIdPreview();
  show('editOverlay');
}
async function postCheck(payload){
  for(let i=0;i<5;i++){
    const r=await fetch('/api/tests',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    if(r.status===409){ payload.name=payload.name.replace(/-\\d+$/,'')+'-'+(i+2); continue; }
    return r;
  }
  return null;
}
async function saveTest(){
  const desc=val('f_desc'), title=val('f_title');
  const err=document.getElementById('editErr'); err.textContent='';
  if(!desc.trim()){ err.textContent=t('err_desc'); return; }
  const base={title:title, description:desc, start_url:val('f_start'),
    enabled:chk('f_enabled'), requires_login:chk('f_login'),
    max_steps:parseInt(val('f_steps')||'20',10)};
  let r;
  if(EDIT_NAME){ base.name=EDIT_NAME;
    r=await fetch('/api/tests/'+EDIT_NAME,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(base)});
  } else {
    base.name=proposedId();
    r=await postCheck(base);
  }
  if(!r || !r.ok){ let d={}; try{ d=await r.json(); }catch(e){} err.textContent=(d&&d.error)||t('err_save'); return; }
  closeModal('editOverlay'); loadTests();
}
async function delTest(name){
  if(!confirm(t('confirm_delete').replace('{name}',name)))return;
  await fetch('/api/tests/'+name,{method:'DELETE'}); loadTests();
}
async function createFromTemplate(id){
  const tp=TEMPLATES.find(z=>z.id===id); if(!tp)return;
  const payload={name:uniqueSlug(tp.id), title:t(tp.title), description:t(tp.desc),
    enabled:true, requires_login:tp.requires_login, max_steps:tp.max_steps, start_url:tp.start_url};
  await postCheck(payload); loadTests();
}

/* ---------- settings ---------- */
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
  closeModal('setOverlay'); refreshSiteChip();
}

/* ---------- helpers ---------- */
function openAbout(){show('aboutOverlay');}
function show(id){document.getElementById(id).classList.add('show');}
function closeModal(id){document.getElementById(id).classList.remove('show');}
function val(id){return document.getElementById(id).value;}
function set(id,v){document.getElementById(id).value=v||'';}
function setNum(id,v){document.getElementById(id).value=v;}
function chk(id){return document.getElementById(id).checked;}
function check(id,v){document.getElementById(id).checked=!!v;}

applyI18n();
initBanner();
refreshSiteChip();
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
