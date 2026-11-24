import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote, urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SL_TZ = timezone(timedelta(hours=5, minutes=30))
STATE_FILE = "state.json"
TEST_WHATSAPP = os.getenv("TEST_WHATSAPP", "false").strip().lower() == "true"

GREEN_API_URL = (os.getenv("GREEN_API_URL") or "https://api.green-api.com").strip().rstrip("/")
GREEN_API_ID_INSTANCE = (os.getenv("GREEN_API_ID_INSTANCE") or "").strip()
GREEN_API_TOKEN = (os.getenv("GREEN_API_TOKEN") or "").strip()
WHATSAPP_PHONES_RAW = (os.getenv("WHATSAPP_PHONE") or "").strip()

BUTTON_TEXT_PATTERN = re.compile(
    r"press here to change to an active organization|change to an active organization",
    re.IGNORECASE,
)
CHECK_STATUS_PATTERN = re.compile(r"check status", re.IGNORECASE)
INACTIVE_TEXT_PATTERN = re.compile(r"this team is currently inactive", re.IGNORECASE)
STOP_LINE_PATTERN = re.compile(
    r"^(active|inactive|check status|this team is|press here|change to an active|team status|normal\b)",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    now = datetime.now(SL_TZ).strftime("%Y-%m-%d %H:%M:%S SLST")
    print(f"[{now}] {msg}", flush=True)


def mask_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    if not parts:
        return parsed.netloc
    tail = parts[-1]
    masked_tail = f"{tail[:3]}***{tail[-3:]}" if len(tail) > 5 else "***"
    return f"{parsed.netloc}/.../{masked_tail}"


def extract_email_from_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [unquote(p) for p in parsed.path.split("/") if p]
    if parts and "@" in parts[-1]:
        return parts[-1]
    return ""


def load_urls() -> list[str]:
    raw = (os.getenv("STATUS_URLS") or "").strip()
    return [
        line.strip()
        for line in re.split(r"[\r\n,]+", raw)
        if line.strip() and not line.strip().startswith("#")
    ]


def load_whatsapp_phones() -> list[str]:
    if not WHATSAPP_PHONES_RAW:
        return []
    return [
        line.strip()
        for line in re.split(r"[\r\n,;]+", WHATSAPP_PHONES_RAW)
        if line.strip()
    ]


def extract_team_name_from_text(body_text: str) -> str:
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    teams = []
    i = 0
    while i < len(lines):
        if lines[i].upper() == "TEAM NAME":
            collected = []
            j = i + 1
            while j < len(lines) and len(collected) < 3:
                candidate = lines[j]
                if candidate.upper() == "TEAM NAME" or STOP_LINE_PATTERN.match(candidate):
                    break
                collected.append(candidate)
                j += 1
            if collected:
                teams.append(" ".join(collected))
            i = j
        else:
            i += 1
    return " / ".join(teams) if teams else "Unknown"


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def set_github_output(name: str, value: str) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def normalize_chat_id(phone: str) -> str:
    cleaned = phone.strip()
    if cleaned.endswith("@c.us") or cleaned.endswith("@g.us"):
        return cleaned
    digits = re.sub(r"\D+", "", cleaned)
    if digits.startswith("0") and len(digits) == 10:
        digits = "94" + digits[1:]
    return f"{digits}@c.us"


def send_whatsapp_message(message: str) -> bool:
    phones = load_whatsapp_phones()
    if not (GREEN_API_ID_INSTANCE and GREEN_API_TOKEN and phones):
        log("WhatsApp skipped: Missing GREEN-API secrets or WHATSAPP_PHONE.")
        return False

    endpoint = f"{GREEN_API_URL}/waInstance{GREEN_API_ID_INSTANCE}/sendMessage/{GREEN_API_TOKEN}"
    all_ok = True

    for idx, phone in enumerate(phones, start=1):
        chat_id = normalize_chat_id(phone)
        payload = json.dumps({
            "chatId": chat_id,
            "message": message,
        }).encode("utf-8")

        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if 200 <= resp.status < 300:
                    log(f"WhatsApp notification #{idx} sent successfully via GREEN-API.")
                else:
                    log(f"WhatsApp recipient #{idx} returned HTTP {resp.status}.")
                    all_ok = False
        except urllib.error.HTTPError as exc:
            log(f"WhatsApp recipient #{idx} HTTP error: {exc.code} {exc.reason}")
            all_ok = False
        except Exception as exc:
            log(f"WhatsApp recipient #{idx} send failed: {exc}")
            all_ok = False

    return all_ok


def check_single_url(context, idx: int, url: str) -> dict:
    label = f"Link #{idx} ({mask_url(url)})"
    log(f"--- Checking {label} ---")
    page = context.new_page()
    page.on("dialog", lambda dialog: (log(f"{label}: Dialog accepted"), dialog.accept()))

    try:
        try:
            page.goto(url, wait_until="networkidle", timeout=60000)
        except PlaywrightTimeoutError:
            log(f"{label}: Page load timed out on networkidle; continuing...")
            page.wait_for_load_state("domcontentloaded")

        page.wait_for_timeout(3000)
        body_text = page.inner_text("body")
        has_status_loaded = bool(
            re.search(r"\b(ACTIVE|INACTIVE)\b", body_text, re.IGNORECASE)
            or BUTTON_TEXT_PATTERN.search(body_text)
        )

        if not has_status_loaded:
            log(f"{label}: Clicking 'Check Status'...")
            email_from_url = extract_email_from_url(url)
            email_input = page.locator("input[type='email'], input[type='text']").first
            if email_input.count() > 0 and email_from_url and not email_input.input_value().strip():
                email_input.fill(email_from_url)
            check_btn = page.locator("button, input[type='submit'], a, [role='button']").filter(
                has_text=CHECK_STATUS_PATTERN
            ).first
            if check_btn.count() > 0:
                check_btn.click()
                try:
                    page.wait_for_load_state("networkidle", timeout=30000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(3000)

        reactivate_buttons = page.locator("button, a, [role='button']").filter(has_text=BUTTON_TEXT_PATTERN)
        btn_count = reactivate_buttons.count()

        if btn_count > 0:
            log(f"{label}: Found {btn_count} INACTIVE team(s). Reactivating...")
            for b_idx in range(btn_count):
                btn = reactivate_buttons.nth(b_idx)
                if btn.is_visible():
                    btn.scroll_into_view_if_needed()
                    btn.click()
                    try:
                        page.wait_for_load_state("networkidle", timeout=30000)
                    except PlaywrightTimeoutError:
                        pass
                    page.wait_for_timeout(4000)

            try:
                page.reload(wait_until="networkidle", timeout=45000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(3000)

            updated_body = page.inner_text("body")
            team_name = extract_team_name_from_text(updated_body)
            remaining = page.locator("button, a, [role='button']").filter(has_text=BUTTON_TEXT_PATTERN).count()
            if remaining == 0:
                log(f"{label}: SUCCESS — Reactivated! New Team: {team_name}")
                return {"status": "REACTIVATED", "team_name": team_name}
            log(f"{label}: WARNING — {remaining} reactivate button(s) still visible.")
            return {"status": "ERROR", "team_name": team_name}

        updated_body = page.inner_text("body")
        team_name = extract_team_name_from_text(updated_body)
        if INACTIVE_TEXT_PATTERN.search(updated_body):
            log(f"{label}: WARNING — Inactive text found, but no clickable button.")
            return {"status": "ERROR", "team_name": team_name}

        log(f"{label}: OK — Already ACTIVE. Team: {team_name}")
        return {"status": "ACTIVE", "team_name": team_name}
    except Exception as exc:
        log(f"{label}: ERROR — {exc}")
        return {"status": "ERROR", "team_name": "Error"}
    finally:
        page.close()


def main() -> int:
    urls = load_urls()
    if not urls:
        log("ERROR: No URLs configured in STATUS_URLS secret.")
        return 1

    log(f"Loaded {len(urls)} status link(s) to check.")
    prev_state = load_state()
    new_state = dict(prev_state)
    summary_counts = {"ACTIVE": 0, "REACTIVATED": 0, "ERROR": 0}
    link_reports = []
    changes_detected = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        )
        for idx, url in enumerate(urls, start=1):
            key = f"link_{idx}"
            account_label = extract_email_from_url(url) or f"Account #{idx}"
            res = check_single_url(context, idx, url)
            status, team_name = res["status"], res["team_name"]
            summary_counts[status] += 1
            old_team = prev_state.get(key)
            if team_name not in ("Unknown", "Error"):
                new_state[key] = team_name

            changed = status == "REACTIVATED" or (
                old_team is not None and team_name not in ("Unknown", "Error") and old_team != team_name
            )
            if changed:
                changes_detected.append(
                    f"• *{account_label}*\n  {old_team or 'Previous'} → {team_name}"
                )
            link_reports.append({
                "idx": idx,
                "account": account_label,
                "status": status,
                "team_name": team_name,
                "changed": changed,
            })
        browser.close()

    state_changed = new_state != prev_state
    save_state(new_state)
    set_github_output("state_changed", "true" if state_changed else "false")

    if changes_detected or TEST_WHATSAPP:
        now_str = datetime.now(SL_TZ).strftime("%Y-%m-%d %I:%M %p SLST")
        header = "🔄 *Team Change / Reactivation Alert*" if changes_detected else "✅ *WhatsApp Test Notification*"
        lines = [header, f"🕒 {now_str}", ""]
        if changes_detected:
            lines.append("*Changed:*")
            lines.extend(changes_detected)
            lines.append("")
        lines.append("*Current Active Teams:*")
        for item in link_reports:
            icon = "🔄" if item["changed"] else ("✅" if item["status"] == "ACTIVE" else "⚠️")
            lines.append(f"{icon} *{item['account']}*\n   ↳ {item['team_name']}")
        send_whatsapp_message("\n".join(lines))

    log(
        f"Summary — Total: {len(urls)} | Active: {summary_counts['ACTIVE']} | "
        f"Reactivated: {summary_counts['REACTIVATED']} | Errors: {summary_counts['ERROR']} | "
        f"Changes: {len(changes_detected)}"
    )
    return 1 if summary_counts["ERROR"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
