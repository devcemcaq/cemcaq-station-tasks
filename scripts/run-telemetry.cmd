@echo off
rem Lo que llama el Programador de tareas cada hora. Actualiza el repo (si hay
rem git) y ejecuta el extractor con el Python de la estación.
setlocal
cd /d "%~dp0.."

where git >nul 2>nul && git pull --ff-only --quiet

set "PYTHON=python"
if defined PYTHON_EXE set "PYTHON=%PYTHON_EXE%"
if not defined PYTHON_EXE if exist "C:\Users\Agilaire\AppData\Local\Programs\Python\Python39\python.exe" set "PYTHON=C:\Users\Agilaire\AppData\Local\Programs\Python\Python39\python.exe"

"%PYTHON%" telemetry\push_readings.py %*
exit /b %ERRORLEVEL%
