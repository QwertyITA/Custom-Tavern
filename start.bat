@echo off
chcp 65001 >nul
rem Update, then start. This is the one to double-click.
rem
rem   start.bat              pull the latest version, then run in this window
rem   start.bat --no-update  skip the pull (offline, or you want this exact code)
rem   start.bat stop         stop a server started from another window
rem   start.bat logs         follow the log
rem
rem The Windows equivalent of start.sh/run.sh (see those for the Termux
rem launcher this mirrors). Same rules, in the same order of how much they
rem matter:
rem   * Your data is never touched. data\tavern.db and data\settings.json are
rem     gitignored, so a pull cannot overwrite chats, characters or settings.
rem   * A failed update never stops the app. No connection at the coffee shop
rem     still starts the version you already have.
rem   * Local edits are never discarded silently. A dirty working tree skips
rem     the pull and says so, rather than stashing or resetting behind your
rem     back.
rem
rem No tmux/wake-lock/foreground-notification dance here — Windows does not
rem reap a background process the way Android does, so a plain console window
rem left open (or minimised) is the whole story. Closing it, or Ctrl+C inside
rem it, stops the server.
rem
rem Opens a browser tab on the app once it's actually up. Set
rem TAVERN_NO_BROWSER=1 to skip that.

setlocal EnableExtensions
cd /d "%~dp0"

if not defined PYTHON set "PYTHON=python"
if not defined TAVERN_REMOTE set "TAVERN_REMOTE=origin"

set "DATA_DIR=%CD%\data"
set "LOG=%DATA_DIR%\tavern.log"
if not exist "%DATA_DIR%" mkdir "%DATA_DIR%" >nul 2>&1

rem host/port from data\settings.json — same fallback chain as run.sh:
rem TAVERN_HOST/TAVERN_PORT win when actually set, then whatever the GUI
rem saved, then the hardcoded default. Missing file/field/JSON all just print
rem nothing, same as the bash version's settings_field().
set "PORT=%TAVERN_PORT%"
if not defined PORT for /f "usebackq delims=" %%P in (`%PYTHON% "%~dp0_settings_field.py" "%DATA_DIR%\settings.json" port 2^>nul`) do set "PORT=%%P"
if not defined PORT set "PORT=8787"
set "HOST=%TAVERN_HOST%"
if not defined HOST for /f "usebackq delims=" %%H in (`%PYTHON% "%~dp0_settings_field.py" "%DATA_DIR%\settings.json" host 2^>nul`) do set "HOST=%%H"
if not defined HOST set "HOST=127.0.0.1"

set "CMD=%~1"
if "%CMD%"=="" set "CMD=start"

if /i "%CMD%"=="__openwhenready__" goto :open_when_ready
if /i "%CMD%"=="stop" goto :stop
if /i "%CMD%"=="logs" goto :logs
if /i "%CMD%"=="--no-update" goto :run
if /i "%CMD%"=="start" goto :update
echo usage: %~nx0 [start^|--no-update^|stop^|logs]
exit /b 2

rem ------------------------------------------------------------------ update

:update
where git >nul 2>&1
if errorlevel 1 (
  echo not a git checkout -- skipping update
  goto :run
)
if not exist "%~dp0.git" (
  echo not a git checkout -- skipping update
  goto :run
)

for /f "usebackq delims=" %%B in (`git rev-parse --abbrev-ref HEAD 2^>nul`) do set "BRANCH=%%B"
if not defined BRANCH goto :run
if "%BRANCH%"=="HEAD" (
  echo detached HEAD -- skipping update
  goto :run
)

set "DIRTY="
for /f %%X in ('git status --porcelain --untracked-files=no 2^>nul') do set "DIRTY=1"
if defined DIRTY (
  echo you have local changes -- skipping update
  echo      commit or discard them, then run again to pick up the latest.
  goto :run
)

echo Checking for updates on %BRANCH%...
git fetch --quiet "%TAVERN_REMOTE%" "%BRANCH%" 2>nul
if errorlevel 1 (
  echo couldn't reach %TAVERN_REMOTE% -- starting the version you have
  goto :run
)

