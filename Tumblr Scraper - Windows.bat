@echo off
setlocal
set "PROJECT_ROOT=%~dp0"
pushd "%PROJECT_ROOT%" >nul 2>&1
if errorlevel 1 (
    echo Tumblr Scraper could not enter "%PROJECT_ROOT%".
    set "status=1"
    goto finish
)

where py >nul 2>&1
if not errorlevel 1 goto use_py_launcher

where python >nul 2>&1
if not errorlevel 1 goto use_python_launcher

echo Python 3 was not found.
echo Install Python 3, then run this launcher again.
set "status=1"
goto finish

:use_py_launcher
py -3 "%PROJECT_ROOT%Tumblr Scraper - Android.py"
set "status=%errorlevel%"
goto finish

:use_python_launcher
python "%PROJECT_ROOT%Tumblr Scraper - Android.py"
set "status=%errorlevel%"
goto finish

:finish
echo.
if not "%status%"=="0" echo The launcher exited with status %status%.
echo Saved work is not removed by a launcher failure.
popd >nul 2>&1
pause
exit /b %status%
