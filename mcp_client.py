"""Client MCP minimale (Model Context Protocol, trasporto stdio) per dare all'agente strumenti di altri programmi.

Perche' scritto qui e non una libreria: sono ~100 righe, nessuna dipendenza scaricata, e si vede TUTTO quello che fa.
Sicurezza (un server MCP e' codice che gira con i TUOI permessi, quindi):
 - spento di default (config "mcp": {"attivo": false});
 - ogni server e' elencato a mano in config.json con il comando ESATTO e la lista degli strumenti consentiti (lista vuota = nessuno);
 - il server parte con un ambiente minimale: nessun segreto, nessuna variabile dell'utente;
 - ogni chiamata passa dalla conferma dell'utente (tools.serve_conferma) e dal registro delle azioni;
 - i risultati sono DATI, non istruzioni (il modello lo sa dal prompt di sistema), e sono troncati.
Config:  "mcp": {"attivo": true, "server": {"nome": {"comando": ["/percorso/server", "--opzione"], "strumenti": ["a", "b"]}}}
"""
import json
import os
import queue
import re
import subprocess
import threading

PROTOCOLLO = "2024-11-05"
TIPI_SEMPLICI = {"string", "integer", "number", "boolean"}


class ErroreMCP(RuntimeError):
    pass


class ServerMCP:
    def __init__(self, nome: str, comando: list[str], consentiti: list[str], timeout: float = 30):
        self.nome, self.consentiti, self.timeout = nome, set(consentiti), timeout
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS") if k in os.environ}
        self.p = subprocess.Popen(comando, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env, bufsize=1)
        self._id, self._lock, self._righe = 0, threading.Lock(), queue.Queue()
        threading.Thread(target=self._leggi, daemon=True).start()
        self._rpc("initialize", {"protocolVersion": PROTOCOLLO, "capabilities": {}, "clientInfo": {"name": "gemini-agent", "version": "1"}})
        self._scrivi({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.strumenti = [t for t in self._rpc("tools/list", {}).get("tools", []) if t.get("name") in self.consentiti]

    def _leggi(self):
        for riga in self.p.stdout:
            self._righe.put(riga)
        self._righe.put(None)

    def _scrivi(self, msg: dict):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def _rpc(self, metodo: str, params: dict):
        with self._lock:
            self._id += 1
            mid = self._id
            self._scrivi({"jsonrpc": "2.0", "id": mid, "method": metodo, "params": params})
            while True:
                try:
                    riga = self._righe.get(timeout=self.timeout)
                except queue.Empty:
                    raise ErroreMCP(f"{self.nome}: nessuna risposta entro {self.timeout:.0f}s")
                if riga is None:
                    raise ErroreMCP(f"{self.nome}: il server si e' chiuso")
                try:
                    msg = json.loads(riga)
                except ValueError:
                    continue
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise ErroreMCP(f"{self.nome}: {str(msg['error'].get('message', msg['error']))[:200]}")
                    return msg.get("result", {})

    def chiama(self, strumento: str, args: dict, max_car: int = 8000) -> str:
        if strumento not in self.consentiti:
            raise ErroreMCP(f"strumento «{strumento}» non consentito in config.json")
        r = self._rpc("tools/call", {"name": strumento, "arguments": args})
        testo = "\n".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text") or json.dumps(r.get("structuredContent", r), ensure_ascii=False)
        if r.get("isError"):
            testo = "Errore: " + testo
        return testo[:max_car]

    def chiudi(self):
        try:
            self.p.terminate()
        except OSError:
            pass


def _schema(s: dict) -> dict:
    """Riduce lo schema JSON di MCP a quello che Gemini e i modelli piccoli accettano: solo tipi semplici."""
    props = {}
    for k, v in ((s or {}).get("properties") or {}).items():
        t = v.get("type") if isinstance(v, dict) else None
        d = (v.get("description", "") if isinstance(v, dict) else "")[:200]
        if t in TIPI_SEMPLICI:
            props[k] = {"type": t, "description": d}
        else:
            props[k] = {"type": "string", "description": (d + " (passa un valore JSON come testo)").strip()}
    return {"type": "object", "properties": props, "required": [r for r in (s or {}).get("required", []) if r in props]}


def nome_qualificato(server: str, strumento: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", f"mcp_{server}_{strumento}")[:60]


class Registro:
    """Server MCP attivi e relative dichiarazioni di strumenti per il modello."""

    def __init__(self):
        self.server: dict[str, ServerMCP] = {}
        self.mappa: dict[str, tuple[str, str]] = {}
        self.dichiarazioni: list[dict] = []
        self.errori: list[str] = []

    def avvia(self, cfg: dict):
        c = (cfg or {}).get("mcp") or {}
        if not c.get("attivo"):
            return
        for nome, s in (c.get("server") or {}).items():
            if nome in self.server or not s.get("comando") or not s.get("strumenti"):
                continue
            try:
                srv = ServerMCP(nome, [str(x) for x in s["comando"]], list(s["strumenti"]), float(s.get("timeout", 30)))
            except (OSError, ErroreMCP, ValueError) as e:
                self.errori.append(f"{nome}: {e}")
                continue
            self.server[nome] = srv
            for t in srv.strumenti:
                q = nome_qualificato(nome, t["name"])
                self.mappa[q] = (nome, t["name"])
                self.dichiarazioni.append({"name": q, "description": f"[MCP:{nome}] " + (t.get("description") or "")[:400],
                                           "parameters": _schema(t.get("inputSchema"))})

    def chiama(self, qualificato: str, args: dict, max_car: int = 8000) -> str:
        nome, strumento = self.mappa[qualificato]
        schema = next(d for d in self.dichiarazioni if d["name"] == qualificato)["parameters"]["properties"]
        pulito = {}
        for k, v in args.items():
            if k not in schema:
                raise ErroreMCP(f"parametro «{k}» sconosciuto")
            if schema[k]["type"] == "string" and "JSON" in schema[k]["description"] and isinstance(v, str):
                try:
                    v = json.loads(v)
                except ValueError:
                    pass
            pulito[k] = v
        return self.server[nome].chiama(strumento, pulito, max_car)

    def chiudi(self):
        for s in self.server.values():
            s.chiudi()
        self.server.clear(), self.mappa.clear(), self.dichiarazioni.clear()


REGISTRO = Registro()
