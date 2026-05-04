#!/usr/bin/env python3
"""
encrypt_build.py — Cifra il build React in un singolo blob AES-256-GCM.

Esecuzione: una volta dopo `npm run build`, prima del deploy.
Token: env var BUILD_TOKEN (32+ caratteri raccomandati).
Output: backend/encrypted_assets/bundle.enc (salt[16] + nonce[12] + ciphertext+tag).

Pipeline tipica:
    npm --prefix frontend run build
    BUILD_TOKEN=<segreto> python scripts/encrypt_build.py

Note:
- Inlina CSS e JS testuali dentro index.html (un solo file da cifrare)
- Convertibile in data: URI gli asset binari piccoli (font/img)
- PBKDF2 SHA-256, 200_000 iterazioni
- Nonce e salt random per ogni esecuzione → ogni build produce ciphertext diverso
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import secrets
import sys
from pathlib import Path

# Cryptography fornisce AES-GCM e PBKDF2 senza dipendenze fuori da OpenSSL
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

PBKDF2_ITERATIONS = 200_000
SALT_LEN = 16
NONCE_LEN = 12
KEY_LEN = 32   # AES-256

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = REPO_ROOT / "frontend" / "build"
OUTPUT_DIR = REPO_ROOT / "backend" / "encrypted_assets"
OUTPUT_FILE = OUTPUT_DIR / "bundle.enc"


def derive_key(token: str, salt: bytes) -> bytes:
    """PBKDF2 SHA-256 → chiave AES-256."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=salt,
        iterations=PBKDF2_ITERATIONS,
    )
    return kdf.derive(token.encode("utf-8"))


