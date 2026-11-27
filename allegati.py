"""Allegati alla chat: immagini, PDF e file di testo.

Sicurezza: il tipo si riconosce dai BYTE del file (non dal nome), le immagini vengono ricodificate in JPEG (si perdono i
dati EXIF, come la posizione GPS), i nomi sono ripuliti, ci sono limiti di dimensione e nessun file viene mai eseguito.
Gli allegati stanno in ~/.local/share/gemini-agent/allegati (fuori dalla cartella di lavoro) e si cancellano dopo 14 giorni.
"""
import io
import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

DIR = Path.home() / ".local/share/gemini-agent/allegati"
MAX_FILE = 10 * 1024 * 1024
MAX_N = 5
MAX_TOTALE = 20 * 1024 * 1024
MAX_TESTO = 200_000
ESTENSIONI_TESTO = {"txt", "md", "csv", "json", "py", "js", "ts", "html", "css", "yml", "yaml", "xml", "log", "ini", "toml", "sh", "sql", "tsv"}


class ErroreAllegato(ValueError):
    pass


def nome_sicuro(nome: str) -> str:
    base = re.sub(r"[^\w.\- ]", "_", Path(nome or "file").name).strip(" .") or "file"
    return base[:80]


def _tipo_da_byte(d: bytes) -> str | None:
    if d.startswith(b"\x89PNG\r\n\x1a\n") or d.startswith(b"\xff\xd8\xff") or d[:4] == b"GIF8" or (d[:4] == b"RIFF" and d[8:12] == b"WEBP"):
        return "immagine"
    if d.startswith(b"%PDF-"):
        return "pdf"
    return None


def prepara(nome: str, dati: bytes) -> tuple[dict, bytes]:
    """Valida e normalizza un file caricato. Restituisce (meta, byte_da_salvare). Alza ErroreAllegato se non va bene."""
    if not dati:
        raise ErroreAllegato("File vuoto.")
    if len(dati) > MAX_FILE:
        raise ErroreAllegato(f"File troppo grande (massimo {MAX_FILE // 1024 // 1024} MB).")
    nome = nome_sicuro(nome)
    tipo = _tipo_da_byte(dati)
    if tipo == "immagine":
        from PIL import Image, UnidentifiedImageError
        try:
            im = Image.open(io.BytesIO(dati))
            im.load()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
            raise ErroreAllegato("Immagine non leggibile o troppo grande.")
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        im = im.convert("RGB")                          # ricodifica: toglie EXIF/GPS e metadati nascosti
        im.thumbnail((2048, 2048))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
        base = re.sub(r"\.\w+$", "", nome) or "immagine"
        return {"nome": base + ".jpg", "tipo": "immagine", "mime": "image/jpeg", "size": out.tell(), "larghezza": im.width, "altezza": im.height}, out.getvalue()
    if tipo == "pdf":
        n = pagine_pdf(dati)
        if n == 0:
            raise ErroreAllegato("PDF non leggibile.")
        return {"nome": nome if nome.lower().endswith(".pdf") else nome + ".pdf", "tipo": "pdf", "mime": "application/pdf", "size": len(dati), "pagine": n}, dati
    ext = nome.rsplit(".", 1)[-1].lower() if "." in nome else ""
    if ext in ESTENSIONI_TESTO:
        if len(dati) > MAX_TESTO:
            raise ErroreAllegato(f"File di testo troppo grande (massimo {MAX_TESTO // 1000} KB).")
        try:
            dati.decode("utf-8")
        except UnicodeDecodeError:
            raise ErroreAllegato("Il file non e' testo UTF-8 valido.")
        return {"nome": nome, "tipo": "testo", "mime": "text/plain", "size": len(dati)}, dati
    raise ErroreAllegato("Tipo di file non supportato: ammessi immagini (PNG, JPEG, WebP, GIF), PDF e file di testo.")


def salva(meta: dict, dati: bytes) -> dict:
    DIR.mkdir(parents=True, exist_ok=True)
    aid = uuid.uuid4().hex[:16]
    (DIR / f"{aid}.bin").write_bytes(dati)
    meta = dict(meta, id=aid, quando=time.time())
    (DIR / f"{aid}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


def carica(aid: str) -> tuple[dict, bytes]:
    if not re.fullmatch(r"[0-9a-f]{16}", aid or ""):
        raise ErroreAllegato("Allegato non valido.")
    try:
        return json.loads((DIR / f"{aid}.json").read_text(encoding="utf-8")), (DIR / f"{aid}.bin").read_bytes()
    except OSError:
        raise ErroreAllegato("Allegato non trovato (scaduto?).")


def pulisci(giorni: int = 14):
    """Cancella gli allegati piu' vecchi di N giorni."""
    limite = time.time() - giorni * 86400
    for f in DIR.glob("*.json") if DIR.exists() else []:
        try:
            if json.loads(f.read_text(encoding="utf-8")).get("quando", 0) < limite:
                f.unlink()
                f.with_suffix(".bin").unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


# ---------------------------------------------------------------- PDF
def pagine_pdf(dati: bytes) -> int:
    try:
        from pypdf import PdfReader
        return len(PdfReader(io.BytesIO(dati)).pages)
    except Exception:
        return 0


def testo_pdf(dati: bytes, max_car: int = 20000) -> str:
    """Testo del PDF (pdftotext, poi pypdf). Vuoto per i PDF scansionati."""
    try:
        r = subprocess.run(["pdftotext", "-layout", "-", "-"], input=dati, capture_output=True, timeout=30)
        t = r.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        t = ""
    if not t.strip():
        try:
            from pypdf import PdfReader
            t = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(dati)).pages[:40])
        except Exception:
            t = ""
    return t.strip()[:max_car]


def pagine_jpeg(dati: bytes, n: int = 3) -> list[bytes]:
    """Prime pagine del PDF come immagini (per i modelli con visione, utile per i PDF scansionati)."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="pdfimg_") as d:
        src = Path(d) / "in.pdf"
        src.write_bytes(dati)
        try:
            subprocess.run(["pdftoppm", "-jpeg", "-r", "80", "-f", "1", "-l", str(n), str(src), str(Path(d) / "p")], capture_output=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return []
        return [f.read_bytes() for f in sorted(Path(d).glob("p-*.jpg"))]
