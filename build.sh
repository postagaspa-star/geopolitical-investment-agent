#!/bin/bash
set -e
pip3 install -r requirements.txt
cd frontend && npm install && npm run build && cd ..
mkdir -p backend/static
cp -r frontend/build/* backend/static/