def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def _to_data_uri(path: Path) -> str:
    """Asset binario → data: URI base64."""
    mime = _guess_mime(path)
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _inline_html(index_html: Path) -> str:
    """
    Inlina CSS/JS dentro index.html, sostituisce immagini/font con data URI.
    Risultato: un singolo HTML autosufficiente.
    """
    if not index_html.is_file():
        raise FileNotFoundError(f"index.html non trovato in {index_html.parent}")

    html = index_html.read_text(encoding="utf-8")
    base = index_html.parent

    def _resolve(rel: str) -> Path | None:
        rel = rel.lstrip("/")
        candidate = base / rel
        if candidate.is_file():
            return candidate
        # Prova senza il prefisso (relative path)
        return None

    # 1) <link ... rel="stylesheet" ... href="..."> → <style>...</style>
    # IMPORTANTE: gli attributi possono essere in QUALSIASI ordine. Il React
    # build di CRA scrive `<link href="X" rel="stylesheet">` (href prima di
    # rel) → il regex precedente che cercava `rel.*href` non matchava → CSS
    # non inlinato → app caricava senza stili (visibile come testo nero su
    # bianco senza layout). Fix: matchamo OGNI <link> e poi controlliamo
    # entrambi gli attributi indipendentemente dall'ordine.
    def replace_link(m: re.Match) -> str:
        tag = m.group(0)
        # Verifica che sia un <link rel="stylesheet">
        if not re.search(r'\brel\s*=\s*["\']stylesheet["\']', tag, re.IGNORECASE):
            return tag
        # Estrai href
        href_m = re.search(r'\bhref\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if not href_m:
            return tag
        href = href_m.group(1)
        f = _resolve(href)
        if not f:
            return tag  # esterno (es. fonts.googleapis.com) o non trovato
        css = f.read_text(encoding="utf-8")
        return f"<style>{css}</style>"

    html = re.sub(r'<link\b[^>]*/?>', replace_link, html)

    # 2) <script src="..."></script> → estrai TUTTI i contenuti e accumula
    # per appenderli a fine body. Importante: il React build usa <script defer>
    # → l'esecuzione avviene DOPO il parsing del DOM. Quando inliniamo come
    # <script>...</script> perdiamo l'attributo defer (non valido su inline) e
    # se lo script si trova in <head> tenta di accedere a #root prima che
    # esista → React error #299 ("target container is not a DOM element").
    # Soluzione: rimuoviamo i tag <script src> ovunque siano e li riattacciamo
    # come <script>...</script> subito prima di </body>, preservando l'ordine
    # originale (importante se ci sono multiple deps che dipendono fra loro).
    inline_scripts: list[str] = []

    def collect_script(m: re.Match) -> str:
        src = m.group(1)
        f = _resolve(src)
        if not f:
            return m.group(0)
        js = f.read_text(encoding="utf-8")
        # Escape `</script>` dentro il codice JS (safety per il parser HTML)
        js = js.replace("</script>", "<\\/script>")
        inline_scripts.append(js)
        return ""  # rimuove il tag dalla posizione originale

    html = re.sub(
        r'<script[^>]+src=["\']([^"\']+)["\'][^>]*>\s*</script>',
        collect_script, html,
    )

    if inline_scripts:
        # Concatena con un newline come separatore — preserva l'ordine
        scripts_block = "\n".join(f"<script>{js}</script>" for js in inline_scripts)
        # Inserisci subito prima di </body>. Uso str.replace (non re.sub)
        # perché il JS minificato contiene \d, \w, ecc. che re.sub
        # interpreterebbe come backreferences nella replacement string,
        # generando re.PatternError: bad escape \d.
        idx = html.lower().rfind("</body>")
        if idx >= 0:
            html = html[:idx] + scripts_block + "\n" + html[idx:]
        else:
            # Fallback: append a fine HTML
            html = html + scripts_block

    # 3) Immagini, font, favicon: converti in data URI
    def replace_asset(m: re.Match) -> str:
        attr_name = m.group(1)
        url = m.group(2)
        if url.startswith(("data:", "http://", "https://", "//", "#")):
            return m.group(0)
        f = _resolve(url)
        if not f:
            return m.group(0)
        # Solo asset binari piccoli (<200KB) → data URI inline
        if f.stat().st_size > 200_000:
            return m.group(0)
        return f'{attr_name}="{_to_data_uri(f)}"'

    html = re.sub(
        r'\b(href|src)=["\']([^"\']+\.(?:png|jpg|jpeg|gif|svg|ico|woff2?|ttf|otf))["\']',
        replace_asset, html, flags=re.IGNORECASE,
    )

    return html


def encrypt_bundle(token: str) -> Path:
    if not BUILD_DIR.is_dir():
        raise FileNotFoundError(
            f"Build directory non trovata: {BUILD_DIR}. "
            f"Esegui prima `npm --prefix frontend run build`."
        )
    if len(token) < 16:
        print(
            f"[WARN] BUILD_TOKEN solo {len(token)} caratteri — raccomandati 32+",
            file=sys.stderr,
        )

    print(f"[1/4] Inline asset da {BUILD_DIR}...")
    inlined = _inline_html(BUILD_DIR / "index.html")
    plaintext = inlined.encode("utf-8")
    print(f"      → {len(plaintext):,} byte (HTML inlined)")

    print("[2/4] Genero salt + nonce random e derivo chiave PBKDF2...")
    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    key = derive_key(token, salt)

    print("[3/4] Cifro con AES-256-GCM...")
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)
    # ciphertext include il GCM tag (16 byte) appeso

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    blob = salt + nonce + ciphertext
    OUTPUT_FILE.write_bytes(blob)

    print(f"[4/4] Scritto {OUTPUT_FILE}")
    print(f"      Dimensione: {len(blob):,} byte")
    print(f"      SHA-256:    {hashlib.sha256(blob).hexdigest()}")
    print()
    print("[OK]   Build cifrato. Per il deploy ricorda di impostare ENCRYPTION_ENABLED=true.")
    return OUTPUT_FILE


def main() -> int:
    token = os.environ.get("BUILD_TOKEN", "").strip()
    if not token:
        print("Errore: BUILD_TOKEN non configurato.", file=sys.stderr)
        print("Esempio: BUILD_TOKEN='un-segreto-lungo-32-caratteri' python scripts/encrypt_build.py", file=sys.stderr)
        return 1
    try:
        encrypt_bundle(token)
        return 0
    except Exception as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
