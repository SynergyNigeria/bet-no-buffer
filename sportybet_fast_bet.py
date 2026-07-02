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
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, TypedDict

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

# API path hints to detect bet placement calls in latency profiling.
BET_API_HINTS = {
    "bet",
    "betslip",
    "place",
    "wager",
    "ticket",
    "coupon",
    "submit",
    "stake",
    "order",
}


@dataclass(frozen=True)
class BettingConfig:
    headless: bool = True
    # Tight timeouts prioritize speed and fail fast when markets move.
    default_timeout_ms: int = 4500
    navigation_timeout_ms: int = 6000


class LatencyState(TypedDict):
    capture_from_ms: Optional[float]
    first_req_ms: Optional[float]
    first_req_url: Optional[str]
    first_req_method: Optional[str]
    first_resp_ms: Optional[float]
    first_resp_url: Optional[str]
    first_resp_status: Optional[int]
    fallback_req_ms: Optional[float]
    fallback_req_url: Optional[str]
    fallback_req_method: Optional[str]
    fallback_resp_ms: Optional[float]
    fallback_resp_url: Optional[str]
    fallback_resp_status: Optional[int]


def log(message: str) -> None:
    print(message, flush=True)


def safe_close_page(page: Optional[Page]) -> None:
    """Best-effort page close that suppresses teardown-time interruptions/errors."""
    if page is None:
        return
    try:
        page.close()
    except (KeyboardInterrupt, Error):
        pass
    except Exception:
        pass


def safe_close_context(context: Optional[BrowserContext]) -> None:
    """Best-effort context close that suppresses teardown-time interruptions/errors."""
    if context is None:
        return
    try:
        context.close()
    except (KeyboardInterrupt, Error):
        pass
    except Exception:
        pass


def safe_close_browser(browser: Optional[Browser]) -> None:
    """Best-effort browser close that suppresses teardown-time interruptions/errors."""
    if browser is None:
        return
    try:
        browser.close()
    except (KeyboardInterrupt, Error):
        pass
    except Exception:
        pass


def is_tracking_request(url: str) -> bool:
    lowered = url.lower()
    return any(token in lowered for token in TRACKING_KEYWORDS)


def is_likely_bet_api(url: str) -> bool:
    lowered = url.lower()
    return any(token in lowered for token in BET_API_HINTS)


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

        safe_close_context(context)
        safe_close_browser(browser)


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


def trigger_submit_attempt(page: Page, trigger_mode: str = "repeat-burst") -> bool:
    """Click the current submit button and stamp the page state for latency tracking."""
    submit_button = find_submit_button(page)
    if submit_button is None:
        return False

    if not submit_button.is_enabled():
        return False

    clicked_text = (submit_button.text_content() or "").strip()
    click_submit_fast(page)
    page.evaluate(
        """({ triggerMode, clickedText }) => {
            const win = window;
            win.__sb_submit_fired = true;
            win.__sb_submit_fired_at = Date.now();
            win.__sb_submit_attempt_count = Number(win.__sb_submit_attempt_count || 0) + 1;
            win.__sb_last_clicked_button_text = String(clickedText || "");
            win.__sb_submit_trigger_mode = String(triggerMode || "repeat-burst");
            win.__sb_last_submit_error = "";
        }""",
        {"triggerMode": trigger_mode, "clickedText": clicked_text},
    )
    return True


