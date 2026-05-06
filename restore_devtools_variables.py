import json
import os
import re
import subprocess
import sys
import uuid
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

KIBANA_URL = os.environ["KIBANA_URL"]
KIBANA_API_KEY = os.environ["KIBANA_API_KEY"]
INPUT_FILE = "devtools_variables.json"

CONFIG_URL = f"{KIBANA_URL.rstrip('/')}/app/dev_tools#/console/config"
LS_KEY = "sense:variables"
AUTH_HEADERS = {
    "Authorization": f"ApiKey {KIBANA_API_KEY}",
    "kbn-xsrf": "true",
}


def load_variables() -> list[dict]:
    with open(INPUT_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def to_kibana_format(variables: list[dict]) -> list[dict]:
    """
    Convert saved {"name": "${FOO}", "value": "bar"} entries back to the
    format Kibana stores in localStorage: {"id": "<uuid>", "name": "FOO", "value": "bar"}.
    """
    result = []
    for item in variables:
        name = item["name"].strip()
        # Strip ${...} wrapper if present
        name = re.sub(r"^\$\{(.+)\}$", r"\1", name)
        result.append({
            "id": str(uuid.uuid4()),
            "name": name,
            "value": item.get("value", ""),
        })
    return result


# ── Strategy 1: AppleScript → running Chrome ─────────────────────────────────

def _run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=10,
    )
    return result.stdout.strip()


def _find_kibana_tab_script(inner_js: str) -> str:
    """
    AppleScript that iterates every window/tab in Chrome, finds the first tab
    whose URL contains the Kibana origin, runs inner_js there, and returns
    the result. Falls back to the active tab if none match.
    """
    origin = KIBANA_URL.rstrip("/")
    escaped_js = inner_js.replace("\\", "\\\\").replace('"', '\\"')
    return f"""
tell application "Google Chrome"
    set kibanaOrigin to "{origin}"
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t contains kibanaOrigin then
                set res to execute t javascript "{escaped_js}"
                return res
            end if
        end repeat
    end repeat
    -- fallback: active tab
    set res to execute active tab of front window javascript "{escaped_js}"
    return res
end tell
"""


def restore_via_applescript(payload: str) -> bool:
    """Write sense:variables into the running Chrome tab that has Kibana open."""
    js = f'localStorage.setItem("{LS_KEY}", {json.dumps(payload)}); "ok"'
    try:
        out = _run_applescript(_find_kibana_tab_script(js))
        return out == "ok"
    except Exception:
        return False


def verify_via_applescript(expected_count: int) -> bool:
    js = f"JSON.parse(localStorage.getItem('{LS_KEY}') || '[]').length"
    try:
        out = _run_applescript(_find_kibana_tab_script(js))
        return out == str(expected_count)
    except Exception:
        return False


# ── Strategy 2: Playwright headless browser ───────────────────────────────────

def restore_via_playwright(payload: str, expected_count: int) -> bool:
    print("Falling back to headless browser …")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_extra_http_headers(AUTH_HEADERS)
            page.goto(CONFIG_URL, wait_until="networkidle")
            page.wait_for_selector("[data-test-subj='variablesTable']", timeout=15_000)

            page.evaluate(f"() => localStorage.setItem('{LS_KEY}', {json.dumps(payload)})")
            page.reload(wait_until="networkidle")
            page.wait_for_selector("[data-test-subj='variablesTable']", timeout=15_000)

            count = page.evaluate(
                f"() => JSON.parse(localStorage.getItem('{LS_KEY}') || '[]').length"
            )
            return count == expected_count
        except PWTimeout as exc:
            print(f"ERROR: timed out — {exc}", file=sys.stderr)
            return False
        finally:
            browser.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not os.path.exists(INPUT_FILE):
        print(f"ERROR: '{INPUT_FILE}' not found. Run extract_devtools_variables.py first.", file=sys.stderr)
        sys.exit(1)

    variables = load_variables()
    kibana_vars = to_kibana_format(variables)
    payload = json.dumps(kibana_vars, ensure_ascii=False)
    count = len(kibana_vars)

    print(f"Loaded {count} variable(s) from '{INPUT_FILE}'.")

    answer = input("You are going to overwrite all of the variables in Kibana. Will you continue? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborted.")
        sys.exit(0)

    print("Strategy 1 — restoring via AppleScript into running Chrome …")

    ok = restore_via_applescript(payload)
    if ok and verify_via_applescript(count):
        print(f"Restored {count} variable(s) successfully via AppleScript.")
        print("Reload the Kibana DevTools Config page to see the changes.")
        return

    print("Strategy 2 — restoring via headless Playwright …")
    ok = restore_via_playwright(payload, count)
    if ok:
        print(f"Restored {count} variable(s) successfully via Playwright.")
        return

    print("ERROR: could not restore variables by any method.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
