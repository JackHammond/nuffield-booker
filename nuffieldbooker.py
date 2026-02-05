import time
import random
import os
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
EMAIL = os.getenv("EMAIL", "your_email@example.com")
PASSWORD = os.getenv("PASSWORD", "your_password")
TARGET_CLASSES = [
    "Reformer Pilates",
]
LOGIN_URL = "https://my.nuffieldhealth.com/"
TARGET_URL = os.getenv("TARGET_URL", "https://www.nuffieldhealth.com/gyms/cambridge/timetable")
BOOKING_TIMEOUT_SECONDS = 150  # How long to keep trying to book classes

# --- Setup Driver ---
chrome_options = Options()
chrome_options.add_argument("--headless")  # Run without a window for automation
chrome_options.add_argument("--window-size=1920,1080")
chrome_options.add_argument("--disable-blink-features=AutomationControlled")
chrome_options.add_argument("--disable-gpu")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")  # For GitHub Actions

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)

def quick_delay(min_sec=0.3, max_sec=0.8):
    """Short delay for automation speed."""
    time.sleep(random.uniform(min_sec, max_sec))

def handle_login():
    try:
        print("Checking for login fields...")
        wait = WebDriverWait(driver, 4)
        email_field = wait.until(EC.presence_of_element_located((By.ID, "email")))
        pass_field = driver.find_element(By.ID, "password")
        next_btn = driver.find_element(By.ID, "next")

        email_field.send_keys(EMAIL)
        pass_field.send_keys(PASSWORD)
        next_btn.click()
        print("Login submitted.")
    except Exception as e:
        print("Login fields not found or already logged in.")

def handle_parent_page():
    """Accept cookies if the banner is present."""
    try:
        wait = WebDriverWait(driver, 5)
        cookie_btn = wait.until(EC.element_to_be_clickable((By.ID, "ccc-notify-accept")))
        cookie_btn.click()
        print("✓ Cookies accepted.")
        time.sleep(0.5)  # Brief pause after accepting cookies
    except:
        print("No cookie banner found or already accepted.")

def click_login_button():
    """Clicks the 'Log in' button inside the booking iframe."""
    try:
        print("Waiting for booking iframe...")
        wait = WebDriverWait(driver, 15)
        iframe = wait.until(EC.presence_of_element_located((By.XPATH, "//iframe[contains(@src, 'nh-booking-microsite')]")))
        driver.switch_to.frame(iframe)
        print("Switched to booking iframe.")

        # Click the first "Log in" button
        login_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button.primary-btn.m--logged_out")))
        print("Clicking 'Log in' button...")
        quick_delay()
        login_btn.click()
        
        # Switch back to main content for login form
        driver.switch_to.default_content()
        time.sleep(1)
    except Exception as e:
        print(f"Could not click login button: {e}")
        driver.switch_to.default_content()

def monitor_booking_result():
    """Handles the 'Done' success modal or the 'Timeout' error modal."""
    start_time = time.time()
    while (time.time() - start_time) < 10:
        # 1. Check for Timeout Modal
        try:
            timeout_modal = driver.find_elements(By.CLASS_NAME, "modal-subtitle")
            if timeout_modal and "Connection timed out" in timeout_modal[0].text:
                ok_btn = driver.find_element(By.CSS_SELECTOR, ".modal-button.modal-cta__button")
                print("Timeout detected. Clicking OK...")
                quick_delay()
                ok_btn.click()
                return "timeout"
        except:
            pass

        # 2. Check for Done Button (Success)
        try:
            done_btn = driver.find_elements(By.CSS_SELECTOR, "button.modal-outline__button")
            if done_btn and "Done" in done_btn[0].text:
                print("Booking successful! Clicking Done.")
                quick_delay()
                done_btn[0].click()
                time.sleep(0.5)
                return "success"
        except:
            pass
        
        time.sleep(0.3)
    return "not_found"

