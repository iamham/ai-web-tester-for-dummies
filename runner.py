"""
Shared core for the AI Web Tester.

Both the CLI (`cli.py`) and the web app (`app.py`) import from here, so test
execution behaves identically no matter how it's launched.

An LLM (Google Gemini) drives a real Chromium browser to carry out test cases
written in plain English. Test cases live as YAML files in `tests/` — see
`tests/_TEMPLATE.yaml`.
"""

from __future__ import annotations

import dataclasses
import os
import re
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

from browser_use import Agent, Browser, ChatGoogle
from report import TestEntry, gif_data_uri, render_report, steps_from_history

TESTS_DIR = Path(__file__).parent / "tests"
NAME_RE = re.compile(r"^[a-z0-9_][a-z0-9_-]*$")

# Control handle for the active run, so callers can stop it mid-flight.
_RUN_CTL: dict = {"agent": None, "stop": False}

STOP_MSG = {"en": "Stopped by user", "th": "ถูกหยุดโดยผู้ใช้"}


def request_stop() -> None:
    """Ask the current run to stop gracefully (stops after the current step)."""
    _RUN_CTL["stop"] = True
    agent = _RUN_CTL["agent"]
    if agent is not None:
        try:
            agent.stop()
        except Exception:
            pass


def is_stop_requested() -> bool:
    return _RUN_CTL["stop"]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    base_url: str
    login_url: str | None
    google_api_key: str | None
    llm_model: str
    headless: bool
    viewport_width: int
    viewport_height: int
    device_scale: float
    user_agent: str | None
    context_hint: str
    allowed_domains: list[str] | None
    report_path: str
    output_lang: str


def load_config() -> Config:
    """Read configuration from environment / .env."""
    load_dotenv(override=True)
    allowed_raw = os.environ.get("ALLOWED_DOMAINS", "").strip()
    return Config(
        base_url=os.environ.get("BASE_URL", "https://example.com").rstrip("/"),
        login_url=(os.environ.get("LOGIN_URL") or None),
        google_api_key=os.environ.get("GOOGLE_API_KEY") or None,
        llm_model=os.environ.get("LLM_MODEL", "gemini-2.5-flash"),
        headless=os.environ.get("HEADLESS") == "1",
        viewport_width=int(os.environ.get("VIEWPORT_WIDTH", "1280")),
        viewport_height=int(os.environ.get("VIEWPORT_HEIGHT", "800")),
        device_scale=float(os.environ.get("DEVICE_SCALE", "1")),
        user_agent=(os.environ.get("USER_AGENT") or None),
        context_hint=os.environ.get("CONTEXT_HINT", ""),
        allowed_domains=[d.strip() for d in allowed_raw.split(",") if d.strip()] or None,
        report_path=os.environ.get("REPORT_PATH", "report.html"),
        output_lang=os.environ.get("OUTPUT_LANG", "th"),
    )


def config_status(cfg: Config) -> dict:
    """Booleans the UI can show without ever exposing secret values."""
    def _set(v: str | None) -> bool:
        return bool(v) and not v.startswith("your-")
    return {
        "has_api_key": _set(cfg.google_api_key),
        "has_login_url": bool(cfg.login_url),
    }


def derive_allowed_domains(cfg: Config) -> list[str] | None:
    """Restrict the agent to the site under test (apex + subdomains).

    Falls back to None (no restriction) when no host can be determined.
    """
    if cfg.allowed_domains:
        return cfg.allowed_domains
    domains: set[str] = set()
    for url in (cfg.base_url, cfg.login_url):
        if not url:
            continue
        host = urlparse(url).hostname
        if not host:
            continue
        labels = host.split(".")
        apex = ".".join(labels[-2:]) if len(labels) >= 2 else host
        domains.add(apex)
        domains.add(f"*.{apex}")
    return sorted(domains) or None


# --------------------------------------------------------------------------- #
# Test cases (YAML)
# --------------------------------------------------------------------------- #

@dataclass
class TestCase:
    name: str
    title: str = ""
    description: str = ""
    enabled: bool = True
    requires_login: bool = False
    start_url: str = ""
    max_steps: int = 20

    def display_title(self) -> str:
        return self.title or self.name

    def to_yaml_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title or self.name,
            "description": self.description,
            "enabled": self.enabled,
            "requires_login": self.requires_login,
            "start_url": self.start_url,
            "max_steps": self.max_steps,
        }