def capture_market_selection_hints(page: Page) -> list[str]:
    """Best-effort capture of visible market/selection text to reselect the same chip later."""
    hints = page.evaluate(
        """() => {
            const badTexts = new Set([
                "place bet",
                "confirm bet",
                "confirm",
                "place",
                "stake",
                "submit",
                "accept",
                "continue",
                "cancel",
                "remove",
                "clear",
                "subtotal",
                "total",
                "possible winnings",
                "possible win",
                "odds",
            ]);

            const containers = Array.from(document.querySelectorAll(
                [
                    "[data-testid*='betslip']",
                    "[class*='betslip']",
                    "[id*='betslip']",
                    "[data-testid*='selection']",
                    "[class*='selection']",
                    "[id*='selection']",
                    "[data-testid*='coupon']",
                    "[class*='coupon']",
                    "[id*='coupon']",
                ].join(",")
            ));

            const results = [];
            const seen = new Set();

            const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                return style && style.display !== "none" && style.visibility !== "hidden" && el.offsetParent !== null;
            };

            const normalize = (text) => text.replace(/\\s+/g, " ").trim();

            const scoreElement = (el, text) => {
                let score = 0;
                const attrs = [
                    el.getAttribute("data-testid") || "",
                    el.getAttribute("class") || "",
                    el.getAttribute("id") || "",
                    el.getAttribute("aria-label") || "",
                    el.getAttribute("title") || "",
                ].join(" ").toLowerCase();

                if (attrs.includes("betslip")) score += 5;
                if (attrs.includes("selection")) score += 5;
                if (attrs.includes("selected")) score += 4;
                if (attrs.includes("coupon")) score += 3;
                if (attrs.includes("market")) score += 2;
                if (el.tagName === "BUTTON") score += 2;
                if (el.getAttribute("aria-pressed") === "true") score += 3;
                if (el.getAttribute("aria-selected") === "true") score += 3;
                if (text.length <= 55) score += 1;
                return score;
            };

            for (const root of containers) {
                const elements = Array.from(root.querySelectorAll("button, [role='button'], a, span, div"));
                for (const el of elements) {
                    if (!isVisible(el)) continue;
                    const text = normalize(el.innerText || el.textContent || "");
                    if (!text) continue;
                    if (text.length > 70) continue;
                    const lower = text.toLowerCase();
                    if (badTexts.has(lower)) continue;
                    if (lower.includes("place bet") || lower.includes("confirm")) continue;
                    if (lower.includes("stake") || lower.includes("odds")) continue;

                    const score = scoreElement(el, text);
                    if (score <= 0) continue;
                    if (seen.has(text)) continue;
                    seen.add(text);
                    results.push({ text, score });
                }
            }

            results.sort((a, b) => b.score - a.score || a.text.length - b.text.length);
            return results.slice(0, 5).map((item) => item.text);
        }"""
    )
    if not isinstance(hints, list):
        return []
    cleaned: list[str] = []
    for hint in hints:
        if not isinstance(hint, str):
            continue
        text = hint.strip()
        if not text:
            continue
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def reselect_market_from_hints(page: Page, hints: list[str]) -> bool:
    """Try to re-click the same market chip or matching market tile using captured text hints."""
    if not hints:
        return False

    candidate_scopes = [
        "[data-testid*='market']",
        "[class*='market']",
        "[id*='market']",
        "[data-testid*='selection']",
        "[class*='selection']",
        "[id*='selection']",
        "button",
        "[role='button']",
        "a",
        "div",
        "span",
    ]

    for hint in hints:
        exact_locators = [
            page.get_by_text(hint, exact=True),
            page.get_by_role("button", name=hint),
        ]

        for locator in exact_locators:
            try:
                candidate = locator.first
                if candidate.count() > 0 and candidate.is_visible():
                    candidate.scroll_into_view_if_needed()
                    candidate.click(timeout=1000)
                    return True
            except Error:
                pass

        for scope in candidate_scopes:
            try:
                candidate = page.locator(scope).filter(has_text=hint).first
                if candidate.count() > 0 and candidate.is_visible():
                    candidate.scroll_into_view_if_needed()
                    candidate.click(timeout=1000)
                    return True
            except Error:
                continue

    return False


