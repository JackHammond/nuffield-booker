import time
import os
import re
import json
import uuid
import requests as http_requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

load_dotenv()

# --- Configuration ---
EMAIL = os.getenv("EMAIL", "")
PASSWORD = os.getenv("PASSWORD", "")
TARGET_CLASSES = os.getenv("TARGET_CLASSES", "").split(",")
LOGIN_URL = "https://my.nuffieldhealth.com/"
TARGET_URL = os.getenv("TARGET_URL", "")
LOCATION_ID = os.getenv("TARGET_LOCATION_ID", "")
API_BASE = "https://api.nuffieldhealth.com/booking/member/1.0"
API_KEY = os.getenv("API_KEY", "")

# Timing: idle until 06:59:55, classes drop at 07:00:00, give up after 30s
WAIT_UNTIL = os.getenv("WAIT_UNTIL", "")
DEADLINE = os.getenv("DEADLINE", "")  # Default to 10s after the hour to allow for clock skew
# DEADLINE = "23:59:59"  # Uncomment for testing outside the 7am window
MAX_LOOP_SECONDS = 30  # Hard safety cap — never loop longer than this

IFRAME_XPATH = "//iframe[contains(@src, 'nh-booking-microsite')]"


def log(msg):
    """Print with timestamp."""
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}")


def matches_target(title, targets):
    """Fuzzy match: all words in any target must appear in the title (order-independent, case-insensitive)."""
    title_lower = title.lower()
    for target in targets:
        target_words = re.findall(r'\w+', target.lower())
        if all(word in title_lower for word in target_words):
            return True
    return False


# ============================================================
# Browser (Selenium) — used ONLY for login + Bearer token capture
# ============================================================

def create_driver():
    opts = Options()
    if os.getenv("CI"):
        opts.add_argument("--headless")
        opts.add_argument("--window-size=1920,1080")
    for arg in [
        "--disable-blink-features=AutomationControlled",
        "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
    ]:
        opts.add_argument(arg)
    # Enable Chrome performance logging so we can capture the Bearer token
    opts.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    return webdriver.Chrome(options=opts)


def accept_cookies(driver):
    """Dismiss cookie consent banner via JS injection."""
    try:
        driver.execute_script("""
            document.cookie = "CookieControl={'necessaryCookies':[],'optionalCookies':{'analytics':'accepted','experience':'accepted','marketing':'accepted'},'statement':{},'consentDate':" + Date.now() + ",'consentExpiry':90,'interactedWith':true,'user':'0'}; path=/; max-age=7776000";
        """)
        driver.execute_script("""
            var banners = document.querySelectorAll('#ccc, .ccc-overlay, .ccc-content, #ccc-notify, .ccc-module--slideout');
            banners.forEach(function(el) { el.style.display = 'none'; });
        """)
        log("Cookies accepted (via JS injection)")
    except Exception:
        pass


def browser_login(driver):
    """Login via browser (Azure AD B2C requires browser-based auth)."""
    log("Logging in...")
    driver.get(LOGIN_URL)
    accept_cookies(driver)
    try:
        wait = WebDriverWait(driver, 5)
        wait.until(EC.presence_of_element_located((By.ID, "email"))).send_keys(EMAIL)
        driver.find_element(By.ID, "password").send_keys(PASSWORD)
        driver.find_element(By.ID, "next").click()
        log("Login submitted")
        time.sleep(2)
    except Exception:
        log("Login fields not found — may already be logged in")


def extract_bearer_token(driver):
    """Navigate to timetable so the booking iframe loads and makes API calls,
    then capture the Bearer token from Chrome's network performance logs.
    Falls back to reading the microsite's sessionStorage if logs fail."""
    log("Navigating to timetable to capture Bearer token...")
    driver.get(TARGET_URL)
    accept_cookies(driver)

    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.XPATH, IFRAME_XPATH))
        )
        log("Iframe loaded, waiting for microsite API calls...")
        time.sleep(8)
    except Exception:
        log("WARNING: Iframe not found on page")

    # Method 1: Chrome performance logs (captures all network traffic incl. iframes)
    token = _token_from_perf_logs(driver)
    if token:
        return token

    # Method 2: sessionStorage inside the iframe (MSAL token cache)
    token = _token_from_session_storage(driver)
    if token:
        return token

    log("ERROR: Could not extract Bearer token")
    return None


