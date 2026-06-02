# technical_analysis.py
# Modulo di analisi tecnica - tutti gli indicatori calcolati da zero senza librerie esterne.
# Commenti in italiano per chiarezza.

import pandas as pd
import numpy as np


# =============================================================================
# INDICATORI DI BASE
# =============================================================================

def sma(series, period):
    """Calcola la Media Mobile Semplice (SMA) per un dato periodo."""
    return series.rolling(window=period, min_periods=period).mean()


def ema(series, period):
    """Calcola la Media Mobile Esponenziale (EMA) per un dato periodo.
    Il moltiplicatore alpha = 2 / (periodo + 1).
    """
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    """Calcola il Relative Strength Index (RSI) con periodo di lookback.

    Procedimento:
    1. Calcola le variazioni giornaliere (delta).
    2. Separa guadagni e perdite.
    3. Calcola la media mobile esponenziale di guadagni e perdite.
    4. RS = media guadagni / media perdite.
    5. RSI = 100 - (100 / (1 + RS)).
    """
    delta = series.diff()
    # Separa guadagni (positivi) e perdite (negativi, resi positivi)
    guadagni = delta.where(delta > 0, 0.0)
    perdite = (-delta).where(delta < 0, 0.0)

    # Media mobile esponenziale di guadagni e perdite
    media_guadagni = guadagni.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    media_perdite = perdite.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()

    rs = media_guadagni / media_perdite.replace(0, np.nan)
    risultato = 100.0 - (100.0 / (1.0 + rs))
    return risultato


def macd(series, periodo_veloce=12, periodo_lento=26, periodo_segnale=9):
    """Calcola il MACD (Moving Average Convergence Divergence).

    - Linea MACD = EMA(12) - EMA(26)
    - Linea di segnale = EMA(9) della linea MACD
    - Istogramma = MACD - Segnale

    Restituisce una tupla: (linea_macd, linea_segnale, istogramma).
    """
    ema_veloce = ema(series, periodo_veloce)
    ema_lenta = ema(series, periodo_lento)
    linea_macd = ema_veloce - ema_lenta
    linea_segnale = ema(linea_macd, periodo_segnale)
    istogramma = linea_macd - linea_segnale
    return linea_macd, linea_segnale, istogramma


def stochastic(high, low, close, periodo_k=5, periodo_d=3):
    """Calcola l'oscillatore stocastico.

    %K = (Chiusura - Minimo_N) / (Massimo_N - Minimo_N) * 100
    %D = SMA a 3 periodi di %K

    Restituisce una tupla: (percent_k, percent_d).
    """
    # Minimo e massimo degli ultimi N periodi
    minimo_n = low.rolling(window=periodo_k, min_periods=periodo_k).min()
    massimo_n = high.rolling(window=periodo_k, min_periods=periodo_k).max()

    # Calcolo %K, gestendo divisione per zero
    intervallo = massimo_n - minimo_n
    percent_k = ((close - minimo_n) / intervallo.replace(0, np.nan)) * 100.0

    # %D e' la media mobile semplice a 3 periodi di %K
    percent_d = sma(percent_k, periodo_d)
    return percent_k, percent_d


def momentum(series, period=10):
    """Calcola il Momentum come differenza tra il prezzo corrente e quello di N giorni fa.
    Momentum = Chiusura_oggi - Chiusura_N_giorni_fa
    """
    return series.diff(period)


# =============================================================================
# SUPPORTO E RESISTENZA
# =============================================================================

def supporto_resistenza(high, low, close, periodi=None):
    """Calcola i livelli statici di supporto e resistenza.

    Usa il minimo e il massimo degli ultimi N giorni come livelli.
    Periodi predefiniti: 20 e 50 giorni.

    Restituisce un dizionario con i livelli per ogni periodo.
    """
    if periodi is None:
        periodi = [20, 50]

    livelli = {}
    for p in periodi:
        if len(close) >= p:
            # Supporto = minimo dei minimi degli ultimi N giorni
            supporto = low.iloc[-p:].min()
            # Resistenza = massimo dei massimi degli ultimi N giorni
            resistenza = high.iloc[-p:].max()
            livelli[f"supporto_{p}"] = round(float(supporto), 4)
            livelli[f"resistenza_{p}"] = round(float(resistenza), 4)
        else:
            livelli[f"supporto_{p}"] = None
            livelli[f"resistenza_{p}"] = None
    return livelli


