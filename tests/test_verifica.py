"""La verifica deterministica smaschera le allucinazioni: modello finto che mente apposta, esiti attesi fissi."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

RADICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RADICE))
sys.path.insert(0, str(RADICE / "tests"))

import agent  # noqa: E402
import verifica  # noqa: E402
from verifica import Passo  # noqa: E402
from test_deterministici import Base, chiama, testo  # noqa: E402


class TestControllo(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def vedi(self, risposta, passi=(), goal="x"):
        return verifica.controlla(risposta, list(passi), goal, self.ws)

    def test_azione_dichiarata_senza_strumenti(self):
        e = self.vedi("Ho creato il file a.txt con successo.")
        self.assertFalse(e["ok"])
        self.assertIn("scrittura", e["errori"][0])

    def test_creazione_vera_e_file_presente(self):
        (self.ws / "a.txt").write_text("x")
        e = self.vedi("Ho creato il file a.txt.", [Passo("write_file", {"path": "a.txt"}, "Scritto a.txt (1 caratteri, verificato)")])
        self.assertTrue(e["ok"], e)

    def test_file_dichiarato_ma_inesistente(self):
        (self.ws / "a.txt").write_text("x")
        e = self.vedi("Ho creato il file b.txt.", [Passo("write_file", {"path": "a.txt"}, "Scritto a.txt (1 caratteri, verificato)")])
        self.assertFalse(e["ok"])
        self.assertIn("b.txt", e["errori"][0])

    def test_cancellazione_dichiarata_ma_file_ancora_li(self):
        (self.ws / "x.txt").write_text("x")
        p = Passo("delete_file", {"path": "x.txt"}, "Cancellato x.txt (verificato)")
        self.assertFalse(self.vedi("Ho eliminato il file x.txt.", [p])["ok"])
        (self.ws / "x.txt").unlink()
        self.assertTrue(self.vedi("Ho eliminato il file x.txt.", [p])["ok"])

    def test_cancellazione_via_shell_rm_si_ma_ls_no(self):
        (self.ws / "x.txt").write_text("x")
        self.assertFalse(self.vedi("Ho cancellato x.txt.", [Passo("run_shell", {"command": "ls"}, "exit=0\nx.txt")])["ok"])
        (self.ws / "x.txt").unlink()
        self.assertTrue(self.vedi("Ho cancellato x.txt.", [Passo("run_shell", {"command": "rm x.txt"}, "exit=0")])["ok"])

    def test_esecuzione_dichiarata_con_sola_lettura(self):
        self.assertFalse(self.vedi("Ho eseguito lo script.", [Passo("read_file", {"path": "s.py"}, "print(1)")])["ok"])
        self.assertTrue(self.vedi("Ho eseguito lo script.", [Passo("run_shell", {"command": "python3 s.py"}, "exit=0\n1")])["ok"])

    def test_comando_fallito_non_conta_come_successo(self):
        self.assertFalse(self.vedi("Ho eseguito lo script.", [Passo("run_shell", {"command": "python3 s.py"}, "exit=1\nTraceback")])["ok"])

    def test_tutti_gli_strumenti_rifiutati_ma_successo_dichiarato(self):
        e = self.vedi("Fatto! Ho creato il file.", [Passo("write_file", {"path": "a"}, "RIFIUTATO dall'utente.")])
        self.assertFalse(e["ok"])

    def test_negazioni_non_sono_accuse(self):
        for frase in ("Non ho potuto creare il file.", "Purtroppo non sono riuscito a eseguire lo script.",
                      "Il file non è stato eliminato perché il percorso è protetto."):
            self.assertTrue(self.vedi(frase)["ok"], frase)

    def test_descrizioni_non_sono_azioni(self):
        # falsi positivi trovati con Qwen: 'c'e' scritto' e 'e' corretta' non dichiarano nessuna azione
        for frase in ("Nella seconda riga c'è scritto **due**.", "La mia risposta è corretta basata sui dati letti:",
                      "Il valore è scritto in maiuscolo.", "La soluzione è corretta."):
            self.assertTrue(self.vedi(frase)["ok"], frase)
        self.assertFalse(self.vedi("Il file è stato creato.")["ok"])             # passivo vero: richiede uno strumento
        self.assertFalse(self.vedi("Abbiamo eseguito lo script con successo.")["ok"])

    def test_risposta_vuota_dopo_strumenti_e_un_errore(self):
        p = Passo("run_shell", {"command": "echo x"}, "exit=0\nx")
        self.assertFalse(self.vedi("", [p])["ok"])
        self.assertTrue(self.vedi("")["ok"])                  # vuota senza strumenti: niente da contestare

    def test_risposta_senza_azioni_e_pulita(self):
        self.assertEqual(self.vedi("Python è un linguaggio di programmazione."), {"errori": [], "avvisi": [], "ok": True})

    def test_numero_non_fondato_e_avviso(self):
        p = Passo("run_shell", {"command": "python3 s.py"}, "exit=0\n42")
        self.assertEqual(self.vedi("Il risultato è 42.", [p])["avvisi"], [])
        a = self.vedi("Il risultato è 12345.", [p])["avvisi"]
        self.assertTrue(any("12345" in x for x in a), a)
        self.assertEqual(self.vedi("Siamo nel 2026.")["avvisi"], [])       # gli anni non contano

    def test_blocco_di_codice_non_fondato_e_avviso(self):
        p = Passo("read_file", {"path": "a.py"}, "print('ciao')")
        self.assertEqual(self.vedi("Contiene:\n```\nprint('ciao')\n```", [p])["avvisi"], [])
        self.assertTrue(self.vedi("Contiene:\n```\nimport os; os.system('ls')\n```", [p])["avvisi"])

    def test_registro_ricostruito_dalla_cronologia(self):
        from google.genai import types
        contents = [types.Content(role="user", parts=[types.Part(text="fai")]), chiama("write_file", path="a"),
                    types.Content(role="user", parts=[types.Part.from_function_response(name="write_file", response={"result": "Scritto a"})]),
                    types.Content(role="user", parts=[types.Part(text="VERIFICA AUTOMATICA: ...")])]
        passi = verifica.libro_da_cronologia(contents)
        self.assertEqual([(p.strumento, p.ok) for p in passi], [("write_file", True)])   # la contestazione non azzera il registro


class TestAgenteContestato(Base):
    def test_allucinazione_smascherata_poi_corretta(self):
        ris, cx, sess, cl = self.corri([testo("Ho creato il file z.txt."),                      # MENTE: nessuno strumento
                                        chiama("write_file", path="z.txt", content="vero"),      # dopo la contestazione lo fa davvero
                                        testo("Ho creato il file z.txt.")], modo="tutto")
        self.assertEqual((self.ws / "z.txt").read_text(), "vero")
        self.assertTrue(cx.verifica["ok"])
        self.assertEqual(len(cl.models.viste), 3)
        contestazioni = [p.text for c in sess.contents for p in c.parts if p.text and p.text.startswith("VERIFICA AUTOMATICA")]
        self.assertEqual(len(contestazioni), 1)

    def test_si_arrende_dopo_due_contestazioni_e_lo_segnala(self):
        bugie = [testo("Ho creato il file q.txt.")] * 3
        ris, cx, sess, cl = self.corri(bugie)
        self.assertFalse(cx.verifica["ok"])
        self.assertEqual(len(cl.models.viste), 3)                 # 1 risposta + 2 correzioni, poi basta
        self.assertFalse((self.ws / "q.txt").exists())

    def test_risposta_onesta_non_viene_contestata(self):
        ris, cx, sess, cl = self.corri([chiama("write_file", path="a.txt", content="x"), testo("Ho creato il file a.txt.")], modo="tutto")
        self.assertTrue(cx.verifica["ok"])
        self.assertEqual(len(cl.models.viste), 2)

    def test_ammette_di_non_aver_fatto(self):
        ris, cx, sess, cl = self.corri([testo("Ho creato il file z.txt."), testo("Non ho creato nessun file: mi sono sbagliato.")])
        self.assertTrue(cx.verifica["ok"])
        self.assertEqual(len(cl.models.viste), 2)

    def test_registro_non_si_perde_dopo_la_contestazione(self):
        ris, cx, sess, cl = self.corri([chiama("write_file", path="a.txt", content="x"),
                                        testo("Ho creato a.txt e ho eliminato b.txt."),     # la seconda parte e' falsa
                                        testo("Ho creato a.txt; non ho eliminato nulla.")], modo="tutto")
        self.assertTrue(cx.verifica["ok"])
        self.assertTrue((self.ws / "a.txt").exists())

    def test_verifica_disattivabile(self):
        cx = self.ctx("chiedi", client=None)
        import copy
        cfg2 = copy.deepcopy(agent.tools.__dict__.get("CFG_X", None) or {})
        # disattivata da config: la menzogna passa senza contestazione (comportamento esplicito, non di default)
        from test_deterministici import CFG, ClientFinto
        CFG["verifica"] = False
        try:
            ris, cx2, _, cl = self.corri([testo("Ho creato il file k.txt.")])
        finally:
            CFG["verifica"] = True
        self.assertEqual(len(cl.models.viste), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