def check_target_date_available():
    """Checks if the target date (8 days ahead) is available in the date list."""
    try:
        # Calculate target date (8 days from today)
        target_date = datetime.now() + timedelta(days=8)
        target_day = target_date.strftime("%d").lstrip("0")  # Day without leading zero (e.g., "5", "13")
        target_month_short = target_date.strftime("%b")  # Short month like "Feb" (proper case)
        target_pattern = f"{target_day} {target_month_short}"  # e.g., "13 Feb"
        
        # Wait for date buttons to load
        wait = WebDriverWait(driver, 10)
        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "day-toggle__button")))
        
        date_btns = driver.find_elements(By.CLASS_NAME, "day-toggle__button")
        if not date_btns:
            return False, None
        
        # Check if target date exists in the list
        for btn in date_btns:
            btn_text = btn.text.strip()  # Format: "Fri\n13 Feb" or "Fri 13 Feb"
            # Check if target pattern (e.g., "13 Feb") is in the button text
            if target_pattern in btn_text:
                print(f"✓ Target date found: {btn_text.replace(chr(10), ' ')}")
                return True, btn
        
        # Target date not found yet
        last_date_text = date_btns[-1].text.strip().replace(chr(10), ' ')
        print(f"⏳ Target date ({target_pattern}) not available yet. Last date: {last_date_text}")
        return False, None
    except Exception as e:
        print(f"Error checking dates: {e}")
        return False, None

def select_target_date():
    """Selects the target date (8 days ahead) if available and not already active."""
    try:
        available, target_btn = check_target_date_available()
        if not available or not target_btn:
            return False
        
        class_attr = target_btn.get_attribute("class") or ""
        
        if "m--active" not in class_attr:
            print(f"Selecting target date: {target_btn.text.strip()}")
            target_btn.click()
            time.sleep(0.5)
        else:
            print(f"Target date already selected: {target_btn.text.strip()}")
        return True
    except Exception as e:
        print(f"Error selecting date: {e}")
        return False

def refresh_and_switch_to_iframe():
    """Refreshes the page and switches back to the booking iframe."""
    print("Refreshing page...")
    driver.switch_to.default_content()
    driver.refresh()
    
    # Re-enter the iframe (wait handles the timing)
    wait = WebDriverWait(driver, 15)
    iframe = wait.until(EC.presence_of_element_located((By.XPATH, "//iframe[contains(@src, 'nh-booking-microsite')]")))
    driver.switch_to.frame(iframe)
    time.sleep(0.5)

