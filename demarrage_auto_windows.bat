@echo off
REM Lance le bot au démarrage de Windows. Remplis tes valeurs UNE FOIS avec setx (voir README) puis lance ce fichier une fois.
cd /d "%~dp0"
schtasks /Create /TN "SniperBot" /TR "\"%~dp0lancer_bot.bat\"" /SC ONLOGON /RL LIMITED /F
echo Le bot demarrera a chaque ouverture de session Windows.
pause
