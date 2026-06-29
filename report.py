"""
Self-contained HTML report builder for Browser Use tests.

Screenshots are embedded as base64 data URIs, so the report is a single HTML
file you can open or email — no external image files needed.
"""

from __future__ import annotations

import base64
import html
import json
from dataclasses import dataclass, field
from pathlib import Path

EXTRACT_CAP = 4000  # max chars of extracted page content to embed per step


@dataclass
class StepData:
    index: int
    url: str | None
    screenshot_b64: str | None
    thinking: str | None
    evaluation: str | None
    next_goal: str | None
    memory: str | None
    actions: list[str]
    error: str | None
    extracted: str | None = None  # page content the agent extracted this step


@dataclass
class TestEntry:
    name: str
    passed: bool
    summary: str
    details: str = ""
    duration: float = 0.0
    error: str | None = None  # set if the test crashed before producing a result
    started_at: str = ""      # wall-clock time the test began
    gif_b64: str | None = None  # base64 of the run animation, if captured
    steps: list[StepData] = field(default_factory=list)


def gif_data_uri(path: str | Path) -> str | None:
    """Read a GIF file and return it as base64, or None if missing/empty."""
    p = Path(path)
    if not p.is_file() or p.stat().st_size == 0:
        return None
    return base64.b64encode(p.read_bytes()).decode("ascii")


def steps_from_history(history) -> list[StepData]:
    """Pull per-step data out of a browser-use AgentHistoryList."""
    screenshots = history.screenshots(return_none_if_not_screenshot=True)
    urls = history.urls()
    thoughts = history.model_thoughts()
    actions_per_step = history.action_history()  # list[list[dict]]
    errors = history.errors()
    items = history.history  # AgentHistory items, aligned with the lists above

    n = max(len(screenshots), len(urls), len(thoughts), len(actions_per_step), len(errors), len(items))

    def get(seq, i):
        return seq[i] if i < len(seq) else None

    steps: list[StepData] = []
    for i in range(n):
        brain = get(thoughts, i)
        raw_actions = get(actions_per_step, i) or []
        actions = [_format_action(a) for a in raw_actions]
        steps.append(
            StepData(
                index=i + 1,
                url=get(urls, i),
                screenshot_b64=get(screenshots, i),
                thinking=getattr(brain, "thinking", None),
                evaluation=getattr(brain, "evaluation_previous_goal", None),
                next_goal=getattr(brain, "next_goal", None),
                memory=getattr(brain, "memory", None),
                actions=actions,
                error=get(errors, i),
                extracted=_extracted_for_item(get(items, i)),
            )
        )
    return steps


def _extracted_for_item(item) -> str | None:
    """Collect extracted_content from an AgentHistory item's action results."""
    if item is None or not getattr(item, "result", None):
        return None
    chunks = [r.extracted_content for r in item.result if getattr(r, "extracted_content", None)]
    if not chunks:
        return None
    text = "\n".join(chunks).strip()
    if len(text) > EXTRACT_CAP:
        text = text[:EXTRACT_CAP] + f"\n… (truncated, {len(text) - EXTRACT_CAP} more chars)"
    return text or None


def _format_action(action: dict) -> str:
    """Turn {'click_element_by_index': {'index': 5}} into 'click_element_by_index(index=5)'."""
    if not isinstance(action, dict) or not action:
        return str(action)
    name, params = next(iter(action.items()))
    if isinstance(params, dict):
        inner = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in params.items())
        return f"{name}({inner})"
    return f"{name}({params})"


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #

# Structural labels for the report, per language. The agent's own output
# (summaries, reasoning) is in whatever language the model produced.
LABELS = {
    "en": {
        "generated": "Generated", "pass": "PASS", "fail": "FAIL", "tests": "tests",
        "passed": "passed", "failed": "failed", "details": "Details:",
        "crashed": "Crashed:", "run_gif": "▶ Run animation (GIF)", "steps": "steps",
        "step": "Step", "evaluation": "evaluation", "thinking": "thinking",
        "next_goal": "next goal", "actions": "actions", "error": "error",
        "extracted": "extracted", "page_content": "page content", "chars": "chars",
        "no_screenshot": "no screenshot",
    },
    "th": {
        "generated": "สร้างเมื่อ", "pass": "ผ่าน", "fail": "ไม่ผ่าน", "tests": "เทสต์",
        "passed": "ผ่าน", "failed": "ไม่ผ่าน", "details": "รายละเอียด:",
        "crashed": "ขัดข้อง:", "run_gif": "▶ ภาพเคลื่อนไหว (GIF)", "steps": "ขั้นตอน",
        "step": "ขั้นตอนที่", "evaluation": "การประเมิน", "thinking": "การวิเคราะห์",
        "next_goal": "เป้าหมายถัดไป", "actions": "การกระทำ", "error": "ข้อผิดพลาด",
        "extracted": "เนื้อหา", "page_content": "เนื้อหาหน้าเว็บ", "chars": "ตัวอักษร",
        "no_screenshot": "ไม่มีภาพหน้าจอ",
    },
}


