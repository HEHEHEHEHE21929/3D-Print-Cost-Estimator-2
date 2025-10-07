@echo off
echo Setting up 3D Print Cost Estimator...
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo Error: Python is not installed or not in PATH.
    echo Please install Python 3.8+ from https://python.org
    pause
    exit /b 1
)

echo Python found. Installing dependencies...
pip install -r requirements.txt

if errorlevel 1 (
    echo.
    echo Error installing dependencies. Please check the error messages above.
    pause
    exit /b 1
)

echo.
echo Setup complete! 
echo.
echo To run the application:
echo   python app.py
echo.
echo The application will be available at: http://localhost:5000
echo.
echo Before running, make sure to:
echo 1. Install SuperSlicer and set SUPERSLICER_PATH environment variable
echo 2. Configure email settings (optional) - see .env.example
echo 3. Create a SuperSlicer profile at profiles/my_config.ini
echo.
echo See README.md for detailed setup instructions.
echo.
pause