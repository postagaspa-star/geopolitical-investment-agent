#!/usr/bin/env bash
set -e

# Create a Python virtual environment for the backend
python3 -m venv ./venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# Build the React frontend
cd frontend && npm install && npm run build && cd ..

# Copy frontend build to backend/static for serving (modalità in chiaro)
mkdir -p backend/static
cp -r frontend/build/* backend/static/

# Client-side encryption: se BUILD_TOKEN è impostato, cifra la build.
# In modalità ENCRYPTION_ENABLED=true a runtime, FastAPI servirà SOLO il
# bundle cifrato + unlock.html (non la build in chiaro sotto backend/static).
if [ -n "${BUILD_TOKEN:-}" ]; then
    echo "BUILD_TOKEN rilevato → cifro il build con AES-256-GCM..."
    ./venv/bin/python scripts/encrypt_build.py
    echo "Bundle cifrato pronto in backend/encrypted_assets/bundle.enc"
else
    echo "BUILD_TOKEN non impostato → skip encryption (modalità in chiaro)."
fi
