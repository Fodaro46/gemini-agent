"""Verifica DETERMINISTICA di quello che un modello dichiara di aver fatto.

Idea: il modello (soprattutto uno piccolo come Qwen 4B) puo' scrivere "ho creato il file X" senza averlo fatto.
Qui non si chiede a un altro modello se e' vero: si confronta la risposta con il REGISTRO degli strumenti realmente
eseguiti (con i loro esiti) e con lo stato reale dei file. Regole fisse, stesso input = stesso verdetto.

Livelli:
  ERRORE  -> la risposta afferma qualcosa di smentito dai fatti (azione mai eseguita, file inesistente, esito ignorato).
             L'agente riceve la contestazione e deve eseguire davvero l'azione o correggersi.
  AVVISO  -> valori o testo che non compaiono in nessun risultato degli strumenti ("non fondato"): da controllare.
"""
import re
from pathlib import Path

# esiti degli strumenti che NON sono un successo
INSUCCESSO = re.compile(r"^(Errore|RIFIUTATO|Comando rifiutato|Comando bloccato|Agenti BLOCCATI|Percorso|Azione non valida|"
                        r"Destinazione non valida|Cattura fallita|Ricerca web fallita|Non e'|Questa cartella|Serve |xdotool non|"
                        r"Coordinate non valide|Combinazione|Timeout|exit=[1-9])", re.I)

INPUT = ("controlla_input", "sequenza_input")      # azioni di mouse/tastiera

# verbo (radice) -> categoria di azione
VERBI = {
    "creat": "scrittura", "scritt": "scrittura", "salvat": "scrittura", "generat": "scrittura", "aggiunt": "scrittura",
    "modificat": "modifica", "aggiornat": "modifica", "sostituit": "modifica",
    "eliminat": "cancellazione", "cancellat": "cancellazione", "rimoss": "cancellazione",
    "eseguit": "esecuzione", "lanciat": "esecuzione", "avviat": "esecuzione", "installat": "esecuzione",
    "cliccat": "schermo", "premut": "schermo", "digitat": "schermo", "selezionat": "schermo", "attivat": "schermo", "abilitat": "schermo",
    "spostat": "esecuzione", "rinominat": "esecuzione", "copiat": "esecuzione", "scaricat": "esecuzione",
}
RADICI = "|".join(VERBI)
# Una vera affermazione e' "ho/abbiamo + participio" oppure "e' stato/stata/stati/state + participio".
# NON basta "e'" + participio ("c'e' scritto due", "e' corretta" sono descrizioni, non azioni).
CLAIM = re.compile(rf"\b(?:(?:ho|hai|ha|abbiamo)\s+(?:\w+\s+){{0,3}}?|(?:è|e'|sono)\s+(?:stato|stata|stati|state)\s+(?:\w+\s+){{0,2}}?)({RADICI})(?:o|a|i|e)\b", re.I)
FILE = re.compile(r"`?([\w./~-]*[\w-]\.[A-Za-z][A-Za-z0-9]{0,4})`?")
NUMERO = re.compile(r"(?<![\w.])(\d[\d.,]*\d|\d)(?![\w])")
CMD_RM = re.compile(r"\b(rm|rmdir|unlink|trash)\b|-delete\b")


class Passo:
    """Un'azione realmente eseguita: strumento, argomenti, esito."""

    def __init__(self, strumento, args, risultato):
        self.strumento, self.args, self.risultato = strumento, args or {}, str(risultato)
        self.ok = not INSUCCESSO.search(self.risultato.strip())

    def __repr__(self):
        return f"Passo({self.strumento}, ok={self.ok})"


def libro_da_cronologia(contents) -> list[Passo]:
    """Ricostruisce i passi eseguiti nel turno corrente (dall'ultimo messaggio di testo dell'utente in poi)."""
    inizio = 0
    for i, c in enumerate(contents):
        if c.role == "user" and any(p.text and not p.text.startswith("VERIFICA AUTOMATICA") for p in (c.parts or [])):
            inizio = i
    passi, in_attesa = [], []
    for c in contents[inizio:]:
        for p in c.parts or []:
            if p.function_call:
                in_attesa.append((p.function_call.name, dict(p.function_call.args or {})))
            elif p.function_response and in_attesa:
                nome, args = in_attesa.pop(0)
                passi.append(Passo(nome, args, (p.function_response.response or {}).get("result", "")))
    return passi


def _frasi(testo: str):
    senza_codice = re.sub(r"```.*?```", " ", testo, flags=re.S)
    return [f.strip() for f in re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-Ü])|\n+", senza_codice) if f.strip()]


def _soddisfatta(cat: str, passi: list[Passo]) -> bool:
    ok = [p for p in passi if p.ok]
    if cat == "scrittura":
        return any(p.strumento in ("write_file", "edit_file", "run_shell", "git", "delega_agenti") for p in ok)
    if cat == "modifica":
        return any(p.strumento in ("edit_file", "write_file", "run_shell", "git", "delega_agenti") for p in ok)
    if cat == "cancellazione":
        return any(p.strumento in ("delete_file", "git", "delega_agenti") or
                   (p.strumento == "run_shell" and CMD_RM.search(str(p.args.get("command", "")))) for p in ok)
    if cat == "schermo":
        # Azioni sullo schermo: servono (1) un'azione di mouse/tastiera riuscita e (2) uno screenshot riuscito fatto DOPO l'ultima azione.
        # Se in questo turno non c'e' stato nessun input, il verbo riguarda altro (es. un servizio avviato da shell): vale come esecuzione.
        ultimo_input = max((i for i, p in enumerate(passi) if p.strumento in INPUT), default=-1)
        if ultimo_input < 0:
            return _soddisfatta("esecuzione", passi)
        return passi[ultimo_input].ok and ("[screenshot allegato dopo l'azione" in passi[ultimo_input].risultato
                                           or any(p.ok for p in passi[ultimo_input + 1:] if p.strumento == "screenshot"))
    return any(p.strumento in ("run_shell", "apri", "controlla_input", "sequenza_input", "git", "delega_agenti") for p in ok)