def start_continuous_booking():
    """Continuously try to book classes for BOOKING_TIMEOUT_SECONDS."""
    start_time = time.time()
    cycle = 0
    booked_classes = []
    failed_classes = set()  # Track classes that failed twice
    
    print(f"\n=== Target classes: {', '.join(TARGET_CLASSES)} ===")
    print(f"=== Running for {BOOKING_TIMEOUT_SECONDS} seconds ===")
    
    while (time.time() - start_time) < BOOKING_TIMEOUT_SECONDS:
        cycle += 1
        elapsed = int(time.time() - start_time)
        remaining = BOOKING_TIMEOUT_SECONDS - elapsed
        print(f"\n[{elapsed}s / {BOOKING_TIMEOUT_SECONDS}s] === Scan cycle {cycle} | {remaining}s remaining ===")
        print(f"Checking for target date (8 days ahead)...")
        
        # Check if target date is available and select it
        if not select_target_date():
            print("Target date not available yet, refreshing in 2 seconds...")
            time.sleep(2)
            refresh_and_switch_to_iframe()
            continue
        
        print("Searching for matching classes...")
        
        try:
            # Wait for class list to load
            wait = WebDriverWait(driver, 5)
            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "class-content__wrapper")))
            
            # Refresh rows each iteration to avoid stale elements
            rows = driver.find_elements(By.CLASS_NAME, "class-content__wrapper")
            
            print(f"Found {len(rows)} total classes on page")
            print(f"Looking for: {', '.join(TARGET_CLASSES)}")
            
            # First pass: identify all matching classes
            matching_classes_info = []
            
            for idx, row in enumerate(rows):
                try:
                    title_el = row.find_element(By.CLASS_NAME, "class-title")
                    title_text = title_el.text.strip()
                    
                    # Check if title matches targets
                    if any(target.lower() in title_text.lower() for target in TARGET_CLASSES):
                        try:
                            time_el = row.find_element(By.CLASS_NAME, "class-time")
                            time_text = time_el.text.strip()
                            class_id = f"{title_text}|{time_text}"
                        except:
                            class_id = f"{title_text}|{idx}"
                            time_text = "unknown time"
                        
                        matching_classes_info.append({
                            'idx': idx,
                            'title': title_text,
                            'time': time_text,
                            'id': class_id
                        })
                except:
                    continue
            
            if matching_classes_info:
                print(f"\nFound {len(matching_classes_info)} matching class(es):")
                for info in matching_classes_info:
                    print(f"  - {info['title']} at {info['time']}")
            
            # Check if any matching classes exist (bookable or not)
            matching_classes_exist = len(matching_classes_info) > 0
            bookable_classes_exist = False
            
            # Second pass: try to book each matching class
            for class_info in matching_classes_info:
                class_id = class_info['id']
                title_text = class_info['title']
                time_text = class_info['time']
                
                current_time = time.strftime('%H:%M:%S')
                print(f"\n[PROCESSING @ {current_time}] {title_text} at {time_text}")
                
                # Skip if already booked or failed this specific instance
                if class_id in booked_classes:
                    print(f"  Status: Already processed (booked/waitlisted)")
                    continue
                
                if class_id in failed_classes:
                    print(f"  Status: Already processed (failed)")
                    continue
                
                # Re-fetch rows to avoid stale element issues
                rows = driver.find_elements(By.CLASS_NAME, "class-content__wrapper")
                
                # Find the correct row again by index
                if class_info['idx'] >= len(rows):
                    print(f"  Status: Class no longer found on page")
                    continue
                
                row = rows[class_info['idx']]
                
                # Try to find Book button first
                book_btn = None
                waitlist_btn = None
                leave_waitlist_btn = None
                cancel_booking_btn = None
                
                # Check for already booked/waitlisted states first
                try:
                    leave_waitlist_btn = row.find_element(By.CSS_SELECTOR, "button.primary-btn.m--leave_waitlist")
                    print(f"  Status: ALREADY ON WAITLIST (skipping)")
                    booked_classes.append(class_id)
                    continue
                except:
                    pass
                
                try:
                    cancel_booking_btn = row.find_element(By.CSS_SELECTOR, "button.primary-btn.m--cancel_booking")
                    print(f"  Status: ALREADY BOOKED (skipping)")
                    booked_classes.append(class_id)
                    continue
                except:
                    pass
                
                # Now check for available booking options
                try:
                    book_btn = row.find_element(By.CSS_SELECTOR, "button.primary-btn.m--book:not([disabled])")
                except:
                    # No book button, try waitlist
                    try:
                        waitlist_btn = row.find_element(By.CSS_SELECTOR, "button.primary-btn.m--join_waitlist:not([disabled])")
                    except:
                        pass
                
                if book_btn:
                    # Book button available
                    bookable_classes_exist = True
                    print(f"  Status: AVAILABLE - Attempting to book...")
                    quick_delay()
                    book_btn.click()
                    
                    result = monitor_booking_result()
                    
                    if result == "success":
                        booked_classes.append(class_id)
                        print(f"  [SUCCESS] Booked: {title_text}")
                    elif result == "timeout":
                        # First timeout - try once more
                        print(f"  [RETRY] Attempting {title_text} again...")
                        quick_delay()
                        # Need to re-find element after modal
                        rows = driver.find_elements(By.CLASS_NAME, "class-content__wrapper")
                        if class_info['idx'] < len(rows):
                            row = rows[class_info['idx']]
                            try:
                                book_btn = row.find_element(By.CSS_SELECTOR, "button.primary-btn.m--book:not([disabled])")
                                book_btn.click()
                                
                                result2 = monitor_booking_result()
                                if result2 == "success":
                                    booked_classes.append(class_id)
                                    print(f"  [SUCCESS] Booked: {title_text} on retry")
                                else:
                                    # Second failure - skip this class
                                    failed_classes.add(class_id)
                                    print(f"  [SKIPPED] {title_text} - Failed twice, moving on")
                            except:
                                failed_classes.add(class_id)
                                print(f"  [SKIPPED] {title_text} - Could not retry")
                        else:
                            failed_classes.add(class_id)
                            print(f"  [SKIPPED] {title_text} - Could not retry")
                    
                elif waitlist_btn:
                    # Waitlist button available - join it
                    bookable_classes_exist = True
                    print(f"  Status: FULLY BOOKED - Joining waitlist...")
                    quick_delay()
                    waitlist_btn.click()
                    
                    result = monitor_booking_result()
                    
                    if result == "success":
                        booked_classes.append(class_id)
                        print(f"  [SUCCESS] Joined waitlist: {title_text}")
                    elif result == "timeout":
                        # First timeout - try once more
                        print(f"  [RETRY] Attempting waitlist for {title_text} again...")
                        quick_delay()
                        rows = driver.find_elements(By.CLASS_NAME, "class-content__wrapper")
                        if class_info['idx'] < len(rows):
                            row = rows[class_info['idx']]
                            try:
                                waitlist_btn = row.find_element(By.CSS_SELECTOR, "button.primary-btn.m--join_waitlist:not([disabled])")
                                waitlist_btn.click()
                                
                                result2 = monitor_booking_result()
                                if result2 == "success":
                                    booked_classes.append(class_id)
                                    print(f"  [SUCCESS] Joined waitlist: {title_text} on retry")
                                else:
                                    failed_classes.add(class_id)
                                    print(f"  [SKIPPED] {title_text} - Waitlist failed twice")
                            except:
                                failed_classes.add(class_id)
                                print(f"  [SKIPPED] {title_text} - Could not retry waitlist")
                        else:
                            failed_classes.add(class_id)
                            print(f"  [SKIPPED] {title_text} - Could not retry waitlist")
                else:
                    # No button available - fully booked with no waitlist
                    print(f"  Status: FULLY BOOKED (no waitlist available)")
                    continue

            # Log summary of scan
            if not matching_classes_exist:
                # No matching classes found at all, refresh and try again
                print(f"❌ None found")
                time.sleep(2)
                refresh_and_switch_to_iframe()
            elif not bookable_classes_exist and (booked_classes or failed_classes):
                # We've already processed all matching classes, job done
                print("✓ All matching classes already booked or failed. Session complete.")
                break
            # If classes exist but none bookable and nothing processed yet, 
            # continue loop (they might be fully booked, keep checking for openings)
                
        except Exception as e:
            print(f"Error during scan: {e}")
            time.sleep(1)
    
    print(f"\n=== Booking session complete ===")
    print(f"Total time: {int(time.time() - start_time)}s")
    print(f"\nClasses successfully booked/waitlisted: {len(booked_classes)}")
    if booked_classes:
        for c in booked_classes:
            print(f"  ✓ {c}")
    else:
        print("  (none)")
    
    print(f"\nClasses skipped/failed: {len(failed_classes)}")
    if failed_classes:
        for c in failed_classes:
            print(f"  ✗ {c}")
    else:
        print("  (none)")

