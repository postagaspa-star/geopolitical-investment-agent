"""
Estrae testo da PDF crypto/technical-analysis e li salva in
backend/preset_documents/crypto/ come .txt + manifest.json.

Eseguire UNA VOLTA per popolare i preset. Lo script tronca ogni doc a
N caratteri per non saturare il context LLM.
"""
import json
import sys
from pathlib import Path
from PyPDF2 import PdfReader

# Mapping: percorso PDF → (slug, titolo amichevole)
PDFS = [
    (r"C:/Users/andre/Downloads/De_Simone_Alberto.pdf",
     "de_simone_strategie_crypto", "De Simone — Strategie crypto avanzate"),
    (r"C:/Users/andre/Downloads/CMC-Glassnode-On-Chain-Analytics-Issue-One.pdf",
     "glassnode_onchain_analytics", "CMC × Glassnode — On-Chain Analytics"),
    (r"C:/Users/andre/Downloads/GLXY_On-Chain_Fundamentals_Whitepaper.pdf",
     "galaxy_onchain_fundamentals", "Galaxy — On-Chain Fundamentals Whitepaper"),
    (r"C:/Users/andre/Downloads/ebook-forex-Ichimoku-bank-en.pdf",
     "ichimoku_kinko_hyo", "Ichimoku Kinko Hyo — Trading Guide"),
    (r"C:/Users/andre/Downloads/ebook-forex-japanese-candlesticks-bank-en.pdf",
     "japanese_candlesticks", "Japanese Candlesticks — Patterns & Reading"),
    (r"C:/Users/andre/Downloads/ebook-forex-chart-patterns-bank-en.pdf",
     "chart_patterns", "Chart Patterns — Classical Technical Analysis"),
    (r"C:/Users/andre/Downloads/ebook-trading-cryptocurrencies-1-en.pdf",
     "trading_crypto_vol1", "Trading Cryptocurrencies — Vol. 1 (Fundamentals)"),
    (r"C:/Users/andre/Downloads/ebook-trading-cryptocurrencies-2-en.pdf",
     "trading_crypto_vol2", "Trading Cryptocurrencies — Vol. 2 (Strategies)"),
    (r"C:/Users/andre/Downloads/ebook-trading-cryptocurrencies-3-en.pdf",
     "trading_crypto_vol3", "Trading Cryptocurrencies — Vol. 3 (Advanced)"),
]

# Limite per documento. ~25K char ≈ ~6K token. Con 9 doc = ~54K token,
# entra in context Sonnet/R1 (200K window) lasciando spazio per buffer + tech.
MAX_CHARS = 25000

OUT_DIR = Path(__file__).resolve().parent.parent / "backend" / "preset_documents" / "crypto"


def extract(pdf_path: str) -> str:
    reader = PdfReader(pdf_path)
    parts = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
            parts.append(t)
        except Exception:
            continue
    full = "\n".join(parts)
    # Cleanup: rimuovi righe vuote consecutive
    import re
    full = re.sub(r"\n{3,}", "\n\n", full)
    full = re.sub(r" {3,}", " ", full)
    return full.strip()


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = []
    for pdf_path, slug, title in PDFS:
        p = Path(pdf_path)
        if not p.is_file():
            print(f"[SKIP] {pdf_path} non esiste")
            continue
        print(f"[..] {slug} ({p.stat().st_size // 1024} KB)")
        try:
            text = extract(str(p))
        except Exception as exc:
            print(f"  [ERR] estrazione fallita: {exc}")
            continue
        original_len = len(text)
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + "\n\n[...documento troncato per limiti context...]"
        out = OUT_DIR / f"{slug}.txt"
        out.write_text(text, encoding="utf-8")
        manifest.append({
            "slug": slug, "filename": f"{slug}.txt", "title": title,
            "source_pdf": p.name, "chars": len(text), "original_chars": original_len,
        })
        print(f"  [OK]  {len(text):,} char (orig: {original_len:,}) -> {out.name}")

    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[DONE] {len(manifest)} preset salvati in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
