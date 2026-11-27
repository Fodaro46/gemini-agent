"""Test della trascrizione vocale (senza rete: modello finto; ffmpeg vero per la conversione)."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

RADICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RADICE))

import agent  # noqa: E402
import usage  # noqa: E402
import voce  # noqa: E402

sys.path.insert(0, str(RADICE / "tests"))
from test_deterministici import CFG, ClientFinto, quota_giornaliera, testo  # noqa: E402


class TestVoce(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        usage.USO.file, usage.USO.dati = self.tmp / "uso.json", {}
        self.lim = agent.RateLimiter(10000)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_conversione_vera_con_ffmpeg(self):
        sorgente = self.tmp / "tono.webm"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "libopus", str(sorgente)], check=True)
        wav = voce.converti_wav(sorgente.read_bytes())
        self.assertEqual(wav[:4], b"RIFF")
        self.assertGreater(len(wav), 20000)          # ~1 s a 16 kHz mono 16 bit = ~32 KB

    def test_audio_vuoto_o_rotto(self):
        with self.assertRaises(voce.ErroreVoce):
            voce.converti_wav(b"")
        with self.assertRaises(voce.ErroreVoce):
            voce.converti_wav(b"non sono audio" * 100)
        with self.assertRaises(voce.ErroreVoce):
            voce.converti_wav(b"x" * (voce.MAX_BYTE + 1))

    def test_trascrizione_e_pulizia_virgolette(self):
        cl = ClientFinto([testo('"Crea il file prova"')])
        self.assertEqual(voce.trascrivi(cl, CFG, self.lim, b"x", convertitore=lambda a: b"RIFFfinto"), "Crea il file prova")
        self.assertEqual(usage.USO.dati[usage.oggi()][CFG["model"]]["richieste"], 1)

    def test_audio_inviato_come_wav_con_prompt(self):
        visti = []
        cl = ClientFinto([testo("ok")])
        originale = cl.models.generate_content
        cl.models.generate_content = lambda **kw: (visti.append(kw), originale(**kw))[1]
        voce.trascrivi(cl, CFG, self.lim, b"x", convertitore=lambda a: b"RIFFfinto")
        parti = visti[0]["contents"][0].parts
        self.assertEqual(parti[0].inline_data.mime_type, "audio/wav")
        self.assertIn("Trascrivi", parti[1].text)

    def test_quota_giornaliera_passa_al_modello_di_riserva(self):
        cl = ClientFinto([quota_giornaliera(), testo("dalla riserva")])
        self.assertEqual(voce.trascrivi(cl, CFG, self.lim, b"x", convertitore=lambda a: b"R"), "dalla riserva")
        self.assertTrue(usage.USO.e_esaurito(CFG["model"]))

    def test_quota_finita_ovunque_messaggio_chiaro(self):
        cl = ClientFinto([quota_giornaliera(), quota_giornaliera()])
        with self.assertRaises(voce.ErroreVoce) as e:
            voce.trascrivi(cl, CFG, self.lim, b"x", convertitore=lambda a: b"R")
        self.assertIn("Scrivi a mano", str(e.exception))

    def test_altri_errori_non_vengono_mascherati(self):
        cl = ClientFinto([RuntimeError("rete caduta")])
        with self.assertRaises(voce.ErroreVoce) as e:
            voce.trascrivi(cl, CFG, self.lim, b"x", convertitore=lambda a: b"R")
        self.assertIn("rete caduta", str(e.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
