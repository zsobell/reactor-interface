@echo off
rem ===========================================================================
rem  Start Reactor Interface
rem ---------------------------------------------------------------------------
rem  Double-click this file to launch the reactor interface and open it in your
rem  browser. No Claude / terminal knowledge needed. Use it after a PC restart.
rem
rem  Reminder: the LabVIEW VI must be CLOSED first, or the DAQ analog inputs
rem  report "resource reserved" (DAQmx gives one program exclusive use).
rem
rem  The server runs in a separate window titled "Reactor Interface Server".
rem  Close that window (or press Ctrl+C in it) to stop the interface.
rem ===========================================================================

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Could not find .venv\Scripts\python.exe next to this script.
  echo Make sure this .bat lives in the Reactor Interface project folder.
  pause
  exit /b 1
)

echo Starting the Reactor Interface server...
start "Reactor Interface Server" ".venv\Scripts\python.exe" -m reactor --port 8000

echo Waiting for the server to come up...
timeout /t 3 /nobreak >nul

echo Opening the interface in your browser...
start "" http://127.0.0.1:8000/

echo.
echo Done. The server is running in the "Reactor Interface Server" window.
echo Close that window to stop the reactor interface.
timeout /t 6 /nobreak >nul
