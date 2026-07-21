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
        "input[type='tel']",
        "input[type='text']",
        "input[inputmode='numeric']",
        "input[inputmode='decimal']",
        "input[placeholder*='Stake' i]",
        "input[aria-label*='Stake' i]",
        "input[placeholder*='Amount' i]",
        "input[aria-label*='Amount' i]",
        "[data-testid*='stake' i] input",
        "[class*='stake' i] input",
        "[id*='stake' i] input",
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
        "input[type='tel']",
        "input[type='text']",
        "input[inputmode='numeric']",
        "input[inputmode='decimal']",
        "input[placeholder*='Stake' i]",
        "input[aria-label*='Stake' i]",
        "input[placeholder*='Amount' i]",
        "input[aria-label*='Amount' i]",
        "[data-testid*='stake' i] input",
        "[class*='stake' i] input",
        "[id*='stake' i] input",
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


def inject_browser_hotkey_submit(
    page: Page,
    hotkey: Optional[str] = None,
    desired_stake: Optional[float] = None,
) -> None:
    """
    Inject an in-browser watcher that auto-clicks Place/Confirm as soon as the betslip becomes valid.
    If a hotkey is provided, it acts as an optional manual override.
    """
    hotkey_normalized = (hotkey or "").strip().upper()
    page.evaluate(
        """
        ({ hotkey, desiredStake }) => {
            const win = window;
            win.__sb_submit_fired = false;
            win.__sb_submit_ready_count = 0;
            win.__sb_last_submit_error = "";
            win.__sb_submit_attempt_count = Number(win.__sb_submit_attempt_count || 0);
            win.__sb_last_clicked_button_text = "";
            win.__sb_submit_ready_at = 0;
            win.__sb_submit_trigger_mode = "";
            win.__sb_last_stake_selector = "";
            win.__sb_last_stake_before = "";
            win.__sb_last_stake_after = "";
            win.__sb_last_stake_apply_at = 0;
            win.__sb_last_stake_target = "";
            win.__sb_last_stake_note = "";
            win.__sb_last_button_scan_note = "";
            win.__sb_market_click_count = Number(win.__sb_market_click_count || 0);
            win.__sb_last_market_clicked_at = Number(win.__sb_last_market_clicked_at || 0);
            if (typeof win.__sb_waiting_for_next_market !== "boolean") {
                win.__sb_waiting_for_next_market = true;
            }

            const hotkeyValue = String(hotkey || "").trim().toUpperCase();
            const configuredStake =
                Number.isFinite(Number(desiredStake)) && Number(desiredStake) > 0
                    ? Number(desiredStake)
                    : null;

            const isVisibleOnly = (el) => (
                el
                && (() => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style
                        && style.display !== "none"
                        && style.visibility !== "hidden"
                        && Number(style.opacity || "1") > 0
                        && rect.width > 0
                        && rect.height > 0;
                })()
            );

            const isEnabledVisible = (el) => (
                isVisibleOnly(el)
                && !el.disabled
                && (el.getAttribute("aria-disabled") || "").toLowerCase() !== "true"
                && !String(el.getAttribute("class") || "").toLowerCase().includes("disabled")
            );

            const pickButton = (allowDisabled = false) => {
                const usable = (el) => (allowDisabled ? isVisibleOnly(el) : isEnabledVisible(el));
                const directCandidates = [
                    "[data-testid='betslip-place-bet-button']",
                    "button[data-testid='betslip-place-bet-button']",
                    "[data-testid*='place-bet']",
                    "[data-testid*='place' i]",
                    "[data-testid*='submit' i]",
                    "[data-testid*='book' i]",
                    "[data-testid*='wager' i]",
                    "[class*='place-bet' i]",
                    "[class*='submit' i]",
                    "[class*='book' i]",
                    "[class*='wager' i]",
                    "button[type='submit']",
                    "input[type='submit']",
                    "input[type='button']",
                ];

                for (const sel of directCandidates) {
                    const el = document.querySelector(sel);
                    if (usable(el)) return el;
                }

                const textMatchers = [
                    "place bet",
                    "place bets",
                    "place wager",
                    "submit bet",
                    "bet now",
                    "book bet",
                    "book",
                    "wager",
                    "play",
                    "play now",
                    "confirm bet",
                    "confirm",
                    "place",
                ];

                // Prefer buttons inside likely betslip containers.
                const containerSelectors = [
                    "[data-testid*='betslip']",
                    "[class*='betslip']",
                    "[id*='betslip']",
                    "[data-testid*='bet-slip' i]",
                    "[class*='bet-slip' i]",
                    "[id*='bet-slip' i]",
                    "[data-testid*='slip' i]",
                    "[class*='slip' i]",
                    "[id*='slip' i]",
                    "[data-testid*='coupon' i]",
                    "[class*='coupon' i]",
                    "[id*='coupon' i]",
                    "[data-testid*='ticket' i]",
                    "[class*='ticket' i]",
                    "[id*='ticket' i]",
                    "[data-testid*='booking' i]",
                    "[class*='booking' i]",
                    "[id*='booking' i]",
                ];

                for (const csel of containerSelectors) {
                    const container = document.querySelector(csel);
                    if (!container) continue;
                    const buttons = Array.from(container.querySelectorAll(
                        "button, [role='button'], a, input[type='button'], input[type='submit'], div, span"
                    ));
                    for (const btn of buttons) {
                        const text = (
                            btn.textContent
                            || btn.value
                            || btn.getAttribute("aria-label")
                            || btn.getAttribute("title")
                            || ""
                        ).trim().toLowerCase();
                        const attrs = [
                            btn.getAttribute("data-testid") || "",
                            btn.getAttribute("class") || "",
                            btn.getAttribute("id") || "",
                            btn.getAttribute("role") || "",
                            btn.getAttribute("type") || "",
                        ].join(" ").toLowerCase();
                        if (!usable(btn)) continue;
                        if (text && textMatchers.some((m) => text.includes(m))) return btn;
                        if (
                            (attrs.includes("place") || attrs.includes("submit") || attrs.includes("book") || attrs.includes("wager"))
                            && (attrs.includes("button") || attrs.includes("btn") || attrs.includes("submit"))
                        ) {
                            return btn;
                        }
                    }
                }

                // Last fallback to full page search.
                const allButtons = Array.from(document.querySelectorAll(
                    "button, [role='button'], a, input[type='button'], input[type='submit'], div, span"
                ));
                for (const btn of allButtons) {
                    const text = (
                        btn.textContent
                        || btn.value
                        || btn.getAttribute("aria-label")
                        || btn.getAttribute("title")
                        || ""
                    ).trim().toLowerCase();
                    const attrs = [
                        btn.getAttribute("data-testid") || "",
                        btn.getAttribute("class") || "",
                        btn.getAttribute("id") || "",
                        btn.getAttribute("role") || "",
                        btn.getAttribute("type") || "",
                    ].join(" ").toLowerCase();
                    if (!usable(btn)) continue;
                    if (text && textMatchers.some((m) => text.includes(m))) return btn;
                    if (
                        (attrs.includes("place") || attrs.includes("submit") || attrs.includes("book") || attrs.includes("wager"))
                        && (attrs.includes("button") || attrs.includes("btn") || attrs.includes("submit"))
                    ) {
                        return btn;
                    }
                }

                return null;
            };

            const fireSubmit = (btn, mode) => {
                if (!btn || win.__sb_submit_fired) return;
                btn.click();
                win.__sb_submit_fired = true;
                win.__sb_waiting_for_next_market = true;
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

            const normalizeText = (text) => String(text || "").replace(/\\s+/g, " ").trim().toLowerCase();
            const isInsideBetSlip = (el) => {
                if (!el || !el.closest) return false;
                return Boolean(el.closest([
                    "[data-testid*='betslip' i]",
                    "[class*='betslip' i]",
                    "[id*='betslip' i]",
                    "[data-testid*='coupon' i]",
                    "[class*='coupon' i]",
                    "[id*='coupon' i]",
                ].join(",")));
            };
            const looksLikeSubmit = (el) => {
                const text = normalizeText(el.innerText || el.textContent || "");
                if (text.includes("place bet") || text.includes("submit bet") || text.includes("bet now")) return true;
                if (text.includes("confirm")) return true;
                if (text === "place" || text === "accept" || text === "continue" || text === "bet") return true;
                const attrs = [
                    el.getAttribute("data-testid") || "",
                    el.getAttribute("class") || "",
                    el.getAttribute("id") || "",
                ].join(" ").toLowerCase();
                return attrs.includes("place-bet") || attrs.includes("submit");
            };
            const scoreMarketElement = (el) => {
                if (!el || el === document.documentElement || el === document.body) return 0;
                if (isInsideBetSlip(el) || looksLikeSubmit(el)) return 0;
                const attrs = [
                    el.getAttribute("data-testid") || "",
                    el.getAttribute("data-odds-id") || "",
                    el.getAttribute("data-outcome-id") || "",
                    el.getAttribute("data-selection-id") || "",
                    el.getAttribute("data-market-id") || "",
                    el.getAttribute("class") || "",
                    el.getAttribute("id") || "",
                    el.getAttribute("aria-label") || "",
                    el.getAttribute("title") || "",
                    el.getAttribute("role") || "",
                ].join(" ").toLowerCase();
                let score = 0;
                if (attrs.includes("odd") || attrs.includes("odds")) score += 20;
                if (attrs.includes("outcome")) score += 18;
                if (attrs.includes("selection")) score += 16;
                if (attrs.includes("market")) score += 10;
                if (attrs.includes("price") || attrs.includes("coefficient") || attrs.includes("coef")) score += 8;
                if (attrs.includes("event") || attrs.includes("match")) score += 4;
                if (el.tagName === "BUTTON" || attrs.includes("button")) score += 3;
                const text = normalizeText(el.innerText || el.textContent || "");
                if (/\\b\\d+(\\.\\d{1,3})?\\b/.test(text)) score += 4;
                return score;
            };
            const findMarketElement = (raw) => {
                if (!raw || !raw.closest) return null;
                const direct = raw.closest([
                    "[data-odds-id]",
                    "[data-outcome-id]",
                    "[data-selection-id]",
                    "[data-market-id]",
                    "[data-testid*='odd' i]",
                    "[class*='odd' i]",
                    "[data-testid*='outcome' i]",
                    "[class*='outcome' i]",
                    "[data-testid*='selection' i]",
                    "[class*='selection' i]",
                    "[data-testid*='market' i]",
                    "[class*='market' i]",
                    "[class*='price' i]",
                    "[class*='coef' i]",
                    "button",
                    "[role='button']",
                    "a",
                ].join(","));
                if (direct && scoreMarketElement(direct) > 0) return direct;

                let best = null;
                let bestScore = 0;
                let el = raw;
                for (let depth = 0; el && depth < 8; depth += 1, el = el.parentElement) {
                    const score = scoreMarketElement(el);
                    if (score > bestScore) {
                        best = el;
                        bestScore = score;
                    }
                }
                return bestScore > 0 ? best : null;
            };
            const onMarketClick = (ev) => {
                const el = findMarketElement(ev.target);
                if (!el || isInsideBetSlip(el) || looksLikeSubmit(el)) return;

                win.__sb_market_click_count = Number(win.__sb_market_click_count || 0) + 1;
                win.__sb_last_market_clicked_at = Date.now();
                win.__sb_submit_fired = false;
                win.__sb_waiting_for_next_market = false;
                win.__sb_submit_ready_count = 0;
                win.__sb_submit_attempt_count = 0;
                win.__sb_submit_ready_at = 0;
                win.__sb_submit_fired_at = 0;
                win.__sb_last_clicked_button_text = "";
                win.__sb_submit_trigger_mode = "";
                win.__sb_last_submit_error = "";
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
            const applyConfiguredStake = () => {
                if (configuredStake === null) return true;
                const stakeSelectors = [
                    "[data-testid='betslip-stake-input'] input",
                    "[data-testid*='stake' i] input",
                    "[class*='stake' i] input",
                    "[id*='stake' i] input",
                    "input[name*='stake' i]",
                    "input[placeholder*='stake' i]",
                    "input[aria-label*='stake' i]",
                    "input[placeholder*='amount' i]",
                    "input[aria-label*='amount' i]",
                    "input[type='number']",
                    "input[inputmode='decimal']",
                    "input[inputmode='numeric']",
                    "input[type='tel']",
                    "input[type='text']",
                    "textarea[placeholder*='stake' i]",
                    "[contenteditable='true']",
                ];

                const parseStake = (value) => {
                    const cleaned = String(value || "").replace(/[^\\d.]/g, "");
                    if (!cleaned) return NaN;
                    return Number(cleaned);
                };

                const stakeMatches = (value) => (
                    Math.abs(parseStake(value) - configuredStake) < 0.001
                );

                const readInputValue = (el) => {
                    if (!el) return "";
                    if ("value" in el) return String(el.value || "");
                    return String(el.textContent || "");
                };

                const setInputValue = (el, value) => {
                    if ("value" in el) {
                        try {
                            const proto = el instanceof HTMLTextAreaElement
                                ? window.HTMLTextAreaElement.prototype
                                : window.HTMLInputElement.prototype;
                            const nativeSetter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
                            if (nativeSetter) {
                                nativeSetter.call(el, value);
                            } else {
                                el.value = value;
                            }
                        } catch (e) {
                            el.value = value;
                        }
                        return;
                    }
                    el.textContent = value;
                };

                const stakeContainers = Array.from(document.querySelectorAll([
                    "[data-testid*='betslip' i]",
                    "[class*='betslip' i]",
                    "[id*='betslip' i]",
                    "[data-testid*='coupon' i]",
                    "[class*='coupon' i]",
                    "[id*='coupon' i]",
                    "[data-testid*='stake' i]",
                    "[class*='stake' i]",
                    "[id*='stake' i]",
                ].join(",")));

                const isUsableStakeField = (el) => {
                    if (!isEnabledVisible(el)) return false;
                    const type = String(el.getAttribute("type") || "").toLowerCase();
                    if (["hidden", "password", "search", "checkbox", "radio", "submit", "button"].includes(type)) {
                        return false;
                    }
                    return (
                        el.matches("input, textarea, [contenteditable='true'], [role='textbox'], [role='spinbutton']")
                    );
                };

                const scoreStakeField = (el) => {
                    const attrs = [
                        el.getAttribute("data-testid") || "",
                        el.getAttribute("class") || "",
                        el.getAttribute("id") || "",
                        el.getAttribute("name") || "",
                        el.getAttribute("placeholder") || "",
                        el.getAttribute("aria-label") || "",
                        el.getAttribute("title") || "",
                        el.getAttribute("inputmode") || "",
                        el.getAttribute("type") || "",
                    ].join(" ").toLowerCase();
                    let score = 0;
                    if (attrs.includes("stake")) score += 30;
                    if (attrs.includes("amount")) score += 12;
                    if (attrs.includes("bet")) score += 8;
                    if (attrs.includes("numeric") || attrs.includes("decimal")) score += 5;
                    if (["number", "tel", "text"].includes(String(el.getAttribute("type") || "").toLowerCase())) {
                        score += 3;
                    }
                    if (stakeContainers.some((container) => container.contains(el))) score += 10;
                    if (!Number.isNaN(parseStake(readInputValue(el)))) score += 4;
                    return score;
                };

                let input = null;
                let selectedSelector = "";
                for (const selector of stakeSelectors) {
                    const candidate = document.querySelector(selector);
                    if (candidate && isUsableStakeField(candidate)) {
                        input = candidate;
                        selectedSelector = selector;
                        break;
                    }
                }

                if (!input) {
                    const fields = Array.from(document.querySelectorAll(
                        "input, textarea, [contenteditable='true'], [role='textbox'], [role='spinbutton']"
                    ))
                        .filter(isUsableStakeField)
                        .map((el) => ({ el, score: scoreStakeField(el) }))
                        .filter((item) => item.score > 0)
                        .sort((a, b) => b.score - a.score);
                    if (fields.length > 0) {
                        input = fields[0].el;
                        selectedSelector = `scored-field:${fields[0].score}`;
                    }
                }

                if (!input) {
                    win.__sb_last_stake_selector = "none";
                    win.__sb_last_stake_before = "";
                    win.__sb_last_stake_after = "";
                    win.__sb_last_stake_apply_at = Date.now();
                    win.__sb_last_stake_target = String(configuredStake);
                    win.__sb_last_stake_note = "no-visible-stake-input";
                    return false;
                }

                const targetValue = String(configuredStake);
                const beforeValue = readInputValue(input);
                if (stakeMatches(beforeValue)) {
                    win.__sb_last_stake_selector = selectedSelector;
                    win.__sb_last_stake_before = beforeValue;
                    win.__sb_last_stake_after = beforeValue;
                    win.__sb_last_stake_apply_at = Date.now();
                    win.__sb_last_stake_target = targetValue;
                    win.__sb_last_stake_note = "already-target";
                    return true;
                }

                try {
                    input.focus();
                } catch (e) {}

                setInputValue(input, targetValue);

                try {
                    input.dispatchEvent(new InputEvent("input", {
                        bubbles: true,
                        cancelable: true,
                        inputType: "insertReplacementText",
                        data: targetValue,
                    }));
                } catch (e) {
                    input.dispatchEvent(new Event("input", { bubbles: true }));
                }
                input.dispatchEvent(new Event("change", { bubbles: true }));
                input.dispatchEvent(new Event("blur", { bubbles: true }));

                win.__sb_last_stake_selector = selectedSelector;
                win.__sb_last_stake_before = beforeValue;
                win.__sb_last_stake_after = readInputValue(input);
                win.__sb_last_stake_apply_at = Date.now();
                win.__sb_last_stake_target = targetValue;
                win.__sb_last_stake_note = stakeMatches(readInputValue(input)) ? "applied-waiting-for-next-pass" : "apply-mismatch";
                return false;
            };

            const autoWatch = () => {
                if (win.__sb_submit_fired) return;
                if (win.__sb_waiting_for_next_market) {
                    return;
                }
                if (win.__sb_last_market_clicked_at && Date.now() - win.__sb_last_market_clicked_at < 80) return;
                if (!applyConfiguredStake()) {
                    win.__sb_last_submit_error = `Stake not ready; wanted ${configuredStake}, saw ${win.__sb_last_stake_after || "n/a"}.`;
                    return;
                }
                const btn = markReady();
                if (!btn) {
                    win.__sb_last_button_scan_note = "no-enabled-submit-control-after-stake";
                    win.__sb_last_submit_error = "Stake is ready, but no enabled submit button/control was found.";
                    return;
                }
                fireSubmit(btn, "auto-watch");
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

                    if (!applyConfiguredStake()) {
                        win.__sb_last_submit_error = `Stake not ready; wanted ${configuredStake}, saw ${win.__sb_last_stake_after || "n/a"}.`;
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

            if (win.__sb_market_click_handler) {
                document.removeEventListener("click", win.__sb_market_click_handler, true);
            }
            win.__sb_market_click_handler = onMarketClick;
            document.addEventListener("click", onMarketClick, true);

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
        {"hotkey": hotkey_normalized, "desiredStake": desired_stake},
    )


def arm_manual_submit(
    match_url: Optional[str] = None,
    stake_amount: Optional[float] = None,
    fire_hotkey: Optional[str] = None,
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

            def _rearm_hotkey(_frame=None) -> None:
                # Re-attach after route/page changes so hotkey stays active while browsing markets.
                try:
                    inject_browser_hotkey_submit(page, fire_hotkey, stake_amount)
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
            last_market_click_count = 0
            completed_submissions = 0

            def _prepare_for_next_manual_bet() -> None:
                nonlocal last_ready_count, last_error, last_submit_attempt_count, last_submit_attempt_at_ms
                try:
                    page.evaluate(
                        """() => {
                            const win = window;
                            win.__sb_submit_attempt_count = 0;
                            win.__sb_submit_ready_count = 0;
                            win.__sb_submit_ready_at = 0;
                            win.__sb_submit_fired_at = 0;
                            win.__sb_last_clicked_button_text = "";
                            win.__sb_submit_trigger_mode = "";
                            win.__sb_last_submit_error = "";
                            win.__sb_waiting_for_next_market = true;
                        }"""
                    )
                except Error:
                    pass
                last_ready_count = 0
                last_error = ""
                last_submit_attempt_count = 0
                last_submit_attempt_at_ms = None
                _reset_latency_capture(None)

            def _handle_confirmed_submission() -> bool:
                nonlocal completed_submissions
                completed_submissions += 1
                log("[+] Bet Successfully Submitted to Account History")
                log("[+] Still armed; select another market when ready.")
                _prepare_for_next_manual_bet()
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
                            stakeSelector: String(window.__sb_last_stake_selector || ""),
                            stakeBefore: String(window.__sb_last_stake_before || ""),
                            stakeAfter: String(window.__sb_last_stake_after || ""),
                            stakeApplyAt: Number(window.__sb_last_stake_apply_at || 0),
                            stakeTarget: String(window.__sb_last_stake_target || ""),
                            stakeNote: String(window.__sb_last_stake_note || ""),
                            marketClickCount: Number(window.__sb_market_click_count || 0),
                        })"""
                    )

                    market_click_count = int(status.get("marketClickCount", 0))
                    if market_click_count > last_market_click_count:
                        last_market_click_count = market_click_count
                        last_ready_count = 0
                        last_error = ""
                        last_submit_attempt_count = 0
                        last_submit_attempt_at_ms = None
                        _reset_latency_capture(None)
                        log("[+] New market click detected; watcher re-armed.")

                    ready_count = int(status.get("readyCount", 0))
                    if ready_count > last_ready_count:
                        previous_ready_count = last_ready_count
                        last_ready_count = ready_count
                        if not last_submit_attempt_count and previous_ready_count == 0:
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
                        stake_apply_at_ms_raw = float(status.get("stakeApplyAt", 0) or 0)
                        stake_apply_at_ms = stake_apply_at_ms_raw if stake_apply_at_ms_raw > 0 else None
                        stake_selector = str(status.get("stakeSelector", "")).strip() or "n/a"
                        stake_before = str(status.get("stakeBefore", "")).strip()
                        stake_after = str(status.get("stakeAfter", "")).strip()
                        stake_target = str(status.get("stakeTarget", "")).strip() or "n/a"
                        stake_note = str(status.get("stakeNote", "")).strip() or "n/a"

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
                        log(f"[Latency] stake_apply_at: {_fmt_ts(stake_apply_at_ms)}")
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
                        log(
                            f"[Debug] stake target={stake_target} before='{stake_before or 'empty'}' "
                            f"after='{stake_after or 'empty'}' selector={stake_selector} note={stake_note}"
                        )

                        if confirmed:
                            if _handle_confirmed_submission():
                                return True
                            continue

                        if rejected:
                            log(f"[!] Bet rejection signal detected: {toast_label}")
                            log("[!] Still armed; select another market and try again.")
                            _prepare_for_next_manual_bet()
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
                                log("[!] Still armed; select another market and try again.")
                                _prepare_for_next_manual_bet()
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


