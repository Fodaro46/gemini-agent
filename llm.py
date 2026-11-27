"""Fallback su altri provider con API compatibile OpenAI (Groq, OpenRouter, ...), con le TUE chiavi.

Quando la quota giornaliera di Gemini finisce, l'agente passa al primo provider configurato che ha una chiave
in Infisical. Non si ruotano account o chiavi multiple per aggirare i limiti (OpenRouter lo vieta nei termini):
un provider = una tua chiave. Per aggiungerne uno: voce in config.json "provider" + segreto in Infisical.

La cronologia resta in formato Gemini: qui la convertiamo al volo in messaggi OpenAI e riconvertiamo la risposta.
"""
import base64
import json
import time
import urllib.error
import urllib.request

from google.genai import types

from secrets_infisical import get_secret

PROVIDER: list[dict] = []   # provider con chiave presente: {"nome","base_url","modello","key"}


class ProviderEsaurito(RuntimeError):
    """429 / quota finita su questo provider: si passa al successivo."""


def carica_provider(cfg: dict) -> list[str]:
    """Legge da Infisical le chiavi dei provider configurati; quelli senza chiave vengono saltati."""
    PROVIDER.clear()
    for p in cfg.get("provider", []):
        try:
            if p.get("locale"):      # modello sul tuo PC (Ollama): nessuna chiave; si usa solo se il server risponde
                urllib.request.urlopen(p["base_url"].rstrip("/").removesuffix("/v1") + "/api/tags", timeout=2).read()
                PROVIDER.append({**p, "key": "locale"})
            else:
                PROVIDER.append({**p, "key": get_secret(p["segreto"], cfg)})
        except Exception:
            pass
    return [f"{p['nome']}:{p['modello']}" for p in PROVIDER]


def _parti_media(parti, visione: bool) -> tuple[list[dict], list[str]]:
    """Allegati (immagini, PDF) -> contenuti OpenAI. Le immagini solo se il modello ha la visione; i PDF diventano testo."""
    import allegati
    contenuti, note = [], []
    for p in parti:
        if not p.inline_data:
            continue
        mime, dati = p.inline_data.mime_type or "", p.inline_data.data or b""
        if mime.startswith("image/"):
            if visione:
                contenuti.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(dati).decode()}"}})
            else:
                note.append("[immagine allegata: questo modello non puo' vederla]")
        elif mime == "application/pdf":
            testo = allegati.testo_pdf(dati)
            if testo:
                note.append("[Testo estratto dal PDF allegato]\n" + testo)
            elif visione:
                for img in allegati.pagine_jpeg(dati):
                    contenuti.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}})
                note.append("[PDF scansionato: ti mostro le prime pagine come immagini]")
            else:
                note.append("[PDF allegato senza testo estraibile]")
    return contenuti, note


def a_messaggi(contents, sistema: str, visione: bool = False) -> list[dict]:
    """Cronologia Gemini -> messaggi OpenAI (con id coerenti per le chiamate di strumento)."""
    msgs, attesa, n = [{"role": "system", "content": sistema}], [], 0
    for c in contents:
        parti = c.parts or []
        if c.role == "user":
            risposte = [p.function_response for p in parti if p.function_response]
            for i, fr in enumerate(risposte):
                msgs.append({"role": "tool", "tool_call_id": attesa[i] if i < len(attesa) else f"call_x{i}",
                             "content": json.dumps(fr.response, ensure_ascii=False)[:8000]})
            if risposte:
                attesa = []
            testo = "\n".join(p.text for p in parti if p.text)
            media, note = _parti_media(parti, visione)
            if note:
                testo = (testo + "\n" if testo else "") + "\n".join(note)
            if media:
                msgs.append({"role": "user", "content": ([{"type": "text", "text": testo}] if testo else []) + media})
            elif testo:
                msgs.append({"role": "user", "content": testo})
        else:
            testo = "".join(p.text or "" for p in parti if not p.thought)
            chiamate = [p.function_call for p in parti if p.function_call]
            if not testo and not chiamate:
                continue                  # un messaggio dell'assistente senza testo ne' chiamate verrebbe rifiutato dal provider
            m = {"role": "assistant", "content": testo or None}
            if chiamate:
                attesa, tc = [], []
                for fc in chiamate:
                    n += 1
                    attesa.append(f"call_{n}")
                    tc.append({"id": f"call_{n}", "type": "function",
                               "function": {"name": fc.name, "arguments": json.dumps(dict(fc.args or {}), ensure_ascii=False)}})
                m["tool_calls"] = tc
            msgs.append(m)
    return msgs


def a_strumenti(dichiarazioni: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": d["name"], "description": d["description"],
                                              "parameters": d["parameters"]}} for d in dichiarazioni]


def genera(p: dict, contents, sistema: str, dichiarazioni: list[dict]):
    """Chiama un provider. Restituisce (Content in formato Gemini, usage dict). Alza ProviderEsaurito su 429."""
    corpo = {"model": p["modello"], "messages": a_messaggi(contents, sistema, bool(p.get("visione"))),
             "tools": a_strumenti(dichiarazioni), "tool_choice": "auto", "temperature": p.get("temperature", 0)}
    if p.get("seed") is not None:
        corpo["seed"] = p["seed"]            # stesso input -> stessa risposta (dove il provider lo supporta)
    req = urllib.request.Request(p["base_url"].rstrip("/") + "/chat/completions", data=json.dumps(corpo).encode(),
                                 headers={"Authorization": "Bearer " + p["key"], "Content-Type": "application/json"})
    for tentativo in range(3):
        try:
            with urllib.request.urlopen(req, timeout=600 if p.get("locale") else 120) as r:
                dati = json.loads(r.read().decode())
            if "choices" not in dati:                     # alcuni provider rispondono 200 con {"error": ...}
                raise ProviderEsaurito(f"{p['nome']}: {str(dati.get('error', dati))[:80]}")
            break
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    attesa = float(e.headers.get("retry-after", "inf"))
                except ValueError:
                    attesa = float("inf")
                if attesa <= 60 and tentativo < 2:      # limite al minuto: aspetto e riprovo
                    time.sleep(attesa + 1)
                    continue
                raise ProviderEsaurito(f"{p['nome']}: limite raggiunto")
            if e.code >= 500 and tentativo == 0:
                time.sleep(3)
                continue
            raise ProviderEsaurito(f"{p['nome']}: errore HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:      # server spento, timeout, JSON rotto
            raise ProviderEsaurito(f"{p['nome']}: {type(e).__name__}")
    try:
        msg = dati["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise ProviderEsaurito(f"{p['nome']}: risposta non valida")
    parti = []
    if msg.get("content"):
        parti.append(types.Part(text=msg["content"]))
    for tc in msg.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except ValueError:
            args = {}
        parti.append(types.Part(function_call=types.FunctionCall(name=tc["function"]["name"], args=args)))
    return types.Content(role="model", parts=parti), dati.get("usage", {})
