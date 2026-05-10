"""
Schemi Pydantic per lo scambio dati tra agenti.
Garantisce contratti rigorosi tra Scout, Decision e Technical Worker.
"""

from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field


# ============================================================
# Scout Agent → Intelligence Buffer
# ============================================================

class ScoutReport(BaseModel):
    """Micro-scheda prodotta dallo Scout ogni 20 minuti."""
    source_type: str = Field(description="GDELT|NEWSAPI|YFINANCE_NEWS|REDDIT|X|FINNHUB|CONGRESSIONAL|COINGECKO")
    micro_summary: str = Field(description="Sintesi in 2-3 frasi dell'evento/dato")
    sentiment_score: float = Field(default=0.0, ge=-1.0, le=1.0, description="Sentiment -1 (bearish) a +1 (bullish)")
    key_tickers: list[str] = Field(default_factory=list, description="Ticker impattati")
    risk_keywords: list[str] = Field(default_factory=list, description="Keyword di rischio identificate")
    raw_data_ref: str = Field(default="", description="Riferimento ai dati grezzi nel buffer")


class DailySnapshot(BaseModel):
    """Recap giornaliero prodotto alle 23:59 CET dallo Scout."""
    date: str = Field(description="Data YYYY-MM-DD")
    summary_text: str = Field(description="Sintesi della giornata")
    key_events: list[dict[str, Any]] = Field(default_factory=list, description="Eventi chiave strutturati")
    macro_bias: str = Field(default="NEUTRAL", description="BULLISH|BEARISH|NEUTRAL")
    hot_tickers: list[str] = Field(default_factory=list, description="Ticker caldi per domani")
    sentiment_shift: str = Field(default="STABLE", description="IMPROVING|WORSENING|STABLE")
    intelligence_count: int = Field(default=0, description="Numero record buffer processati")


class WeeklyMatrix(BaseModel):
    """Matrice settimanale prodotta la domenica alle 23:59 CET dallo Scout."""
    week_id: str = Field(description="Formato '2026-W13'")
    synthesis: str = Field(description="Visione macro della settimana entrante")
    long_term_risks: str = Field(default="", description="Rischi a medio-lungo termine")
    sector_rotation_signals: dict[str, str] = Field(default_factory=dict, description="Settore -> segnale rotazione")
    macro_strategy: str = Field(default="", description="Strategia suggerita per la settimana")
    daily_snapshots_used: int = Field(default=0, description="Numero di daily usate")


# ============================================================
# Technical Worker → Analysis Results
# ============================================================

class IndicatorResult(BaseModel):
    """Risultato di un singolo indicatore tecnico."""
    value: float = Field(description="Valore corrente dell'indicatore")
    signal: str = Field(default="NEUTRAL", description="BUY|SELL|NEUTRAL")
    detail: str = Field(default="", description="Dettaglio aggiuntivo")


class TechnicalAnalysis(BaseModel):
    """Report tecnico completo per un singolo ticker, prodotto da DeepSeek-V3."""
    ticker: str
    signal: str = Field(default="HOLD", description="BUY|SELL|HOLD")
    confidence: float = Field(default=50.0, ge=0, le=100, description="Confidence 0-100")
    current_price: float = Field(default=0.0)
    support: float = Field(default=0.0, description="Livello di supporto")
    resistance: float = Field(default=0.0, description="Livello di resistenza")
    atr: float = Field(default=0.0, description="Average True Range (per stop-loss)")
    rsi: Optional[IndicatorResult] = None
    macd: Optional[IndicatorResult] = None
    bollinger: Optional[IndicatorResult] = None
    sma_cross: Optional[IndicatorResult] = None
    stochastic: Optional[IndicatorResult] = None
    volume_trend: str = Field(default="NORMAL", description="HIGH|LOW|NORMAL")
    market_regime: str = Field(default="RANGING", description="TRENDING_UP|TRENDING_DOWN|RANGING|VOLATILE")
    reasoning: str = Field(default="", description="Motivazione tecnica")
    engine: str = Field(default="deepseek-v3", description="Motore utilizzato")


class TechnicalBatch(BaseModel):
    """Batch di analisi tecniche per multipli ticker."""
    analyses: list[TechnicalAnalysis] = Field(default_factory=list)
    market_regime: str = Field(default="RANGING", description="Regime di mercato globale")
    summary: str = Field(default="")
    engine: str = Field(default="deepseek-v3")


# ============================================================
# Decision Agent → Trade Orders
# ============================================================

class TradeDecision(BaseModel):
    """Decisione di trading prodotta da Opus 4.6."""
    ticker: str
    action: str = Field(description="BUY|SELL")
    quantity: int = Field(ge=1)
    entry_price: float = Field(gt=0)
    stop_loss: Optional[float] = Field(default=None, description="Livello stop-loss (opzionale, Opus decide)")
    take_profit: Optional[float] = Field(default=None, description="Livello take-profit (opzionale)")
    confidence_level: float = Field(ge=0, le=100)
    logic_chain: str = Field(description="Chain of Thought completo di Opus - integra geo+tech")
    geo_reasoning: str = Field(default="", description="Componente geopolitica del reasoning")
    tech_reasoning: str = Field(default="", description="Componente tecnica del reasoning")
    allocation_pct: float = Field(default=0, ge=0, le=50, description="% del portafoglio allocata")


class DecisionResult(BaseModel):
    """Risultato complessivo del ciclo decisionale di Opus."""
    run_id: str
    decision: str = Field(description="TRADE|NO_TRADE|ERROR")
    trades: list[TradeDecision] = Field(default_factory=list)
    no_trade_reasoning: str = Field(default="", description="Se NO_TRADE, motivazione dettagliata")
    context_loaded: dict[str, bool] = Field(default_factory=dict, description="Contesto caricato con successo")
    duration_seconds: float = Field(default=0)
    thinking_tokens: int = Field(default=0, description="Token usati nel thinking di Opus")


# ============================================================
# Agent Checkpoint (per Render resilience)
# ============================================================

class AgentCheckpoint(BaseModel):
    """Stato intermedio di un agente per recovery dopo restart."""
    run_id: str
    agent_name: str = Field(description="scout|decision|technical")
    status: str = Field(default="RUNNING", description="RUNNING|COMPLETED|FAILED|INTERRUPTED")
    checkpoint_data: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=lambda: datetime.now())
    updated_at: datetime = Field(default_factory=lambda: datetime.now())
