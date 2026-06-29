# SportyBet Fast Bet Automation (Private Use)

This project provides a speed-optimized Python Playwright script for automated live bet placement with saved login session support.

## What this script does

- Performs one-time manual login and stores authenticated browser session in auth_state.json.
- Reuses saved session on future runs to skip login flow.
- Runs Chromium in headless mode for faster execution in auto bet mode.
- Blocks non-essential heavy resources to reduce rendering lag:
  - images
  - media
  - fonts
  - stylesheets
  - common tracking and analytics scripts
- Navigates quickly to live match pages using fast page readiness strategy.
- Finds and clicks a target odds element using your selector.
- Enters stake amount in bet slip and clicks Place Bet or Confirm Bet.
- Supports manual selection mode where you choose teams/markets yourself, then trigger instant submit from browser hotkey.
- Prints milestone logs for each critical step.
- Handles common runtime issues such as missing markets, locked odds, or timeout.

## Project files

- sportybet_fast_bet.py: Main automation script.
- requirements.txt: Python dependency list.
- auth_state.json: Created after successful one-time manual login.

## Environment setup

You already created a virtual environment and installed dependencies. For reference, these are the setup commands:

    C:/Python314/python.exe -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt

Install Playwright browser engine if not already installed:

    .\.venv\Scripts\python.exe -m playwright install chromium

## How to run

### 1) One-time authentication setup

Run this once to save your login state:

    .\.venv\Scripts\python.exe .\sportybet_fast_bet.py setup-auth

What happens:

- A visible browser opens.
- You log in manually with phone/password and complete OTP if prompted.
- Return to terminal and press Enter.
- Session state is saved to auth_state.json.

### 2) Place a fast live wager

Run this command after auth state is created:

    .\.venv\Scripts\python.exe .\sportybet_fast_bet.py place-bet --match-url "https://www.sportybet.com/ng/sport/football/live/your-match" --target-market-selector "button[data-odds-id='123456']" --stake 500

Arguments:

- --match-url: Full URL to the target live match.
- --target-market-selector: Selector for the specific odds button.
- --stake: Stake amount as number.

### 3) Manual select + instant submit (new)

Use this mode when you want to physically pick teams/markets in the browser, then submit instantly with a browser hotkey.

    .\.venv\Scripts\python.exe .\sportybet_fast_bet.py arm-submit

Optional start URL:

    .\.venv\Scripts\python.exe .\sportybet_fast_bet.py arm-submit --start-url "https://www.sportybet.com/ng/sport/football/live/your-match"

Optional stake prefill:

    .\.venv\Scripts\python.exe .\sportybet_fast_bet.py arm-submit --stake 500

Optional custom hotkey (default is F7):

    .\.venv\Scripts\python.exe .\sportybet_fast_bet.py arm-submit --stake 500 --hotkey F9

How it works:

- Opens logged-in headed browser using saved auth_state.json.
- You manually select match markets in the live UI.
- If --stake is provided, script pre-fills stake.
- When ready, focus the browser and press the hotkey (default F7).
- Script triggers immediate in-page Place/Confirm click (near-zero extra buffer).
- If no success confirmation appears, script stays armed so you can adjust and press hotkey again.

Recommended hotkeys:

- Prefer non-function keys like S, Q, or K to avoid browser/OS function-key shortcuts.
- Example: --hotkey S

## How to get the correct selector

1. Open the target live match page in a normal browser.
2. Open Developer Tools and inspect the exact odds button.
3. Copy a stable selector (prefer data attributes, aria labels, or test IDs).
4. Avoid long dynamic class chains.
5. Test selector in browser console with:

    document.querySelector("your selector here")

6. Use that selector value for --target-market-selector.

## Common logs

Typical successful progression:

- [+] Session Loaded
- [+] Match Page Request Committed
- [+] Match Found
- [+] Target Odds Clicked
- [+] Stake Entered: value
- [+] Place/Confirm Button Clicked
- [+] Bet Successfully Submitted to Account History

## Troubleshooting

- If auth_state.json is missing, run setup-auth first.
- If target market is not found, re-check selector and page variant.
- If submit button is disabled, odds or stake rules may have changed.
- If you get timeouts during live updates, retry quickly with a fresh selector.
- If setup-auth page looks broken, ensure you are running latest script where setup-auth does not block UI resources.

## Important note

Use this privately and responsibly. Ensure usage complies with platform terms, local laws, and responsible betting practices.
