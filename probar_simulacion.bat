@echo off
title Trackside Pulse - Modo Simulacion en Vivo
color 0B

echo ========================================================
echo    TRACKSIDE PULSE - PRUEBA DE SIMULACION EN LA NUBE
echo ========================================================
echo.
echo Transmitiendo 22 pilotos en vivo hacia Upstash Redis...
echo.
echo 1. Abre en tu celular: https://visorf1.vercel.app
echo 2. Podras comprobar todo funcionando en tiempo real:
echo    - Punto verde ("En vivo")
echo    - TOP 11 (P1-11) y BOTTOM 11 (P12-22) completos
echo    - TODOS con los 22 autos
echo    - Cambios de deltas, neumaticos, paradas en boxes e incidencias
echo.
echo Para detener la prueba presiona Ctrl + C o cierra esta ventana.
echo ========================================================
echo.

cd /d "%~dp0"
python worker\ingest.py --replay worker\data\replay_sample.json --speed 3 --loop

pause
