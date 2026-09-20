@echo off
rem Registra (o reemplaza) la tarea programada "CEMCAQ Telemetria": cada hora,
rem 5 minutos después de la hora en punto, con el usuario actual (la PC de la
rem estación se queda con sesión abierta para el software de Agilaire).
rem Ejecutar una sola vez, como administrador, desde esta carpeta.
setlocal
schtasks /Create /TN "CEMCAQ Telemetria" /TR "\"%~dp0run-telemetry.cmd\"" /SC HOURLY /MO 1 /ST 00:05 /RL HIGHEST /F
if errorlevel 1 (
  echo No se pudo registrar la tarea. ^¿Se ejecuto como administrador?
  exit /b 1
)
echo Tarea registrada. Prueba manual:  schtasks /Run /TN "CEMCAQ Telemetria"
