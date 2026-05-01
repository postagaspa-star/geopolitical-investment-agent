"""
run_once.py — Entry point leggero per GitHub Actions (nessun server web).

Esegue UN singolo ciclo del pipeline multi-agente (Watchdog → Scout → Technical
→ Decision) e termina. Progettato per la variante ClawStreet Tournament:
  - Schedulato da GitHub Actions (cron ogni 2h, gratuito su repo pubblici)
  - Stato persistente su Supabase (configurato via SUPABASE_URL + SUPABASE_KEY)
  - Decision Engine: DeepSeek-R1 (DECISION_ENGINE=deepseek-r1)
  - Mirrorizza trade su ClawStreet bot del torneo (CLAWSTREET_BOT_ID + CLAWSTREET_API_KEY)

Variabili d'ambiente richieste (configurate come GitHub Secrets):
  DECISION_ENGINE       = deepseek-r1
  DEEPSEEK_API_KEY      = sk-...
  SUPABASE_URL          = https://....supabase.co
  SUPABASE_KEY          = eyJ...
  NEWS_API_KEY          = ...  (opzionale, fallback a GDELT)
  CLAWSTREET_BOT_ID     = ...  (dopo registrazione bot torneo)
  CLAWSTREET_API_KEY    = ...
"""

import asyncio
import logging
import os
import sys

# Configura logging su stdout (GitHub Actions cattura stdout nei log)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def main() -> None:
    """Esegue il pipeline completo una volta e termina."""
    # Import tardivo: lascia che il modulo database legga SUPABASE_URL prima
    import database  # noqa: F401 — triggera init automatico Supabase/SQLite

    from agents.orchestrator import run_watchdog_pipeline
    from scheduler import is_market_open

    engine = os.environ.get("DECISION_ENGINE", "claude")
    market_open = is_market_open()

    logger.info(
        "=== ClawStreet Tournament Run === engine=%s | market=%s",
        engine,
        "OPEN" if market_open else "CLOSED",
    )

    if engine == "deepseek-r1":
        ds_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not ds_key:
            logger.error("DEEPSEEK_API_KEY non configurata — impossibile procedere")
            sys.exit(1)

    try:
        result = await run_watchdog_pipeline()
        triggered = result.get("triggered", False)
        urgency = result.get("urgency", 0)
        decision = result.get("decision", "N/A")
        reason = result.get("reason", "")

        if triggered:
            logger.info(
                "Pipeline completata: TRIGGERED — urgency=%d, decision=%s",
                urgency,
                decision,
            )
        else:
            logger.info(
                "Pipeline completata: NO TRIGGER — reason='%s'", reason
            )

    except Exception as exc:
        logger.error("Pipeline fallita con eccezione: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