# =============================================================================
# RILEVAMENTO TREND
# =============================================================================

def rileva_trend(sma_20, sma_50, sma_200):
    """Rileva lo stato del trend confrontando le medie mobili a diverse velocita'.

    Logica:
    - TRENDING_UP: SMA20 > SMA50 > SMA200 (tutte allineate al rialzo)
    - TRENDING_DOWN: SMA20 < SMA50 < SMA200 (tutte allineate al ribasso)
    - TRADING: le medie sono intrecciate, mercato laterale

    Restituisce una stringa: TRENDING_UP, TRENDING_DOWN, o TRADING.
    """
    # Verifica che tutti i valori siano disponibili
    if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in [sma_20, sma_50, sma_200]):
        return "TRADING"

    if sma_20 > sma_50 > sma_200:
        return "TRENDING_UP"
    elif sma_20 < sma_50 < sma_200:
        return "TRENDING_DOWN"
    else:
        return "TRADING"


# =============================================================================
# ANALISI DEL VOLUME
# =============================================================================

def analisi_volume(close_series, volume_series, periodo=20):
    """Analizza il volume per confermare il trend del prezzo.

    Confronta il volume medio recente (ultimi 5 giorni) con la media a lungo
    termine (ultimi N giorni). Se il volume recente e' superiore e il prezzo
    sta salendo, e' una conferma rialzista.

    Restituisce un dizionario con:
    - volume_medio_breve: media volume ultimi 5 giorni
    - volume_medio_lungo: media volume ultimi N giorni
    - volume_in_aumento: booleano, True se volume recente > media lungo termine
    - prezzo_in_aumento: booleano, True se il prezzo e' salito negli ultimi 5 giorni
    - conferma_rialzista: volume in aumento + prezzo in aumento
    - conferma_ribassista: volume in aumento + prezzo in calo
    """
    if len(close_series) < periodo or len(volume_series) < periodo:
        return {
            "volume_medio_breve": None,
            "volume_medio_lungo": None,
            "volume_in_aumento": False,
            "prezzo_in_aumento": False,
            "conferma_rialzista": False,
            "conferma_ribassista": False,
        }

    vol_breve = float(volume_series.iloc[-5:].mean())
    vol_lungo = float(volume_series.iloc[-periodo:].mean())

    volume_in_aumento = vol_breve > vol_lungo

    # Variazione prezzo negli ultimi 5 giorni
    variazione_prezzo = float(close_series.iloc[-1]) - float(close_series.iloc[-5])
    prezzo_in_aumento = variazione_prezzo > 0

    return {
        "volume_medio_breve": round(vol_breve, 2),
        "volume_medio_lungo": round(vol_lungo, 2),
        "volume_in_aumento": volume_in_aumento,
        "prezzo_in_aumento": prezzo_in_aumento,
        "conferma_rialzista": volume_in_aumento and prezzo_in_aumento,
        "conferma_ribassista": volume_in_aumento and not prezzo_in_aumento,
    }


# =============================================================================
# INTERPRETAZIONE DEI SEGNALI
# =============================================================================

def interpreta_sma(prezzo, sma_20_val, sma_50_val, sma_200_val):
    """Interpreta il segnale delle medie mobili semplici.

    Regola:
    - BUY se il prezzo > SMA200 e SMA20 > SMA50 (trend rialzista confermato)
    - SELL se il prezzo < SMA200 e SMA20 < SMA50 (trend ribassista confermato)
    - NEUTRAL altrimenti
    """
    if any(v is None or (isinstance(v, float) and np.isnan(v))
           for v in [prezzo, sma_20_val, sma_50_val, sma_200_val]):
        return "NEUTRAL"

    if prezzo > sma_200_val and sma_20_val > sma_50_val:
        return "BUY"
    elif prezzo < sma_200_val and sma_20_val < sma_50_val:
        return "SELL"
    else:
        return "NEUTRAL"


def interpreta_rsi(rsi_val, stato_trend):
    """Interpreta il segnale dell'RSI.

    Regole:
    - RSI < 30: zona di ipervenduto -> opportunita' di acquisto (BUY)
    - RSI > 70: zona di ipercomprato -> opportunita' di vendita (SELL)
      MA se il trend e' forte (TRENDING_UP), RSI > 70 e' CONFERMA DEL TREND
    - Altrimenti: NEUTRAL
    """
    if rsi_val is None or (isinstance(rsi_val, float) and np.isnan(rsi_val)):
        return "NEUTRAL"

    if rsi_val < 30:
        return "BUY"
    elif rsi_val > 70:
        # In un trend rialzista forte, ipercomprato conferma la forza
        if stato_trend == "TRENDING_UP":
            return "BUY"
        else:
            return "SELL"
    else:
        return "NEUTRAL"


