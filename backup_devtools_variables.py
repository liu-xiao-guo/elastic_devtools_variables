import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlparse
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

load_dotenv()

KIBANA_URL = os.environ["KIBANA_URL"]
KIBANA_API_KEY = os.environ["KIBANA_API_KEY"]
OUTPUT_FILE = "devtools_variables.json"

CONFIG_URL = f"{KIBANA_URL.rstrip('/')}/app/dev_tools#/console/config"
LS_KEY = "sense:variables"
AUTH_HEADERS = {
    "Authorization": f"ApiKey {KIBANA_API_KEY}",
    "kbn-xsrf": "true",
}

_CHROME_BASE = os.path.expanduser("~/Library/Application Support/Google/Chrome")
_CHROME_BETA_BASE = os.path.expanduser("~/Library/Application Support/Google/Chrome Beta")
_CHROMIUM_BASE = os.path.expanduser("~/Library/Application Support/Chromium")
_EDGE_BASE = os.path.expanduser("~/Library/Application Support/Microsoft Edge")


def _enumerate_profiles(base: str) -> list[str]:
    """Return all existing Chrome-style profile dirs under base."""
    candidates = [os.path.join(base, "Default")]
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if name.startswith("Profile"):
            candidates.append(os.path.join(base, name))
    return [p for p in candidates if os.path.isdir(p)]


CHROME_PROFILE_CANDIDATES: list[str] = (
    _enumerate_profiles(_CHROME_BASE)
    + _enumerate_profiles(_CHROME_BETA_BASE)
    + _enumerate_profiles(_CHROMIUM_BASE)
    + _enumerate_profiles(_EDGE_BASE)
)


# ── Strategy 0: direct Chrome LevelDB scan (no browser, no auth) ─────────────


def _extract_json_latin1(data: bytes, start: int) -> str | None:
    depth = 0
    in_str = escaped = False
    for i in range(start, len(data)):
        b = data[i]
        if b > 0x7E and depth == 0:
            break
        c = chr(b)
        if escaped:
            escaped = False
        elif c == "\\" and in_str:
            escaped = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c in "[{":
                depth += 1
            elif c in "]}":
                depth -= 1
                if depth == 0:
                    return data[start : i + 1].decode("latin-1")
    return None


def _scan_leveldb_dir(leveldb_dir: str, ls_key_bytes: bytes) -> list[dict] | None:
    """Scan LevelDB files in a Chrome Local Storage directory for ls_key_bytes."""
    if not os.path.isdir(leveldb_dir):
        return None
    # Prefer .log (WAL, most recent writes) then .ldb (SST)
    files = sorted(glob.glob(os.path.join(leveldb_dir, "*.log")), reverse=True)
    files += sorted(glob.glob(os.path.join(leveldb_dir, "*.ldb")), reverse=True)
    for fpath in files:
        try:
            data = open(fpath, "rb").read()
        except OSError:
            continue
        pos = data.rfind(ls_key_bytes)
        if pos == -1:
            continue
        after = pos + len(ls_key_bytes)
        # In .log files: value_len(varint) + type(1) + json
        # In .ldb files: 8-byte internal tag + value_len(varint) + type(1) + json
        # Scan a small window for Chrome type byte 0x01 followed by '[' or '{'
        window = data[after : after + 24]
        for i, b in enumerate(window):
            if b == 0x01 and i + 1 < len(window) and window[i + 1] in (ord("["), ord("{")):
                json_str = _extract_json_latin1(data, after + i + 1)
                if json_str:
                    try:
                        return json.loads(json_str)
                    except json.JSONDecodeError:
                        continue
    return None


def read_via_leveldb() -> list[dict] | None:
    parsed = urlparse(KIBANA_URL)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    # Chrome prefixes localStorage keys with '_'
    ls_key_bytes = f"_{origin}\x00\x01{LS_KEY}".encode("utf-8")

    for profile in CHROME_PROFILE_CANDIDATES:
        leveldb_dir = os.path.join(profile, "Local Storage", "leveldb")
        result = _scan_leveldb_dir(leveldb_dir, ls_key_bytes)
        if result is not None:
            print(f"  Found in profile: {profile}")
            return result
    return None


# ── Strategy 1: AppleScript → running Chrome (reads live memory) ─────────────

