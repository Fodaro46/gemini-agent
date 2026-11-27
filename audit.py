"""Registro di tutto quello che fanno gli agenti (per vederlo dal telefono) e interruttore "Blocca tutto".

Il registro e' in ~/.local/share/gemini-agent/audit.jsonl (fuori dalla cartella di lavoro e protetto dagli strumenti).
Il blocco e' un file: finche' esiste, nessuno strumento parte e non si avviano nuovi agenti, anche dopo un riavvio.
"""
import json
import threading
import time
from pathlib import Path

DATI = Path.home() / ".local/share/gemini-agent"
FILE = DATI / "audit.jsonl"
BLOCCO = DATI / "BLOCCATO"
_L = threading.Lock()


def registra(chi: str, strumento: str, args, esito, redact=()):
    DATI.mkdir(parents=True, exist_ok=True)
    riga = json.dumps({"t": time.time(), "chi": chi, "strumento": strumento,
                       "args": json.dumps(args, ensure_ascii=False)[:400], "esito": str(esito).replace("\n", " ")[:160]},
                      ensure_ascii=False)
    for s in redact:
        if s:
            riga = riga.replace(s, "[SEGRETO]")
    with _L:
        with FILE.open("a", encoding="utf-8") as f:
            f.write(riga + "\n")


def ultime(n: int = 200) -> list[dict]:
    n = max(1, min(int(n), 2000))
    try:
        righe = FILE.read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for r in reversed(righe):
        try:
            out.append(json.loads(r))
        except ValueError:
            pass
    return out


def bloccato() -> bool:
    return BLOCCO.exists()


def imposta_blocco(attivo: bool):
    DATI.mkdir(parents=True, exist_ok=True)
    if attivo:
        BLOCCO.write_text("bloccato dall'utente\n", encoding="utf-8")
    elif BLOCCO.exists():
        BLOCCO.unlink()
