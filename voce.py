"""Voce -> testo. L'audio registrato dal telefono (webm/mp4/ogg...) viene convertito in WAV 16 kHz mono con ffmpeg
e trascritto da Gemini (un messaggio audio = 1 richiesta al modello, con la stessa gestione delle quote).

Nota privacy: l'audio va ai server del provider del modello (Google, nel piano gratuito: puo' essere usato per
migliorare i prodotti). Una trascrizione locale con Whisper evita questo: vedi README.
"""
import subprocess
import tempfile
from pathlib import Path

from google.genai import types

import usage

MAX_BYTE = 10 * 1024 * 1024     # ~10 MB di audio compresso (parecchi minuti)
MAX_SECONDI = 120
PROMPT = ("Trascrivi fedelmente questo messaggio vocale in italiano. Rispondi SOLO con il testo trascritto, "
          "senza commenti, senza virgolette. Se non senti parole, rispondi con una stringa vuota.")


class ErroreVoce(RuntimeError):
    pass


def converti_wav(dati: bytes) -> bytes:
    """Qualunque formato audio supportato da ffmpeg -> WAV 16 kHz mono (max 2 minuti)."""
    if not dati or len(dati) > MAX_BYTE:
        raise ErroreVoce("Audio vuoto o troppo grande.")
    with tempfile.TemporaryDirectory(prefix="voce_") as d:
        src, dst = Path(d) / "in.bin", Path(d) / "out.wav"
        src.write_bytes(dati)
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-t", str(MAX_SECONDI), "-ac", "1", "-ar", "16000", str(dst)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not dst.exists():
            raise ErroreVoce("Audio non leggibile: " + (r.stderr or "")[:150])
        return dst.read_bytes()


def trascrivi(client, cfg, limiter, audio: bytes, convertitore=converti_wav, errori_quota=()) -> str:
    """Trascrive l'audio. Prova il modello principale, poi quello di riserva; se la quota e' finita lo dice chiaramente."""
    wav = convertitore(audio)
    modelli = [m for m in (cfg.get("model"), cfg.get("fallback_model")) if m]
    ultimo = None
    for m in modelli:
        if usage.USO.e_esaurito(m):
            continue
        try:
            limiter.wait()
            r = client.models.generate_content(
                model=m, contents=[types.Content(role="user", parts=[types.Part.from_bytes(data=wav, mime_type="audio/wav"),
                                                                     types.Part(text=PROMPT)])])
            u = getattr(r, "usage_metadata", None)
            usage.USO.registra(m, getattr(u, "prompt_token_count", 0), getattr(u, "candidates_token_count", 0))
            testo = "".join(p.text or "" for p in (r.candidates[0].content.parts or []) if not getattr(p, "thought", False)).strip()
            return testo.strip('"“”')
        except Exception as e:            # quota giornaliera -> provo il modello successivo
            ultimo = e
            msg = str(e).lower()
            if "per day" in msg or "perday" in msg:
                usage.USO.esaurito(m)
                continue
            raise ErroreVoce("Trascrizione fallita: " + str(e)[:150])
    raise ErroreVoce("Quota finita: non riesco a trascrivere ora. Scrivi a mano (la quota si azzera alle 9:00).")
