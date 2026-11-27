#!/usr/bin/python3
"""Legge e usa gli elementi dell'interfaccia grafica PER NOME (accessibilita' AT-SPI), senza indovinare pixel.

Gira col Python di SISTEMA (serve il modulo 'gi', che il venv del progetto non ha). Codice nostro, nessuna libreria scaricata.
Uso:  atspi_helper.py '<json>'   con  {"cmd": "app"}
                                      {"cmd": "elenca", "app": "brave", "cerca": "nuova", "max": 60}
                                      {"cmd": "agisci", "app": "brave", "nome": "Nuova scheda", "ruolo": "", "indice": 0,
                                       "azione": "click" | "scrivi" | "fuoco", "testo": "..."}
Risposta: un JSON su una riga. Non legge MAI i valori dei campi password e non restituisce il contenuto dei campi di testo.
"""
import json
import sys

import gi

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

INTERATTIVI = {"push button", "toggle button", "check box", "radio button", "menu item", "check menu item", "radio menu item", "page tab",
               "link", "entry", "text", "combo box", "list item", "tree item", "spin button", "slider", "menu", "tab", "switch"}
SEGRETI = {"password text"}
MAX_NODI = 6000


def app_aperte():
    d = Atspi.get_desktop(0)
    nomi = []
    for i in range(d.get_child_count()):
        try:
            a = d.get_child_at_index(i)
            if a and a.get_name() and a.get_child_count() > 0:
                nomi.append(a.get_name())
        except Exception:
            pass
    return nomi


def trova_app(nome):
    d = Atspi.get_desktop(0)
    trovate = []
    for i in range(d.get_child_count()):
        try:
            a = d.get_child_at_index(i)
            if a and nome.lower() in (a.get_name() or "").lower() and a.get_child_count() > 0:
                trovate.append(a)
        except Exception:
            pass
    return trovate


def stati(o):
    try:
        s = o.get_state_set()
        return s.contains(Atspi.StateType.SHOWING), s.contains(Atspi.StateType.ENABLED) or s.contains(Atspi.StateType.SENSITIVE), \
            s.contains(Atspi.StateType.EDITABLE)
    except Exception:
        return False, False, False


def raccogli(app, cerca="", massimo=60):
    """Elementi interattivi VISIBILI, in ordine di apparizione."""
    out, visti = [], [0]
    cerca = (cerca or "").lower()

    def giu(o, depth):
        if len(out) >= massimo or visti[0] > MAX_NODI or depth > 40:
            return
        visti[0] += 1
        try:
            ruolo = o.get_role_name()
            nome = (o.get_name() or "").strip()
        except Exception:
            return
        mostrato, abilitato, editabile = stati(o)
        if mostrato and ruolo in INTERATTIVI and ruolo not in SEGRETI:
            nome_vis = nome[:80]
            if (nome_vis or ruolo in ("entry", "text")) and (not cerca or cerca in nome_vis.lower() or cerca in ruolo):
                try:
                    e = o.get_extents(Atspi.CoordType.SCREEN)
                    cx, cy = e.x + e.width // 2, e.y + e.height // 2
                    ok_pos = e.width > 0 and e.height > 0 and e.x >= 0 and e.y >= 0
                except Exception:
                    cx = cy = 0
                    ok_pos = False
                if any(x["ruolo"] == ruolo and x["nome"] == nome_vis and x["x"] == (cx if ok_pos else None) and x["y"] == (cy if ok_pos else None) for x in out):
                    return                                        # stesso elemento riportato due volte dall'albero
                out.append({"ruolo": ruolo, "nome": nome_vis, "abilitato": bool(abilitato), "modificabile": bool(editabile),
                            "x": cx if ok_pos else None, "y": cy if ok_pos else None, "_o": o})
        try:
            for i in range(o.get_child_count()):
                giu(o.get_child_at_index(i), depth + 1)
        except Exception:
            pass

    giu(app, 0)
    return out


