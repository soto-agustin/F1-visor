@echo off
title Trackside Pulse - Live Worker F1
color 0A

echo ========================================================
echo        TRACKSIDE PULSE - LIVE TIMING WORKER
echo ========================================================
echo.
echo Iniciando conexion en vivo con F1 Live Timing...
echo Este proceso debe permanecer abierto durante la sesion.
echo Para detenerlo al finalizar, presiona Ctrl + C o cierra la ventana.
echo.

cd /d "%~dp0"
python worker\ingest.py --live

pause
