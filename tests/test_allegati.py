"""Allegati: validazione dai byte, ricodifica delle immagini, PDF, API, passaggio al modello (Gemini e fallback)."""
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

RADICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RADICE))
sys.path.insert(0, str(RADICE / "tests"))

from PIL import Image  # noqa: E402
from google.genai import types  # noqa: E402

import agent  # noqa: E402
import allegati  # noqa: E402
import llm  # noqa: E402
import tools  # noqa: E402
import web  # noqa: E402
import verifica  # noqa: E402
from test_deterministici import Base, CFG, ClientFinto, TestWeb, chiama, testo  # noqa: E402


def png(w=40, h=30, colore=(200, 30, 30)):
    b = io.BytesIO()
    Image.new("RGB", (w, h), colore).save(b, "PNG")
    return b.getvalue()


def jpeg_con_exif():
    exif = Image.Exif()
    exif[0x010F] = "MarcaFotocameraSegreta"
    exif[0x0132] = "2026:01:01 10:00:00"
    b = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 120, 200)).save(b, "JPEG", exif=exif)
    return b.getvalue()


def pdf_con_testo(t="Ciao dal PDF"):
    flusso = f"BT /F1 24 Tf 72 700 Td ({t}) Tj ET".encode()
    ogg = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
           b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
           b"<< /Length %d >>\nstream\n" % len(flusso) + flusso + b"\nendstream", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, pos = b"%PDF-1.4\n", []
    for i, o in enumerate(ogg, 1):
        pos.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(ogg) + 1) + b"".join(b"%010d 00000 n \n" % p for p in pos)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(ogg) + 1, xref)
    return out


class IsolaDati:
    def setUp(self):
        super().setUp()
        allegati.DIR = self.tmp / "allegati"


class TestPrepara(IsolaDati, Base):
    def test_png_diventa_jpeg_e_toglie_i_metadati(self):
        meta, dati = allegati.prepara("foto.png", png())
        self.assertEqual((meta["tipo"], meta["mime"], meta["nome"]), ("immagine", "image/jpeg", "foto.jpg"))
        self.assertTrue(dati.startswith(b"\xff\xd8"))
        meta2, dati2 = allegati.prepara("scatto.jpg", jpeg_con_exif())
        self.assertEqual(len(Image.open(io.BytesIO(dati2)).getexif()), 0)          # niente EXIF (posizione, fotocamera...)
        self.assertNotIn(b"MarcaFotocameraSegreta", dati2)

    def test_immagini_enormi_vengono_ridotte(self):
        meta, _ = allegati.prepara("g.png", png(5000, 3000))
        self.assertLessEqual(max(meta["larghezza"], meta["altezza"]), 2048)

    def test_il_tipo_si_decide_dai_byte_non_dal_nome(self):
        for nome, dati in (("virus.png", b"MZ\x90\x00 eseguibile"), ("a.pdf", b"non e' un pdf"), ("x.jpg", b"testo qualunque")):
            with self.assertRaises(allegati.ErroreAllegato, msg=nome):
                allegati.prepara(nome, dati)
        with self.assertRaises(allegati.ErroreAllegato):
            allegati.prepara("falso.png", b"\x89PNG\r\n\x1a\n" + b"rotto" * 10)         # intestazione giusta, contenuto rotto

    def test_pdf_valido(self):
        meta, dati = allegati.prepara("doc.pdf", pdf_con_testo())
        self.assertEqual((meta["tipo"], meta["mime"], meta["pagine"]), ("pdf", "application/pdf", 1))
        self.assertIn("Ciao dal PDF", allegati.testo_pdf(dati))

    def test_testo_solo_utf8_e_estensioni_ammesse(self):
        meta, _ = allegati.prepara("note.md", "# Titolo àèì".encode())
        self.assertEqual(meta["tipo"], "testo")
        with self.assertRaises(allegati.ErroreAllegato):
            allegati.prepara("binario.txt", b"\xff\xfe\x00\x01 rotto")
        with self.assertRaises(allegati.ErroreAllegato):
            allegati.prepara("programma.exe", b"ciao")
        with self.assertRaises(allegati.ErroreAllegato):
            allegati.prepara("grande.txt", b"a" * (allegati.MAX_TESTO + 1))

    def test_limiti_e_file_vuoto(self):
        with self.assertRaises(allegati.ErroreAllegato):
            allegati.prepara("v.txt", b"")
        with self.assertRaises(allegati.ErroreAllegato):
            allegati.prepara("g.pdf", b"%PDF-" + b"0" * (allegati.MAX_FILE + 1))

    def test_nome_ripulito(self):
        self.assertEqual(allegati.nome_sicuro("../../etc/passwd"), "passwd")
        self.assertEqual(allegati.nome_sicuro("a<b>\"c|d?.txt"), "a_b__c_d_.txt")
        self.assertLessEqual(len(allegati.nome_sicuro("x" * 500 + ".txt")), 80)
        self.assertEqual(allegati.nome_sicuro("   "), "file")

    def test_salva_carica_e_pulizia(self):
        meta, dati = allegati.prepara("n.txt", b"ciao")
        m = allegati.salva(meta, dati)
        self.assertEqual(allegati.carica(m["id"])[1], b"ciao")
        for brutto in ("../x", "", "zz", "0" * 15):
            with self.assertRaises(allegati.ErroreAllegato):
                allegati.carica(brutto)
        j = allegati.DIR / f"{m['id']}.json"
        d = json.loads(j.read_text())
        d["quando"] -= 20 * 86400
        j.write_text(json.dumps(d))
        allegati.pulisci(14)
        self.assertFalse((allegati.DIR / f"{m['id']}.bin").exists())


