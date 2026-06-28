"""
Command-line runner for the AI Web Tester (for developers / CI).

Test cases live in `tests/*.yaml`. Non-technical users should use the web app
instead — double-click the launcher, or run `python app.py`.

Usage:
    source .venv/bin/activate
    python cli.py                       # run all enabled tests, browser visible
    HEADLESS=1 python cli.py            # headless (CI)
    python cli.py homepage_loads        # run specific tests by name
"""

import asyncio
import sys

from runner import config_status, load_config, load_tests, run_suite


async def main():
    cfg = load_config()
    if not config_status(cfg)["has_api_key"]:
        sys.exit("❌ GOOGLE_API_KEY is not set. Edit .env (https://aistudio.google.com/apikey).")

    available = [tc.name for tc in load_tests()]
    if not available:
        sys.exit("❌ No test cases found in tests/*.yaml")

    names = sys.argv[1:] or None
    if names:
        unknown = [n for n in names if n not in available]
        if unknown:
            sys.exit(f"❌ Unknown test(s): {unknown}. Available: {available}")

    print(
        f"🤖 model: {cfg.llm_model}  |  🌐 {cfg.base_url}  |  "
        f"viewport {cfg.viewport_width}x{cfg.viewport_height}  |  headless: {cfg.headless}"
    )

    entries, path = await run_suite(names, cfg)

    print(f"\n{'=' * 70}\nSUMMARY")
    for e in entries:
        print(f"  {'✅' if e.passed else '❌'}  {e.name}")
    print(f"\n📄 Report written to {path.resolve()}")

    if not all(e.passed for e in entries):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
