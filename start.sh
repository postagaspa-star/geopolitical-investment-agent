#!/bin/bash
set -e
pip3 install -r requirements.txt
cd backend && python3 -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
