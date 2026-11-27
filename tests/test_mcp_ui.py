"""Test degli strumenti per NOME (accessibilita') e del client MCP, con programmi finti: niente schermo vero, niente rete."""
import json
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_client  # noqa: E402
import tools  # noqa: E402
import verifica  # noqa: E402
from test_deterministici import Base  # noqa: E402

SERVER_FINTO = textwrap.dedent('''
    import json, os, sys
    for riga in sys.stdin:
        m = json.loads(riga)
        if "id" not in m:
            continue
        mid, met, p = m["id"], m["method"], m.get("params", {})
        if met == "initialize":
            r = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "finto"}}
        elif met == "tools/list":
            r = {"tools": [
                {"name": "saluta", "description": "Saluta", "inputSchema": {"type": "object", "properties": {"chi": {"type": "string"}, "n": {"type": "integer"}, "lista": {"type": "array"}}, "required": ["chi"]}},
                {"name": "pericoloso", "description": "Non consentito", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "ambiente", "description": "Mostra l'ambiente", "inputSchema": {"type": "object", "properties": {}}}]}
        elif met == "tools/call":
            if p["name"] == "ambiente":
                r = {"content": [{"type": "text", "text": ",".join(sorted(os.environ))}]}
            else:
                r = {"content": [{"type": "text", "text": "ciao " + str(p["arguments"])}]}
        else:
            r = {}
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": r}), flush=True)
''')


class TestMCP(Base):
    def server(self, strumenti=("saluta", "ambiente")):
        f = self.tmp / "finto.py"
        f.write_text(SERVER_FINTO)
        return {"mcp": {"attivo": True, "server": {"finto": {"comando": [sys.executable, str(f)], "strumenti": list(strumenti), "timeout": 10}}}}

    def setUp(self):
        super().setUp()
        self.reg = mcp_client.Registro()
        self.addCleanup(self.reg.chiudi)
        p = mock.patch.object(mcp_client, "REGISTRO", self.reg)
        p.start()
        self.addCleanup(p.stop)

    def test_spento_di_default(self):
        self.reg.avvia({})
        self.reg.avvia({"mcp": {"attivo": False, "server": {"x": {"comando": ["/bin/false"], "strumenti": ["a"]}}}})
        self.assertEqual(self.reg.dichiarazioni, [])
        self.assertEqual(json.load(open(Path(tools.BASE_PROGETTO) / "config.json"))["mcp"]["attivo"], False)

    def test_solo_gli_strumenti_consentiti_sono_esposti(self):
        self.reg.avvia(self.server())
        nomi = {d["name"] for d in tools.declarations(0)}
        self.assertIn("mcp_finto_saluta", nomi)
        self.assertNotIn("mcp_finto_pericoloso", nomi)
        self.reg.avvia(self.server(strumenti=()))          # lista vuota = niente
        r2 = mcp_client.Registro()
        r2.avvia(self.server(strumenti=()))
        self.assertEqual(r2.dichiarazioni, [])
        self.assertFalse(self.reg.server["finto"].chiama.__self__ is None)
        with self.assertRaises(mcp_client.ErroreMCP):
            self.reg.server["finto"].chiama("pericoloso", {})

    def test_schema_ridotto_a_tipi_semplici(self):
        self.reg.avvia(self.server())
        d = next(x for x in self.reg.dichiarazioni if x["name"] == "mcp_finto_saluta")["parameters"]
        self.assertEqual(d["properties"]["n"]["type"], "integer")
        self.assertEqual(d["properties"]["lista"]["type"], "string")         # tipo complesso -> testo JSON
        self.assertEqual(d["required"], ["chi"])

    def test_chiamata_con_conferma_e_risultato_come_dato(self):
        self.reg.avvia(self.server())
        out = tools.richiama(self.ctx("chiedi"), "mcp_finto_saluta", {"chi": "Mondo", "lista": "[1, 2]"})
        self.assertIn("non istruzioni", out)
        self.assertIn("ciao", out)
        self.assertIn("[1, 2]", out)
        self.assertTrue(self.domande, "ogni chiamata MCP deve chiedere conferma in modalita' 'chiedi'")

    def test_chiamata_rifiutata_non_parte(self):
        self.reg.avvia(self.server())
        out = tools.richiama(self.ctx("chiedi", rispondi=False), "mcp_finto_saluta", {"chi": "x"})
        self.assertIn("RIFIUTATO", out)

    def test_parametro_sconosciuto_rifiutato(self):
        self.reg.avvia(self.server())
        self.assertIn("sconosciuto", tools.richiama(self.ctx("tutto"), "mcp_finto_saluta", {"chi": "x", "extra": 1}))

    def test_il_server_non_riceve_segreti(self):
        import os
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "SEGRETO123", "INFISICAL_TOKEN": "t", "ALTRO": "x"}):
            self.reg.avvia(self.server())
            out = tools.richiama(self.ctx("tutto"), "mcp_finto_ambiente", {})
        self.assertNotIn("SEGRETO123", out)
        self.assertNotIn("GEMINI_API_KEY", out)
        self.assertNotIn("INFISICAL_TOKEN", out)
        self.assertNotIn("ALTRO", out)

    def test_server_che_non_parte_non_blocca_l_agente(self):
        self.reg.avvia({"mcp": {"attivo": True, "server": {"rotto": {"comando": ["/non/esiste"], "strumenti": ["a"]}}}})
        self.assertEqual(self.reg.dichiarazioni, [])
        self.assertTrue(self.reg.errori)

    def test_strumento_sconosciuto(self):
        with self.assertRaises(KeyError):
            tools.richiama(self.ctx("tutto"), "non_esiste", {})