def inject_browser_hotkey_submit(page: Page, hotkey: Optional[str] = None) -> None:
    """
    Inject an in-browser watcher that auto-clicks Place/Confirm as soon as the betslip becomes valid.
    If a hotkey is provided, it acts as an optional manual override.
    """
    hotkey_normalized = (hotkey or "").strip().upper()
    page.evaluate(
        """
        ({ hotkey }) => {
            const win = window;
            win.__sb_submit_fired = false;
            win.__sb_submit_ready_count = 0;
            win.__sb_last_submit_error = "";
            win.__sb_submit_attempt_count = Number(win.__sb_submit_attempt_count || 0);
            win.__sb_last_clicked_button_text = "";
            win.__sb_submit_ready_at = 0;
            win.__sb_submit_trigger_mode = "";

            const hotkeyValue = String(hotkey || "").trim().toUpperCase();

            const isEnabledVisible = (el) => (
                el
                && !el.disabled
                && (el.getAttribute("aria-disabled") || "").toLowerCase() !== "true"
                && el.offsetParent !== null
            );

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

            const fireSubmit = (btn, mode) => {
                if (!btn || win.__sb_submit_fired) return;
                btn.click();
                win.__sb_submit_fired = true;
                win.__sb_submit_fired_at = Date.now();
                win.__sb_submit_attempt_count = (win.__sb_submit_attempt_count || 0) + 1;
                win.__sb_last_clicked_button_text = (btn.textContent || "").trim();
                win.__sb_submit_trigger_mode = mode;
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

            const normalize = (text) => String(text || "").replace(/\\s+/g, " ").trim();
            const isInsideBetSlip = (el) => {
                if (!el || !el.closest) return false;
                return Boolean(el.closest([
                    "[data-testid*='betslip']",
                    "[class*='betslip']",
                    "[id*='betslip']",
                    "[data-testid*='coupon']",
                    "[class*='coupon']",
                    "[id*='coupon']",
                ].join(",")));
            };

            const looksLikeSubmitControl = (el, text) => {
                const lower = normalize(text).toLowerCase();
                if (lower.includes("place bet") || lower.includes("confirm")) return true;
                if (lower === "place" || lower === "accept" || lower === "continue") return true;
                const attrs = [
                    el && el.getAttribute ? el.getAttribute("data-testid") || "" : "",
                    el && el.getAttribute ? el.getAttribute("class") || "" : "",
                    el && el.getAttribute ? el.getAttribute("id") || "" : "",
                ].join(" ").toLowerCase();
                return attrs.includes("place-bet") || attrs.includes("submit");
            };

            const buildMarketInfo = (el) => {
                const dataset = {};
                if (el && el.dataset) {
                    for (const key of Object.keys(el.dataset)) {
                        dataset[key] = el.dataset[key];
                    }
                }
                const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
                return {
                    text: normalize(el.innerText || el.textContent || ""),
                    tag: el.tagName || "",
                    id: el.id || "",
                    className: String(el.className || ""),
                    ariaLabel: el.getAttribute ? el.getAttribute("aria-label") || "" : "",
                    title: el.getAttribute ? el.getAttribute("title") || "" : "",
                    dataTestId: el.getAttribute ? el.getAttribute("data-testid") || "" : "",
                    dataOddsId: el.getAttribute ? el.getAttribute("data-odds-id") || "" : "",
                    dataset,
                    x: rect ? rect.left + rect.width / 2 : 0,
                    y: rect ? rect.top + rect.height / 2 : 0,
                    capturedAt: Date.now(),
                };
            };

            const onMarketClickCapture = (ev) => {
                const raw = ev.target;
                if (!raw || !raw.closest) return;
                const el = raw.closest("button, [role='button'], a, [data-odds-id], [data-testid*='market'], [class*='market']");
                if (!el || !isEnabledVisible(el)) return;
                const text = normalize(el.innerText || el.textContent || "");
                if (!text && !el.getAttribute("data-odds-id") && !el.getAttribute("data-testid")) return;
                if (isInsideBetSlip(el)) return;
                if (looksLikeSubmitControl(el, text)) return;

                win.__sb_last_market_element = el;
                win.__sb_last_market_info = buildMarketInfo(el);
            };

            const clickElement = (el) => {
                if (!isEnabledVisible(el)) return false;
                try {
                    el.scrollIntoView({ block: "center", inline: "center" });
                } catch (e) {}
                el.click();
                return true;
            };

            win.__sb_reclick_last_market = () => {
                const saved = win.__sb_last_market_element;
                if (saved && saved.isConnected && clickElement(saved)) {
                    return true;
                }

                const info = win.__sb_last_market_info || {};
                const candidates = [];
                if (info.dataOddsId) candidates.push(`[data-odds-id="${CSS.escape(info.dataOddsId)}"]`);
                if (info.dataTestId) candidates.push(`[data-testid="${CSS.escape(info.dataTestId)}"]`);

                for (const [key, value] of Object.entries(info.dataset || {})) {
                    if (value) candidates.push(`[data-${key.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase())}="${CSS.escape(String(value))}"]`);
                }

                for (const selector of candidates) {
                    const el = document.querySelector(selector);
                    if (el && clickElement(el)) return true;
                }

                const text = normalize(info.text || info.ariaLabel || info.title || "");
                if (text) {
                    const controls = Array.from(document.querySelectorAll("button, [role='button'], a, [data-odds-id]"));
                    const exact = controls.find((el) => !isInsideBetSlip(el) && normalize(el.innerText || el.textContent || el.getAttribute("aria-label") || "") === text);
                    if (exact && clickElement(exact)) return true;

                    const loose = controls.find((el) => {
                        if (isInsideBetSlip(el)) return false;
                        const value = normalize(el.innerText || el.textContent || el.getAttribute("aria-label") || "");
                        return value && (value.includes(text) || text.includes(value));
                    });
                    if (loose && clickElement(loose)) return true;
                }

                if (Number(info.x || 0) > 0 && Number(info.y || 0) > 0) {
                    const el = document.elementFromPoint(Number(info.x), Number(info.y));
                    const control = el && el.closest ? el.closest("button, [role='button'], a, [data-odds-id]") : null;
                    if (control && !isInsideBetSlip(control) && clickElement(control)) return true;
                }

                return false;
            };

            const markReady = () => {
                const btn = pickButton();
                if (btn) {
                    if (!win.__sb_submit_ready_at) {
                        win.__sb_submit_ready_at = Date.now();
                    }
                    win.__sb_submit_ready_count = (win.__sb_submit_ready_count || 0) + 1;
                    return btn;
                }
                return null;
            };

            const autoWatch = () => {
                if (win.__sb_submit_fired) return;
                const btn = markReady();
                if (btn) {
                    fireSubmit(btn, "auto-watch");
                }
            };

            if (hotkeyValue) {
                const onKey = (ev) => {
                    const key = (ev.key || "").toUpperCase();
                    const code = (ev.code || "").toUpperCase();
                    const matches = key === hotkeyValue || code === hotkeyValue;
                    if (!matches) return;

                    win.__sb_submit_ready_count = (win.__sb_submit_ready_count || 0) + 1;
                    win.__sb_last_hotkey_seen_key = key;
                    win.__sb_last_hotkey_seen_code = code;
                    win.__sb_last_hotkey_seen_at = Date.now();

                    const btn = pickButton();
                    if (!btn) {
                        win.__sb_submit_fired = false;
                        win.__sb_last_submit_error = "Hotkey pressed but no enabled Place/Confirm button was found.";
                        return;
                    }

                    fireSubmit(btn, "hotkey");
                };

                if (win.__sb_submit_hotkey_handler) {
                    document.removeEventListener("keydown", win.__sb_submit_hotkey_handler, true);
                    window.removeEventListener("keydown", win.__sb_submit_hotkey_handler, true);
                }
                win.__sb_submit_hotkey_handler = onKey;
                document.addEventListener("keydown", onKey, true);
                window.addEventListener("keydown", onKey, true);
            }

            if (win.__sb_market_click_capture_handler) {
                document.removeEventListener("click", win.__sb_market_click_capture_handler, true);
            }
            win.__sb_market_click_capture_handler = onMarketClickCapture;
            document.addEventListener("click", onMarketClickCapture, true);

            if (win.__sb_submit_watch_interval) {
                clearInterval(win.__sb_submit_watch_interval);
            }
            if (win.__sb_submit_watch_observer) {
                try { win.__sb_submit_watch_observer.disconnect(); } catch (e) {}
            }

            win.__sb_submit_watch_interval = setInterval(autoWatch, 25);
            try {
                win.__sb_submit_watch_observer = new MutationObserver(autoWatch);
                win.__sb_submit_watch_observer.observe(document.documentElement, {
                    childList: true,
                    subtree: true,
                    attributes: true,
                    attributeFilter: ["disabled", "aria-disabled", "class", "style"],
                });
            } catch (e) {
                // Some pages may temporarily not expose documentElement early in load.
            }

            autoWatch();
        }
        """,
        {"hotkey": hotkey_normalized},
    )


