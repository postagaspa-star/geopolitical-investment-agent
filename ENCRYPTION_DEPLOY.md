# Client-side Encryption — Deploy Pipeline

## Architettura

```
[Browser]                                        [Render Server]
unlock.html (vanilla)                            FastAPI
    |                                              |
    |--- POST token nel form (locale, non sale!)   |
    |--- PBKDF2 SHA-256 200k iter → AES-256 key    |
    |                                              |
    |--- GET /encrypted-bundle  ────────────────▶  |
    |                                              |  (blob cifrato AES-GCM
    |◀──────────────  salt+nonce+ciphertext        |   senza chiave)
    |                                              |
    |--- AES-GCM decrypt → HTML inlined            |
    |--- document.write(html) → React app live     |
    |    (chiave AES esce di scope, GC la libera)  |
```

Il server non vede mai il token né la chiave. Senza token corretto, l'utente vede solo la pagina `unlock.html` + bytes cifrati.

## Setup locale (una volta)

```bash
# 1. Build React normale
npm --prefix frontend install
npm --prefix frontend run build

# 2. Cifra il build (genera backend/encrypted_assets/bundle.enc)
pip install cryptography
BUILD_TOKEN='un-segreto-lungo-32-caratteri' python scripts/encrypt_build.py
```

L'output `backend/encrypted_assets/bundle.enc` è ~stessa dimensione del build (compressione ~zero su HTML+JS+CSS, ma cifratura AES-GCM).

## Deploy su Render

### 1. Aggiorna `build.sh` (o equivalente Build Command su Render)

Sostituisci il vecchio:
```bash
# vecchio
npm --prefix frontend install && npm --prefix frontend run build
cp -r frontend/build/* backend/static/
pip install -r backend/requirements.txt
```

Con:
```bash
# nuovo
npm --prefix frontend install && npm --prefix frontend run build
pip install -r backend/requirements.txt
python scripts/encrypt_build.py        # legge BUILD_TOKEN da env
```

`BUILD_TOKEN` deve essere accessibile durante il build: aggiungilo come **Build Environment Variable** su Render (Settings → Environment → "Add build-only env var" se disponibile, altrimenti come env normale che è già accessibile al build script).

### 2. Render env vars da aggiungere

| Var | Valore | Scope |
|---|---|---|
| `BUILD_TOKEN` | il segreto da condividere con l'utente legittimo | Build (anche runtime se vuoi rotazione facile) |
| `ENCRYPTION_ENABLED` | `true` | Runtime |

### 3. Deploy

```bash
git push
```

Render esegue:
1. `npm run build` → `frontend/build/`
2. `pip install -r requirements.txt` (include `cryptography`)
3. `python scripts/encrypt_build.py` → `backend/encrypted_assets/bundle.enc`
4. Avvia FastAPI con `ENCRYPTION_ENABLED=true`
5. FastAPI serve solo `unlock.html` e `/encrypted-bundle`

### 4. Test post-deploy

```bash
# Senza token: ricevi solo unlock.html
curl https://<tua-app>.onrender.com/
# → <!DOCTYPE html><html lang="it">... unlock form

# Bundle è bytes cifrati
curl -I https://<tua-app>.onrender.com/encrypted-bundle
# → Content-Type: application/octet-stream

# Dal browser: apri / → unlock form → digita BUILD_TOKEN → app carica
```

## Verifica criteri di successo

| Criterio | Verifica |
|---|---|
| Senza token → solo bytes cifrati | `curl /encrypted-bundle | xxd | head` mostra random bytes |
| Con token → app funziona | Apri browser, inserisci token, verifica funzionamento normale |
| Refresh → token reinserito | Ricarica la pagina, vedi di nuovo il form di unlock |
| Server non logga segreti | Render logs non contengono `BUILD_TOKEN` (controllato: il backend non lo legge mai) |
| Chiave solo in memoria JS | DevTools → Application → Storage: nessuna chiave né in `localStorage` né `sessionStorage` |
| Chiave non extractable | `crypto.subtle.deriveKey(..., false, ...)` → la chiave AES non è esportabile via `exportKey` |

## Rotazione del token

Per cambiare il token senza rebuild dei sorgenti:
1. Aggiorna `BUILD_TOKEN` su Render
2. Trigger manuale del build (Manual Deploy → Clear cache → Deploy)
3. Distribuisci il nuovo token agli utenti legittimi

Il bundle vecchio resta inutilizzabile col nuovo token (salt diverso → chiave diversa → tag GCM non valida).

## Limiti / note

- **Refresh = token da reinserire**: by design (`document.write` sostituisce la pagina, ma F5 ricarica `unlock.html` da server).
- **HTTPS obbligatorio**: Web Crypto API richiede contesto sicuro.
- **Asset > 200KB**: lo script `encrypt_build.py` non li inlina come data URI (limitazione hardcoded). Se ne hai, aumenta il limite o serve assets cifrati separatamente. Per la tua dashboard React: tipicamente sotto-soglia.
- **Brute-force**: PBKDF2 200k iter rende l'attacco offline ~2-3s/tentativo su CPU desktop. Usa token ≥ 24 char random per sicurezza pratica. Lo script logga warning sotto i 16 char.
