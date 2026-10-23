@echo off
REM A lancer sur ton PC : traite seulement les sites que GitHub ne peut pas atteindre (Marktplaats, 2dehands, 2ememain, OLX Pologne).
cd /d "%~dp0"
:boucle
python sniper.py --loop 10 --only marktplaats,2dehands,2ememain,olx:pl >> bot_pc.log 2>&1
timeout /t 30 >nul
goto boucle
