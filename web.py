"""Interfaccia web locale: vedi gli agenti attivi, il loro stato, lanci nuovi compiti e approvi le conferme
dal telefono (stessa rete Wi-Fi). Avvio:  python web.py

Sicurezza: serve un token (letto da Infisical, segreto "AGENT_WEB_TOKEN"). Dopo il login il browser riceve
un cookie di sessione casuale. Attenzione: e' HTTP in chiaro sulla tua LAN (vedi README).
"""
import argparse
import hashlib
import hmac
import os
import re
import shutil
import subprocess
import json
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, make_response, request

import tools
from agent import Fermato, QuotaGiornalieraFinita, Sessione, esegui_obiettivo
import allegati
import audit
import llm
import voce
from main import BASE, prepara
from usage import USO
from secrets_infisical import SecretError, get_secret

L = threading.RLock()          # protegge lo stato condiviso
RUNS: dict[str, "Run"] = {}
REVOCATE: set[str] = set()      # cookie disconnessi con 'Esci' (salvati su disco: sopravvivono ai riavvii)
FILE_REVOCHE = audit.DATI / "revocate.json"
FILE_CHIAVE = audit.DATI / "cookie.key"


def chiave_cookie() -> bytes:
    """Chiave casuale (permessi 600), distinta dal token: un cookie intercettato non permette di indovinare il token."""
    audit.DATI.mkdir(parents=True, exist_ok=True)
    if not FILE_CHIAVE.exists():
        FILE_CHIAVE.write_bytes(secrets.token_bytes(32))
        FILE_CHIAVE.chmod(0o600)
    return FILE_CHIAVE.read_bytes()


def carica_revoche():
    try:
        REVOCATE.update(json.loads(FILE_REVOCHE.read_text()))
    except (OSError, ValueError):
        pass
DURATA_SESSIONE = 30 * 86400    # 30 giorni
TENTATIVI: dict[str, tuple[int, float, float]] = {}  # chi -> (errori, bloccato fino a, ultimo errore)
NONCE: dict[str, float] = {}   # codici monouso per la finestra desktop (valgono 30 s, solo da localhost)


def nuovo_nonce():
    n = secrets.token_urlsafe(24)
    NONCE[n] = time.time() + 30
    return n


class Run:
    """Un agente lanciato dalla pagina web."""

    def __init__(self, obiettivo, ruolo):
        self.id = uuid.uuid4().hex[:8]
        self.nome = f"agente-{self.id[:4]}"
        self.obiettivo, self.ruolo = obiettivo, ruolo
        self.cartella = ""          # nome della cartella di lavoro scelta ('' = predefinita)
        self.inizio = time.time()
        self.stato, self.risultato = "in corso", ""
        self.log: list[str] = []
        self.conferme: list[dict] = []
        self.turni: list[dict] = []     # [{'q': richiesta, 'a': risposta}] per mostrare la conversazione
        self.stop = threading.Event()
        self.sessione = None
        self.ctx = None
        self.thread = None

    def add(self, testo):
        with L:
            self.log.append(f"{datetime.now():%H:%M:%S} {testo}")
            del self.log[:-400]

    def attivo(self):
        return self.thread is not None and self.thread.is_alive()


