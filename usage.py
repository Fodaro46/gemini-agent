"""Contatore dei consumi: richieste e token per modello, per giorno. Salvato in uso.json (nessun segreto).

Il reset delle quote gratuite di Google avviene a mezzanotte, ora del Pacifico (America/Los_Angeles).
I limiti NON si possono leggere da un'API: li metti tu in config.json ("limiti") guardando
aistudio.google.com/rate-limit; se una richiesta riceve l'errore 'quota giornaliera finita' lo segniamo noi.
"""
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PACIFICO = ZoneInfo("America/Los_Angeles")


def oggi():
    return datetime.now(PACIFICO).strftime("%Y-%m-%d")


def prossimo_reset() -> float:
    """Timestamp (secondi) del prossimo reset: mezzanotte nel Pacifico."""
    adesso = datetime.now(PACIFICO)
    nxt = (adesso + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()


class Uso:
    def __init__(self, file: Path):
        self.file, self.lock = file, threading.Lock()
        try:
            d = json.loads(file.read_text(encoding="utf-8"))
            self.dati = d if isinstance(d, dict) else {}
        except Exception:
            self.dati = {}

    def recupera_da_cronologia(self, cartella: Path):
        """Una tantum: stima le richieste di OGGI (giorno del Pacifico) dalle sessioni salvate prima che il contatore esistesse.
        Ogni risposta del modello nella cronologia = 1 richiesta. E' una stima: non vede altri usi della stessa chiave."""
        with self.lock:
            if self.dati.get("_recuperato") == oggi():
                return 0
            n = 0
            for f in cartella.glob("*.json"):
                if datetime.fromtimestamp(f.stat().st_mtime, PACIFICO).strftime("%Y-%m-%d") != oggi():
                    continue
                try:
                    n += sum(1 for c in json.loads(f.read_text(encoding="utf-8"))["contents"] if c.get("role") == "model")
                except Exception:
                    pass
            if n:
                m = self.dati.setdefault(oggi(), {}).setdefault("gemini (stima da cronologia)", {"richieste": 0, "in": 0, "out": 0})
                m["richieste"] += n
            self.dati["_recuperato"] = oggi()
            self._salva()
            return n

    def _salva(self):
        try:
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.dati, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.file)
        except OSError:
            pass

    def registra(self, modello: str, tok_in: int = 0, tok_out: int = 0):
        with self.lock:
            m = self.dati.setdefault(oggi(), {}).setdefault(modello, {"richieste": 0, "in": 0, "out": 0})
            m["richieste"] += 1
            m["in"] += int(tok_in or 0)
            m["out"] += int(tok_out or 0)
            self._salva()

    def esaurito(self, modello: str):
        with self.lock:
            self.dati.setdefault("_esauriti", {})[modello] = oggi()
            self._salva()

    def e_esaurito(self, modello: str) -> bool:
        return self.dati.get("_esauriti", {}).get(modello) == oggi()

    def riepilogo(self, cfg: dict, provider_attivi: list[str]) -> dict:
        with self.lock:
            g = oggi()
            limiti = cfg.get("limiti", {})
            modelli = []
            nomi = {cfg.get("model"), cfg.get("fallback_model")} | set(self.dati.get(g, {})) | set(provider_attivi)
            for nome in sorted(n for n in nomi if n):
                u = self.dati.get(g, {}).get(nome, {"richieste": 0, "in": 0, "out": 0})
                modelli.append({"nome": nome, "richieste": u["richieste"], "tok_in": u["in"], "tok_out": u["out"],
                                "limite": limiti.get(nome, {}).get("rpd"), "rpm": limiti.get(nome, {}).get("rpm"),
                                "tpm": limiti.get(nome, {}).get("tpm"), "esaurito": self.e_esaurito(nome)})
            storia = []
            for i in range(6, -1, -1):
                d = (datetime.now(PACIFICO) - timedelta(days=i)).strftime("%Y-%m-%d")
                tot = self.dati.get(d, {})
                storia.append({"giorno": d, "richieste": sum(x["richieste"] for x in tot.values()),
                               "token": sum(x["in"] + x["out"] for x in tot.values())})
            return {"giorno": g, "reset": prossimo_reset(), "modelli": modelli, "storia": storia,
                    "rpm": cfg.get("requests_per_minute")}


USO = Uso(Path(__file__).parent / "uso.json")
USO.recupera_da_cronologia(Path(__file__).parent / "sessions")
