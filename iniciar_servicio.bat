@echo off
title Trackside Pulse - Live Worker F1
color 0A

echo ========================================================
echo        TRACKSIDE PULSE - LIVE TIMING WORKER
echo ========================================================
echo.
echo Conectando con F1 Live Timing oficial (SignalR Core)...
echo Sincronizando con Upstash Redis para tu celular.
echo.
echo NOTA: Si la tanda oficial aun no ha comenzado en pista,
echo el worker mantendra la grilla oficial de 22 pilotos en Standby
echo activa ("En vivo"). En cuanto los autos salgan a pista
echo en FP1/FP2/FP3/Qualy/Carrera, los tiempos se transmitiran
echo automaticamente a tu celular en tiempo real.
echo.
echo Mantener esta ventana abierta durante la sesion.
echo Para detener al finalizar, presiona Ctrl + C o cierra la ventana.
echo ========================================================
echo.

cd /d "%~dp0"
python worker\ingest.py --live

pause