def interpreta_macd(macd_val, segnale_val, macd_precedente, segnale_precedente):
    """Interpreta il segnale del MACD basato sull'incrocio.

    Regole:
    - BUY: la linea MACD incrocia la linea di segnale dal basso verso l'alto
    - SELL: la linea MACD incrocia la linea di segnale dall'alto verso il basso
    - NEUTRAL: nessun incrocio recente
    """
    valori = [macd_val, segnale_val, macd_precedente, segnale_precedente]
    if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in valori):
        return "NEUTRAL"

    # Incrocio rialzista: MACD era sotto il segnale, ora e' sopra
    if macd_precedente <= segnale_precedente and macd_val > segnale_val:
        return "BUY"
    # Incrocio ribassista: MACD era sopra il segnale, ora e' sotto
    elif macd_precedente >= segnale_precedente and macd_val < segnale_val:
        return "SELL"
    else:
        return "NEUTRAL"


def interpreta_stocastico(k_val, d_val, k_precedente, d_precedente):
    """Interpreta il segnale dello stocastico.

    Regole:
    - BUY: %K incrocia %D dal basso verso l'alto nella zona di ipervenduto (< 20)
    - SELL: %K incrocia %D dall'alto verso il basso nella zona di ipercomprato (> 80)
    - NEUTRAL: nessun segnale chiaro
    """
    valori = [k_val, d_val, k_precedente, d_precedente]
    if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in valori):
        return "NEUTRAL"

    # Incrocio rialzista in zona ipervenduto
    if k_precedente <= d_precedente and k_val > d_val and k_val < 20:
        return "BUY"
    # Incrocio ribassista in zona ipercomprato
    elif k_precedente >= d_precedente and k_val < d_val and k_val > 80:
        return "SELL"
    else:
        return "NEUTRAL"


def interpreta_momentum(mom_val):
    """Interpreta il segnale del Momentum.

    Regole:
    - Momentum positivo: trend al rialzo -> BUY
    - Momentum negativo: trend al ribasso -> SELL
    - Zero o non disponibile: NEUTRAL
    """
    if mom_val is None or (isinstance(mom_val, float) and np.isnan(mom_val)):
        return "NEUTRAL"

    if mom_val > 0:
        return "BUY"
    elif mom_val < 0:
        return "SELL"
    else:
        return "NEUTRAL"


def interpreta_volume(analisi_vol):
    """Interpreta il segnale del volume.

    Regole:
    - Volume in aumento + prezzo in aumento = conferma rialzista -> BUY
    - Volume in aumento + prezzo in calo = conferma ribassista -> SELL
    - Altrimenti: NEUTRAL
    """
    if analisi_vol.get("conferma_rialzista"):
        return "BUY"
    elif analisi_vol.get("conferma_ribassista"):
        return "SELL"
    else:
        return "NEUTRAL"


# =============================================================================
# RIEPILOGO DEI SEGNALI
# =============================================================================

def riepilogo_segnali(segnali):
    """Calcola il segnale complessivo in base alla maggioranza dei segnali individuali.

    Conta i BUY, SELL e NEUTRAL e restituisce il segnale prevalente.
    Se parita' tra BUY e SELL, restituisce NEUTRAL.

    Restituisce un dizionario con conteggi e segnale finale.
    """
    conteggio = {"BUY": 0, "SELL": 0, "NEUTRAL": 0}
    for segnale in segnali.values():
        conteggio[segnale] = conteggio.get(segnale, 0) + 1

    if conteggio["BUY"] > conteggio["SELL"]:
        segnale_finale = "BUY"
    elif conteggio["SELL"] > conteggio["BUY"]:
        segnale_finale = "SELL"
    else:
        segnale_finale = "NEUTRAL"

    return {
        "segnale_finale": segnale_finale,
        "conteggio_buy": conteggio["BUY"],
        "conteggio_sell": conteggio["SELL"],
        "conteggio_neutral": conteggio["NEUTRAL"],
        "totale_indicatori": len(segnali),
    }


# =============================================================================
# FUNZIONE PRINCIPALE
# =============================================================================