def _token_from_perf_logs(driver):
    """Extract Bearer token from Chrome's network performance logs."""
    try:
        logs = driver.get_log("performance")
        for entry in logs:
            try:
                msg = json.loads(entry["message"])["message"]
                if msg["method"] == "Network.requestWillBeSent":
                    req = msg["params"]["request"]
                    if "api.nuffieldhealth.com" in req.get("url", ""):
                        auth = req.get("headers", {}).get("Authorization", "")
                        if auth.startswith("Bearer "):
                            log("Bearer token captured from network logs")
                            return auth[7:]
            except (KeyError, json.JSONDecodeError):
                continue
    except Exception as e:
        log(f"Performance log read failed: {e}")
    return None


def _token_from_session_storage(driver):
    """Fallback: extract token from the microsite iframe's sessionStorage (MSAL cache)."""
    try:
        driver.switch_to.default_content()
        iframe = driver.find_element(By.XPATH, IFRAME_XPATH)
        driver.switch_to.frame(iframe)
        storage_json = driver.execute_script("""
            var items = {};
            for (var i = 0; i < sessionStorage.length; i++) {
                var k = sessionStorage.key(i);
                items[k] = sessionStorage.getItem(k);
            }
            return JSON.stringify(items);
        """)
        storage = json.loads(storage_json)
        for key, value in storage.items():
            if not value:
                continue
            # Direct JWT value
            if value.startswith("eyJ"):
                log(f"Token found in sessionStorage (key: {key})")
                return value
            # JSON object containing a token field
            try:
                obj = json.loads(value)
                if isinstance(obj, dict):
                    for f in ("secret", "access_token", "accessToken", "idToken", "id_token"):
                        v = obj.get(f, "")
                        if isinstance(v, str) and v.startswith("eyJ"):
                            log(f"Token found in sessionStorage (key: {key}, field: {f})")
                            return v
            except (json.JSONDecodeError, TypeError):
                pass
    except Exception as e:
        log(f"SessionStorage extraction failed: {e}")
    return None


# ============================================================
# Pure API calls — no browser needed from here on
# ============================================================

def api_headers(token):
    """Standard headers for Nuffield booking API."""
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Ocp-Apim-Subscription-Key": API_KEY,
        "Origin": "https://nh-booking-microsite.nuffieldhealth.com",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "X-Transaction-Id": str(uuid.uuid4()),
        "Device-Time": str(int(time.time() * 1000)),
    }


def fetch_classes(token, target_date):
    """GET bookable classes for a specific date from the API."""
    url = f"{API_BASE}/bookable_items/gym/"
    params = {
        "location": LOCATION_ID,
        "from_date": target_date.strftime("%Y-%m-%dT00:00:00.000+00:00"),
        "to_date": target_date.strftime("%Y-%m-%dT23:59:59.999+00:00"),
    }
    resp = http_requests.get(url, params=params, headers=api_headers(token), timeout=10)
    resp.raise_for_status()
    return resp.json()


