# Agente di Investimento Geopolitico

Agente AI di investimento simulato guidato da analisi geopolitica. Il sistema monitora notizie e eventi geopolitici in tempo reale, analizza il loro potenziale impatto sui mercati finanziari e gestisce un portafoglio di investimenti simulato utilizzando Claude di Anthropic come motore decisionale.

## Architettura

```
frontend/          React + TypeScript (dashboard di visualizzazione)
backend/           FastAPI + Python (API, agente AI, gestione portafoglio)
```

- **Backend**: API REST con FastAPI, integrazione con Claude (Anthropic) per l'analisi geopolitica, raccolta notizie tramite News API, database SQLite per lo storico operazioni e portafoglio.
- **Frontend**: Dashboard React con grafici interattivi per monitorare il portafoglio, le operazioni e gli eventi geopolitici analizzati.

## Prerequisiti

- Python 3.11+
- Node.js 18+

## Sviluppo Locale

### 1. Installare le dipendenze

```bash
# Backend
cd backend
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scriptsctivate
pip install -r requirements.txt

# Frontend
cd ../frontend
npm install
```

### 2. Configurare le variabili d'ambiente

Creare un file `.env` nella cartella `backend/`:

```env
ANTHROPIC_API_KEY=la-tua-chiave-api
NEWS_API_KEY=la-tua-chiave-news-api
INITIAL_PORTFOLIO_BALANCE=100000
AGENT_RUN_INTERVAL_HOURS=6
```

### 3. Avviare i servizi

```bash
# Backend (dalla cartella backend/)
uvicorn main:app --reload --port 8000

# Frontend (dalla cartella frontend/)
npm run dev
```

## Variabili d'Ambiente

| Variabile | Descrizione | Default |
|-----------|-------------|---------|
| `ANTHROPIC_API_KEY` | Chiave API di Anthropic (obbligatoria) | - |
| `NEWS_API_KEY` | Chiave API di News API (obbligatoria) | - |
| `FINNHUB_API_KEY` | Chiave API di Finnhub per congressional trading (opzionale) | - |
| `DB_PATH` | Percorso del database SQLite (su Render: `/data/portfolio.db`) | `backend/investment_agent.db` |
| `DOCUMENTS_PATH` | Percorso documenti caricati (su Render: `/data/documents`) | `backend/documents/` |

## Deploy su Render

1. Creare un nuovo **Web Service** su [Render](https://render.com)
2. Collegare il repository GitHub
3. Render rileverà automaticamente `render.yaml`
4. Configurare le variabili d'ambiente: `ANTHROPIC_API_KEY`, `NEWS_API_KEY`, `FINNHUB_API_KEY`
5. Il disco persistente (`/data`, 1GB) viene creato automaticamente per database e documenti

## Endpoint API

| Metodo | Percorso | Descrizione |
|--------|----------|-------------|
| `GET` | `/health` | Controllo stato del servizio |
| `GET` | `/api/portfolio` | Stato attuale del portafoglio |
| `GET` | `/api/trades` | Storico delle operazioni |
| `GET` | `/api/analysis` | Ultime analisi geopolitiche |
| `POST` | `/api/agent/run` | Avvia manualmente un ciclo dell'agente |

## Stack Tecnologico

- **Backend**: Python, FastAPI, SQLite, Anthropic SDK
- **Frontend**: React, TypeScript, Vite, Recharts
- **AI**: Claude (Anthropic) per analisi geopolitica e decisioni di investimento
- **Deploy**: Railway (Nixpacks)
