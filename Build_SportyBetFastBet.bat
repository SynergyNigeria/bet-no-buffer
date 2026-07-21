@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --name SportyBetFastBet sportybet_fast_bet.py
