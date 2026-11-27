"""Avvio da terminale:  python main.py [--resume ID] [--workspace CARTELLA] [--ruolo NOME]

La chiave Gemini viene letta da Infisical ad ogni avvio (solo in memoria).
Per l'interfaccia web (telefono) usa invece:  python web.py
"""
import argparse
import json
import sys
import threading
from pathlib import Path

from google import genai

import llm
import tools
from agent import Fermato, QuotaGiornalieraFinita, RateLimiter, Sessione, esegui_obiettivo
from secrets_infisical import SecretError, get_secret

BASE = Path(__file__).parent
_io = threading.Lock()   # con piu' agenti in parallelo, un solo prompt alla volta


def prepara(workspace_arg=None):
    """Carica config, workspace e chiave Gemini (da Infisical). Si ferma con un messaggio chiaro se manca."""
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    ws = Path(workspace_arg or cfg["workspace"]).expanduser()
    ws = ws if ws.is_absolute() else BASE / ws
    ws.mkdir(parents=True, exist_ok=True)
    try:
        chiave = get_secret(cfg["infisical"]["gemini_secret"], cfg)
    except SecretError as e:   # niente richiesta della chiave in chiaro
        sys.exit(f"\n✖ {e}")
    tools.carica_brave(cfg)   # chiave Brave facoltativa: se manca si usa Bing
    prov = llm.carica_provider(cfg)   # provider di riserva con chiave in Infisical (facoltativi)
    if prov:
        print("Provider di riserva attivi:", ", ".join(prov))
    client = genai.Client(api_key=chiave)
    return cfg, ws, chiave, client, RateLimiter(cfg["requests_per_minute"])


def log_terminale(testo):
    with _io:
        print(testo, flush=True)


def conferma_terminale(testo: str) -> bool:
    """Chiede conferma nel terminale (default: no)."""
    with _io:
        return input(f"\n  ⚠ {testo}\n  Confermi? [s/N] ").strip().lower() in ("s", "si", "sì", "y", "yes")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", help="ID di una sessione salvata in sessions/")
    ap.add_argument("--workspace", help="cartella di lavoro (default: config.json)")
    ap.add_argument("--ruolo", default="generico", help="uno dei ruoli di config.json")
    ap.add_argument("--conferme", choices=tools.MODI, help="chiedi | comodo | tutto (default: config.json)")
    a = ap.parse_args()

    cfg, ws, chiave, client, limiter = prepara(a.workspace)
    ruolo = cfg["ruoli"].get(a.ruolo)
    if ruolo is None:
        sys.exit(f"Ruolo sconosciuto. Disponibili: {', '.join(cfg['ruoli'])}")
    ctx = tools.Context(cfg, ws, conferma_terminale, client, limiter, log=log_terminale, redact=[chiave, tools.brave_key()],
                       modo=a.conferme or cfg.get("modo_conferme", "chiedi"))
    sessione = Sessione(BASE / "sessions", [chiave, tools.brave_key()], a.resume)

    print(f"Modello: {cfg['model']} | ruolo: {a.ruolo} | workspace: {ws} | max passi: {cfg['max_steps']} | sessione: {sessione.sid}")
    print("Scrivi un obiettivo (/exit per uscire).")
    try:
        while True:
            obiettivo = input("\nObiettivo> ").strip()
            if obiettivo in ("/exit", "/quit"):
                break
            if not obiettivo:
                continue
            try:
                print("\n=== Risultato ===\n" + esegui_obiettivo(ctx, sessione, obiettivo, ruolo=ruolo))
                if ctx.verifica:
                    v = ctx.verifica
                    print("✓ Verificato dal sistema" if v["ok"] and not v["avvisi"] else
                          ("✓ Verificato, con avvisi: " if v["ok"] else "⚠ NON verificato: ") + " | ".join(v["errori"] + v["avvisi"]))
                print(f"(richieste a Gemini in questa esecuzione: {limiter.total})")
            except QuotaGiornalieraFinita as e:
                print(f"\n✖ {e}")
                break
            except Fermato:
                print("\nFermato.")
            except Exception as e:
                print(f"\n✖ Errore: {str(e)[:500]}")
    except (KeyboardInterrupt, EOFError):
        print()
    print(f"Sessione salvata: python main.py --resume {sessione.sid}")


if __name__ == "__main__":
    main()
