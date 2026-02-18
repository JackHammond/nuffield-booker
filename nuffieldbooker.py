import time
import os
import re
import json
import uuid
import requests as http_requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def busy_wait_until(target_time_str):
    """Tight busy-wait (no sleep) until HH:MM:SS.mmm. Accurate to ~1ms."""
    log(f"Busy-waiting until {target_time_str}...")
    while True:
        now = datetime.now()
        now_str = now.strftime("%H:%M:%S.") + f"{now.microsecond // 1000:03d}"
        if now_str >= target_time_str:
            log(f"GO! (actual: {now_str})")
            return


BOOK_AT = "07:00:00.100"  # Fire bookings 100ms after the hour


def prefetch_targets(token, target_date):
    """Fetch the class list and return only the matching target classes with reservation IDs."""
    data = fetch_classes(token, target_date)
    items = data.get("items", []) if isinstance(data, dict) else data

    if not items:
        log("  Pre-fetch returned no classes")
        return []

    log(f"  Pre-fetch returned {len(items)} class(es)")
    names = [i.get("title", "?") for i in items]
    log(f"  Classes: {names}")

    targets = []
    for item in items:
        name = item.get("title", "")
        if not matches_target(name, TARGET_CLASSES):
            continue

        res_id = item.get("sfid", "")
        start = item.get("from_date", "")
        end = item.get("to_date", "")
        is_full = item.get("is_full", False)
        has_waitlist = item.get("has_waitlist", False)
        my_booking = item.get("my_booking")
        capacity = item.get("attendance_capacity", 0)
        attendees = item.get("attendees", 0)

        label = f"{name} @ {format_time(start)} - {format_time(end)}"

        if my_booking is not None:
            booking_status = my_booking.get("status", "") if isinstance(my_booking, dict) else str(my_booking)
            log(f"  SKIP (already {booking_status}): {label}")
            continue

        if is_full and not has_waitlist:
            log(f"  SKIP (full, no waitlist): {label}")
            continue

        if not res_id:
            log(f"  SKIP (no reservation ID): {label}")
            continue

        log(f"  QUEUED: {label} (res: {res_id}, {attendees}/{int(capacity)})")
        targets.append({"sfid": res_id, "label": label, "item": item})

    return targets


