@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\\Scripts\\python.exe" (
  where py >nul 2>nul
  if %errorlevel%==0 (
    set "PYTHON_CMD=py"
  ) else (
    set "PYTHON_CMD=python"
  )
  echo Creating local Python environment...
  %PYTHON_CMD% -m venv .venv
  if not %errorlevel%==0 (
    echo.
    echo Could not create the local Python environment.
    pause
    exit /b 1
  )
)

set "PYTHON_CMD=.venv\\Scripts\\python.exe"
%PYTHON_CMD% -c "import flask, cv2, numpy, PIL" >nul 2>nul
if not %errorlevel%==0 (
  echo Installing required packages into the local environment...
  %PYTHON_CMD% -m pip install -r requirements.txt
  if not %errorlevel%==0 (
    echo.
    echo Installation failed. Please check the network, Python and pip.
    pause
    exit /b 1
  )
)

echo Starting Suzuran Keyframe Studio...
%PYTHON_CMD% video_keyframe_tool.py
pause