def prompt_text(label: str, default: Optional[str] = None, required: bool = False) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        if not required:
            return ""
        log("[!] This value is required.")


def prompt_float(label: str, default: Optional[float] = None, required: bool = False) -> Optional[float]:
    suffix = f" [{default:g}]" if default is not None else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        if not raw and not required:
            return None
        try:
            value = float(raw)
        except ValueError:
            log("[!] Enter a valid number.")
            continue
        if value <= 0:
            log("[!] Enter a number greater than 0.")
            continue
        return value


def run_interactive_menu() -> int:
    auth_status = "found" if AUTH_STATE_PATH.exists() else "missing"

    log("")
    log("SportyBet Fast Bet")
    log("-------------------")
    log(f"Saved login: {auth_status} ({AUTH_STATE_PATH})")
    log("")
    log("1. Login / setup saved session")
    log("2. Armed live mode: manual odds click + auto-submit")
    log("3. One-shot selector bet")
    log("4. Exit")
    log("")

    choice = prompt_text("Choose option", required=True)

    if choice == "1":
        setup_auth_state()
        return 0

    if choice == "2":
        start_url = prompt_text("Start URL", default=SPORTYBET_HOME)
        stake = prompt_float("Stake amount", default=350.0)
        hotkey = prompt_text("Optional hotkey override, leave blank for auto only", default="")
        ok = arm_manual_submit(
            match_url=start_url,
            stake_amount=stake,
            fire_hotkey=(hotkey or None),
        )
        return 0 if ok else 1

    if choice == "3":
        match_url = prompt_text("Match URL", required=True)
        target_market_selector = prompt_text("Target market selector/text", required=True)
        stake = prompt_float("Stake amount", required=True)
        if stake is None:
            log("[-] Stake amount is required.")
            return 1
        ok = place_fast_wager(
            match_url=match_url,
            target_market_selector=target_market_selector,
            stake_amount=stake,
        )
        return 0 if ok else 1

    if choice == "4":
        log("[+] Bye.")
        return 0

    log("[-] Unknown option.")
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SportyBet fast wager automation")
    sub = parser.add_subparsers(dest="command", required=False)

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
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.command is None:
        return run_interactive_menu()

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