class TestUiPerNome(Base):
    def helper(self, risposte):
        return mock.patch.object(tools, "_helper_ui", side_effect=risposte if isinstance(risposte, list) else [risposte])

    def test_elenca_formatta_e_chiede_conferma(self):
        r = {"ok": True, "elementi": [{"i": 0, "ruolo": "push button", "nome": "Nuova sessione", "abilitato": True, "x": 10, "y": 20, "modificabile": False},
                                      {"i": 1, "ruolo": "push button", "nome": "Elimina", "abilitato": False, "x": None, "y": None, "modificabile": False}]}
        with self.helper(r):
            out = tools.ui_elenca(self.ctx("chiedi"), "claude")
        self.assertIn("[0] push button «Nuova sessione» @(10,20)", out)
        self.assertIn("(disabilitato)", out)
        self.assertTrue(self.domande)

    def test_elenca_vuoto_spiega_cosa_fare(self):
        with self.helper({"ok": True, "elementi": []}):
            self.assertIn("accessibilita'", tools.ui_elenca(self.ctx("tutto"), "claude"))

    def test_click_nativo_allega_screenshot(self):
        r = {"ok": True, "metodo": "azione", "elemento": {"ruolo": "push button", "nome": "Nuova sessione", "x": 1, "y": 1}}
        with self.helper(r), mock.patch.object(tools, "_cattura", return_value=("n.jpg", 10, 10)):
            out = tools.ui_azione(self.ctx("tutto"), "claude", "Nuova sessione")
        self.assertIn("Fatto: click", out)
        self.assertIn("[screenshot allegato dopo l'azione", out)

    def test_click_rifiutato_non_chiama_l_helper(self):
        with mock.patch.object(tools, "_helper_ui") as h:
            out = tools.ui_azione(self.ctx("chiedi", rispondi=False), "claude", "X")
        self.assertIn("RIFIUTATO", out)
        h.assert_not_called()

    def test_ambiguo_elenca_i_candidati(self):
        r = {"ok": False, "errore": "piu' elementi corrispondono: indica 'indice'.", "candidati": [{"i": 0, "ruolo": "link", "nome": "A"}, {"i": 1, "ruolo": "link", "nome": "A"}]}
        with self.helper(r):
            out = tools.ui_azione(self.ctx("tutto"), "brave", "A")
        self.assertTrue(out.startswith("Errore"))
        self.assertIn("[1] link «A»", out)

    def test_azione_non_valida_e_testo_obbligatorio(self):
        self.assertIn("non valida", tools.ui_azione(self.ctx("tutto"), "a", "b", azione="elimina"))
        self.assertIn("Serve 'testo'", tools.ui_azione(self.ctx("tutto"), "a", "b", azione="scrivi"))

    def test_scrivi_via_tastiera_digita_con_xdotool(self):
        r = {"ok": True, "metodo": "tastiera", "elemento": {"ruolo": "entry", "nome": "Messaggio", "x": 1, "y": 1}}
        with self.helper(r), mock.patch.object(tools.shutil, "which", return_value="/usr/bin/xdotool"), \
                mock.patch.object(tools.subprocess, "run") as run, mock.patch.object(tools, "_cattura", return_value=("n.jpg", 1, 1)):
            out = tools.ui_azione(self.ctx("tutto"), "claude", "Messaggio", azione="scrivi", testo="ciao")
        self.assertIn("Fatto: scrivi", out)
        self.assertIn("ciao", str(run.call_args_list))

    def test_helper_senza_accessibilita_da_errore_leggibile(self):
        with mock.patch.object(tools.subprocess, "run", side_effect=OSError):
            self.assertEqual(tools._helper_ui({"cmd": "app"})["ok"], False)

    def test_la_verifica_riconosce_ui_azione(self):
        ob = 'scrivi "ciao" nella chat'
        p = [verifica.Passo("ui_azione", {"azione": "scrivi", "testo": "ciao"}, "Fatto: scrivi su entry (metodo: testo). [screenshot allegato dopo l'azione, 1x1]")]
        self.assertFalse(verifica.controlla("Ho scritto ciao.", p, ob, self.ws)["errori"])
        p = [verifica.Passo("ui_azione", {"azione": "click", "nome": "x"}, "Fatto: click su push button «x» (metodo: azione).")]
        self.assertTrue(verifica.controlla("Ho cliccato il pulsante.", p, "clicca", self.ws)["errori"])        # niente screenshot dopo


if __name__ == "__main__":
    unittest.main()