def main():
    print(f"=== Nuffield Booker Started at {time.strftime('%H:%M:%S')} ===")
    try:
        # --- Step 1: Login at my.nuffieldhealth.com ---
        print(f"Navigating to login page: {LOGIN_URL}")
        driver.get(LOGIN_URL)
        time.sleep(1)
        
        # Accept cookies on login page if present
        handle_parent_page()
        
        # Handle login form
        handle_login()
        time.sleep(1)
        
        # --- Step 2: Navigate to timetable ---
        print(f"Navigating to timetable: {TARGET_URL}")
        driver.get(TARGET_URL)
        time.sleep(1)
        
        # Accept cookies on timetable page FIRST before interacting with page
        print("Checking for cookie banner on timetable page...")
        handle_parent_page()
        time.sleep(0.5)

        # --- Switch to Iframe ---
        print("Waiting for booking iframe...")
        wait = WebDriverWait(driver, 15)
        iframe = wait.until(EC.presence_of_element_located((By.XPATH, "//iframe[contains(@src, 'nh-booking-microsite')]")))
        driver.switch_to.frame(iframe)
        print("Switched to booking iframe.")

        # --- Verify Login Status ---
        print("Verifying login status...")
        time.sleep(1)  # Give the iframe time to load
        
        # Check if "Log in" button still exists (which means not logged in)
        try:
            logout_btn = driver.find_element(By.CSS_SELECTOR, "button.primary-btn.m--logged_out")
            if logout_btn:
                print("\n" + "="*60)
                print("❌ LOGIN FAILED")
                print("="*60)
                print("The 'Log in' button is still showing, which means login was unsuccessful.")
                print("="*60 + "\n")
                driver.switch_to.default_content()
                return
        except:
            # Button not found - good, we're logged in
            pass
        
        print("✓ Login verified - user is logged in")

        # Wait for date buttons to be available
        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "day-toggle__button")))

        start_continuous_booking()

    finally:
        driver.quit()

if __name__ == "__main__":
    main()
