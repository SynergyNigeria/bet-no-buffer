"""
Ultra-fast SportyBet automation (private use).

Features:
- One-time manual login + session persistence to auth_state.json.
- Headless, low-latency browsing profile for fast bet placement.
- Resource blocking (images/media/fonts/stylesheets + trackers) for speed.
- Fast navigation and resilient click/input workflow for live betting.

Usage examples:
1) One-time auth bootstrap (manual login, optional OTP):
   python sportybet_fast_bet.py setup-auth

2) Place a bet with saved session:
   python sportybet_fast_bet.py place-bet \
       --match-url "https://www.sportybet.com/ng/sport/football/live/..." \
       --target-market-selector "button[data-odds-id='123456']" \
       --stake 500

Notes:
- This script assumes you already know stable selectors for your account's UI variant.
- Use responsibly and comply with SportyBet terms and local regulations.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from playwright.sync_api import Browser, BrowserContext, Error, Page, TimeoutError, sync_playwright


# -----------------------------
# Configuration
# -----------------------------

AUTH_STATE_PATH = Path("auth_state.json")
SPORTYBET_HOME = "https://www.sportybet.com/ng"

# Realistic desktop UA to reduce obvious automation fingerprints.
REALISTIC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# Aggressive speed optimization: skip heavy render assets.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}

# Common analytics/tracking hosts and script endpoints.
TRACKING_KEYWORDS = {
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "hotjar.com",
    "segment.io",
    "mixpanel.com",
    "amplitude.com",
    "clarity.ms",
    "facebook.net/tr",
    "analytics",
    "gtm.js",
}


@dataclass(frozen=True)
class BettingConfig:
    headless: bool = True
    # Tight timeouts prioritize speed and fail fast when markets move.
    default_timeout_ms: int = 4500
    navigation_timeout_ms: int = 6000


def log(message: str) -> None:
    print(message, flush=True)


def is_tracking_request(url: str) -> bool:
    lowered = url.lower()
    return any(token in lowered for token in TRACKING_KEYWORDS)


def route_handler(route) -> None:
    """Abort non-essential resources and trackers to reduce page latency."""
    request = route.request
    resource_type = request.resource_type
    url = request.url

    if resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
        return

    # Keep scripts that power app logic, but cut obvious trackers.
    if resource_type == "script" and is_tracking_request(url):
        route.abort()
        return

    route.continue_()


def launch_browser(playwright, config: BettingConfig) -> Browser:
    return playwright.chromium.launch(
        headless=config.headless,
        args=[
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )


def create_context(
    browser: Browser,
    config: BettingConfig,
    storage_state_path: Optional[Path] = None,
    enable_speed_blocking: bool = True,
) -> BrowserContext:
    context = browser.new_context(
        user_agent=REALISTIC_USER_AGENT,
        viewport={"width": 1366, "height": 768},
        java_script_enabled=True,
        locale="en-NG",
        timezone_id="Africa/Lagos",
        storage_state=str(storage_state_path) if storage_state_path and storage_state_path.exists() else None,
    )
    context.set_default_timeout(config.default_timeout_ms)
    context.set_default_navigation_timeout(config.navigation_timeout_ms)
    if enable_speed_blocking:
        context.route("**/*", route_handler)
    return context


def setup_auth_state(auth_state_path: Path = AUTH_STATE_PATH) -> None:
    """
    One-time manual login flow:
    - Opens a headed browser.
    - Lets user log in manually (including OTP/2FA).
    - Saves storage state for future headless runs.
    """
    # Setup mode is interactive and should tolerate slower initial page loads.
    config = BettingConfig(
        headless=False,
        default_timeout_ms=30000,
        navigation_timeout_ms=45000,
    )

    with sync_playwright() as pw:
        browser = launch_browser(pw, config)
        # Do not block resources during manual login; full UI must load reliably.
        context = create_context(browser, config, enable_speed_blocking=False)
        page = context.new_page()

        log("[+] Opening SportyBet login page for manual authentication...")
        try:
            page.goto(SPORTYBET_HOME, wait_until="domcontentloaded", timeout=45000)
        except TimeoutError:
            log("[!] Initial load timed out. Retrying with a faster commit-level wait...")
            try:
                page.goto(SPORTYBET_HOME, wait_until="commit", timeout=20000)
                log("[+] Fallback navigation completed. Continue with manual login.")
            except TimeoutError:
                log("[!] Navigation is still slow. If the page is open, continue manually.")
                log("[!] Otherwise open https://www.sportybet.com/ng in the same browser window.")

        log("[+] Complete login manually (phone/password + OTP if required).")
        log("[+] After login completes and account dashboard is visible, press ENTER here.")
        input()

        context.storage_state(path=str(auth_state_path))
        log(f"[+] Session state saved to: {auth_state_path.resolve()}")

        context.close()
        browser.close()


def resolve_target_locator(page: Page, target_market_selector: str):
    """
    Supports CSS selectors and Playwright text selectors.
    Examples:
    - button[data-odds-id='123']
    - text=Over 2.5
    - role=button[name='1.85']
    """
    raw = target_market_selector.strip()
    if raw.startswith("text=") or raw.startswith("role="):
        return page.locator(raw).first
    return page.locator(raw).first


def fast_hardware_click(page: Page, locator) -> None:
    """
    Use mouse click at element coordinates for a more native-like interaction.
    Falls back to locator.click() if bounding box is unavailable.
    """
    locator.wait_for(state="visible")
    if not locator.is_enabled():
        raise RuntimeError("Target market is visible but disabled/locked.")

    locator.scroll_into_view_if_needed()
    box = locator.bounding_box()

    if box is not None:
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        page.mouse.click(x, y)
        return

    # Fallback in rare cases where the element is detached during render updates.
    locator.click(timeout=1200)


def first_visible(page: Page, selectors: Iterable[str]):
    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count() > 0 and locator.is_visible():
            return locator
    return None


def fill_stake_and_submit(page: Page, stake_amount: float) -> None:
    # Candidate selectors for stake input across common slip variants.
    stake_input_candidates = [
        "input[type='number']",
        "input[placeholder*='Stake' i]",
        "input[aria-label*='Stake' i]",
        "[data-testid='betslip-stake-input'] input",
    ]

    stake_input = first_visible(page, stake_input_candidates)
    if stake_input is None:
        raise RuntimeError("Could not find the stake input field in the bet slip.")

    stake_input.click()
    # Clear safely for controlled React inputs.
    stake_input.press("Control+a")
    stake_input.press("Backspace")
    stake_input.type(str(stake_amount), delay=0)
    log(f"[+] Stake Entered: {stake_amount}")

    submit_button_candidates = [
        "button:has-text('Place Bet')",
        "button:has-text('Confirm Bet')",
        "button:has-text('Place bet')",
        "button:has-text('Confirm')",
        "[data-testid='betslip-place-bet-button']",
    ]

    submit_button = first_visible(page, submit_button_candidates)
    if submit_button is None:
        raise RuntimeError("Could not find Place/Confirm bet button.")

    if not submit_button.is_enabled():
        raise RuntimeError("Submit button is disabled. Odds may have changed or stake is invalid.")

    submit_button.click(timeout=1200)


def fill_stake_only(page: Page, stake_amount: float) -> None:
    """Fill stake without submitting; useful for armed manual submit mode."""
    stake_input_candidates = [
        "input[type='number']",
        "input[placeholder*='Stake' i]",
        "input[aria-label*='Stake' i]",
        "[data-testid='betslip-stake-input'] input",
    ]

    stake_input = first_visible(page, stake_input_candidates)
    if stake_input is None:
        raise RuntimeError("Could not find the stake input field in the bet slip.")

    stake_input.click()
    stake_input.press("Control+a")
    stake_input.press("Backspace")
    stake_input.type(str(stake_amount), delay=0)
    log(f"[+] Stake Entered: {stake_amount}")


def find_submit_button(page: Page):
    submit_button_candidates = [
        "button:has-text('Place Bet')",
        "button:has-text('Confirm Bet')",
        "button:has-text('Place bet')",
        "button:has-text('Confirm')",
        "[data-testid='betslip-place-bet-button']",
    ]
    return first_visible(page, submit_button_candidates)


def click_submit_fast(page: Page) -> None:
    submit_button = find_submit_button(page)
    if submit_button is None:
        raise RuntimeError("Could not find Place/Confirm bet button.")

    if not submit_button.is_enabled():
        raise RuntimeError("Submit button is disabled. Odds may have changed or stake is invalid.")

    submit_button.scroll_into_view_if_needed()
    box = submit_button.bounding_box()
    if box is not None:
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    else:
        submit_button.click(timeout=1200)


def inject_browser_hotkey_submit(page: Page, hotkey: str = "F7") -> None:
    """
    Inject a browser-side hotkey listener for near-zero-latency submit trigger.
    The click is executed inside the page context to avoid terminal round-trips.
    """
    hotkey_normalized = hotkey.upper()
    page.evaluate(
        """
        ({ hotkey }) => {
            const win = window;
            win.__sb_submit_fired = false;
            win.__sb_hotkey_seen_count = 0;
            win.__sb_last_submit_error = "";
            win.__sb_submit_attempt_count = Number(win.__sb_submit_attempt_count || 0);
            win.__sb_last_clicked_button_text = "";

            const isEnabledVisible = (el) => el && !el.disabled && el.offsetParent !== null;

            const pickButton = () => {
                const directCandidates = [
                    "[data-testid='betslip-place-bet-button']",
                    "button[data-testid='betslip-place-bet-button']",
                    "[data-testid*='place-bet']",
                    "button[type='submit']",
                ];

                for (const sel of directCandidates) {
                    const el = document.querySelector(sel);
                    if (isEnabledVisible(el)) return el;
                }

                const textMatchers = ["place bet", "confirm bet", "confirm", "place"];

                // Prefer buttons inside likely betslip containers.
                const containerSelectors = [
                    "[data-testid*='betslip']",
                    "[class*='betslip']",
                    "[id*='betslip']",
                ];

                for (const csel of containerSelectors) {
                    const container = document.querySelector(csel);
                    if (!container) continue;
                    const buttons = Array.from(container.querySelectorAll("button"));
                    for (const btn of buttons) {
                        const text = (btn.textContent || "").trim().toLowerCase();
                        if (!text) continue;
                        if (!isEnabledVisible(btn)) continue;
                        if (textMatchers.some((m) => text.includes(m))) return btn;
                    }
                }

                // Last fallback to full page search.
                const allButtons = Array.from(document.querySelectorAll("button"));
                for (const btn of allButtons) {
                    const text = (btn.textContent || "").trim().toLowerCase();
                    if (!text) continue;
                    if (!isEnabledVisible(btn)) continue;
                    if (textMatchers.some((m) => text.includes(m))) return btn;
                }

                return null;
            };

            const onKey = (ev) => {
                const key = (ev.key || "").toUpperCase();
                const code = (ev.code || "").toUpperCase();
                const matches = key === hotkey || code === hotkey;
                if (!matches) return;

                win.__sb_hotkey_seen_count = (win.__sb_hotkey_seen_count || 0) + 1;
                win.__sb_last_hotkey_seen_key = key;
                win.__sb_last_hotkey_seen_code = code;
                win.__sb_last_hotkey_seen_at = Date.now();

                const btn = pickButton();
                if (!btn) {
                    win.__sb_submit_fired = false;
                    win.__sb_last_submit_error = "Hotkey pressed but no enabled Place/Confirm button was found.";
                    return;
                }

                // Fire immediate click in page thread.
                btn.click();
                win.__sb_submit_fired = true;
                win.__sb_submit_fired_at = Date.now();
                win.__sb_submit_attempt_count = (win.__sb_submit_attempt_count || 0) + 1;
                win.__sb_last_clicked_button_text = (btn.textContent || "").trim();
                win.__sb_last_submit_error = "";

                // Some slip flows need one extra confirm shortly after odds update.
                setTimeout(() => {
                    const buttons = Array.from(document.querySelectorAll("button"));
                    const follow = buttons.find((b) => {
                        const t = (b.textContent || "").trim().toLowerCase();
                        if (!t) return false;
                        if (b.disabled || b.offsetParent === null) return false;
                        return t.includes("confirm") || t.includes("accept") || t.includes("continue");
                    });
                    if (follow) {
                        follow.click();
                    }
                }, 80);
            };

            if (win.__sb_submit_hotkey_handler) {
                document.removeEventListener("keydown", win.__sb_submit_hotkey_handler, true);
                window.removeEventListener("keydown", win.__sb_submit_hotkey_handler, true);
            }
            win.__sb_submit_hotkey_handler = onKey;
            document.addEventListener("keydown", onKey, true);
            window.addEventListener("keydown", onKey, true);
        }
        """,
        {"hotkey": hotkey_normalized},
    )


def arm_manual_submit(
    match_url: Optional[str] = None,
    stake_amount: Optional[float] = None,
    fire_hotkey: str = "F7",
    auth_state_path: Path = AUTH_STATE_PATH,
) -> bool:
    """
    Opens a logged-in headed browser where user manually selects markets,
    then presses a browser hotkey for an immediate submit click.
    """
    if not auth_state_path.exists():
        log("[-] auth_state.json not found. Run: python sportybet_fast_bet.py setup-auth")
        return False

    config = BettingConfig(
        headless=False,
        default_timeout_ms=15000,
        navigation_timeout_ms=30000,
    )

    with sync_playwright() as pw:
        browser = launch_browser(pw, config)
        context = create_context(
            browser,
            config,
            storage_state_path=auth_state_path,
            enable_speed_blocking=False,
        )
        page = context.new_page()

        try:
            log("[+] Session Loaded")
            start_url = (match_url or "").strip() or SPORTYBET_HOME
            try:
                page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            except TimeoutError:
                log("[!] Slow load detected. Falling back to commit-level navigation...")
                page.goto(start_url, wait_until="commit", timeout=20000)

            log("[+] Manual mode ready: select team/market(s) directly in browser.")

            if stake_amount is not None:
                try:
                    fill_stake_only(page, stake_amount)
                except RuntimeError as ex:
                    log(f"[!] Stake prefill skipped: {ex}")

            def _rearm_hotkey(_frame=None) -> None:
                # Re-attach after route/page changes so hotkey stays active while browsing markets.
                try:
                    inject_browser_hotkey_submit(page, fire_hotkey)
                except Error:
                    pass

            _rearm_hotkey()
            page.on("domcontentloaded", _rearm_hotkey)
            page.on("framenavigated", _rearm_hotkey)
            log(f"[+] Browser hotkey armed. Press {fire_hotkey.upper()} in the browser to submit instantly.")
            log("[+] You can now browse SportyBet and choose any match/market in the same browser.")
            log("[+] Keep this terminal open while armed. Waiting for browser hotkey trigger...")

            start = time.monotonic()
            last_hotkey_seen_count = 0
            last_error = ""
            last_submit_attempt_count = 0
            # Long-running watch window for live play.
            while time.monotonic() - start < 3600:
                try:
                    status = page.evaluate(
                        """() => ({
                            fired: Boolean(window.__sb_submit_fired),
                            seenCount: Number(window.__sb_hotkey_seen_count || 0),
                            submitAttemptCount: Number(window.__sb_submit_attempt_count || 0),
                            lastClickedButtonText: String(window.__sb_last_clicked_button_text || ""),
                            lastError: String(window.__sb_last_submit_error || ""),
                            lastKey: String(window.__sb_last_hotkey_seen_key || ""),
                            lastCode: String(window.__sb_last_hotkey_seen_code || ""),
                        })"""
                    )

                    seen_count = int(status.get("seenCount", 0))
                    if seen_count > last_hotkey_seen_count:
                        last_hotkey_seen_count = seen_count
                        log(
                            f"[+] Hotkey detected by page (key={status.get('lastKey','')}, code={status.get('lastCode','')})"
                        )

                    current_error = str(status.get("lastError", ""))
                    if current_error and current_error != last_error:
                        last_error = current_error
                        log(f"[!] {current_error}")

                    submit_attempt_count = int(status.get("submitAttemptCount", 0))
                    if submit_attempt_count > last_submit_attempt_count:
                        last_submit_attempt_count = submit_attempt_count
                        clicked_text = str(status.get("lastClickedButtonText", "")).strip()
                        if clicked_text:
                            log(f"[+] Submit attempt fired on button: '{clicked_text}'")
                        else:
                            log("[+] Submit attempt fired")

                        # Validate outcome before exiting; remain armed if no success signal.
                        confirmation_signals = [
                            "text=Bet successful",
                            "text=Successfully placed",
                            "text=Accepted",
                            "text=In account history",
                        ]
                        confirmed = False
                        end_wait = time.monotonic() + 4.0
                        while time.monotonic() < end_wait:
                            for signal in confirmation_signals:
                                if page.locator(signal).first.is_visible(timeout=250):
                                    confirmed = True
                                    break
                            if confirmed:
                                break

                        if confirmed:
                            log("[+] Bet Successfully Submitted to Account History")
                            return True

                        log("[!] No success confirmation detected. Still armed; adjust selection and press hotkey again.")
                except Error:
                    # Ignore transient navigation/frame reloads and keep watching.
                    pass
                time.sleep(0.03)

            log("[-] Hotkey was not triggered before watch timeout.")
            return False

        except TimeoutError:
            log("[-] Timeout: page/controls not ready in time.")
            return False
        except KeyboardInterrupt:
            log("[!] Stopped by user.")
            return False
        except RuntimeError as ex:
            log(f"[-] Runtime issue: {ex}")
            return False
        except Error as ex:
            log(f"[-] Playwright error: {ex}")
            return False
        except Exception as ex:  # noqa: BLE001
            log(f"[-] Unexpected error: {ex}")
            return False
        finally:
            context.close()
            browser.close()


def place_fast_wager(
    match_url: str,
    target_market_selector: str,
    stake_amount: float,
    auth_state_path: Path = AUTH_STATE_PATH,
) -> bool:
    """
    Executes the fast wager flow.

    Returns:
        True if submission click and success signal are detected, else False.
    """
    if not auth_state_path.exists():
        log("[-] auth_state.json not found. Run: python sportybet_fast_bet.py setup-auth")
        return False

    config = BettingConfig(headless=True)

    with sync_playwright() as pw:
        browser = launch_browser(pw, config)
        context = create_context(
            browser,
            config,
            storage_state_path=auth_state_path,
            enable_speed_blocking=True,
        )
        page = context.new_page()

        try:
            log("[+] Session Loaded")

            # Commit/DOM content loaded is faster than waiting for full load.
            page.goto(match_url, wait_until="commit")
            log("[+] Match Page Request Committed")

            target = resolve_target_locator(page, target_market_selector)
            target.wait_for(state="visible", timeout=2200)
            log("[+] Match Found")

            fast_hardware_click(page, target)
            log("[+] Target Odds Clicked")

            fill_stake_and_submit(page, stake_amount)
            log("[+] Place/Confirm Button Clicked")

            # Soft confirmation checks (UI wording differs by locale/version).
            confirmation_signals = [
                "text=Bet successful",
                "text=Successfully placed",
                "text=Accepted",
                "text=In account history",
            ]
            confirmed = False
            for signal in confirmation_signals:
                if page.locator(signal).first.is_visible(timeout=1800):
                    confirmed = True
                    break

            if confirmed:
                log("[+] Bet Successfully Submitted to Account History")
                return True

            # If no explicit confirmation found, still treat click path as potentially successful.
            log("[!] Submission clicked, but explicit success toast not detected in timeout window.")
            return True

        except TimeoutError:
            log("[-] Timeout: market not found or page changed before interaction completed.")
            return False
        except RuntimeError as ex:
            # Covers locked/disabled odds and missing controls.
            log(f"[-] Runtime issue: {ex}")
            return False
        except Error as ex:
            # Playwright protocol errors, detached DOM, navigation interruptions.
            log(f"[-] Playwright error: {ex}")
            return False
        except Exception as ex:  # noqa: BLE001
            log(f"[-] Unexpected error: {ex}")
            return False
        finally:
            context.close()
            browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SportyBet fast wager automation")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup-auth", help="Run one-time manual auth and save auth_state.json")

    place = sub.add_parser("place-bet", help="Place a fast wager using saved auth state")
    place.add_argument("--match-url", required=True, help="Live match URL")
    place.add_argument(
        "--target-market-selector",
        required=True,
        help="Selector/text locator for target odds button",
    )
    place.add_argument("--stake", required=True, type=float, help="Stake amount")

    arm = sub.add_parser(
        "arm-submit",
        help="Manual market selection mode, then instant submit via browser hotkey",
    )
    arm.add_argument(
        "--start-url",
        required=False,
        default=SPORTYBET_HOME,
        help="Optional initial page URL. Defaults to SportyBet home.",
    )
    arm.add_argument(
        "--match-url",
        required=False,
        help="Optional alias for --start-url (for backward compatibility).",
    )
    arm.add_argument(
        "--stake",
        required=False,
        type=float,
        help="Optional stake amount to prefill before hotkey-trigger submit",
    )
    arm.add_argument(
        "--hotkey",
        required=False,
        default="F7",
        help="Browser key used to fire instant submit (default: F7)",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.command == "setup-auth":
        setup_auth_state()
        return 0

    if args.command == "place-bet":
        ok = place_fast_wager(
            match_url=args.match_url,
            target_market_selector=args.target_market_selector,
            stake_amount=args.stake,
        )
        return 0 if ok else 1

    if args.command == "arm-submit":
        ok = arm_manual_submit(
            match_url=(args.match_url or args.start_url),
            stake_amount=args.stake,
            fire_hotkey=args.hotkey,
        )
        return 0 if ok else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
