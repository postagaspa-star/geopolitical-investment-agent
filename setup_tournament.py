"""
setup_tournament.py — Setup una-tantum per il bot ClawStreet Tournament.

Esegui questo script UNA SOLA VOLTA dalla tua macchina locale.
Registra un nuovo bot su ClawStreet e stampa i GitHub Secrets da impostare.

Uso:
    python setup_tournament.py --app-url https://geopolitical-investment-agent.onrender.com

oppure, se hai già le credenziali del bot (bot_id + api_key):
    python setup_tournament.py --bot-id <ID> --api-key <KEY>
"""

import argparse
import json
import sys
import urllib.request
import urllib.error


def register_via_app(app_url: str) -> dict:
    """Registra un nuovo bot ClawStreet tramite l'endpoint dell'app di production."""
    url = f"{app_url.rstrip('/')}/api/clawstreet/register"
    print(f"\n→ Chiamo {url} ...")
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"❌ HTTP {e.code}: {body[:500]}")
        sys.exit(1)
    except Exception as exc:
        print(f"❌ Errore: {exc}")
        sys.exit(1)


def print_secrets(bot_id: str, api_key: str, deepseek_key: str = "", news_key: str = ""):
    """Stampa le istruzioni per impostare i GitHub Secrets."""
    print("\n" + "=" * 60)
    print("✅  BOT REGISTRATO — imposta questi GitHub Secrets:")
    print("    (repo → Settings → Secrets and variables → Actions → New repository secret)")
    print("=" * 60)
    print(f"\n  CS_BOT_ID   =  {bot_id}")
    print(f"  CS_API_KEY  =  {api_key}")
    if deepseek_key:
        print(f"  DEEPSEEK_API_KEY  =  {deepseek_key}")
    else:
        print("  DEEPSEEK_API_KEY  =  <la tua chiave DeepSeek>")
    if news_key:
        print(f"  NEWS_API_KEY  =  {news_key}")
    else:
        print("  NEWS_API_KEY  =  <la tua chiave NewsAPI, opzionale>")
    print("\n" + "=" * 60)
    print("\nDopo aver impostato i secret, vai su:")
    print("  GitHub → Actions → 'ClawStreet Tournament Bot' → 'Run workflow'")
    print("per testare il primo run manuale.\n")


def main():
    parser = argparse.ArgumentParser(description="Setup bot ClawStreet Tournament")
    parser.add_argument("--app-url", help="URL dell'app Render (es: https://....onrender.com)")
    parser.add_argument("--bot-id", help="Bot ID già esistente (salta la registrazione)")
    parser.add_argument("--api-key", help="API key già esistente (salta la registrazione)")
    parser.add_argument("--deepseek-key", default="", help="La tua chiave DeepSeek API")
    parser.add_argument("--news-key", default="", help="La tua chiave NewsAPI")
    args = parser.parse_args()

    if args.bot_id and args.api_key:
        print(f"\n✅ Usando credenziali fornite direttamente.")
        print_secrets(args.bot_id, args.api_key, args.deepseek_key, args.news_key)
        return

    if not args.app_url:
        print("\n❌ Specifica --app-url oppure --bot-id + --api-key")
        print("Esempio: python setup_tournament.py --app-url https://geopolitical-investment-agent.onrender.com")
        sys.exit(1)

    result = register_via_app(args.app_url)
    print(f"\nRisposta API: {json.dumps(result, indent=2)}")

    # Estrai credenziali dalla risposta
    bot_id = (
        result.get("bot_id")
        or result.get("data", {}).get("bot_id")
        or result.get("clawstreet_bot_id")
        or ""
    )
    api_key = (
        result.get("api_key")
        or result.get("data", {}).get("api_key")
        or result.get("clawstreet_api_key")
        or ""
    )

    if not bot_id or not api_key:
        print("\n⚠️  Impossibile estrarre bot_id/api_key dalla risposta.")
        print("   Controlla la risposta sopra e imposta manualmente CS_BOT_ID e CS_API_KEY.")
        return

    print_secrets(bot_id, api_key, args.deepseek_key, args.news_key)


if __name__ == "__main__":
    main()
