#!/usr/bin/env bash
set -e

# Install Python dependencies using pip (Render provides pip in PATH)
pip install --upgrade pip
pip install -r requirements.txt

# Build the React frontend
cd frontend && npm install && npm run build && cd ..

# Copy frontend build to backend/static for serving
mkdir -p backend/static
cp -r frontend/build/* backend/static/
