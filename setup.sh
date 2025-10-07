#!/bin/bash

echo "Setting up 3D Print Cost Estimator..."
echo

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed or not in PATH."
    echo "Please install Python 3.8+ from your package manager or python.org"
    exit 1
fi

echo "Python found. Installing dependencies..."
pip3 install -r requirements.txt

if [ $? -ne 0 ]; then
    echo
    echo "Error installing dependencies. Please check the error messages above."
    exit 1
fi

echo
echo "Setup complete!"
echo
echo "To run the application:"
echo "  python3 app.py"
echo
echo "The application will be available at: http://localhost:5000"
echo
echo "Before running, make sure to:"
echo "1. Install SuperSlicer and set SUPERSLICER_PATH environment variable"
echo "2. Configure email settings (optional) - see .env.example"
echo "3. Create a SuperSlicer profile at profiles/my_config.ini"
echo
echo "See README.md for detailed setup instructions."
echo