def read_via_applescript() -> list[dict] | None:
    """Read localStorage directly from the running Chrome — no auth needed."""
    js = f"localStorage.getItem('{LS_KEY}')"
    script = f"""
tell application "Google Chrome"
    set kibanaOrigin to "{KIBANA_URL.rstrip('/')}"
    repeat with w in windows
        repeat with t in tabs of w
            if URL of t contains kibanaOrigin then
                set raw to execute t javascript "{js}"
                return raw
            end if
        end repeat
    end repeat
    set raw to execute active tab of front window javascript "{js}"
    return raw
end tell
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        raw = result.stdout.strip()
        if not raw or raw.lower() in ("null", "missing value", ""):
            return None
        return json.loads(raw)
    except Exception:
        return None


# ── Strategy 2: Playwright with copied Chrome profile (reads disk data) ───────

def find_chrome_profile() -> str | None:
    return CHROME_PROFILE_CANDIDATES[0] if CHROME_PROFILE_CANDIDATES else None


def read_via_profile() -> list[dict] | None:
    """Try each Chrome profile: copy it, navigate to the Kibana origin (auth
    state doesn't matter — localStorage is origin-scoped), then read the key."""
    for profile_src in CHROME_PROFILE_CANDIDATES:
        tmp = tempfile.mkdtemp(prefix="kb_playwright_")
        print(f"  Trying profile: {profile_src}")
        try:
            shutil.copytree(
                profile_src,
                os.path.join(tmp, "Default"),
                ignore_dangling_symlinks=True,
                ignore=shutil.ignore_patterns("*.lock", "lockfile"),
            )
            with sync_playwright() as pw:
                ctx = pw.chromium.launch_persistent_context(
                    tmp, headless=True, args=["--profile-directory=Default"],
                )
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.set_extra_http_headers(AUTH_HEADERS)
                try:
                    page.goto(CONFIG_URL, wait_until="networkidle", timeout=30_000)
                except PWTimeout:
                    pass  # localStorage is accessible regardless of page load state
                raw = page.evaluate(f"() => localStorage.getItem('{LS_KEY}')")
                ctx.close()
            if raw:
                return json.loads(raw)
        except Exception as e:
            print(f"    failed: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return None


# ── Strategy 3: fresh headless browser with API key auth ─────────────────────

def read_via_fresh_browser() -> list[dict] | None:
    """Navigate with API key auth; only works if Kibana syncs variables
    server-side (Kibana 8.9+ stores console variables in saved objects)."""
    print("Using fresh headless browser with API key auth …")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_extra_http_headers(AUTH_HEADERS)
            try:
                page.goto(CONFIG_URL, wait_until="networkidle", timeout=30_000)
            except PWTimeout:
                pass
            final_url = page.url
            if any(s in final_url for s in ("/login", "/signin", "login?")):
                print(f"  Auth failed — redirected to: {final_url}")
                return None
            raw = page.evaluate(f"() => localStorage.getItem('{LS_KEY}')")
            return json.loads(raw) if raw else None
        except Exception as e:
            print(f"  failed: {e}")
            return None
        finally:
            browser.close()


# ── Normalise entries → [{"name": "${FOO}", "value": "bar"}] ─────────────────

def normalise(entries: list[dict]) -> list[dict]:
    result = []
    for item in entries:
        name = item.get("name", "").strip()
        value = item.get("value", "")
        if not name:
            continue
        if not name.startswith("${"):
            name = f"${{{name}}}"
        result.append({"name": name, "value": value})
    return result


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Strategy 0 — reading Chrome localStorage from disk (LevelDB) …")
    raw = read_via_leveldb()

    if raw is None:
        print("Strategy 1 — reading from running Chrome via AppleScript …")
        raw = read_via_applescript()

    if raw is None:
        print("Strategy 2 — reading from Chrome profile via Playwright …")
        raw = read_via_profile()

    if raw is None:
        print("Strategy 3 — fresh headless browser …")
        raw = read_via_fresh_browser()

    if raw is None:
        print("ERROR: could not read localStorage by any method.", file=sys.stderr)
        sys.exit(1)

    variables = normalise(raw)

    print(f"\nExtracted {len(variables)} variable(s):")
    for v in variables:
        print(f"  {v['name']}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(variables, fh, indent=2, ensure_ascii=False)
    print(f"\nSaved to '{OUTPUT_FILE}'.")


if __name__ == "__main__":
    main()