def render_report(entries: list[TestEntry], out_path: str | Path, *,
                  generated_at: str, title: str, lang: str = "th") -> Path:
    out_path = Path(out_path)
    lab = LABELS.get(lang, LABELS["th"])
    total = len(entries)
    passed = sum(1 for e in entries if e.passed)
    failed = total - passed

    body = [_render_summary(title, generated_at, total, passed, failed, lab)]
    for e in entries:
        body.append(_render_test(e, lab))

    doc = _PAGE.format(title=html.escape(title), lang=html.escape(lang),
                       style=_STYLE, script=_SCRIPT, body="\n".join(body))
    out_path.write_text(doc, encoding="utf-8")
    return out_path


def _render_summary(title, generated_at, total, passed, failed, lab) -> str:
    overall = lab["pass"] if failed == 0 else lab["fail"]
    cls = "pass" if failed == 0 else "fail"
    return f"""
    <header>
      <h1>{html.escape(title)}</h1>
      <div class="meta">{lab['generated']} {html.escape(generated_at)}</div>
      <div class="badges">
        <span class="badge {cls}">{overall}</span>
        <span class="badge total">{total} {lab['tests']}</span>
        <span class="badge pass">{passed} {lab['passed']}</span>
        <span class="badge fail">{failed} {lab['failed']}</span>
      </div>
    </header>"""


def _render_test(e: TestEntry, lab) -> str:
    cls = "pass" if e.passed else "fail"
    status = lab["pass"] if e.passed else lab["fail"]
    parts = [f"""
    <section class="test {cls}">
      <div class="test-head" onclick="this.parentElement.classList.toggle('collapsed')">
        <span class="status {cls}">{status}</span>
        <span class="test-name">{html.escape(e.name)}</span>
        <span class="test-summary">{html.escape(e.summary or '')}</span>
        <span class="test-time">{html.escape(e.started_at)}</span>
        <span class="test-dur">{e.duration:.1f}s · {len(e.steps)} {lab['steps']}</span>
      </div>
      <div class="test-body">"""]

    if e.details:
        parts.append(f'<div class="details"><strong>{lab["details"]}</strong> {html.escape(e.details)}</div>')
    if e.error:
        parts.append(f'<div class="crash"><strong>{lab["crashed"]}</strong> <pre>{html.escape(e.error)}</pre></div>')
    if e.gif_b64:
        parts.append(
            f'<details class="gifbox"><summary>{lab["run_gif"]}</summary>'
            f'<img loading="lazy" src="data:image/gif;base64,{e.gif_b64}" alt="run animation"></details>'
        )

    parts.append('<div class="steps">')
    for s in e.steps:
        parts.append(_render_step(s, lab))
    parts.append("</div></div></section>")
    return "".join(parts)


def _render_step(s: StepData, lab) -> str:
    img = ""
    if s.screenshot_b64:
        src = f"data:image/png;base64,{s.screenshot_b64}"
        img = f'<img loading="lazy" src="{src}" onclick="zoom(this.src)" alt="step {s.index}">'
    else:
        img = f'<div class="noshot">{lab["no_screenshot"]}</div>'

    def row(label, value, klass=""):
        if not value:
            return ""
        return f'<div class="kv {klass}"><span class="k">{label}</span><span class="v">{html.escape(str(value))}</span></div>'

    actions_html = ""
    if s.actions:
        items = "".join(f"<li><code>{html.escape(a)}</code></li>" for a in s.actions)
        actions_html = f'<div class="kv"><span class="k">{lab["actions"]}</span><ul class="actions">{items}</ul></div>'

    err_html = row(lab["error"], s.error, "err") if s.error else ""

    extracted_html = ""
    if s.extracted:
        extracted_html = (
            f'<div class="kv"><span class="k">{lab["extracted"]}</span>'
            f'<details class="extracted"><summary>{lab["page_content"]} ({len(s.extracted)} {lab["chars"]})</summary>'
            f'<pre>{html.escape(s.extracted)}</pre></details></div>'
        )

    return f"""
      <div class="step">
        <div class="shot">{img}</div>
        <div class="info">
          <div class="step-no">{lab['step']} {s.index}{f' · <a href="{html.escape(s.url)}" target="_blank">{html.escape(_short_url(s.url))}</a>' if s.url else ''}</div>
          {row(lab["evaluation"], s.evaluation)}
          {row(lab["thinking"], s.thinking)}
          {row(lab["next_goal"], s.next_goal)}
          {actions_html}
          {extracted_html}
          {err_html}
        </div>
      </div>"""