class TestWebAllegati(IsolaDati, TestWeb):
    H = {"X-Requested-With": "gemini-agent"}

    def carica(self, c, nome, dati):
        return c.post("/api/allegato", data=dati, headers={**self.H, "X-Nome": nome})

    def test_upload_sicurezza(self):
        app = self.app()
        self.assertEqual(app.test_client().post("/api/allegato", data=png(), headers=self.H).status_code, 401)
        c = self.entra(app)
        self.assertEqual(c.post("/api/allegato", data=png()).status_code, 400)                       # senza header
        self.assertEqual(self.carica(c, "x.png", b"MZ eseguibile").status_code, 422)
        self.assertEqual(self.carica(c, "x.png", b"a" * (11 * 1024 * 1024)).status_code, 413)
        r = self.carica(c, "foto.png", png())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["tipo"], "immagine")

    def test_servire_l_allegato_in_sicurezza(self):
        c = self.entra(self.app())
        aid = self.carica(c, "doc.pdf", pdf_con_testo()).get_json()["id"]
        r = c.get(f"/api/allegato/{aid}")
        self.assertEqual((r.status_code, r.mimetype), (200, "application/pdf"))
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        aid2 = self.carica(c, "n.txt", b"<script>alert(1)</script>").get_json()["id"]       # un testo con HTML non deve poter girare
        r2 = c.get(f"/api/allegato/{aid2}")
        self.assertEqual(r2.mimetype, "text/plain")
        self.assertIn("sandbox", r2.headers["Content-Security-Policy"])
        aid3 = self.carica(c, "f.png", png()).get_json()["id"]
        self.assertIn("sandbox", c.get(f"/api/allegato/{aid3}").headers["Content-Security-Policy"])
        self.assertEqual(c.get("/api/allegato/..%2f..%2fetc").status_code, 404)
        self.assertEqual(self.app().test_client().get(f"/api/allegato/{aid}").status_code, 401)

    def test_nome_con_percorso_non_esce(self):
        c = self.entra(self.app())
        r = self.carica(c, "..%2F..%2Fevil.txt", b"x")
        self.assertEqual(r.get_json()["nome"], "evil.txt")

    def test_l_immagine_arriva_a_gemini_e_resta_per_le_domande_dopo(self):
        app = self.app([testo("Vedo un quadrato rosso."), testo("E' rosso.")])
        c = self.entra(app)
        aid = self.carica(c, "foto.png", png()).get_json()["id"]
        rid = c.post("/api/nuovo", json={"obiettivo": "Cosa vedi?", "allegati": [aid]}).get_json()["id"]
        st = self.aspetta(c, lambda s: s["runs"] and not s["runs"][0]["attivo"])
        self.assertEqual(st["runs"][0]["turni"][0]["allegati"][0]["nome"], "foto.jpg")
        # cio' che e' stato davvero inviato al modello: testo + immagine
        run = web.RUNS[rid]
        primo = run.sessione.contents[0].parts
        self.assertEqual(primo[0].text, "Cosa vedi?")
        self.assertEqual(primo[1].text, "[Allegato: foto.jpg]")
        self.assertEqual(primo[2].inline_data.mime_type, "image/jpeg")
        self.assertTrue((self.ws / "allegati" / "foto.jpg").exists())                                  # copia nel workspace
        self.assertIn("allegati/foto.jpg", primo[3].text)
        # domanda successiva: l'immagine e' ancora nella cronologia (non viene tolta come uno screenshot)
        c.post("/api/continua", json={"id": rid, "obiettivo": "Di che colore?"})
        self.aspetta(c, lambda s: not s["runs"][0]["attivo"])
        self.assertIsNotNone(run.sessione.contents[0].parts[2].inline_data)
        # nel file salvato non ci sono i byte
        self.assertNotIn("inline_data", run.sessione.file.read_text())
        self.assertIn("[file allegato omesso]", run.sessione.file.read_text())

    def test_solo_allegati_senza_testo(self):
        c = self.entra(self.app([testo("ok")]))
        aid = self.carica(c, "n.txt", b"contenuto di prova").get_json()["id"]
        rid = c.post("/api/nuovo", json={"allegati": [aid]}).get_json()["id"]
        st = self.aspetta(c, lambda s: s["runs"] and not s["runs"][0]["attivo"])
        self.assertIn("Analizza gli allegati", st["runs"][0]["turni"][0]["q"])
        testi = [p.text for p in web.RUNS[rid].sessione.contents[0].parts]
        self.assertTrue(any("contenuto di prova" in (t or "") for t in testi))                        # il testo e' nel messaggio

    def test_limiti_per_messaggio(self):
        c = self.entra(self.app([testo("ok")] * 3))
        ids = [self.carica(c, f"n{i}.txt", b"x").get_json()["id"] for i in range(6)]
        self.assertEqual(c.post("/api/nuovo", json={"obiettivo": "x", "allegati": ids}).status_code, 422)   # 6 > 5
        self.assertEqual(c.post("/api/nuovo", json={"obiettivo": "x", "allegati": ["0123456789abcdef"]}).status_code, 422)
        self.assertEqual(c.post("/api/nuovo", json={"obiettivo": "x", "allegati": ids[:2]}).status_code, 200)

    def test_il_blocco_ferma_i_caricamenti(self):
        c = self.entra(self.app())
        import audit
        audit.imposta_blocco(True)
        self.assertEqual(self.carica(c, "a.png", png()).status_code, 423)


