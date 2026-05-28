#!/usr/bin/env bash
set -e

# ── Riduzione memoria glibc (fix OOM su Render 512MB) ────────────────────
# MALLOC_ARENA_MAX=2: di default glibc crea fino a 8*ncore "arene" di malloc
# per i thread. FastAPI/uvicorn + il threadpool degli executor (yfinance,
# pandas) ne usano parecchi, e ogni arena trattiene MB di RSS che non
# tornano all'OS. Limitarle a 2 taglia tipicamente il 30-50% di RSS nelle
# app Python multi-thread — e' il singolo fix piu' efficace per l'OOM.
# MALLOC_TRIM_THRESHOLD_: soglia piu' bassa = restituisce RAM all'OS prima.
export MALLOC_ARENA_MAX=2
export MALLOC_TRIM_THRESHOLD_=100000
export PYTHONUNBUFFERED=1

cd backend
../venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