def analyze_ticker(df):
    """Analizza un ticker finanziario calcolando tutti gli indicatori tecnici.

    Parametri:
        df: pandas DataFrame con colonne [Open, High, Low, Close, Volume].
            L'indice dovrebbe essere ordinato cronologicamente (dal piu' vecchio
            al piu' recente).

    Restituisce:
        Un dizionario contenente:
        - indicators: valori correnti di tutti gli indicatori
        - signals: interpretazione BUY/SELL/NEUTRAL per ogni indicatore
        - summary: riepilogo complessivo dei segnali
        - trend: stato del trend (TRENDING_UP, TRENDING_DOWN, TRADING)
        - support_resistance: livelli di supporto e resistenza
        - volume_analysis: analisi dettagliata del volume
    """
    # Verifica che il DataFrame abbia le colonne necessarie
    colonne_richieste = ["Open", "High", "Low", "Close", "Volume"]
    for col in colonne_richieste:
        if col not in df.columns:
            raise ValueError(f"Colonna mancante nel DataFrame: {col}")

    # DEFENSIVE: questa funzione ASSUME l'ordine cronologico ascending
    # (oldest→newest) — vedi docstring — ma finora non lo IMPONEVA. rsi()/macd()
    # usano series.diff(): su una serie invertita (newest-first) ogni indicatore
    # si calcola ALL'INDIETRO, e un titolo che SALE viene letto come se SCENDESSE
    # → RSI "oversold" falso su prezzo bullish (bug osservato in produzione).
    # Riordina solo se necessario (no-op se l'indice e' gia' monotono crescente:
    # date ISO "YYYY-MM-DD" o RangeIndex). Non altera dati gia' ordinati.
    try:
        if not df.index.is_monotonic_increasing:
            df = df.sort_index()
    except Exception:
        pass

    # Verifica che ci siano abbastanza dati
    if len(df) < 200:
        # Possiamo procedere ma alcuni indicatori potrebbero essere NaN
        pass

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

    # --- Calcolo delle Medie Mobili Semplici ---
    sma_20 = sma(close, 20)
    sma_50 = sma(close, 50)
    sma_200 = sma(close, 200)

    # --- Calcolo delle Medie Mobili Esponenziali ---
    ema_9 = ema(close, 9)
    ema_12 = ema(close, 12)
    ema_26 = ema(close, 26)

    # --- Calcolo RSI ---
    rsi_14 = rsi(close, 14)

    # --- Calcolo MACD ---
    linea_macd, linea_segnale, istogramma = macd(close)

    # --- Calcolo Stocastico ---
    percent_k, percent_d = stochastic(high, low, close, periodo_k=5, periodo_d=3)

    # --- Calcolo Momentum ---
    mom_10 = momentum(close, 10)

    # --- Estrazione degli ultimi valori per l'analisi ---
    prezzo_corrente = float(close.iloc[-1])
    sma_20_val = float(sma_20.iloc[-1]) if not np.isnan(sma_20.iloc[-1]) else None
    sma_50_val = float(sma_50.iloc[-1]) if not np.isnan(sma_50.iloc[-1]) else None
    sma_200_val = float(sma_200.iloc[-1]) if not np.isnan(sma_200.iloc[-1]) else None
    ema_9_val = float(ema_9.iloc[-1]) if not np.isnan(ema_9.iloc[-1]) else None
    ema_12_val = float(ema_12.iloc[-1]) if not np.isnan(ema_12.iloc[-1]) else None
    ema_26_val = float(ema_26.iloc[-1]) if not np.isnan(ema_26.iloc[-1]) else None
    rsi_val = float(rsi_14.iloc[-1]) if not np.isnan(rsi_14.iloc[-1]) else None
    macd_val = float(linea_macd.iloc[-1]) if not np.isnan(linea_macd.iloc[-1]) else None
    segnale_macd_val = float(linea_segnale.iloc[-1]) if not np.isnan(linea_segnale.iloc[-1]) else None
    istogramma_val = float(istogramma.iloc[-1]) if not np.isnan(istogramma.iloc[-1]) else None
    k_val = float(percent_k.iloc[-1]) if not np.isnan(percent_k.iloc[-1]) else None
    d_val = float(percent_d.iloc[-1]) if not np.isnan(percent_d.iloc[-1]) else None
    mom_val = float(mom_10.iloc[-1]) if not np.isnan(mom_10.iloc[-1]) else None

    # Valori precedenti per rilevare incroci (MACD e Stocastico)
    macd_prec = float(linea_macd.iloc[-2]) if len(linea_macd) >= 2 and not np.isnan(linea_macd.iloc[-2]) else None
    segnale_prec = float(linea_segnale.iloc[-2]) if len(linea_segnale) >= 2 and not np.isnan(linea_segnale.iloc[-2]) else None
    k_prec = float(percent_k.iloc[-2]) if len(percent_k) >= 2 and not np.isnan(percent_k.iloc[-2]) else None
    d_prec = float(percent_d.iloc[-2]) if len(percent_d) >= 2 and not np.isnan(percent_d.iloc[-2]) else None

    # --- Rilevamento del trend ---
    stato_trend = rileva_trend(sma_20_val, sma_50_val, sma_200_val)

    # --- Supporto e Resistenza ---
    livelli_sr = supporto_resistenza(high, low, close)

    # --- Analisi del volume ---
    analisi_vol = analisi_volume(close, volume)

    # --- Interpretazione dei segnali individuali ---
    segnali = {
        "sma": interpreta_sma(prezzo_corrente, sma_20_val, sma_50_val, sma_200_val),
        "rsi": interpreta_rsi(rsi_val, stato_trend),
        "macd": interpreta_macd(macd_val, segnale_macd_val, macd_prec, segnale_prec),
        "stochastic": interpreta_stocastico(k_val, d_val, k_prec, d_prec),
        "momentum": interpreta_momentum(mom_val),
        "volume": interpreta_volume(analisi_vol),
    }

    # --- Riepilogo complessivo ---
    sommario = riepilogo_segnali(segnali)

    # --- Funzione di utilita' per arrotondare i valori ---
    def _round(val, decimali=4):
        if val is None:
            return None
        return round(val, decimali)

    # --- Composizione del risultato finale ---
    risultato = {
        "indicators": {
            "prezzo_corrente": _round(prezzo_corrente),
            "sma_20": _round(sma_20_val),
            "sma_50": _round(sma_50_val),
            "sma_200": _round(sma_200_val),
            "ema_9": _round(ema_9_val),
            "ema_12": _round(ema_12_val),
            "ema_26": _round(ema_26_val),
            "rsi_14": _round(rsi_val, 2),
            "macd_line": _round(macd_val),
            "macd_signal": _round(segnale_macd_val),
            "macd_histogram": _round(istogramma_val),
            "stochastic_k": _round(k_val, 2),
            "stochastic_d": _round(d_val, 2),
            "momentum_10": _round(mom_val),
        },
        "signals": segnali,
        "summary": sommario,
        "trend": stato_trend,
        "support_resistance": livelli_sr,
        "volume_analysis": analisi_vol,
    }

    return risultato


