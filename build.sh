#!/bin/bash
set -e

# Create a Python virtual environment for the backend
python3 -m venv /opt/venv
/opt/venv/bin/pip install --upgrade pip
/opt/venv/bin/pip install -r requirements.txt

# Build the React frontend
cd frontend && npm install && npm run build && cd ..

# Copy frontend build to backend/static for serving
mkdir -p backend/static
cp -r frontend/build/* backend/static/