def crea_app(cfg, ws, chiave, client, limiter, token):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = max(voce.MAX_BYTE, allegati.MAX_FILE) + 1024
    allegati.pulisci()
    timeout_conferma = cfg.get("confirm_timeout_s", 600)

    def cartella_ws(nome):
        """Solo cartelle elencate in config.json ('cartelle': nome -> percorso): il browser non puo' indicare percorsi liberi."""
        if not nome:
            return ws
        p = cfg.get("cartelle", {}).get(nome)
        return Path(p).expanduser().resolve() if p and Path(p).expanduser().is_dir() else None

    def conferma_web(run: Run, testo: str) -> bool:
        """Mette la richiesta in coda e aspetta che la approvi dal telefono (default: no, con timeout)."""
        c = {"id": uuid.uuid4().hex[:6], "testo": testo, "ev": threading.Event(), "ok": False}
        with L:
            run.conferme.append(c)
            run.stato = "attende conferma"
        inizio = time.time()
        while not c["ev"].wait(1):
            if run.stop.is_set() or time.time() - inizio > timeout_conferma:
                run.add("conferma scaduta o annullata: rifiutata")
                break
        with L:
            run.conferme.remove(c)
            if not run.conferme and run.stato == "attende conferma":
                run.stato = "in corso"
        return c["ok"] and not run.stop.is_set()

    def lavora(run: Run, obiettivo: str, allegati_ric=None):
        run.turni.append({"q": obiettivo, "a": "", "allegati": [{"id": x["id"], "nome": x["nome"], "tipo": x["tipo"]} for x in allegati_ric or []]})
        run.add(f"▶ {obiettivo}" + (f" (+{len(allegati_ric)} allegati)" if allegati_ric else ""))
        try:
            run.risultato = esegui_obiettivo(run.ctx, run.sessione, obiettivo, ruolo=cfg["ruoli"].get(run.ruolo, ""), allegati=allegati_ric)
            run.turni[-1]["a"] = run.risultato
            run.turni[-1]["verifica"] = run.ctx.verifica
            run.stato = "finito"
        except Fermato:
            run.stato, run.risultato = "fermato", "Fermato dall'utente."
            run.turni[-1]["a"] = run.risultato
        except QuotaGiornalieraFinita as e:
            run.stato, run.risultato = "quota finita", str(e)
            run.turni[-1]["a"] = run.risultato
        except Exception as e:
            run.stato, run.risultato = "errore", str(e)[:500]
            run.turni[-1]["a"] = run.risultato
        run.add(f"■ {run.stato}")

    def avvia(run: Run, obiettivo: str, allegati_ric=None):
        run.stato, run.risultato = "in corso", ""
        run.ctx.verifica = None
        run.inizio = time.time()
        run.stop.clear()
        run.thread = threading.Thread(target=lavora, args=(run, obiettivo, allegati_ric), daemon=True)
        run.thread.start()

    # ------------------------------------------------------------ autenticazione
    # chiave = HMAC(chiave casuale su disco, token): cambiando il token tutti i cookie vecchi smettono di valere
    _k = hmac.new(chiave_cookie(), token.encode(), hashlib.sha256).digest()
    carica_revoche()

    def nuovo_sid():
        """Cookie firmato con il token: valido anche dopo un riavvio; cambiando il token si invalidano tutti."""
        r, exp = secrets.token_urlsafe(16), int(time.time()) + DURATA_SESSIONE
        firma = hmac.new(_k, f"{r}.{exp}".encode(), hashlib.sha256).hexdigest()
        return f"{r}.{exp}.{firma}"

    def imposta_cookie(resp, sid):
        resp.set_cookie("sid", sid, httponly=True, samesite="Lax", max_age=DURATA_SESSIONE,
                        secure=request.is_secure or request.headers.get("X-Forwarded-Proto") == "https")
        return resp

    def autenticato():
        sid = request.cookies.get("sid", "")
        try:
            r, exp, firma = sid.split(".")
            atteso = hmac.new(_k, f"{r}.{exp}".encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(firma, atteso) and int(exp) > time.time() and sid not in REVOCATE
        except ValueError:
            return False

    def protetto(f):
        def w(*a, **k):
            if not autenticato():
                return jsonify(errore="non autenticato"), 401
            if request.method == "POST" and not request.is_json:   # difesa CSRF di base
                return jsonify(errore="richiesta non valida"), 400
            return f(*a, **k)
        w.__name__ = f.__name__
        return w

    @app.after_request
    def intestazioni(r):
        r.headers["Cache-Control"] = "no-store"
        r.headers["X-Content-Type-Options"] = "nosniff"
        r.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
        r.headers.setdefault("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        return r

    LOGIN = ("<!doctype html><html lang=it><meta charset=utf-8>"
             "<meta name=viewport content='width=device-width,initial-scale=1'><meta name=theme-color content='#000000'>"
             "<title>Gemini agent</title><style>"
             "*{box-sizing:border-box}:root{color-scheme:dark}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#000;"
             "color:#ededed;font:14px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif;padding:16px;-webkit-font-smoothing:antialiased}"
             "form{width:100%;max-width:360px}"
             "h1{margin:0 0 6px;font-size:22px;font-weight:600;letter-spacing:-.02em}p{color:#a1a1a1;margin:0 0 22px}"
             "input{width:100%;padding:12px 14px;font-size:16px;border-radius:10px;border:1px solid #333;background:#0a0a0a;color:inherit;outline:0}"
             "input:focus{border-color:#666}"
             "button{width:100%;padding:12px;margin-top:12px;font-size:15px;font-weight:500;border:0;border-radius:10px;background:#ededed;color:#000;cursor:pointer}"
             "button:hover{background:#fff}.e{color:#e5484d;margin-top:12px;min-height:1.2em}</style>"
             "<form method=post action=/login><h1>Gemini Agent</h1><p>Inserisci il token di accesso.</p>"
             "<input name=username value=agente autocomplete=username style='position:absolute;left:-9999px' tabindex=-1 aria-hidden=true>"
             "<input type=password name=token placeholder='Token' autocomplete=current-password autofocus aria-label=Token>"
             "<button>Entra</button><div class=e>{msg}</div></form>")

    @app.get("/")
    def home():
        if not autenticato():
            return LOGIN.replace("{msg}", "")
        return Response((BASE / "pagina.html").read_text(encoding="utf-8"), mimetype="text/html")

    @app.get("/entra")
    def entra():
        """Login automatico per la finestra desktop: codice monouso, valido 30 s, solo da questo PC."""
        scad = NONCE.pop(request.args.get("c", ""), 0)
        if request.remote_addr not in ("127.0.0.1", "::1") or time.time() > scad:
            return "Link scaduto: riapri l'app.", 403
        return imposta_cookie(make_response("", 303, {"Location": "/"}), nuovo_sid())

    @app.post("/interno/nonce")
    def interno_nonce():
        """Solo per l'icona dell'app sul PC: chi legge la chiave su disco (solo l'utente) puo' ottenere un accesso monouso.
        Rifiutato se la richiesta arriva da Tailscale/proxy (header di inoltro) o senza la chiave giusta."""
        if request.headers.get("X-Forwarded-For") or request.headers.get("Tailscale-User-Login") or request.remote_addr not in ("127.0.0.1", "::1"):
            return "vietato", 403
        atteso = hashlib.sha256(chiave_cookie()).hexdigest()
        if not hmac.compare_digest(request.headers.get("X-Interno", ""), atteso):
            return "vietato", 403
        return jsonify(nonce=nuovo_nonce())

    @app.get("/manifest.json")
    def manifest():
        return Response((BASE / "manifest.json").read_text(encoding="utf-8"), mimetype="application/manifest+json")

    @app.get("/icona.svg")
    def icona():
        return Response((BASE / "icona.svg").read_text(encoding="utf-8"), mimetype="image/svg+xml")

    @app.post("/login")
    def login():
        # dietro 'tailscale serve' remote_addr e' sempre 127.0.0.1: uso l'identita' Tailscale o l'IP inoltrato
        chi = (request.headers.get("Tailscale-User-Login") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
               or request.remote_addr or "?")
        if hmac.compare_digest(request.form.get("token", "").encode(), token.encode()):    # confronto a tempo costante
            TENTATIVI.pop(chi, None)
            return imposta_cookie(make_response("", 303, {"Location": "/"}), nuovo_sid())  # il token giusto passa SEMPRE
        errori, fino, ultimo = TENTATIVI.get(chi, (0, 0, 0))
        if time.time() < fino:
            return LOGIN.replace("{msg}", "Troppi tentativi: riprova tra un minuto."), 429
        if time.time() - ultimo > 600:        # gli errori decadono dopo 10 minuti
            errori = 0
        errori += 1
        TENTATIVI[chi] = (errori, time.time() + 60 if errori >= 5 else 0, time.time())
        return LOGIN.replace("{msg}", "Token errato."), 401

    @app.post("/logout")
    @protetto
    def logout():
        REVOCATE.add(request.cookies.get("sid", ""))
        try:
            FILE_REVOCHE.write_text(json.dumps(sorted(REVOCATE)[-200:]))
        except OSError:
            pass
        return jsonify(ok=True)

    # ------------------------------------------------------------ API
    def descrivi(r: Run, completo=False):
        with L:
            d = {"id": r.id, "nome": r.nome, "stato": r.stato, "obiettivo": r.obiettivo, "ruolo": r.ruolo,
                 "attivo": r.attivo(), "risultato": r.risultato, "inizio": r.inizio, "cartella": r.cartella, "modo": r.ctx.modo if r.ctx else None,
                 "sid": r.sessione.sid if r.sessione else None, "turni": [dict(t) for t in r.turni],   # copie: il JSON si scrive fuori dal lock
                 "conferme": [{"id": c["id"], "testo": c["testo"]} for c in r.conferme],
                 "log": list(r.log) if completo else r.log[-8:]}
        return d

    def _oggi():
        with USO.lock:
            return sum(x["richieste"] for x in USO.dati.get(__import__("usage").oggi(), {}).values())

    @app.get("/api/stato")
    @protetto
    def stato():
        aperta = request.args.get("run")         # la conversazione aperta riceve il registro completo: serve a mettere ogni passo sotto il suo messaggio
        with L:
            runs = [descrivi(r, r.id == aperta) for r in reversed(list(RUNS.values()))]
        return jsonify(bloccato=audit.bloccato(), oggi=_oggi(), richieste=limiter.total, rpm=cfg["requests_per_minute"], modello=cfg["model"],
                       ruoli=list(cfg["ruoli"]), cartelle=list(cfg.get("cartelle", {})), modo=cfg.get("modo_conferme", "comodo"), runs=runs)

    @app.get("/api/run/<rid>")
    @protetto
    def dettaglio(rid):
        r = RUNS.get(rid)
        return jsonify(descrivi(r, True)) if r else (jsonify(errore="non trovato"), 404)

    @app.get("/api/files")
    @protetto
    def files():
        """File prodotti nel workspace (solo lettura), i piu' recenti per primi."""
        base = cartella_ws(request.args.get("cartella", ""))
        if base is None:
            return jsonify(errore="cartella non consentita"), 400
        out, pesanti = [], {"node_modules", ".venv", "venv", "__pycache__", ".git", "site-packages", "target", "dist", "build"}
        for radice, dirs, nomi in os.walk(base):
            rel = Path(radice).relative_to(base)
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in pesanti and not tools.protetto(Path(radice) / d)]
            if len(rel.parts) >= 4:
                dirs[:] = []
            for n in nomi:
                f = Path(radice) / n
                if n.startswith(".") or tools.protetto(f):
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                out.append({"path": str(f.relative_to(base)), "size": st.st_size, "mtime": st.st_mtime})
            if len(out) > 3000:
                break
        out.sort(key=lambda x: -x["mtime"])
        return jsonify(files=out[:200])

    @app.get("/api/file")
    @protetto
    def file():
        base = cartella_ws(request.args.get("cartella", ""))
        if base is None:
            return jsonify(errore="cartella non consentita"), 400
        if audit.bloccato():
            return jsonify(errore="Agenti bloccati"), 423
        p = (base / request.args.get("path", "")).resolve()
        rel = p.relative_to(base.resolve()) if base.resolve() in p.parents else None
        if rel is None or not p.is_file() or tools.protetto(p) or any(x.startswith(".") for x in rel.parts):
            return jsonify(errore="file non valido"), 404          # fuori dal workspace, protetto o nascosto
        audit.registra("utente", "anteprima_file", {"path": str(rel)}, "letto")
        if p.stat().st_size > 200_000:
            return jsonify(errore="file troppo grande per l'anteprima (>200 KB)"), 413
        try:
            return jsonify(path=request.args["path"], testo=p.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            return jsonify(errore="file binario: nessuna anteprima"), 415

    @app.get("/api/sessioni")
    @protetto
    def sessioni():
        """Sessioni salvate (solo quelle principali), le piu' recenti per prime."""
        out = []
        cartella = BASE / "sessions"
        for f in sorted(cartella.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if not re.fullmatch(r"\d{8}-\d{6}", f.stem):
                continue      # i file dei sotto-agenti hanno un suffisso
            try:
                parti = json.loads(f.read_text(encoding="utf-8"))["contents"][0]["parts"]
                titolo = next(p["text"] for p in parti if p.get("text"))
            except Exception:
                continue
            out.append({"sid": f.stem, "titolo": titolo[:140], "quando": f.stat().st_mtime})
        return jsonify(sessioni=out[:40])

    @app.get("/api/sessione/<sid>")
    @protetto
    def sessione(sid):
        """Ricostruisce la conversazione di una sessione salvata: richieste, passi e risposte."""
        if not re.fullmatch(r"\d{8}-\d{6}", sid):
            return jsonify(errore="sessione non valida"), 400
        f = BASE / "sessions" / f"{sid}.json"
        if not f.is_file():
            return jsonify(errore="sessione non trovata"), 404
        turni = []
        for c in json.loads(f.read_text(encoding="utf-8"))["contents"]:
            parti = c.get("parts", [])
            if c.get("role") == "user":
                testo = [p["text"] for p in parti if p.get("text")]
                if testo:
                    turni.append({"q": "\n".join(testo), "a": "", "log": []})
            elif turni:
                chiamate = [p["function_call"] for p in parti if p.get("function_call")]
                for fc in chiamate:
                    turni[-1]["log"].append(f"00:00:00 [agente] strumento: {fc.get('name')}("
                                            f"{json.dumps(fc.get('args', {}), ensure_ascii=False)[:150]})")
                if not chiamate:
                    turni[-1]["a"] = "".join(p.get("text", "") for p in parti if not p.get("thought"))
        return jsonify(sid=sid, turni=turni)

    def prendi_allegati(ids, wsel):
        """ids -> lista di allegati pronti per l'agente (e copia nel workspace, tranne home e radice)."""
        if not isinstance(ids, list) or len(ids) > allegati.MAX_N:
            raise allegati.ErroreAllegato(f"Massimo {allegati.MAX_N} allegati per messaggio.")
        out, totale = [], 0
        for aid in ids:
            meta, dati = allegati.carica(str(aid))
            totale += len(dati)
            if totale > allegati.MAX_TOTALE:
                raise allegati.ErroreAllegato("Allegati troppo grandi in totale (massimo 20 MB).")
            percorso = None
            if str(wsel) not in ("/", str(Path.home())):
                try:
                    cartella = wsel / "allegati"
                    dest = cartella / meta["nome"]
                    if tools.scrittura_vietata(dest) is None:
                        cartella.mkdir(exist_ok=True)
                        if dest.exists():
                            dest = cartella / f"{meta['id'][:6]}-{meta['nome']}"
                        dest.write_bytes(dati)
                        percorso = str(dest.relative_to(wsel))
                except OSError:
                    percorso = None
            out.append({**meta, "dati": dati, "percorso": percorso})
        return out

    @app.post("/api/allegato")
    def carica_allegato():
        if not autenticato():
            return jsonify(errore="non autenticato"), 401
        if request.headers.get("X-Requested-With") != "gemini-agent":
            return jsonify(errore="richiesta non valida"), 400
        if audit.bloccato():
            return jsonify(errore="Agenti bloccati"), 423
        nome = urllib.parse.unquote(request.headers.get("X-Nome", "file"))
        try:
            meta, dati = allegati.prepara(nome, request.get_data(cache=False))
        except allegati.ErroreAllegato as e:
            return jsonify(errore=str(e)), 422
        meta = allegati.salva(meta, dati)
        audit.registra("utente", "allegato", {"nome": meta["nome"], "tipo": meta["tipo"], "byte": meta["size"]}, "caricato")
        return jsonify({k: meta[k] for k in ("id", "nome", "tipo", "mime", "size")})

    @app.get("/api/allegato/<aid>")
    @protetto
    def mostra_allegato(aid):
        try:
            meta, dati = allegati.carica(aid)
        except allegati.ErroreAllegato:
            return jsonify(errore="non trovato"), 404
        r = Response(dati, mimetype=meta["mime"])
        r.headers["Content-Disposition"] = "inline; filename*=UTF-8''" + urllib.parse.quote(meta["nome"])
        if meta["mime"] != "application/pdf":      # il visualizzatore PDF del browser non funziona in una sandbox: per i PDF basta nosniff
            r.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
        return r

    @app.post("/api/voce")
    def voce_api():
        """Messaggio vocale -> testo (ffmpeg + Gemini). Header personalizzato obbligatorio: un altro sito non puo' chiamarlo."""
        if not autenticato():
            return jsonify(errore="non autenticato"), 401
        if request.headers.get("X-Requested-With") != "gemini-agent":
            return jsonify(errore="richiesta non valida"), 400
        if audit.bloccato():
            return jsonify(errore="Agenti bloccati"), 423
        dati = request.get_data(cache=False)
        try:
            testo = voce.trascrivi(client, cfg, limiter, dati)
        except voce.ErroreVoce as e:
            return jsonify(errore=str(e)), 422
        audit.registra("utente", "voce", {"byte": len(dati)}, "trascritto")
        return jsonify(testo=testo)

    @app.get("/api/registro")
    @protetto
    def registro():
        return jsonify(righe=audit.ultime(int(request.args.get("n", 200))), bloccato=audit.bloccato())

    @app.post("/api/blocco")
    @protetto
    def blocco():
        attivo = bool((request.get_json(silent=True) or {}).get("attivo"))
        audit.imposta_blocco(attivo)
        audit.registra("utente", "blocco", {"attivo": attivo}, "bloccato" if attivo else "sbloccato")
        if attivo:
            with L:
                for r in list(RUNS.values()):
                    r.stop.set()
        return jsonify(bloccato=attivo)

    @app.get("/api/screen/<nome>")
    @protetto
    def screen(nome):
        if not re.fullmatch(r"scr-\d{8}-\d{6}\.jpg", nome) or not (tools.SCREEN_DIR / nome).is_file():
            return jsonify(errore="non trovato"), 404
        return Response((tools.SCREEN_DIR / nome).read_bytes(), mimetype="image/jpeg")

    @app.get("/api/consumi")
    @protetto
    def consumi():
        r = USO.riepilogo(cfg, [f"{p['nome']}:{p['modello']}" for p in llm.PROVIDER])
        r["provider"] = [{"nome": p["nome"], "modello": p["modello"], "locale": bool(p.get("locale")), "attivo": any(x["nome"] == p["nome"] for x in llm.PROVIDER)}
                         for p in cfg.get("provider", [])]
        r["richieste_sessione"] = limiter.total
        return jsonify(r)

    @app.post("/api/elimina")
    @protetto
    def elimina():
        """Elimina conversazioni salvate (file in sessions/) e le schede in memoria. Mai quelle ancora attive."""
        d = request.get_json(silent=True) or {}
        attivi = {r.sessione.sid for r in RUNS.values() if r.attivo() and r.sessione}
        if d.get("tutte_vecchie"):
            sids = {f.stem.split("-sotto")[0] for f in (BASE / "sessions").glob("*.json")}
        else:
            sid = str(d.get("sid", ""))
            if not re.fullmatch(r"\d{8}-\d{6}", sid):
                return jsonify(errore="sessione non valida"), 400
            sids = {sid}
        sids -= attivi
        n = 0
        for sid in sids:
            for f in (BASE / "sessions").glob(f"{sid}*.json"):
                if re.fullmatch(rf"{sid}(-sotto\d+-\d{{6}})?", f.stem):
                    f.unlink()
                    n += 1
        with L:
            for rid in [r.id for r in RUNS.values() if r.sessione and r.sessione.sid in sids and not r.attivo()]:
                del RUNS[rid]
        return jsonify(eliminati=n, saltati=len(attivi & ({d.get("sid")} if d.get("sid") else attivi)))

    @app.post("/api/nuovo")
    @protetto
    def nuovo():
        if audit.bloccato():
            return jsonify(errore="Agenti bloccati: sbloccali dal menu."), 423
        d = request.get_json(silent=True) or {}
        obiettivo = str(d.get("obiettivo", "")).strip()
        ids = d.get("allegati") or []
        if not obiettivo and ids:
            obiettivo = "Analizza gli allegati e dimmi cosa contengono."
        ruolo = d.get("ruolo", "generico")
        if not obiettivo or ruolo not in cfg["ruoli"]:
            return jsonify(errore="obiettivo o ruolo non validi"), 400
        with L:
            if sum(r.attivo() for r in RUNS.values()) >= cfg.get("max_runs_parallel", 3):
                return jsonify(errore="Troppi agenti attivi: aspetta che finiscano (protegge la quota)."), 429
            sid = d.get("sid")      # se presente: riprende una sessione salvata
            if sid is not None and not re.fullmatch(r"\d{8}-\d{6}", str(sid)):
                return jsonify(errore="sessione non valida"), 400
            if sid and any(r.sessione and r.sessione.sid == sid for r in RUNS.values()):
                return jsonify(errore="Questa conversazione e' gia' aperta: usa quella."), 409
            modo = d.get("modo") or cfg.get("modo_conferme", "chiedi")
            if modo not in tools.MODI:
                return jsonify(errore="modalita' non valida"), 400
            wsel = cartella_ws(d.get("cartella", ""))
            if wsel is None:
                return jsonify(errore="cartella di lavoro non consentita"), 400
            try:
                allegati_ric = prendi_allegati(ids, wsel)
            except allegati.ErroreAllegato as e:
                return jsonify(errore=str(e)), 422
            run = Run(obiettivo, ruolo)
            run.cartella = d.get("cartella", "")
            try:
                run.sessione = Sessione(BASE / "sessions", [chiave, tools.brave_key()], sid)
            except ValueError as e:
                return jsonify(errore=str(e)), 422
            run.ctx = tools.Context(cfg, wsel, lambda t, r=run: conferma_web(r, t), client, limiter,
                                    nome=run.nome, log=run.add, stop=run.stop, redact=[chiave, tools.brave_key()], modo=modo)
            RUNS[run.id] = run
            avvia(run, obiettivo, allegati_ric)
        return jsonify(id=run.id)

    @app.post("/api/continua")
    @protetto
    def continua():
        if audit.bloccato():
            return jsonify(errore="Agenti bloccati: sbloccali dal menu."), 423
        d = request.get_json(silent=True) or {}
        run, obiettivo = RUNS.get(d.get("id")), str(d.get("obiettivo", "")).strip()
        ids = d.get("allegati") or []
        if not obiettivo and ids:
            obiettivo = "Analizza gli allegati e dimmi cosa contengono."
        if not run or not obiettivo:
            return jsonify(errore="dati non validi"), 400
        try:
            allegati_ric = prendi_allegati(ids, run.ctx.workspace)
        except allegati.ErroreAllegato as e:
            return jsonify(errore=str(e)), 422
        with L:
            if run.attivo():
                return jsonify(errore="L'agente sta ancora lavorando."), 409
            if d.get("modo") in tools.MODI:
                run.ctx.modo = d["modo"]
            avvia(run, obiettivo, allegati_ric)
        return jsonify(ok=True)

    @app.post("/api/conferma")
    @protetto
    def conferma():
        d = request.get_json(silent=True) or {}
        run = RUNS.get(d.get("run"))
        with L:
            for c in (run.conferme if run else []):
                if c["id"] == d.get("id"):
                    c["ok"] = bool(d.get("ok"))
                    c["ev"].set()
                    return jsonify(ok=True)
        return jsonify(errore="conferma non trovata (scaduta?)"), 404

    @app.post("/api/stop")
    @protetto
    def ferma():
        run = RUNS.get((request.get_json(silent=True) or {}).get("run"))
        if run:
            run.stop.set()
        return jsonify(ok=True)

    return app


def ip_lan():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))   # nessun pacchetto inviato: serve solo a scoprire l'IP locale
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def trova_browser():
    for n in ("brave-browser", "google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        if shutil.which(n):
            return shutil.which(n)
    return None


def avvisa(msg):
    """Errore visibile anche quando l'app parte dal menu (senza terminale)."""
    print(msg, file=sys.stderr)
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "Gemini Agent", str(msg)[:400]])