def _riferimento(obiettivo: str, passi: list[Passo]) -> str:
    """Tutto cio' che e' 'fondato': richiesta dell'utente + argomenti e risultati degli strumenti."""
    pezzi = [obiettivo]
    for p in passi:
        pezzi.append(str(p.args))
        pezzi.append(p.risultato)
    return re.sub(r"\s+", " ", " ".join(pezzi))


def _esiste(ws: Path, nome: str) -> bool:
    try:
        return (ws / nome).resolve().exists()
    except OSError:
        return False


def controlla(risposta: str, passi: list[Passo], obiettivo: str, ws: Path, con_allegati: bool = False) -> dict:
    """Confronta la risposta con i fatti. Restituisce {"errori": [...], "avvisi": [...], "ok": bool}."""
    errori, avvisi = [], []
    if not risposta.strip() and passi:
        return {"errori": ["La risposta finale e' vuota: dopo aver usato gli strumenti devi riportare il risultato all'utente."],
                "avvisi": [], "ok": False}
    ref = _riferimento(obiettivo, passi)
    ultimo_esito = {}                                # strumento -> ultimo risultato (per 'esito ignorato')
    for p in passi:
        ultimo_esito[p.strumento] = p
    for frase in _frasi(risposta):
        m = CLAIM.search(frase)
        if not m or re.search(r"\b(non|né|mai|impossibile|senza|purtroppo)\b", frase, re.I):
            continue                    # niente affermazione, o e' una negazione ("non ho potuto creare...")
        cat = VERBI[m.group(1).lower()]
        if not _soddisfatta(cat, passi):
            errori.append(f"Dici «{frase[:110]}» ma non risulta nessuna azione di tipo '{cat}' eseguita con successo"
                          + (" E CONFERMATA da uno screenshot fatto dopo l'ultimo click/tasto: fallo, guarda, e riporta solo cio' che vedi." if cat == "schermo" else "."))
            continue
        # i file citati nella stessa frase devono rispecchiare lo stato reale
        for nome in FILE.findall(frase):
            if re.search(r"^\d+\.\d+$|^https?|\.\.\.|^[A-Z]\.", nome) or "/" in nome and nome.startswith("http"):
                continue
            if cat in ("scrittura", "modifica") and not _esiste(ws, nome):
                if any(nome in str(p.args) for p in passi if p.ok):
                    # nominato in un comando riuscito ma assente dal workspace (es. generato altrove): solo un avviso
                    avvisi.append(f"«{nome}» e' citato in un comando ma non si trova nel workspace.")
                else:
                    errori.append(f"Dici di aver creato/modificato «{nome}» ma il file non esiste.")
            if cat == "cancellazione" and _esiste(ws, nome):
                errori.append(f"Dici di aver eliminato «{nome}» ma il file esiste ancora.")
    # successo dichiarato dopo un fallimento senza nuovo tentativo riuscito
    if passi and not any(p.ok for p in passi) and re.search(r"\b(successo|fatto|completat|riuscit)", risposta, re.I) and CLAIM.search(risposta):
        errori.append("Tutti gli strumenti usati sono falliti o sono stati rifiutati, ma la risposta dichiara un successo.")
    # valori non fondati (avviso)
    for n in set(NUMERO.findall(re.sub(r"```.*?```", " ", risposta, flags=re.S))):
        nn = n.strip(".,")
        if len(nn.replace(".", "").replace(",", "")) >= 3 and not (nn.isdigit() and 1900 <= int(nn) <= 2100) and nn not in ref:
            avvisi.append(f"Il valore {nn} non compare nei risultati degli strumenti.")
    for blocco in re.findall(r"```[a-z]*\n?(.*?)```", risposta, flags=re.S):
        for riga in blocco.splitlines():
            r = re.sub(r"\s+", " ", riga).strip()
            if len(r) >= 8 and r not in ref:
                avvisi.append(f"Nel blocco di codice la riga «{r[:60]}» non compare nei risultati degli strumenti.")
                break
    if con_allegati:      # il contenuto di immagini/PDF e' una fonte che non vediamo: non segnaliamo "valori non fondati"
        avvisi = []
    return {"errori": errori, "avvisi": avvisi[:6], "ok": not errori}


def contestazione(esito: dict) -> str:
    """Messaggio da rimandare al modello quando la verifica trova errori."""
    elenco = "\n".join("- " + e for e in esito["errori"])
    return ("VERIFICA AUTOMATICA: la tua risposta contiene affermazioni smentite dai fatti.\n" + elenco +
            "\nEsegui DAVVERO le azioni con gli strumenti oppure correggi la risposta dicendo cosa NON hai fatto. "
            "Non dichiarare un'azione se non risulta nei risultati degli strumenti.")