def _short_url(url: str | None, n: int = 70) -> str:
    if not url:
        return ""
    return url if len(url) <= n else url[: n - 1] + "…"


_STYLE = """
:root { --bg:#0f1115; --card:#171a21; --line:#262b36; --txt:#e6e8ec; --mut:#9aa3b2;
        --pass:#2ecc71; --fail:#ff5c5c; --accent:#5b9dff; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--txt);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
header { padding:24px 28px; border-bottom:1px solid var(--line); }
h1 { margin:0 0 4px; font-size:20px; }
.meta { color:var(--mut); font-size:12px; }
.badges { margin-top:12px; display:flex; gap:8px; flex-wrap:wrap; }
.badge { padding:3px 10px; border-radius:99px; font-weight:600; font-size:12px; background:var(--card); border:1px solid var(--line); }
.badge.pass { color:var(--pass); } .badge.fail { color:var(--fail); } .badge.total { color:var(--accent); }
.test { margin:18px 28px; background:var(--card); border:1px solid var(--line); border-left:4px solid var(--mut); border-radius:8px; overflow:hidden; }
.test.pass { border-left-color:var(--pass); } .test.fail { border-left-color:var(--fail); }
.test-head { display:flex; align-items:center; gap:12px; padding:14px 16px; cursor:pointer; user-select:none; }
.test-head:hover { background:#1c2029; }
.status { font-weight:700; font-size:12px; padding:2px 8px; border-radius:4px; }
.status.pass { background:rgba(46,204,113,.15); color:var(--pass); }
.status.fail { background:rgba(255,92,92,.15); color:var(--fail); }
.test-name { font-weight:600; }
.test-summary { color:var(--mut); flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.test-time { color:var(--mut); font-size:12px; white-space:nowrap; font-variant-numeric:tabular-nums; }
.test-dur { color:var(--mut); font-size:12px; white-space:nowrap; }
.gifbox { margin:10px 0; }
.gifbox summary { cursor:pointer; color:var(--accent); padding:8px 0; }
.gifbox img { max-width:100%; border:1px solid var(--line); border-radius:6px; margin-top:8px; }
.extracted { flex:1; }
.extracted summary { cursor:pointer; color:var(--accent); }
.extracted pre { white-space:pre-wrap; word-break:break-word; background:#11141a; padding:10px; border-radius:6px; margin:6px 0 0; max-height:320px; overflow:auto; font-size:12px; }
.test.collapsed .test-body { display:none; }
.test-body { padding:0 16px 16px; }
.details, .crash { padding:10px 12px; margin:8px 0; background:#11141a; border-radius:6px; font-size:13px; }
.crash pre { white-space:pre-wrap; color:var(--fail); margin:6px 0 0; }
.step { display:grid; grid-template-columns:340px 1fr; gap:16px; padding:16px 0; border-top:1px solid var(--line); }
.shot img { width:100%; border:1px solid var(--line); border-radius:6px; cursor:zoom-in; display:block; }
.noshot { color:var(--mut); padding:40px; text-align:center; border:1px dashed var(--line); border-radius:6px; }
.step-no { font-weight:600; margin-bottom:8px; }
.step-no a { color:var(--accent); text-decoration:none; font-weight:400; }
.kv { display:flex; gap:10px; margin:6px 0; }
.kv .k { color:var(--mut); min-width:84px; text-transform:uppercase; font-size:11px; letter-spacing:.04em; padding-top:2px; }
.kv .v { flex:1; }
.kv.err .v { color:var(--fail); }
.actions { margin:0; padding-left:0; list-style:none; }
.actions code { background:#11141a; padding:2px 6px; border-radius:4px; font-size:12px; }
#lightbox { position:fixed; inset:0; background:rgba(0,0,0,.9); display:none; align-items:center; justify-content:center; cursor:zoom-out; z-index:99; }
#lightbox img { max-width:95vw; max-height:95vh; border-radius:6px; }
@media (max-width:720px){ .step{ grid-template-columns:1fr; } }
"""

_SCRIPT = """
function zoom(src){ var lb=document.getElementById('lightbox'); lb.querySelector('img').src=src; lb.style.display='flex'; }
document.addEventListener('DOMContentLoaded',function(){
  var lb=document.getElementById('lightbox'); lb.addEventListener('click',function(){ lb.style.display='none'; });
});
"""

_PAGE = """<!doctype html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{style}</style>
</head>
<body>
{body}
<div id="lightbox"><img alt="zoom"></div>
<script>{script}</script>
</body>
</html>
"""
