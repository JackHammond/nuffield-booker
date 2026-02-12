import time
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

load_dotenv()

# --- Configuration ---
EMAIL = os.getenv("EMAIL", "")
PASSWORD = os.getenv("PASSWORD", "")
TARGET_CLASSES = ["Reformer Pilates"]
LOGIN_URL = "https://my.nuffieldhealth.com/"
TARGET_URL = os.getenv("TARGET_URL", "https://www.nuffieldhealth.com/gyms/cambridge/timetable")

# Timing: idle until 06:59:55, classes drop at 07:00:00, give up at 07:00:15
WAIT_UNTIL = "06:59:55"
DEADLINE = "10:00:15"
# DEADLINE = "23:59:59"  # Uncomment for testing outside the 7am window

IFRAME_XPATH = "//iframe[contains(@src, 'nh-booking-microsite')]"


def log(msg):
    """Print with timestamp."""
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}")


def create_driver():
    opts = Options()
    for arg in [
        #"--headless", "--window-size=1920,1080",
        "--disable-blink-features=AutomationControlled",
        "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage",
    ]:
        opts.add_argument(arg)
    return webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)


def accept_cookies(driver):
    try:
        WebDriverWait(driver, 3).until(
            EC.element_to_be_clickable((By.ID, "ccc-notify-accept"))
        ).click()
        log("Cookies accepted")
    except Exception:
        pass


def login(driver):
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


def navigate_to_timetable(driver):
    log("Navigating to timetable...")
    driver.get(TARGET_URL)
    accept_cookies(driver)


def switch_to_iframe(driver):
    driver.switch_to.default_content()
    iframe = WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.XPATH, IFRAME_XPATH))
    )
    driver.switch_to.frame(iframe)


def verify_logged_in(driver):
    """Return True if logged in (no 'Log in' button visible)."""
    try:
        driver.find_element(By.CSS_SELECTOR, "button.primary-btn.m--logged_out")
        return False
    except Exception:
        return True


def select_target_date(driver):
    """Click the date button for 8 days from now. Returns True if found & selected."""
    target = datetime.now() + timedelta(days=8)
    pattern = f"{target.day} {target.strftime('%b')}"  # e.g. "20 Feb"

    try:
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CLASS_NAME, "day-toggle__button"))
        )
        for btn in driver.find_elements(By.CLASS_NAME, "day-toggle__button"):
            if pattern in btn.text:
                if "m--active" not in (btn.get_attribute("class") or ""):
                    btn.click()
                log(f"Target date selected: {pattern}")
                return True
    except Exception:
        pass
    return False


def has_available_at_7am_text(driver):
    """Check if the page still shows 'Available at 7am' (pre-release state)."""
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text
        return "available at 7am" in page_text.lower()
    except Exception:
        return False


def wait_for_modal_result(driver):
    """Wait up to 8s for booking confirmation or timeout modal. Returns 'success'/'timeout'/'none'."""
    end = time.time() + 8
    while time.time() < end:
        try:
            modals = driver.find_elements(By.CLASS_NAME, "modal-subtitle")
            if modals and "timed out" in modals[0].text.lower():
                driver.find_element(By.CSS_SELECTOR, ".modal-button.modal-cta__button").click()
                return "timeout"
        except Exception:
            pass
        try:
            done_btns = driver.find_elements(By.CSS_SELECTOR, "button.modal-outline__button")
            if done_btns and "Done" in done_btns[0].text:
                done_btns[0].click()
                return "success"
        except Exception:
            pass
        time.sleep(0.1)
    return "none"


def try_book_classes(driver):
    """Scan page for target classes and attempt to book/waitlist. Returns list of booked class names."""
    booked = []

    try:
        WebDriverWait(driver, 3).until(
            EC.presence_of_element_located((By.CLASS_NAME, "class-content__wrapper"))
        )
    except Exception:
        return booked

    rows = driver.find_elements(By.CLASS_NAME, "class-content__wrapper")

    for idx, row in enumerate(rows):
        try:
            title = row.find_element(By.CLASS_NAME, "class-title").text.strip()
        except Exception:
            continue

        if not any(t.lower() in title.lower() for t in TARGET_CLASSES):
            continue

        try:
            class_time = row.find_element(By.CLASS_NAME, "class-time").text.strip()
        except Exception:
            class_time = "?"

        label = f"{title} @ {class_time}"

        # Already booked or on waitlist?
        try:
            row.find_element(By.CSS_SELECTOR, "button.primary-btn.m--cancel_booking, button.primary-btn.m--leave_waitlist")
            log(f"  Already booked/waitlisted: {label}")
            booked.append(label)
            continue
        except Exception:
            pass

        # Try Book button, then Waitlist button
        btn = None
        for selector in ["button.primary-btn.m--book:not([disabled])", "button.primary-btn.m--join_waitlist:not([disabled])"]:
            try:
                btn = row.find_element(By.CSS_SELECTOR, selector)
                break
            except Exception:
                continue

        if not btn:
            log(f"  No bookable button for: {label}")
            continue

        log(f"  Clicking book/waitlist for: {label}")
        btn.click()

        result = wait_for_modal_result(driver)
        if result == "success":
            log(f"  BOOKED: {label}")
            booked.append(label)
        elif result == "timeout":
            log(f"  Timeout on: {label} — will retry next cycle")
        else:
            log(f"  No confirmation for: {label}")

    return booked


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


def main():
    log("=== Nuffield Booker ===")

    if not EMAIL or not PASSWORD:
        log("ERROR: EMAIL and PASSWORD env vars must be set")
        return

    driver = create_driver()
    try:
        # Step 1: Login & navigate (do this before idling so we're ready)
        login(driver)
        navigate_to_timetable(driver)
        switch_to_iframe(driver)

        if not verify_logged_in(driver):
            log("LOGIN FAILED — aborting")
            return

        log("Logged in successfully")

        # Select the target date now while we wait
        if not select_target_date(driver):
            log("Target date not visible yet — will retry after idle")

        # Step 2: Idle until 06:59:55
        idle_until_ready()

        # Step 3: Rapid-fire booking loop until 07:00:15
        cycle = 0
        all_booked = []

        if past_deadline():
            log(f"Current time is past {DEADLINE} — booking window has closed, nothing to do")
        else:
            log(f"Booking window open — refreshing until {DEADLINE}")

        while not past_deadline():
            cycle += 1
            log(f"--- Cycle {cycle} ---")

            # Refresh page & re-enter iframe
            driver.switch_to.default_content()
            driver.refresh()
            switch_to_iframe(driver)

            # Select target date
            if not select_target_date(driver):
                log("Target date not found, retrying...")
                continue

            # If still showing "Available at 7am", classes haven't dropped yet
            if has_available_at_7am_text(driver):
                log("Classes not released yet (Available at 7am) — refreshing...")
                continue

            # Try to book
            booked = try_book_classes(driver)
            all_booked.extend(booked)

            if all_booked:
                log(f"Successfully booked {len(all_booked)} class(es) — done!")
                break

        # Summary
        log("=== Session complete ===")
        if all_booked:
            log(f"Booked {len(all_booked)} class(es) in {cycle} cycle(s):")
            for c in all_booked:
                log(f"  Booked: {c}")
        elif cycle == 0:
            log("No booking cycles ran — script started after the 07:00:15 deadline")
        else:
            log(f"No classes booked after {cycle} cycle(s) — all slots were taken")

    finally:
        driver.quit()


if __name__ == "__main__":
    main()