# =============================================================================
# ESECUZIONE DI TEST (se eseguito direttamente)
# =============================================================================

if __name__ == "__main__":
    # Test rapido con dati sintetici per verificare il funzionamento
    np.random.seed(42)
    n = 250  # Circa un anno di dati di trading

    # Genera un prezzo simulato con trend rialzista
    prezzi_close = 100 + np.cumsum(np.random.randn(n) * 0.5 + 0.05)
    prezzi_high = prezzi_close + np.abs(np.random.randn(n) * 0.3)
    prezzi_low = prezzi_close - np.abs(np.random.randn(n) * 0.3)
    prezzi_open = prezzi_close + np.random.randn(n) * 0.1
    volumi = np.random.randint(100000, 1000000, n).astype(float)

    df_test = pd.DataFrame({
        "Open": prezzi_open,
        "High": prezzi_high,
        "Low": prezzi_low,
        "Close": prezzi_close,
        "Volume": volumi,
    })

    risultato = analyze_ticker(df_test)

    print("=" * 60)
    print("RISULTATI ANALISI TECNICA (dati sintetici)")
    print("=" * 60)
    print(f"\nPrezzo corrente: {risultato['indicators']['prezzo_corrente']}")
    print(f"Trend: {risultato['trend']}")
    print(f"\nIndicatori:")
    for chiave, valore in risultato["indicators"].items():
        print(f"  {chiave}: {valore}")
    print(f"\nSegnali:")
    for chiave, valore in risultato["signals"].items():
        print(f"  {chiave}: {valore}")
    print(f"\nRiepilogo: {risultato['summary']}")
    print(f"\nSupporto/Resistenza: {risultato['support_resistance']}")
    print(f"\nAnalisi Volume: {risultato['volume_analysis']}")