def pubblico(e, i):
    return {k: v for k, v in dict(e, i=i).items() if k != "_o"}


def main():
    req = json.loads(sys.argv[1])
    cmd = req.get("cmd")
    if cmd == "app":
        return {"ok": True, "app": app_aperte()}
    app_trovate = trova_app(req.get("app", ""))
    if not app_trovate:
        return {"ok": False, "errore": f"nessuna app con accessibilita' che contenga «{req.get('app', '')}». App visibili: {app_aperte()[:15]}"}
    elementi = []
    for a in app_trovate:
        elementi += raccogli(a, req.get("cerca", "") if cmd == "elenca" else "", int(req.get("max", 60)))
    if cmd == "elenca":
        return {"ok": True, "elementi": [pubblico(e, i) for i, e in enumerate(elementi[:int(req.get("max", 60))])]}
    if cmd != "agisci":
        return {"ok": False, "errore": "comando sconosciuto"}
    nome, ruolo = (req.get("nome") or "").lower(), (req.get("ruolo") or "").lower()
    esatti = [e for e in elementi if e["nome"].lower() == nome and (not ruolo or e["ruolo"] == ruolo)]
    cand = esatti or [e for e in elementi if nome and nome in e["nome"].lower() and (not ruolo or e["ruolo"] == ruolo)]
    if not cand:
        return {"ok": False, "errore": f"nessun elemento «{req.get('nome')}» visibile. Usa ui_elenca per vedere i nomi esatti."}
    if len(cand) > 1 and req.get("indice") is None:
        return {"ok": False, "errore": "piu' elementi corrispondono: indica 'indice'.", "candidati": [pubblico(e, i) for i, e in enumerate(cand[:10])]}
    e = cand[int(req.get("indice") or 0)] if int(req.get("indice") or 0) < len(cand) else None
    if e is None:
        return {"ok": False, "errore": "indice fuori intervallo"}
    if not e["abilitato"]:
        return {"ok": False, "errore": f"l'elemento «{e['nome']}» e' disabilitato"}
    o, azione = e["_o"], req.get("azione", "click")
    info = pubblico(e, 0)
    if azione == "fuoco":
        try:
            o.get_component_iface().grab_focus()
            return {"ok": True, "metodo": "fuoco", "elemento": info}
        except Exception as ex:
            return {"ok": False, "errore": f"fuoco impossibile: {ex}", "elemento": info}
    if azione == "scrivi":
        testo = req.get("testo", "")
        if e["modificabile"]:
            try:
                if Atspi.EditableText.set_text_contents(o, testo):
                    return {"ok": True, "metodo": "testo", "elemento": info}
            except Exception:
                pass
        try:
            o.get_component_iface().grab_focus()
            a_fuoco = o.get_state_set().contains(Atspi.StateType.FOCUSED)
        except Exception:
            a_fuoco = False
        if not a_fuoco:                                                   # mai digitare alla cieca: il testo finirebbe in un'altra finestra
            return {"ok": False, "errore": f"non riesco a mettere il cursore nel campo «{e['nome']}»: non digito. Prova azione 'click' sul campo e poi 'scrivi'.",
                    "elemento": info}
        return {"ok": True, "metodo": "tastiera", "elemento": info}      # il chiamante digita con xdotool nell'elemento che ha il fuoco
    try:                                                                  # click: prima l'azione nativa, poi le coordinate
        ai = o.get_action_iface()
        if ai and ai.get_n_actions() > 0 and ai.do_action(0):
            return {"ok": True, "metodo": "azione", "elemento": info}
    except Exception:
        pass
    if e["x"] is not None:
        return {"ok": True, "metodo": "coordinate", "elemento": info}
    return {"ok": False, "errore": "l'elemento non ha ne' un'azione ne' una posizione", "elemento": info}


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except Exception as ex:                      # qualunque problema: risposta d'errore leggibile, mai un traceback
        print(json.dumps({"ok": False, "errore": f"{type(ex).__name__}: {str(ex)[:200]}"}, ensure_ascii=False))