def apri_finestra(cfg=None):
    """Apre la finestra dell'app (senza barra del browser) collegata al servizio gia' in esecuzione, gia' autenticata."""
    import urllib.request
    cfg = cfg or json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    port = cfg["web"]["port"]
    try:
        key = hashlib.sha256((audit.DATI / "cookie.key").read_bytes()).hexdigest()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/interno/nonce", method="POST", headers={"X-Interno": key})
        nonce = json.loads(urllib.request.urlopen(req, timeout=5).read())["nonce"]
    except Exception as e:
        return avvisa(f"Gemini Agent non risponde ({type(e).__name__}). Avvialo con: systemctl --user start gemini-agent")
    browser = trova_browser()
    if not browser:
        return avvisa("Nessun browser basato su Chromium trovato (Brave, Chrome, Chromium).")
    profilo = Path.home() / ".local/share/gemini-agent/profilo"
    subprocess.Popen([browser, f"--app=http://127.0.0.1:{port}/entra?c={nonce}", f"--user-data-dir={profilo}", "--no-first-run",
                      "--no-default-browser-check", "--class=gemini-agent", "--window-size=1100,820"], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def installa_launcher():
    """Crea la voce 'Gemini Agent' nel menu delle applicazioni (con icona)."""
    py = BASE / ".venv" / "bin" / "python"
    d = Path.home() / ".local/share/applications"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "gemini-agent.desktop"
    f.write_text(f"[Desktop Entry]\nType=Application\nName=Gemini Agent\nComment=Agente locale con Gemini\n"
                 f"Exec={py} {BASE / 'web.py'} --apri\nPath={BASE}\nIcon={BASE / 'icona.svg'}\n"
                 f"Terminal=false\nCategories=Utility;\nStartupWMClass=gemini-agent\n", encoding="utf-8")
    print(f"Creato {f}: cerca 'Gemini Agent' nel menu delle applicazioni.")


def installa_servizio():
    """Servizio utente di systemd: l'app parte da sola all'accesso (senza finestra) e si riavvia se cade."""
    py = BASE / ".venv" / "bin" / "python"
    d = Path.home() / ".config/systemd/user"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "gemini-agent.service"
    f.write_text(f"[Unit]\nDescription=Gemini Agent (app locale)\nAfter=network-online.target\n\n[Service]\n"
                 f"WorkingDirectory={BASE}\nExecStart={py} {BASE / 'web.py'}\nRestart=on-failure\nRestartSec=5\n"
                 f"Environment=PATH={Path.home() / '.local/bin'}:/usr/local/bin:/usr/bin:/bin\n"
                 f"Environment=DISPLAY=:0\nEnvironment=XAUTHORITY={Path.home() / '.Xauthority'}\n\n"
                 f"[Install]\nWantedBy=default.target\n", encoding="utf-8")
    for cmd in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "gemini-agent.service"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        print(" ".join(cmd), "->", "ok" if r.returncode == 0 else r.stderr.strip())
    r = subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")], capture_output=True, text=True)
    print("avvio anche senza login (linger):", "ok" if r.returncode == 0 else "non attivato (" + r.stderr.strip()[:80] + ")")
    print(f"Creato {f}. Stato: systemctl --user status gemini-agent")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace")
    ap.add_argument("--host", help="default: config.json (0.0.0.0 = visibile sulla LAN; 127.0.0.1 = solo questo PC)")
    ap.add_argument("--finestra", action="store_true", help="apre l'app in una finestra propria (come un'app desktop)")
    ap.add_argument("--installa", action="store_true", help="crea la voce nel menu delle applicazioni")
    ap.add_argument("--apri", action="store_true", help="apre la finestra dell'app collegata al servizio gia' acceso")
    ap.add_argument("--servizio", action="store_true", help="installa l'avvio automatico (servizio utente systemd)")
    a = ap.parse_args()
    if a.installa:
        return installa_launcher()
    if a.servizio:
        return installa_servizio()
    if a.apri:
        return apri_finestra()
    try:
        cfg, ws, chiave, client, limiter = prepara(a.workspace)
        try:
            token = get_secret(cfg["web"]["token_secret"], cfg)   # anche il token viene da Infisical
        except SecretError as e:
            sys.exit(f"{e}\nCrea in Infisical il segreto '{cfg['web']['token_secret']}' (stringa casuale lunga).")
        if len(token) < 16:
            sys.exit("Il token web deve avere almeno 16 caratteri.")
    except SystemExit as e:
        if a.finestra:
            avvisa(e.code)
        raise
    app = crea_app(cfg, ws, chiave, client, limiter, token)
    host, port = a.host or cfg["web"]["host"], cfg["web"]["port"]
    print(f"Workspace: {ws}\nAscolto su http://{host}:{port}"
          + ("  (solo questo PC: per il telefono vedi 'Accesso da fuori' nel README)" if host in ("127.0.0.1", "localhost") else
             "\n!! ATTENZIONE: visibile su tutta la rete locale, in HTTP in chiaro"))
    if not a.finestra:
        return app.run(host=host, port=port, threaded=True, use_reloader=False)
    from werkzeug.serving import make_server
    try:
        srv = make_server(host, port, app, threaded=True)
    except OSError:
        return avvisa(f"La porta {port} e' occupata: Gemini Agent e' gia' aperto?")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    browser = trova_browser()
    if not browser:
        return avvisa("Nessun browser basato su Chromium trovato (Brave, Chrome, Chromium).")
    profilo = Path.home() / ".local/share/gemini-agent/profilo"   # profilo separato: non tocca il tuo browser
    url = f"http://127.0.0.1:{port}/entra?c={nuovo_nonce()}"
    p = subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profilo}", "--no-first-run",
                          "--no-default-browser-check", "--class=gemini-agent", "--window-size=460,860"])
    p.wait()          # chiudendo la finestra l'app si ferma
    srv.shutdown()


if __name__ == "__main__":
    main()