def book_class(token, reservation_id):
    """POST to book a class by its reservation ID."""
    url = f"{API_BASE}/bookings/gym/"
    resp = http_requests.post(
        url,
        json={"reservation": reservation_id},
        headers=api_headers(token),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def format_time(iso_str):
    """Convert ISO datetime string to HH:MM for display."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%H:%M")
    except Exception:
        return iso_str or "?"


# ============================================================
# Timing helpers
# ============================================================

def past_deadline():
    return datetime.now().strftime("%H:%M:%S") >= DEADLINE


def idle_until_ready():
    """Sleep until WAIT_UNTIL time. If already past it, return immediately."""
    now = datetime.now().strftime("%H:%M:%S")
    if now >= WAIT_UNTIL:
        log(f"Already past {WAIT_UNTIL}, skipping idle")
        return
    log(f"Idling until {WAIT_UNTIL} (currently {now})...")
    while datetime.now().strftime("%H:%M:%S") < WAIT_UNTIL:
        time.sleep(0.5)
    log("Idle complete — go time!")


# ============================================================
# Main
# ============================================================

def main():
    log("=== Nuffield Booker (API Mode) ===")

    if not EMAIL or not PASSWORD:
        log("ERROR: EMAIL and PASSWORD env vars must be set")
        return

    # ---- Phase 1: Browser login + token capture ----
    driver = create_driver()
    token = None
    try:
        browser_login(driver)
        token = extract_bearer_token(driver)
    finally:
        driver.quit()
        log("Browser closed")

    if not token:
        log("FATAL: Could not obtain Bearer token — aborting")
        return

    log(f"Bearer token acquired (length: {len(token)})")
    log("All further operations use direct API calls — no browser needed")

    # ---- Phase 2: Prepare ----
    target_date = datetime.now() + timedelta(days=8)
    log(f"Target date: {target_date.strftime('%A %d %B %Y')}")

    # Quick API sanity check with today's classes
    try:
        test = fetch_classes(token, datetime.now())
        items = test.get("items", []) if isinstance(test, dict) else test
        log(f"API sanity check OK — {len(items)} classes returned for today")
        if items and isinstance(items[0], dict):
            log(f"  Sample item keys: {list(items[0].keys())}")
    except Exception as e:
        log(f"API sanity check FAILED: {e} — token may be invalid")
        return

    # ---- Phase 3: Idle until booking window ----
    idle_until_ready()

    # ---- Phase 4: Rapid-fire API booking loop ----
    cycle = 0
    all_booked = []
    loop_start = time.time()

    if past_deadline():
        log(f"Current time is past {DEADLINE} — booking window has closed, nothing to do")
    else:
        log(f"Booking window open — polling API until {DEADLINE} (hard cap {MAX_LOOP_SECONDS}s)")

    while not past_deadline() and (time.time() - loop_start) < MAX_LOOP_SECONDS:
        cycle += 1
        log(f"--- Cycle {cycle} ---")

        try:
            data = fetch_classes(token, target_date)
        except Exception as e:
            log(f"  API error: {e}")
            time.sleep(0.3)
            continue

        # Response format: {"items": [...]}
        items = data.get("items", []) if isinstance(data, dict) else data

        if not items:
            log("  No classes returned by API")
            if cycle == 1:
                log(f"  Response preview: {json.dumps(data)[:500]}")
            time.sleep(0.3)
            continue

        log(f"  {len(items)} class(es) returned")

        # Log all class names on the first cycle
        if cycle == 1:
            names = [i.get("title", "?") for i in items]
            log(f"  Classes: {names}")

        matched_any = False
        for item in items:
            name = item.get("title", "")
            if not matches_target(name, TARGET_CLASSES):
                continue

            matched_any = True
            res_id = item.get("sfid", "")
            start = item.get("from_date", "")
            end = item.get("to_date", "")
            is_full = item.get("is_full", False)
            has_waitlist = item.get("has_waitlist", False)
            my_booking = item.get("my_booking")  # None if not booked
            status = item.get("status", "")
            capacity = item.get("attendance_capacity", 0)
            attendees = item.get("attendees", 0)

            label = f"{name} @ {format_time(start)} - {format_time(end)}"
            log(f"  TARGET MATCH: {label} (status: {status}, full: {is_full}, attendees: {attendees}/{int(capacity)})")

            # Already booked
            if my_booking is not None:
                booking_status = my_booking.get("status", "") if isinstance(my_booking, dict) else str(my_booking)
                log(f"  Already booked/waitlisted: {label} ({booking_status})")
                all_booked.append(label)
                continue

            # Full and no waitlist
            if is_full and not has_waitlist:
                log(f"  CLASS FULL (no waitlist): {label}")
                continue

            # Full but has waitlist — we'll still try to book (API returns waitlist)
            if is_full and has_waitlist:
                log(f"  CLASS FULL but has waitlist: {label} — attempting...")

            # No reservation ID means we can't book
            if not res_id:
                log(f"  No reservation ID for {label} — cannot book yet")
                continue

            # Attempt booking via API
            try:
                log(f"  Booking: {label} (reservation: {res_id})...")
                result = book_class(token, res_id)
                result_status = result.get("status", "UNKNOWN").upper()
                log(f"  RESULT: {result_status} — {label}")
                if result_status in ("BOOKED", "WAITLISTED"):
                    all_booked.append(label)
            except http_requests.HTTPError as e:
                log(f"  Booking FAILED (HTTP {e.response.status_code}): {label}")
                try:
                    log(f"  Error body: {e.response.text[:300]}")
                except Exception:
                    pass
            except Exception as e:
                log(f"  Booking FAILED: {label} — {e}")

        if not matched_any:
            log(f"  No target classes ({TARGET_CLASSES}) found in response")

        if all_booked:
            log(f"Successfully booked {len(all_booked)} class(es) — done!")
            break

        time.sleep(0.3)

    # ---- Summary ----
    elapsed = round(time.time() - loop_start, 1)
    if (time.time() - loop_start) >= MAX_LOOP_SECONDS:
        log(f"Hard timeout reached after {elapsed}s — stopping")

    log("=== Session complete ===")
    if all_booked:
        log(f"Booked {len(all_booked)} class(es) in {cycle} cycle(s):")
        for c in all_booked:
            log(f"  Booked: {c}")
    elif cycle == 0:
        log("No booking cycles ran — script started after the deadline")
    else:
        log(f"No classes booked after {cycle} cycle(s) — all slots were taken")


if __name__ == "__main__":
    main()