def valid_name(name: str) -> bool:
    return bool(name) and not name.startswith("_") and bool(NAME_RE.match(name))


def _path_for(name: str) -> Path:
    return TESTS_DIR / f"{name}.yaml"


def load_tests(tests_dir: Path = TESTS_DIR) -> list[TestCase]:
    """Load every test case (sorted by name); files starting with '_' are ignored."""
    cases: list[TestCase] = []
    if not tests_dir.is_dir():
        return cases
    for path in sorted(tests_dir.glob("*.yaml")):
        if path.stem.startswith("_"):
            continue
        cases.append(_case_from_path(path))
    return cases


def load_test(name: str) -> TestCase:
    path = _path_for(name)
    if not path.is_file():
        raise FileNotFoundError(f"No such test: {name}")
    return _case_from_path(path)


def _case_from_path(path: Path) -> TestCase:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return TestCase(
        name=data.get("name", path.stem),
        title=data.get("title", ""),
        description=(data.get("description") or "").strip(),
        enabled=bool(data.get("enabled", True)),
        requires_login=bool(data.get("requires_login", False)),
        start_url=(data.get("start_url") or "").strip(),
        max_steps=int(data.get("max_steps", 20)),
    )


def save_test(tc: TestCase) -> Path:
    if not valid_name(tc.name):
        raise ValueError(
            "Invalid name. Use lowercase letters, numbers, '_' or '-', "
            "and don't start with '_'."
        )
    TESTS_DIR.mkdir(exist_ok=True)
    path = _path_for(tc.name)
    path.write_text(
        yaml.safe_dump(tc.to_yaml_dict(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def delete_test(name: str) -> None:
    path = _path_for(name)
    if path.is_file():
        path.unlink()


# --------------------------------------------------------------------------- #
# Task composition (context hint + starting navigation + scenario)
# --------------------------------------------------------------------------- #

def start_url_for(tc: TestCase, cfg: Config) -> str:
    """Where the agent should navigate before carrying out the scenario."""
    if tc.start_url:
        return tc.start_url
    if tc.requires_login and cfg.login_url:
        return cfg.login_url
    return cfg.base_url


def compose_task(tc: TestCase, cfg: Config, lang: str = "th") -> str:
    """Build the full agent prompt from a test case's plain-English description."""
    parts: list[str] = []
    if cfg.context_hint.strip():
        parts.append(cfg.context_hint.strip() + " ")
    start = start_url_for(tc, cfg)
    if start:
        parts.append(
            f"First, go to {start} and wait for the page to fully load. "
            "Then do the following: "
        )
    parts.append(tc.description.strip())
    if lang == "th":
        parts.append(
            "\n\nIMPORTANT: Write the 'summary' and 'details' fields of your final "
            "result in Thai language (ภาษาไทย)."
        )
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #

class TestResult(BaseModel):
    """Structured agent output so we can assert like a normal test."""
    passed: bool
    summary: str
    details: str = ""


def make_llm(cfg: Config) -> ChatGoogle:
    return ChatGoogle(model=cfg.llm_model)


def make_browser(cfg: Config) -> Browser:
    kwargs = dict(
        headless=cfg.headless,
        viewport={"width": cfg.viewport_width, "height": cfg.viewport_height},
        window_size={"width": cfg.viewport_width, "height": cfg.viewport_height},
        device_scale_factor=cfg.device_scale,
        no_viewport=False,
    )
    allowed = derive_allowed_domains(cfg)
    if allowed:
        kwargs["allowed_domains"] = allowed
    if cfg.user_agent:
        kwargs["user_agent"] = cfg.user_agent
    return Browser(**kwargs)


async def run_test(
    tc: TestCase,
    cfg: Config,
    *,
    on_step: Callable[[int], None] | None = None,
    lang: str = "th",
) -> TestEntry:
    """Run one test case and return a report entry. Never raises."""
    print(f"\n{'=' * 70}\n▶  {tc.name}\n{'=' * 70}")
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    Path("artifacts").mkdir(exist_ok=True)
    gif_path = f"artifacts/{tc.name}.gif"

    step_counter = {"n": 0}

    async def _on_step_end(_agent) -> None:
        step_counter["n"] += 1
        if on_step:
            on_step(step_counter["n"])

    try:
        agent = Agent(
            task=compose_task(tc, cfg, lang),
            llm=make_llm(cfg),
            browser=make_browser(cfg),
            output_model_schema=TestResult,
            generate_gif=gif_path,
            save_conversation_path=f"conversations/{tc.name}",
        )
        _RUN_CTL["agent"] = agent
        try:
            history = await agent.run(max_steps=tc.max_steps, on_step_end=_on_step_end)
        finally:
            _RUN_CTL["agent"] = None
        result = history.get_structured_output(TestResult)
        steps = steps_from_history(history)
        duration = history.total_duration_seconds()
        gif_b64 = gif_data_uri(gif_path)

        if _RUN_CTL["stop"]:  # user pressed Stop during this test
            entry = TestEntry(
                name=tc.display_title(), passed=False,
                summary=STOP_MSG.get(lang, STOP_MSG["en"]),
                duration=duration, started_at=started_at, gif_b64=gif_b64, steps=steps,
            )
        elif result is None:
            entry = TestEntry(
                name=tc.display_title(), passed=False,
                summary="Agent finished without a structured result",
                details=history.final_result() or "",
                duration=duration, started_at=started_at, gif_b64=gif_b64, steps=steps,
            )
        else:
            entry = TestEntry(
                name=tc.display_title(), passed=result.passed, summary=result.summary,
                details=result.details, duration=duration, started_at=started_at,
                gif_b64=gif_b64, steps=steps,
            )
    except Exception as exc:  # keep the suite going; record the crash
        if _RUN_CTL["stop"]:  # interruption from a user Stop, not a real failure
            entry = TestEntry(
                name=tc.display_title(), passed=False,
                summary=STOP_MSG.get(lang, STOP_MSG["en"]),
                started_at=started_at, gif_b64=gif_data_uri(gif_path),
            )
        else:
            entry = TestEntry(
                name=tc.display_title(), passed=False, summary="Test crashed",
                started_at=started_at, gif_b64=gif_data_uri(gif_path),
                error="".join(traceback.format_exception(exc)),
            )

    status = "✅ PASS" if entry.passed else "❌ FAIL"
    print(f"{status}  {tc.name}: {entry.summary}")
    if entry.details:
        print(f"        details: {entry.details}")
    return entry


REPORT_TITLES = {
    "en": "AI Web Tester — Report",
    "th": "AI Web Tester — รายงานผลการทดสอบ",
}


async def run_suite(
    names: list[str] | None,
    cfg: Config,
    *,
    on_event: Callable[[dict], None] | None = None,
    lang: str | None = None,
) -> tuple[list[TestEntry], Path]:
    """Run selected tests (or all enabled if names is None) and write the report."""
    lang = lang or cfg.output_lang
    _RUN_CTL["stop"] = False  # clear any stop request from a previous run
    all_cases = {tc.name: tc for tc in load_tests()}
    if names is None:
        selected = [tc for tc in all_cases.values() if tc.enabled]
    else:
        selected = [all_cases[n] for n in names if n in all_cases]

    entries: list[TestEntry] = []
    for i, tc in enumerate(selected):
        if _RUN_CTL["stop"]:  # user stopped between tests — don't start the next one
            break
        if on_event:
            on_event({"type": "start", "index": i, "name": tc.display_title(),
                      "max_steps": tc.max_steps})

        def _step(k: int, _i=i):
            if on_event:
                on_event({"type": "step", "index": _i, "step": k})

        entry = await run_test(tc, cfg, on_step=_step, lang=lang)
        entries.append(entry)
        if on_event:
            on_event({"type": "end", "index": i, "passed": entry.passed,
                      "summary": entry.summary})
        if _RUN_CTL["stop"]:
            break

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = REPORT_TITLES.get(lang, REPORT_TITLES["th"])
    path = render_report(
        entries, cfg.report_path, generated_at=generated_at,
        title=f"{title} ({cfg.viewport_width}x{cfg.viewport_height})", lang=lang,
    )
    return entries, path


def with_headless(cfg: Config, headless: bool) -> Config:
    """Return a copy of cfg with headless overridden (used by the web app)."""
    return dataclasses.replace(cfg, headless=headless)
