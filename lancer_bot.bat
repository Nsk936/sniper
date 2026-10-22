@echo off
cd /d "%~dp0"
:boucle
python sniper.py --loop 5 >> bot.log 2>&1
timeout /t 30 >nul
goto boucle