class TestModelli(IsolaDati, Base):
    def contents(self, mime, dati):
        return [types.Content(role="user", parts=[types.Part(text="guarda"), types.Part(text="[Allegato: a]"),
                                                  types.Part.from_bytes(data=dati, mime_type=mime)])]

    def test_immagine_a_un_modello_con_visione(self):
        m = llm.a_messaggi(self.contents("image/jpeg", b"\xff\xd8abc"), "s", visione=True)
        parti = m[1]["content"]
        self.assertEqual([p["type"] for p in parti], ["text", "image_url"])
        self.assertTrue(parti[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_immagine_a_un_modello_senza_visione_lo_dice(self):
        m = llm.a_messaggi(self.contents("image/jpeg", b"\xff\xd8abc"), "s", visione=False)
        self.assertIsInstance(m[1]["content"], str)
        self.assertIn("non puo' vederla", m[1]["content"])

    def test_pdf_diventa_testo_per_i_modelli_che_non_lo_leggono(self):
        m = llm.a_messaggi(self.contents("application/pdf", pdf_con_testo("Fattura 4421")), "s", visione=False)
        self.assertIn("Fattura 4421", m[1]["content"])

    def test_pdf_scansionato_diventa_immagini_per_la_visione(self):
        b = io.BytesIO()
        Image.new("RGB", (300, 300), (255, 255, 255)).save(b, "PDF")          # PDF solo immagine, senza testo
        m = llm.a_messaggi(self.contents("application/pdf", b.getvalue()), "s", visione=True)
        self.assertTrue(any(p["type"] == "image_url" for p in m[1]["content"]), m[1]["content"])

    def test_estrai_testo_strumento(self):
        (self.ws / "d.pdf").write_bytes(pdf_con_testo("Totale 99"))
        self.assertIn("Totale 99", tools.estrai_testo(self.ctx(), "d.pdf"))
        (self.ws / "x.txt").write_text("a")
        self.assertIn("Errore", tools.estrai_testo(self.ctx(), "x.txt"))
        with self.assertRaises(PermissionError):
            tools.estrai_testo(self.ctx(), "../x.pdf")

    def test_con_allegati_niente_avvisi_sui_valori(self):
        r = verifica.controlla("Il totale e' 123456 euro.", [], "x", self.ws)
        self.assertTrue(r["avvisi"])
        r = verifica.controlla("Il totale e' 123456 euro.", [], "x", self.ws, con_allegati=True)
        self.assertEqual(r["avvisi"], [])

    def test_domanda_su_un_allegato_non_genera_avvisi_inutili(self):
        client = ClientFinto([testo("Nel PDF il totale e' 987654 euro.")])
        cx = self.ctx("chiedi", client=client)
        sess = agent.Sessione(self.tmp / "s", [], None)
        al = [{"nome": "f.pdf", "tipo": "pdf", "mime": "application/pdf", "dati": pdf_con_testo()}]
        agent.esegui_obiettivo(cx, sess, "Qual e' il totale?", allegati=al)
        self.assertEqual(cx.verifica["avvisi"], [])
        self.assertTrue(cx.verifica["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