def reset_submit_watcher_state(page: Page) -> None:
    """Reset watcher state so the same slip can be submitted again."""
    page.evaluate(
        """() => {
            const win = window;
            win.__sb_submit_fired = false;
            win.__sb_submit_ready_count = 0;
            win.__sb_last_submit_error = "";
            win.__sb_submit_attempt_count = 0;
            win.__sb_last_clicked_button_text = "";
            win.__sb_submit_ready_at = 0;
            win.__sb_submit_trigger_mode = "";
            win.__sb_submit_fired_at = 0;
        }"""
    )


def reselect_last_market_fast(page: Page) -> bool:
    """Ask the in-browser watcher to re-click the last market the user selected."""
    try:
        return bool(
            page.evaluate(
                """() => {
                    if (typeof window.__sb_reclick_last_market !== "function") {
                        return false;
                    }
                    return Boolean(window.__sb_reclick_last_market());
                }"""
            )
        )
    except Error:
        return False


def arm_manual_submit(
    match_url: Optional[str] = None,
    stake_amount: Optional[float] = None,
    fire_hotkey: Optional[str] = None,
    repeat_count: int = 1,
    repeat_delay_ms: int = 250,
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

        # Attempt-scoped latency capture state.
        latency_state: LatencyState = {
            "capture_from_ms": None,
            "first_req_ms": None,
            "first_req_url": None,
            "first_req_method": None,
            "first_resp_ms": None,
            "first_resp_url": None,
            "first_resp_status": None,
            # Fallback captures first XHR/fetch if no hint-based API is matched.
            "fallback_req_ms": None,
            "fallback_req_url": None,
            "fallback_req_method": None,
            "fallback_resp_ms": None,
            "fallback_resp_url": None,
            "fallback_resp_status": None,
        }

        def _now_ms() -> float:
            return time.time() * 1000.0

        def _reset_latency_capture(capture_from_ms: Optional[float]) -> None:
            latency_state["capture_from_ms"] = capture_from_ms
            latency_state["first_req_ms"] = None
            latency_state["first_req_url"] = None
            latency_state["first_req_method"] = None
            latency_state["first_resp_ms"] = None
            latency_state["first_resp_url"] = None
            latency_state["first_resp_status"] = None
            latency_state["fallback_req_ms"] = None
            latency_state["fallback_req_url"] = None
            latency_state["fallback_req_method"] = None
            latency_state["fallback_resp_ms"] = None
            latency_state["fallback_resp_url"] = None
            latency_state["fallback_resp_status"] = None

        def _on_request(request) -> None:
            capture_from_ms = latency_state["capture_from_ms"]
            if capture_from_ms is None:
                return
            if request.resource_type not in {"xhr", "fetch"}:
                return

            now_ms = _now_ms()
            if now_ms < capture_from_ms:
                return

            url = request.url
            method = request.method
            if is_likely_bet_api(url):
                if latency_state["first_req_ms"] is None:
                    latency_state["first_req_ms"] = now_ms
                    latency_state["first_req_url"] = url
                    latency_state["first_req_method"] = method
            elif latency_state["fallback_req_ms"] is None:
                latency_state["fallback_req_ms"] = now_ms
                latency_state["fallback_req_url"] = url
                latency_state["fallback_req_method"] = method

        def _on_response(response) -> None:
            capture_from_ms = latency_state["capture_from_ms"]
            if capture_from_ms is None:
                return
            request = response.request
            if request.resource_type not in {"xhr", "fetch"}:
                return

            now_ms = _now_ms()
            if now_ms < capture_from_ms:
                return

            url = response.url
            status_code = response.status
            if is_likely_bet_api(url):
                if latency_state["first_resp_ms"] is None:
                    latency_state["first_resp_ms"] = now_ms
                    latency_state["first_resp_url"] = url
                    latency_state["first_resp_status"] = status_code
            elif latency_state["fallback_resp_ms"] is None:
                latency_state["fallback_resp_ms"] = now_ms
                latency_state["fallback_resp_url"] = url
                latency_state["fallback_resp_status"] = status_code

        page.on("request", _on_request)
        page.on("response", _on_response)

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

            target_repeats = max(1, repeat_count)
            completed_submissions = 0

            def _rearm_hotkey(_frame=None) -> None:
                # Re-attach after route/page changes so hotkey stays active while browsing markets.
                try:
                    inject_browser_hotkey_submit(page, fire_hotkey)
                except Error:
                    pass

            _rearm_hotkey()
            page.on("domcontentloaded", _rearm_hotkey)
            page.on("framenavigated", _rearm_hotkey)
            if fire_hotkey:
                log(f"[+] Browser hotkey override armed: {fire_hotkey.upper()}")
            log("[+] Auto-submit watcher armed. Select any match/market in the same browser; submit will fire when the slip becomes valid.")
            log("[+] Keep this terminal open while armed. Waiting for betslip validity...")

            start = time.monotonic()
            last_ready_count = 0
            last_error = ""
            last_submit_attempt_count = 0
            last_submit_attempt_at_ms: Optional[float] = None
            last_market_hints: list[str] = []

            def _handle_confirmed_submission() -> bool:
                """Return True when all requested repeats are complete."""
                nonlocal completed_submissions
                nonlocal last_ready_count, last_error, last_submit_attempt_count, last_submit_attempt_at_ms

                log("[+] Bet Successfully Submitted to Account History")
                completed_submissions += 1
                if completed_submissions >= target_repeats:
                    return True

                log(
                    f"[+] Repeat cycle {completed_submissions}/{target_repeats} complete; re-arming for another submission."
                )
                time.sleep(max(0, repeat_delay_ms) / 1000.0)
                reset_submit_watcher_state(page)
                last_ready_count = 0
                last_error = ""
                last_submit_attempt_count = 0
                last_submit_attempt_at_ms = None
                _reset_latency_capture(None)

                if reselect_last_market_fast(page):
                    log("[+] Re-selected the last clicked market for repeat submission.")
                    time.sleep(0.08)
                elif last_market_hints and reselect_market_from_hints(page, last_market_hints):
                    log("[+] Re-selected the captured market for repeat submission.")
                    time.sleep(0.08)
                else:
                    log("[!] Could not reselect the captured market automatically; watcher remains armed.")

                if trigger_submit_attempt(page, "repeat-burst"):
                    log("[+] Repeat burst submit fired on the same selection.")
                else:
                    log("[!] Repeat burst could not find an enabled submit button yet; watcher remains armed.")

                return False

            # Long-running watch window for live play.
            while time.monotonic() - start < 3600:
                try:
                    status = page.evaluate(
                        """() => ({
                            fired: Boolean(window.__sb_submit_fired),
                            readyCount: Number(window.__sb_submit_ready_count || 0),
                            submitAttemptCount: Number(window.__sb_submit_attempt_count || 0),
                            lastClickedButtonText: String(window.__sb_last_clicked_button_text || ""),
                            lastError: String(window.__sb_last_submit_error || ""),
                            lastKey: String(window.__sb_last_hotkey_seen_key || ""),
                            lastCode: String(window.__sb_last_hotkey_seen_code || ""),
                            lastHotkeySeenAt: Number(window.__sb_last_hotkey_seen_at || 0),
                            submitFiredAt: Number(window.__sb_submit_fired_at || 0),
                            submitReadyAt: Number(window.__sb_submit_ready_at || 0),
                            submitTriggerMode: String(window.__sb_submit_trigger_mode || ""),
                        })"""
                    )

                    ready_count = int(status.get("readyCount", 0))
                    if ready_count > last_ready_count:
                        last_ready_count = ready_count
                        if not last_submit_attempt_count:
                            log("[+] Bet slip is valid; auto-submit watcher is checking for submit button availability.")

                    current_error = str(status.get("lastError", ""))
                    if current_error and current_error != last_error:
                        last_error = current_error
                        log(f"[!] {current_error}")

                    submit_attempt_count = int(status.get("submitAttemptCount", 0))
                    if submit_attempt_count > last_submit_attempt_count:
                        last_submit_attempt_count = submit_attempt_count
                        last_submit_attempt_at_ms = _now_ms()
                        clicked_text = str(status.get("lastClickedButtonText", "")).strip()
                        trigger_mode = str(status.get("submitTriggerMode", "")).strip() or "auto-watch"

                        hotkey_seen_at_ms_raw = float(status.get("lastHotkeySeenAt", 0) or 0)
                        submit_fired_at_ms_raw = float(status.get("submitFiredAt", 0) or 0)
                        hotkey_seen_at_ms = hotkey_seen_at_ms_raw if hotkey_seen_at_ms_raw > 0 else None
                        submit_fired_at_ms = submit_fired_at_ms_raw if submit_fired_at_ms_raw > 0 else None
                        submit_ready_at_ms_raw = float(status.get("submitReadyAt", 0) or 0)
                        submit_ready_at_ms = submit_ready_at_ms_raw if submit_ready_at_ms_raw > 0 else None

                        captured_hints = capture_market_selection_hints(page)
                        if captured_hints:
                            last_market_hints = captured_hints
                            log(f"[+] Captured market hints for repeat: {', '.join(last_market_hints[:3])}")

                        _reset_latency_capture(submit_ready_at_ms or submit_fired_at_ms or _now_ms())

                        if clicked_text:
                            log(f"[+] Submit attempt fired on button: '{clicked_text}' (mode={trigger_mode})")
                        else:
                            log(f"[+] Submit attempt fired (mode={trigger_mode})")

                        # Validate outcome before exiting; remain armed if no success signal.
                        success_signals = [
                            "text=Bet successful",
                            "text=Successfully placed",
                            "text=Accepted",
                            "text=In account history",
                        ]
                        error_signals = [
                            "text=Bet failed",
                            "text=Rejected",
                            "text=Odds changed",
                            "text=Odds has changed",
                            "text=Market suspended",
                            "text=Selection unavailable",
                            "text=Insufficient balance",
                        ]
                        submitting_signals = [
                            "text=Submitting",
                            "text=Processing",
                            "text=Placing",
                            "text=Please wait",
                            "text=Submitting...",
                            "text=Processing...",
                            "text=Loading...",
                        ]

                        confirmed = False
                        rejected = False
                        submitting_seen = False
                        toast_label = "none"
                        toast_seen_at_ms: Optional[float] = None
                        submitting_seen_at_ms: Optional[float] = None

                        # Some flows complete quickly, others stay in submitting state for several seconds.
                        end_wait = time.monotonic() + 20.0
                        while time.monotonic() < end_wait:
                            for signal in success_signals:
                                if page.locator(signal).first.is_visible(timeout=250):
                                    confirmed = True
                                    toast_label = signal.replace("text=", "")
                                    toast_seen_at_ms = _now_ms()
                                    break
                            if confirmed:
                                break

                            for signal in error_signals:
                                if page.locator(signal).first.is_visible(timeout=250):
                                    rejected = True
                                    toast_label = signal.replace("text=", "")
                                    toast_seen_at_ms = _now_ms()
                                    break
                            if rejected:
                                break

                            if not submitting_seen:
                                for signal in submitting_signals:
                                    if page.locator(signal).first.is_visible(timeout=200):
                                        submitting_seen = True
                                        submitting_seen_at_ms = _now_ms()
                                        break

                            time.sleep(0.02)

                        # Use fallback XHR/fetch markers when hint-based ones are unavailable.
                        req_ms = latency_state["first_req_ms"] or latency_state["fallback_req_ms"]
                        req_url = latency_state["first_req_url"] or latency_state["fallback_req_url"]
                        req_method = latency_state["first_req_method"] or latency_state["fallback_req_method"]
                        resp_ms = latency_state["first_resp_ms"] or latency_state["fallback_resp_ms"]
                        resp_url = latency_state["first_resp_url"] or latency_state["fallback_resp_url"]
                        resp_status = latency_state["first_resp_status"] or latency_state["fallback_resp_status"]

                        def _delta_ms(start_ms: Optional[float], end_ms: Optional[float]) -> str:
                            if start_ms is None or end_ms is None:
                                return "n/a"
                            return f"{int(max(0.0, end_ms - start_ms))}ms"

                        def _fmt_ts(ms: Optional[float]) -> str:
                            if ms is None:
                                return "n/a"
                            dt = datetime.fromtimestamp(ms / 1000.0)
                            return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

                        log("[Latency] ---- Attempt Breakdown ----")
                        log(f"[Latency] submit_ready_at: {_fmt_ts(submit_ready_at_ms)}")
                        log(f"[Latency] hotkey_detected_at: {_fmt_ts(hotkey_seen_at_ms)}")
                        log(f"[Latency] submit_click_fired_at: {_fmt_ts(submit_fired_at_ms)}")
                        log(f"[Latency] first_api_request_at: {_fmt_ts(req_ms)}")
                        log(f"[Latency] first_api_response_at: {_fmt_ts(resp_ms)}")
                        log(
                            f"[Latency] submitting_seen_at: {_fmt_ts(submitting_seen_at_ms)} "
                            f"({'yes' if submitting_seen else 'no'})"
                        )
                        log(f"[Latency] toast_seen_at: {_fmt_ts(toast_seen_at_ms)} ({toast_label})")
                        log(
                            f"[Latency] ready->click: {_delta_ms(submit_ready_at_ms, submit_fired_at_ms)} | "
                            f"hotkey->click: {_delta_ms(hotkey_seen_at_ms, submit_fired_at_ms)} | "
                            f"click->request: {_delta_ms(submit_fired_at_ms, req_ms)} | "
                            f"request->response: {_delta_ms(req_ms, resp_ms)} | "
                            f"response->submitting: {_delta_ms(resp_ms, submitting_seen_at_ms)} | "
                            f"response->toast: {_delta_ms(resp_ms, toast_seen_at_ms)}"
                        )
                        if req_url:
                            log(f"[Latency] request: {req_method or 'n/a'} {req_url}")
                        if resp_url:
                            log(f"[Latency] response: {resp_status or 'n/a'} {resp_url}")

                        if confirmed:
                            if _handle_confirmed_submission():
                                return True
                            continue

                        if rejected:
                            log(f"[!] Bet rejection signal detected: {toast_label}")
                            log("[!] Still armed; adjust selection and press hotkey again.")
                            continue

                        log("[!] No success confirmation detected. Still armed; select a market or wait for the slip to become valid again.")

                    if last_submit_attempt_count > 0:
                        late_success_signals = [
                            "text=Bet successful",
                            "text=Successfully placed",
                            "text=Accepted",
                            "text=In account history",
                        ]
                        late_error_signals = [
                            "text=Bet failed",
                            "text=Rejected",
                            "text=Odds changed",
                            "text=Odds has changed",
                            "text=Market suspended",
                            "text=Selection unavailable",
                            "text=Insufficient balance",
                        ]

                        for signal in late_success_signals:
                            if page.locator(signal).first.is_visible(timeout=50):
                                log(f"[+] Late success confirmation detected ({signal.replace('text=', '')})")
                                if _handle_confirmed_submission():
                                    return True
                                break

                        for signal in late_error_signals:
                            if page.locator(signal).first.is_visible(timeout=50):
                                log(f"[!] Late rejection detected ({signal.replace('text=', '')})")
                                log("[!] Still armed; adjust selection and try again.")
                                last_submit_attempt_count = 0
                                break
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
            safe_close_page(page)
            safe_close_context(context)
            safe_close_browser(browser)


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
            safe_close_page(page)
            safe_close_context(context)
            safe_close_browser(browser)


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
        help="Manual market selection mode with auto-submit watcher",
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
        help="Optional stake amount to prefill before auto-submit",
    )
    arm.add_argument(
        "--hotkey",
        required=False,
        default=None,
        help="Optional browser key override for manual submit (auto-submit watcher is always on)",
    )
    arm.add_argument(
        "--repeat",
        required=False,
        type=int,
        default=1,
        help="How many successful submissions to place for the same selection (default: 1)",
    )
    arm.add_argument(
        "--repeat-delay-ms",
        required=False,
        type=int,
        default=150,
        help="Delay before repeat burst in milliseconds (default: 150)",
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
            repeat_count=args.repeat,
            repeat_delay_ms=args.repeat_delay_ms,
        )
        return 0 if ok else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