def fire_all_bookings(token, targets):
    """Book all target classes in parallel. Returns list of (label, result_or_error)."""
    results = []

    def _book_one(target):
        label = target["label"]
        res_id = target["sfid"]
        try:
            log(f"  >> Booking: {label}")
            result = book_class(token, res_id)
            status = result.get("status", "UNKNOWN").upper()
            log(f"  << {status}: {label}")
            return (label, result)
        except http_requests.HTTPError as e:
            body = ""
            try:
                body = e.response.text[:300]
            except Exception:
                pass
            log(f"  << FAILED (HTTP {e.response.status_code}): {label} — {body}")
            return (label, {"status": "FAILED", "error": body})
        except Exception as e:
            log(f"  << FAILED: {label} — {e}")
            return (label, {"status": "FAILED", "error": str(e)})

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {pool.submit(_book_one, t): t for t in targets}
        for future in as_completed(futures):
            results.append(future.result())

    return results


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
    except Exception as e:
        log(f"API sanity check FAILED: {e} — token may be invalid")
        return

    # ---- Phase 3: Pre-fetch target classes (before 7am) ----
    log("--- Pre-fetching target classes for booking day ---")
    try:
        targets = prefetch_targets(token, target_date)
    except Exception as e:
        log(f"Pre-fetch FAILED: {e}")
        targets = []

    if not targets:
        log("No bookable target classes found in pre-fetch — will retry at booking time")

    log(f"Pre-fetched {len(targets)} target class(es) to book")

    # ---- Phase 4: Wait until exactly 07:00:00.500 ----
    now_str = datetime.now().strftime("%H:%M:%S")
    if now_str >= BOOK_AT:
        log(f"Already past {BOOK_AT} — firing bookings immediately")
    else:
        idle_until_ready()  # Coarse sleep until ~06:59:55
        busy_wait_until(BOOK_AT)  # Tight spin until 07:00:00.500

    # ---- Phase 5: Fire all bookings in parallel ----
    all_booked = []
    loop_start = time.time()

    if targets:
        log(f"=== Firing {len(targets)} booking(s) in parallel ===")
        results = fire_all_bookings(token, targets)
        for label, result in results:
            status = result.get("status", "UNKNOWN").upper() if isinstance(result, dict) else "UNKNOWN"
            waitlist_pos = result.get("waitlist_position") if isinstance(result, dict) else None
            if status in ("BOOKED", "WAITLISTED", "WAITLIST", "CONFIRMED") or waitlist_pos is not None:
                all_booked.append(label)

    # ---- Phase 6: Retry loop for any that failed or weren't pre-fetched ----
    cycle = 0
    retry_start = time.time()
    # Only retry if we didn't get everything or had no targets
    need_retry = (len(all_booked) < len(targets)) or (not targets)
    # Deadline is used only as an idle timeout cap for retries (not a gate on bookings)

    if need_retry and not past_deadline() and (time.time() - loop_start) < MAX_LOOP_SECONDS:
        log("--- Retry loop (re-fetching + booking remaining) ---")

    while need_retry and not past_deadline() and (time.time() - loop_start) < MAX_LOOP_SECONDS:
        cycle += 1
        log(f"--- Retry cycle {cycle} ---")

        try:
            data = fetch_classes(token, target_date)
        except Exception as e:
            log(f"  API error: {e}")
            time.sleep(0.3)
            continue

        items = data.get("items", []) if isinstance(data, dict) else data
        if not items:
            log("  No classes returned")
            time.sleep(0.3)
            continue

        retry_targets = []
        for item in items:
            name = item.get("title", "")
            if not matches_target(name, TARGET_CLASSES):
                continue

            res_id = item.get("sfid", "")
            start = item.get("from_date", "")
            end = item.get("to_date", "")
            is_full = item.get("is_full", False)
            has_waitlist = item.get("has_waitlist", False)
            my_booking = item.get("my_booking")
            capacity = item.get("attendance_capacity", 0)
            attendees = item.get("attendees", 0)

            label = f"{name} @ {format_time(start)} - {format_time(end)}"

            # Skip if already booked (either already in our list, or server says so)
            if my_booking is not None:
                if label not in all_booked:
                    booking_status = my_booking.get("status", "") if isinstance(my_booking, dict) else str(my_booking)
                    log(f"  Already {booking_status}: {label}")
                    all_booked.append(label)
                continue

            if is_full and not has_waitlist:
                log(f"  FULL: {label}")
                continue

            if not res_id:
                continue

            retry_targets.append({"sfid": res_id, "label": label, "item": item})

        if retry_targets:
            log(f"  Firing {len(retry_targets)} retry booking(s) in parallel")
            results = fire_all_bookings(token, retry_targets)
            for label, result in results:
                status = result.get("status", "UNKNOWN").upper() if isinstance(result, dict) else "UNKNOWN"
                waitlist_pos = result.get("waitlist_position") if isinstance(result, dict) else None
                if status in ("BOOKED", "WAITLISTED", "WAITLIST", "CONFIRMED") or waitlist_pos is not None:
                    all_booked.append(label)

        if all_booked:
            log(f"Booked {len(all_booked)} class(es) so far")
            # Check if there's anything left to book
            if not retry_targets:
                break

        time.sleep(0.3)

    # ---- Summary ----
    elapsed = round(time.time() - loop_start, 1)
    if (time.time() - loop_start) >= MAX_LOOP_SECONDS:
        log(f"Hard timeout reached after {elapsed}s — stopping")

    if past_deadline():
        log(f"Deadline {DEADLINE} reached — ending retry loop")

    log("=== Session complete ===")
    if all_booked:
        log(f"Successfully booked/waitlisted {len(all_booked)} class(es):")
        for c in all_booked:
            log(f"  ✓ {c}")
    elif cycle == 0 and not targets:
        log("No target classes found to book")
    else:
        log(f"No classes booked after {elapsed}s — all slots were taken")


if __name__ == "__main__":
    main()
