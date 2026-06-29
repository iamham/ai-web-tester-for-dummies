# AI Web Tester for Dummies

<center>
<img width="1024" height="1536" alt="ChatGPT Image Jun 29, 2026, 03_59_17 PM" src="https://github.com/user-attachments/assets/2a639116-64c7-41f3-9ae6-97cafc015189" />
</center>

Test any website in plain English. An LLM (Google Gemini) drives a real Chromium
browser to carry out test cases you write as everyday sentences — *"open the
first product, add it to the cart, and confirm it appears"* — and produces a
self-contained HTML report with a screenshot of **every step**, the agent's
reasoning, and a pass/fail result.

Built on [Browser Use](https://github.com/browser-use/browser-use). Runs entirely
on your own machine. The interface is **bilingual (ไทย / English)** with a
switcher and defaults to Thai — and the AI writes its result summaries in the
selected language.

There are two ways to use it:

| Audience | How | Start here |
|----------|-----|------------|
| **Non-technical (QA, PM)** | A local web app in your browser | **[QA_GUIDE.md](QA_GUIDE.md)** · 🇹🇭 **[ภาษาไทย](QA_GUIDE.th.md)** |
| **Developers / CI** | Command line | [Developer setup](#developer-setup) |

> ⚠️ AI-driven tests are **non-deterministic** — great for smoke and exploratory
> testing, and for catching obvious breakage, but not a 1:1 replacement for
> deterministic end-to-end suites on critical regression paths.

---

## For non-technical users (web app)

Double-click the launcher for your OS — it installs everything on first run, then
opens a web page where you create test cases in plain English, run them, watch
live progress, and read the screenshot report. No terminal required.

- macOS: **`Start (macOS).command`**
- Windows: **`Start (Windows).bat`**

Full step-by-step (including the macOS/Windows "allow this app" prompts and how to
enter your own Gemini key) is in **[QA_GUIDE.md](QA_GUIDE.md)**.

You can also run it manually: `python app.py` (serves on `http://127.0.0.1:8765`
and opens your browser).

---

## Test cases (YAML)

Tests live as simple files in **`tests/*.yaml`** — decoupled from code so anyone
can add one (via the web app's **＋ New test**, or by copying
[`tests/_TEMPLATE.yaml`](tests/_TEMPLATE.yaml)):

```yaml
name: signup_form
title: Sign-up form appears
description: >
  Click the "Sign up" button and confirm a registration form with email and
  password fields appears.
enabled: true
requires_login: false   # if true, start at LOGIN_URL first
start_url: ""           # optional: start at this exact URL instead of BASE_URL
max_steps: 20
```

You only write `description` — the runner navigates to your site first
automatically (`runner.compose_task`). Three runnable examples ship in `tests/`
(`homepage_loads`, `follow_first_link`, `responsive_layout`) and work against any
site, including the default `https://example.com`.

---

## Configuration

Set these via the web app's **⚙ Settings**, or in a `.env` file (see
[`.env.example`](.env.example)):

| Variable | Purpose | Default |
|----------|---------|---------|
| `GOOGLE_API_KEY` | Your Gemini API key ([get one](https://aistudio.google.com/apikey)) | — (required) |
| `BASE_URL` | The website under test | `https://example.com` |
| `LOGIN_URL` | Optional URL that logs you in (used by `requires_login` tests) | — |
| `CONTEXT_HINT` | Extra standing instructions added to every test | — |
| `LLM_MODEL` | `gemini-2.5-flash` (fast) or `gemini-2.5-pro` (smarter) | `gemini-2.5-flash` |
| `OUTPUT_LANG` | Report + AI summary language: `th` or `en` (web UI has its own switcher) | `th` |
| `VIEWPORT_WIDTH` / `VIEWPORT_HEIGHT` | Browser size | `1280` / `800` |
| `DEVICE_SCALE` / `USER_AGENT` | For mobile emulation (e.g. `390x844`, scale `2`) | `1` / browser default |
| `ALLOWED_DOMAINS` | Restrict the agent (comma-separated); auto-derived if blank | — |

---

## Developer setup

```bash
# 1. Isolated Python 3.12 env (browser-use needs 3.11–3.12)
pip install uv
uv venv --python 3.12 && source .venv/bin/activate

# 2. Deps + Chromium
uv pip install -r requirements.txt
python -m browser_use install

# 3. Settings
cp .env.example .env
#   -> set GOOGLE_API_KEY and BASE_URL
```

### CLI

```bash
source .venv/bin/activate
python cli.py                    # all enabled tests, browser visible
HEADLESS=1 python cli.py         # headless (CI)
python cli.py homepage_loads     # specific tests by name
LLM_MODEL=gemini-2.5-pro python cli.py
```

Exit code is non-zero if any test fails, so it drops into CI cleanly.

## Architecture

- **`runner.py`** — shared core: config, YAML test loading, task composition, and
  execution. Used by both the CLI and the web app.
- **`app.py`** — local web UI (Starlette + uvicorn): manage/run tests, live
  progress, settings; binds to `127.0.0.1` only.
- **`cli.py`** — thin command-line wrapper over `runner`.
- **`report.py`** — builds the self-contained `report.html`.
- **`tests/*.yaml`** — the test cases.

## The report

After every run a self-contained **`report.html`** is written / served at
`/report`. For each test it shows pass/fail, start time, duration, and step count;
and for every step: the **screenshot** (click to zoom), the agent's reasoning, the
actions it took, the extracted page content, plus a **run GIF**. Everything is
embedded as base64, so the single file is portable. Preview the format without
running anything in [`report.sample.html`](report.sample.html).

## Notes

- **Cost:** each step is an LLM call. `gemini-2.5-flash` keeps this cheap;
  `max_steps` caps runaway runs.
- **Privacy/secrets:** the server binds to `127.0.0.1` only; your API key lives in
  a gitignored `.env` and is never echoed back by the UI. Never commit `.env`.
- **One run at a time:** the web app runs a single suite at once (returns 409 if
  busy).

## Origin

<img src="https://campaigns.amazebiz.com/logo.png" alt="Amaze" height="48">

Originally developed for **Amaze** internal use, and open-sourced so anyone can
use it freely.

## Author & contributing

Created with ❤️ by **Sarun Peetasai** — [GitHub](https://github.com/iamham) ·
[sarunp.com](https://sarunp.com).

This is free and open source, built to make web testing accessible to everyone —
including non-developers and teams who prefer working in Thai. Contributions are
very welcome: ⭐ star the repo, [open an issue](https://github.com/iamham/ai-web-tester-for-dummies/issues),
or send a pull request on [GitHub](https://github.com/iamham/ai-web-tester-for-dummies).

## License

MIT © Sarun Peetasai — see [LICENSE](LICENSE).