for /f "usebackq delims=" %%L in (`git rev-parse HEAD`) do set "LOCAL_HEAD=%%L"
for /f "usebackq delims=" %%R in (`git rev-parse "%TAVERN_REMOTE%/%BRANCH%" 2^>nul`) do set "REMOTE_HEAD=%%R"
if "%LOCAL_HEAD%"=="%REMOTE_HEAD%" (
  echo      already up to date
  goto :run
)

for /f "usebackq delims=" %%C in (`certutil -hashfile requirements.txt SHA256 2^>nul ^| findstr /v ":"`) do set "REQS_BEFORE=%%C"

git merge --ff-only "%TAVERN_REMOTE%/%BRANCH%" --quiet 2>nul
if errorlevel 1 (
  echo can't fast-forward -- your branch and %TAVERN_REMOTE%/%BRANCH% have diverged.
  echo      Your data is safe either way ^(data\ is gitignored^). To take the
  echo      remote version as-is:
  echo        git reset --hard %TAVERN_REMOTE%/%BRANCH%
  goto :run
)
for /f "usebackq delims=" %%S in (`git rev-parse --short "%LOCAL_HEAD%"`) do set "SHORT_BEFORE=%%S"
for /f "usebackq delims=" %%S in (`git rev-parse --short HEAD`) do set "SHORT_AFTER=%%S"
echo      updated %SHORT_BEFORE% -^> %SHORT_AFTER%
git --no-pager log --oneline "%LOCAL_HEAD%..HEAD"

for /f "usebackq delims=" %%C in (`certutil -hashfile requirements.txt SHA256 2^>nul ^| findstr /v ":"`) do set "REQS_AFTER=%%C"
if not "%REQS_BEFORE%"=="%REQS_AFTER%" (
  echo Dependencies changed -- installing...
  %PYTHON% -m pip install -r requirements.txt
)
goto :run

rem -------------------------------------------------------------------- deps

:check_deps
%PYTHON% -c "import fastapi, uvicorn, httpx, pydantic" >nul 2>&1
if errorlevel 1 (
  echo Installing dependencies...
  %PYTHON% -m pip install -r requirements.txt
)
exit /b 0

rem --------------------------------------------------------------------- run

:run
call :check_deps

powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*uvicorn app.main:app*' }) { exit 0 } else { exit 1 }" >nul 2>&1
if not errorlevel 1 (
  echo a server is already running -- stop it first with: %~nx0 stop
  exit /b 1
)

if not "%TAVERN_NO_BROWSER%"=="1" start "" /b cmd /c "%~f0" __openwhenready__

echo Personal Tavern -^> http://localhost:%PORT%
echo === %DATE% %TIME% starting on %HOST%:%PORT% === >>"%LOG%"
rem tee-less: PowerShell's Tee-Object both prints and appends, same job
rem run.sh's `tee -a` does. No [standard] extras (uvloop/httptools are
rem POSIX-only anyway) — plain uvicorn. Unbuffered so the console and the
rem log fill in as replies stream, not in one dump when the pipe closes.
set "PYTHONUNBUFFERED=1"
%PYTHON% -m uvicorn app.main:app --host %HOST% --port %PORT% --app-dir "%CD%" 2>&1 | powershell -NoProfile -Command "$input | Tee-Object -FilePath '%LOG%' -Append"
goto :eof

rem ---------------------------------------------------- background helpers

:open_when_ready
rem Polls the port and opens one browser tab once it actually answers —
rem not at spawn, which would be a blank page more often than not.
for /l %%I in (1,1,25) do (
  %PYTHON% -c "import socket,sys;s=socket.socket();s.settimeout(1.5);sys.exit(0 if s.connect_ex(('%HOST%','%PORT%'))==0 else 1)" >nul 2>&1
  if not errorlevel 1 (
    start "" "http://localhost:%PORT%"
    exit /b 0
  )
  timeout /t 1 >nul
)
exit /b 1

rem ------------------------------------------------------------------- stop

:stop
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*uvicorn app.main:app*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Write-Host 'stopped'"
exit /b 0

rem ------------------------------------------------------------------- logs

:logs
powershell -NoProfile -Command "Get-Content -Path '%LOG%' -Wait -Tail 25"
exit /b 0
