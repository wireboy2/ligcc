@echo off
REM ==========================================================================
REM  One-click packaging (Windows). Always uses build.spec (onedir mode).
REM
REM  Usage:
REM     build.bat            build + copy aiKey.txt / .paddle_cache into dist
REM     build.bat deps       install requirements first, then build
REM
REM  Why not --onefile: the paddle stack is huge, so onefile has to unpack
REM  ~1.5 GB into a temp dir on every launch (minutes). onedir is the only
REM  usable mode here; output goes to dist\SystemHelper\.
REM
REM  NOTE: this file is intentionally ASCII-only. cmd.exe parses .bat files
REM  with the OEM codepage, so non-ASCII text (Chinese comments/messages)
REM  gets mis-tokenized into bogus commands. Keep it English.
REM ==========================================================================
setlocal
cd /d "%~dp0"

if /i "%1"=="deps" (
    echo [1/3] Installing requirements ...
    python -m pip install -r requirements.txt || goto :failed
) else (
    echo [1/3] Skipping dependency install ^(run "build.bat deps" to install^)
)

echo [2/3] Running PyInstaller ^(onedir, build.spec^) ...
REM "call" matters: if pyinstaller resolves to a .bat/.cmd wrapper (conda,
REM some venv layouts), invoking it without call would transfer control and
REM never come back here, silently skipping step 3.
call pyinstaller --noconfirm build.spec || goto :failed

echo [3/3] Copying runtime files into dist\SystemHelper\ ...
if exist "..\aiKey.txt" (
    copy /y "..\aiKey.txt" "dist\SystemHelper\aiKey.txt" >nul
    echo     - aiKey.txt copied
) else (
    echo     ! ..\aiKey.txt not found - the exe will not be able to call the API.
    echo       Copy aiKey.example.txt to aiKey.txt and fill in your key.
)
if exist "..\.paddle_cache" (
    xcopy /e /i /y /q "..\.paddle_cache" "dist\SystemHelper\.paddle_cache\" >nul
    echo     - .paddle_cache copied ^(skips the ~200MB first-run download^)
) else (
    echo     - ..\.paddle_cache not found; the exe will download OCR models on first run.
)

echo.
echo [DONE] dist\SystemHelper\SystemHelper.exe
echo        Verify:  dist\SystemHelper\SystemHelper.exe --once
echo        Stealth build without console: set CONSOLE = False in build.spec, rerun.
endlocal
exit /b 0

:failed
echo.
echo [FAILED] Build aborted, see the error output above.
endlocal
exit /b 1
