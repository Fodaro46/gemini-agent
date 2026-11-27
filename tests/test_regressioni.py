"""Regressioni dei bug trovati dalla revisione indipendente del codice: ognuno ha un test che PRIMA falliva."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

RADICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RADICE))
sys.path.insert(0, str(RADICE / "tests"))

import agent  # noqa: E402
import audit  # noqa: E402
import llm  # noqa: E402
import tools  # noqa: E402
import usage  # noqa: E402
import verifica  # noqa: E402
import web  # noqa: E402
from google.genai import types  # noqa: E402
from test_deterministici import Base, CFG, ClientFinto, TestWeb, chiama, testo  # noqa: E402


class TestSandboxEScrittura(Base):
    def test_protetti_anche_fuori_dalla_home(self):
        for p in ("/srv/app/.env", "/srv/app/.env.production", "/opt/x/server.pem", "/var/k/id_rsa", "/x/.npmrc", "/x/.pgpass", "/x/a.gpg", "/x/a.ovpn"):
            self.assertTrue(tools.protetto(Path(p)), p)

    def test_scrittura_vietata(self):
        for rel in (".bashrc", ".profile", ".gitconfig", ".git/config", ".git/hooks/pre-commit", "sub/.gitattributes"):
            with self.assertRaises(PermissionError, msg=rel):
                tools.write_file(self.ctx("tutto"), rel, "x")
        self.assertIn("scritto", tools.write_file(self.ctx("tutto"), "normale.txt", "x").lower())

    def test_il_codice_del_programma_non_si_modifica(self):
        cx = self.ctx("tutto", ws=RADICE)
        for rel in ("tools.py", "config.json", "web.py", "tests/test_voce.py"):
            with self.assertRaises(PermissionError, msg=rel):
                tools.write_file(cx, rel, "x")

    def test_il_workspace_del_programma_e_scrivibile(self):
        ws = RADICE / "workspace" / "_prova_test"
        try:
            self.assertIsNone(tools.scrittura_vietata(ws / "a.txt"))
        finally:
            shutil.rmtree(ws, ignore_errors=True)

    def test_fetch_url_blocca_cgnat_tailscale(self):
        for u in ("http://100.64.0.1/", "http://100.126.238.91/", "http://169.254.169.254/", "http://0.0.0.0/"):
            with self.assertRaises(Exception, msg=u):
                tools._check_public_url(u)

    def test_fetch_url_con_query_enorme_rifiutata(self):
        self.assertIn("troppo lungo", tools.fetch_url(self.ctx(), "https://example.com/?d=" + "a" * 400))

    def test_read_file_non_carica_tutto_in_memoria(self):
        (self.ws / "g.bin").write_bytes(b"a" * 5_000_000)
        self.assertIn("troncato", tools.read_file(self.ctx(), "g.bin"))

    def test_shell_stdin_chiuso_e_timeout_uccide_i_figli(self):
        cfg = dict(CFG, shell_timeout_s=1)
        cx = tools.Context(cfg, self.ws, lambda t: True, log=lambda t: None, modo="tutto")
        t0 = time.time()
        out = tools.run_shell(cx, "sleep 30 & sleep 30")
        self.assertIn("Timeout", out)
        self.assertLess(time.time() - t0, 6)
        out = tools.run_shell(self.ctx("tutto"), "cat")           # senza stdin chiuso resterebbe in attesa
        self.assertIn("exit=0", out)


class TestGitSicuro(Base):
    def test_fsmonitor_malevolo_non_viene_eseguito(self):
        repo = self.tmp / "r"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        marker = self.tmp / "ESEGUITO"
        evil = self.tmp / "evil.sh"
        evil.write_text(f"#!/bin/sh\ntouch {marker}\n")
        evil.chmod(0o755)
        with (repo / ".git/config").open("a") as f:
            f.write(f"[core]\n\tfsmonitor = {evil}\n")
        tools.git(self.ctx("tutto", ws=repo), "status")
        self.assertFalse(marker.exists(), "git ha eseguito un comando scritto in .git/config")

    def test_hook_malevolo_non_viene_eseguito(self):
        repo = self.tmp / "r2"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "-C", str(repo), "config", k, v], check=True)
        marker = self.tmp / "HOOK"
        hook = repo / ".git/hooks/pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        (repo / "a.txt").write_text("1")
        cx = self.ctx("tutto", ws=repo)
        tools.git(cx, "add", "a.txt")
        tools.git(cx, "commit", "prova")
        self.assertFalse(marker.exists(), "git ha eseguito un hook")

    def test_add_non_mette_in_stage_i_segreti(self):
        repo = self.tmp / "r3"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "a.txt").write_text("1")
        (repo / ".env").write_text("TOKEN=1")
        (repo / "k.pem").write_text("x")
        tools.git(self.ctx("tutto", ws=repo), "add")
        staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"], capture_output=True, text=True).stdout.split()
        self.assertEqual(staged, ["a.txt"])

    def test_add_con_sottocartella_repo(self):
        outer = self.tmp / "outer"
        repo = outer / "sub"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "a.txt").write_text("1")
        cx = self.ctx("tutto", ws=outer)
        tools.git(cx, "add", "a.txt", "sub")
        staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"], capture_output=True, text=True).stdout.split()
        self.assertEqual(staged, ["a.txt"])

    def test_la_conferma_del_push_mostra_la_destinazione(self):
        bare, repo = self.tmp / "remoto.git", self.tmp / "rp"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "clone", "-q", str(bare), str(repo)], check=True, capture_output=True)
        tools.git(self.ctx("tutto", rispondi=False, ws=repo), "push")
        self.assertTrue(any(str(bare) in d for d in self.domande), self.domande)


class TestLoopRobusto(Base):
    def test_stop_a_meta_non_lascia_chiamate_orfane(self):
        client = ClientFinto([types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name="list_dir", args={})),
            types.Part(function_call=types.FunctionCall(name="list_dir", args={}))])])
        cx = self.ctx("tutto", client=client)
        n = []
        cx.log = lambda t: (n.append(t), cx.stop.set() if "strumento: list_dir" in t else None)
        sess = agent.Sessione(self.tmp / "s", [], None)
        with self.assertRaises(agent.Fermato):
            agent.esegui_obiettivo(cx, sess, "x")
        ultimo = sess.contents[-1]
        self.assertEqual(ultimo.role, "user")
        self.assertEqual(len([p for p in ultimo.parts if p.function_response]), 2)         # una risposta per ogni chiamata
        m = llm.a_messaggi(sess.contents, "s")
        ids = [t["id"] for t in m[2]["tool_calls"]]
        self.assertEqual([x["tool_call_id"] for x in m[3:5]], ids)
        # e si puo' riprendere
        ripresa = agent.Sessione(sess.file.parent, [], sess.sid)
        cx2 = self.ctx("tutto", client=ClientFinto([testo("ok")]))
        self.assertEqual(agent.esegui_obiettivo(cx2, ripresa, "riprendi"), "ok")

    def test_risposta_senza_parti_non_entra_nella_cronologia(self):
        ris, cx, sess, cl = self.corri([types.Content(role="model", parts=[])])
        self.assertIn("vuota", ris)
        self.assertEqual(len(sess.contents), 1)         # solo il messaggio dell'utente

    def test_risposta_finale_vuota_dopo_gli_strumenti_viene_contestata(self):
        ris, cx, sess, cl = self.corri([chiama("list_dir"), testo(""), testo("La cartella e' vuota.")])
        self.assertEqual(ris, "La cartella e' vuota.")
        self.assertTrue(cx.verifica["ok"])
        self.assertEqual(len(cl.models.viste), 3)

    def test_errore_di_rete_viene_ritentato(self):
        import requests
        cfg2 = dict(CFG, max_retries=2)
        client = ClientFinto([requests.exceptions.ConnectionError("giu'"), testo("ok")])
        cx = tools.Context(cfg2, self.ws, lambda t: True, client, agent.RateLimiter(100000), log=self.log.append, modo="tutto")
        with mock.patch.object(cx.stop, "wait", lambda s: None):
            self.assertEqual(agent.esegui_obiettivo(cx, agent.Sessione(self.tmp / "s", [], None), "x"), "ok")

    def test_salvataggio_sessione_atomico(self):
        sess = agent.Sessione(self.tmp / "s", [], None)
        sess.contents = [types.Content(role="user", parts=[types.Part(text="uno")])]
        sess.salva()
        sess.contents.append(types.Content(role="model", parts=[types.Part(text="due")]))
        with mock.patch("agent.os.replace", side_effect=OSError("disco pieno")):
            with self.assertRaises(OSError):
                sess.salva()
        self.assertEqual(len(agent.Sessione(sess.file.parent, [], sess.sid).contents), 1)     # il file buono e' intatto

    def test_sessione_corrotta_errore_chiaro(self):
        d = self.tmp / "s"
        d.mkdir()
        (d / "20260101-000000.json").write_text('{"contents": [{"role"')
        with self.assertRaises(ValueError):
            agent.Sessione(d, [], "20260101-000000")

    def test_uso_json_non_valido_non_blocca_l_avvio(self):
        for contenuto in ("null", "[]", "non json", '"x"'):
            f = self.tmp / "u.json"
            f.write_text(contenuto)
            self.assertEqual(usage.Uso(f).dati, {})


class TestInstallaPacchetto(Base):
    def setUp(self):
        super().setUp()
        self.finto = self.tmp / "helper.sh"
        self.finto.write_text("#!/bin/sh\necho \"RICEVUTO: $@\" > %s/chiamata.txt\necho ok-finto\n" % self.tmp)
        self.finto.chmod(0o755)
        self._orig = tools.COMANDO_APT[:]
        tools.COMANDO_APT[:] = [str(self.finto)]                # niente sudo vero nei test

    def tearDown(self):
        tools.COMANDO_APT[:] = self._orig
        super().tearDown()

    def test_nomi_non_validi_rifiutati_senza_chiedere(self):
        for nome in ("", "-o", "a b", "Foo", "x;y", "$(id)", "../x", "a" * 70, "x"):
            self.assertIn("non valido", tools.installa_pacchetto(self.ctx("tutto"), nome), nome)
        self.assertEqual(self.domande, [])
        self.assertFalse((self.tmp / "chiamata.txt").exists())

    def test_chiede_sempre_conferma_anche_in_modalita_mai(self):
        for modo in ("chiedi", "comodo", "tutto"):
            self.domande.clear()
            out = tools.installa_pacchetto(self.ctx(modo, rispondi=False), "xdotool")
            self.assertIn("RIFIUTATO", out, modo)
            self.assertEqual(len(self.domande), 1, modo)
            self.assertFalse((self.tmp / "chiamata.txt").exists())

    def test_installazione_approvata_chiama_l_helper_con_argomenti_fissi(self):
        out = tools.installa_pacchetto(self.ctx("tutto"), "xdotool")
        self.assertIn("installato", out)
        self.assertEqual((self.tmp / "chiamata.txt").read_text().strip(), "RICEVUTO: install xdotool")
        tools.installa_pacchetto(self.ctx("tutto"), "x", aggiorna_elenco=True)
        self.assertEqual((self.tmp / "chiamata.txt").read_text().strip(), "RICEVUTO: update")

    def test_helper_non_abilitato_spiega_cosa_fare(self):
        tools.COMANDO_APT[:] = ["sudo", "-n", "/percorso/che/non/esiste"]
        with mock.patch.object(tools, "HELPER_APT", "/percorso/che/non/esiste"):
            out = tools.installa_pacchetto(self.ctx("tutto"), "xdotool")
        self.assertIn("abilita_installazione_pacchetti.sh", out)
        self.assertEqual(self.domande, [])                         # non chiede conferma per qualcosa che non puo' partire

    def test_sudo_nella_shell_resta_rifiutato_e_indica_lo_strumento(self):
        out = tools.run_shell(self.ctx("tutto"), "sudo apt install xdotool")
        self.assertIn("installa_pacchetto", out)

    def test_istruzioni_oneste_all_agente(self):
        for frase in ("COLLABORA", "installa_pacchetto", "ESATTAMENTE", "non inventare"):
            self.assertIn(frase, agent.SYSTEM)

    def test_lo_script_di_root_rifiuta_input_malevoli(self):
        import re
        s = (RADICE / "scripts/abilita_installazione_pacchetti.sh").read_text()
        interno = re.search(r"<<'EOF'\n(.*?)\nEOF\n", s, re.S).group(1)
        f = self.tmp / "root.sh"
        f.write_text(interno + "\n")
        f.chmod(0o755)
        for args in (["install"], ["install", "-o"], ["install", "--reinstall"], ["install", "../x"], ["install", "a;b"], ["install", "Foo"],
                     ["install", "$(id)"], ["remove", "vim"], [], ["install", "a" * 70], ["install", "a", "b"]):
            r = subprocess.run([str(f), *args], capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, args)
        self.assertEqual(subprocess.run(["bash", "-n", str(RADICE / "scripts/abilita_installazione_pacchetti.sh")]).returncode, 0)


class TestAuditCloudflare(Base):
    """Falle trovate dai due audit eseguiti con la skill security-audit di Cloudflare."""

    def test_sort_non_scrive_ne_esegue(self):
        victim = self.ws / "bersaglio.sh"
        victim.write_text("originale")
        (self.ws / "payload.txt").write_text("touch %s/ESEGUITO\n" % self.tmp)
        for cmd in (f"sort -o {victim.name} payload.txt", "sort --compress-program=sh payload.txt", "uniq payload.txt bersaglio.sh"):
            self.domande.clear()
            tools.run_shell(self.ctx("comodo", rispondi=False), cmd)
            self.assertTrue(self.domande, f"{cmd} deve chiedere conferma")
        self.assertEqual(victim.read_text(), "originale")
        self.assertFalse((self.tmp / "ESEGUITO").exists())

    def test_la_catena_git_con_il_gitdir_falso_non_esegue_nulla(self):
        marker = self.tmp / "FILTRO_ESEGUITO"
        gd = self.tmp / "gd"
        (gd / "info").mkdir(parents=True)
        (gd / "HEAD").write_text("ref: refs/heads/main\n")
        (gd / "config").write_text(f"[core]\n\trepositoryformatversion = 0\n[filter \"x\"]\n\tclean = touch {marker}\n")
        (gd / "info" / "attributes").write_text("* filter=x\n")
        repo = self.ws / "r"
        repo.mkdir()
        # 1) l'agente non puo' nemmeno creare il file '.git' che dirotta git
        with self.assertRaises(PermissionError):
            tools.write_file(self.ctx("tutto"), "r/.git", f"gitdir: {gd}\n")
        # 2) anche se il file esistesse (creato da altri), git() lo rifiuta e non parte nulla
        (repo / ".git").write_text(f"gitdir: {gd}\n")
        (repo / "a.txt").write_text("x")
        out = tools.git(self.ctx("tutto", ws=self.ws), "add", "a.txt", repo="r")
        self.assertIn("non e' un repository git valido", out)
        self.assertFalse(marker.exists(), "il filtro scritto in un gitdir esterno e' stato eseguito")

    def test_nessuna_scrittura_in_dot_git_in_nessuna_forma(self):
        for rel in (".git", "sub/.git", ".git/config", ".git/hooks/x", ".git/info/attributes", ".git/anything", ".gitmodules", "x/.gitattributes"):
            with self.assertRaises(PermissionError, msg=rel):
                tools.write_file(self.ctx("tutto"), rel, "x")

    def test_file_di_avvio_e_cartelle_eseguibili_non_scrivibili(self):
        h = Path.home()
        for rel in (".bash_aliases", ".zshenv", ".bash_logout", ".xprofile", ".pam_environment", ".local/bin/ls", ".local/share/applications/x.desktop",
                    "Progetti/x/evil.pth", "Desktop/lancia.desktop"):
            self.assertIsNotNone(tools.scrittura_vietata(h / rel), rel)
        self.assertIsNone(tools.scrittura_vietata(h / "Documenti" / "note.txt"))

    def test_deny_list_shell_coincide_con_i_percorsi_protetti(self):
        for c in ("cat .docker/config.json", "cat .npmrc", "cat .pypirc", "cat .pgpass", "cat .bash_history", "ls .thunderbird", "cat .pki/x",
                  "cat db.kdbx", "cat a.gpg", "cat x.ovpn", "cat .kube/config", "cat .aws/credentials"):
            self.assertIn("bloccato", tools.run_shell(self.ctx("tutto"), c), c)
        for nome in tools.CARTELLE_PROTETTE:                      # ogni cartella protetta e' bloccata anche per la shell
            self.assertTrue(tools.BLOCCO_SHELL.search("cat " + nome + "/x"), nome)

    def test_file_nuovi_con_cartella_ampia_chiedono_conferma(self):
        cx = self.ctx("comodo", rispondi=False, ws=Path.home().resolve())
        out = tools.write_file(cx, "Progetti/gemini-agent/workspace/_prova_nuovo.txt", "x")
        self.assertIn("RIFIUTATO", out)
        self.assertTrue(self.domande)
        self.assertFalse((RADICE / "workspace/_prova_nuovo.txt").exists())
        self.domande.clear()
        tools.write_file(self.ctx("comodo", ws=self.ws), "nuovo.txt", "x")          # workspace normale: nessuna domanda
        self.assertEqual(self.domande, [])

    def test_le_conferme_non_troncano_in_silenzio(self):
        (self.ws / "a.txt").write_text("v")
        tools.write_file(self.ctx("chiedi"), "a.txt", "z" * 2000)
        self.assertIn("altri 1400 caratteri non mostrati", self.domande[-1])
        self.domande.clear()
        (self.ws / "b.txt").write_text("q" * 1500)
        tools.edit_file(self.ctx("chiedi"), "b.txt", "q" * 1500, "r")
        self.assertIn("non mostrati", self.domande[-1])

    def test_fetch_url_ipv4_mappato_e_cgnat(self):
        for u in ("http://[::ffff:100.64.0.1]/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:192.168.1.1]/", "http://100.100.100.100/"):
            with self.assertRaises(Exception, msg=u):
                tools._check_public_url(u)

    def test_fetch_url_chiede_con_query_e_in_modo_chiedi(self):
        casi = {("comodo", "https://example.com/pagina"): False, ("comodo", "https://example.com/?d=abc"): True,
                ("chiedi", "https://example.com/pagina"): True, ("tutto", "https://example.com/?d=abc"): False}
        for (modo, url), atteso in casi.items():
            self.domande.clear()
            with mock.patch.object(tools, "_check_public_url", side_effect=Exception("niente rete nei test")):
                tools.fetch_url(self.ctx(modo, rispondi=False), url)
            self.assertEqual(bool(self.domande), atteso, f"{modo} {url}")

    def test_pacchetti_nomi_esatti(self):
        for nome in ("nano-", "nan.+", "x+", "pkg.", "-o", "a", "ufw-", "a b"):
            self.assertIn("non valido", tools.installa_pacchetto(self.ctx("tutto"), nome), nome)
        with mock.patch.object(tools, "HELPER_APT", "/bin/true"), mock.patch.object(tools, "COMANDO_APT", ["/bin/true"]):
            self.assertIn("non trovato", tools.installa_pacchetto(self.ctx("tutto"), "pacchetto-che-non-esiste-xyz"))

    def test_script_root_rifiuta_nomi_che_apt_leggerebbe_come_regex_o_rimozioni(self):
        import re
        s = (RADICE / "scripts/abilita_installazione_pacchetti.sh").read_text()
        f = self.tmp / "root.sh"
        f.write_text(re.search(r"<<'EOF'\n(.*?)\nEOF\n", s, re.S).group(1) + "\n")
        f.chmod(0o755)
        for nome in ("nano-", "nan.+", "x+", "pkg.", "ufw-", "-o", "Foo", "a;b", "pacchetto-che-non-esiste-xyz"):
            self.assertEqual(subprocess.run([str(f), "install", nome], capture_output=True).returncode, 2, nome)


class TestVerificaSchermo(Base):
    """Azioni sullo schermo: dichiarare un successo senza uno screenshot DOPO l'ultimo input e' contestato."""

    def _passi(self, *seq):
        return [verifica.Passo(n, {}, ris) for n, ris in seq]

    def test_click_senza_screenshot_dopo_e_contestato(self):
        ok = ("controlla_input", "Fatto (mouse in (5, 5) confermato).")
        shot = ("screenshot", "Screenshot acquisito (1920x1080 pixel REALI)")
        for seq, attesi in (([ok], 1), ([shot, ok], 1), ([ok, shot], 0)):
            e = verifica.controlla("Ho cliccato il pulsante e ho attivato la funzione.", self._passi(*seq), "clicca", self.ws)
            self.assertEqual(len(e["errori"]) > 0, bool(attesi), seq)

    def test_click_fallito_non_e_confermato_dallo_screenshot(self):
        passi = self._passi(("controlla_input", "Errore: il mouse e' in (1, 1) invece che in (9, 9)"), ("screenshot", "Screenshot acquisito (1x1)"))
        self.assertTrue(verifica.controlla("Ho cliccato il tasto.", passi, "x", self.ws)["errori"])

    def test_attivato_senza_input_e_un_comando_normale(self):
        passi = self._passi(("run_shell", "exit=0 --- stdout"))
        self.assertFalse(verifica.controlla("Ho attivato il servizio.", passi, "x", self.ws)["errori"])

    def test_screenshot_automatico_nel_risultato_vale_come_conferma(self):
        passi = self._passi(("controlla_input", "Fatto (mouse in (5, 5) confermato). [screenshot allegato dopo l'azione, 1920x1080]"))
        self.assertFalse(verifica.controlla("Ho cliccato il pulsante.", passi, "x", self.ws)["errori"])
        passi = self._passi(("sequenza_input", "Sequenza eseguita."))
        self.assertTrue(verifica.controlla("Ho digitato l'indirizzo.", passi, "x", self.ws)["errori"])

    def test_sequenza_input_valida_e_si_ferma_al_primo_errore(self):
        for bad in ("non json", "[]", "[1]", "[{\"azione\":\"click\",\"x\":1,\"y\":1,\"extra\":1}]", json.dumps([{"azione": "tasti", "tasti": "a"}] * 13)):
            self.assertTrue(tools.sequenza_input(self.ctx("tutto"), bad).startswith("Errore"), bad)
        chiamate = []
        with mock.patch.object(tools, "_input_singolo", side_effect=lambda c, a, *r: chiamate.append(a) or ("Errore: x" if a == "scrivi" else "Fatto.")), \
             mock.patch.object(tools, "_cattura", return_value=("n.jpg", 10, 10)):
            out = tools.sequenza_input(self.ctx("tutto"), json.dumps([{"azione": "tasti", "tasti": "a"}, {"azione": "scrivi", "testo": "x"}, {"azione": "tasti", "tasti": "b"}]))
            self.assertEqual(chiamate, ["tasti", "scrivi"])
            self.assertIn("interrotta", out)
            out = tools.sequenza_input(self.ctx("tutto"), json.dumps([{"azione": "tasti", "tasti": "a"}]))
            self.assertIn("screenshot allegato", out)

    def test_il_mouse_fuori_schermo_e_segnalato(self):
        with mock.patch.object(tools.shutil, "which", return_value="/usr/bin/xdotool"), \
             mock.patch.object(tools.subprocess, "run") as run:
            run.side_effect = [mock.Mock(returncode=0, stdout="", stderr=""),
                               mock.Mock(returncode=0, stdout="x:1919 y:1079 screen:0 window:1", stderr="")]
            out = tools.controlla_input(self.ctx("tutto"), "click", 3000, 500)
        self.assertTrue(out.startswith("Errore: il mouse e' in (1919, 1079)"), out)


class TestConversione(Base):
    def test_messaggio_vuoto_dell_assistente_non_viene_inviato(self):
        contents = [types.Content(role="user", parts=[types.Part(text="ciao")]),
                    types.Content(role="model", parts=[types.Part(text="")]),
                    types.Content(role="user", parts=[types.Part(text="VERIFICA AUTOMATICA: rispondi")])]
        ruoli = [m["role"] for m in llm.a_messaggi(contents, "s")]
        self.assertEqual(ruoli, ["system", "user", "user"])


class TestProviderRobusto(Base):
    def _server(self, risposta):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                codice, corpo = risposta
                self.send_response(codice)
                self.end_headers()
                self.wfile.write(corpo)
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return {"nome": "t", "base_url": f"http://127.0.0.1:{srv.server_port}/v1", "modello": "m", "key": "k"}

    def prova(self, p):
        with self.assertRaises(llm.ProviderEsaurito):
            llm.genera(p, [types.Content(role="user", parts=[types.Part(text="x")])], "s", [])

    def test_ogni_errore_diventa_provider_esaurito(self):
        self.prova(self._server((200, b'{"error": {"message": "limite"}}')))     # 200 con errore (OpenRouter)
        self.prova(self._server((200, b"non json")))
        self.prova(self._server((200, b'{"choices": []}')))
        self.prova(self._server((400, b"{}")))
        self.prova({"nome": "t", "base_url": "http://127.0.0.1:1/v1", "modello": "m", "key": "k"})     # server spento


class TestWebRegressioni(TestWeb):
    def test_anteprima_rispetta_le_protezioni(self):
        (self.ws / ".env").write_text("SEGRETO=1")
        (self.ws / "k.pem").write_text("x")
        (self.ws / ".nascosto").write_text("x")
        (self.ws / "ok.txt").write_text("ciao")
        c = self.entra(self.app())
        for p in (".env", "k.pem", ".nascosto"):
            self.assertEqual(c.get("/api/file?path=" + p).status_code, 404, p)
        self.assertEqual(c.get("/api/file?path=ok.txt").status_code, 200)
        audit.imposta_blocco(True)
        self.assertEqual(c.get("/api/file?path=ok.txt").status_code, 423)

    def test_elenco_file_nasconde_i_protetti(self):
        (self.ws / ".env").write_text("x")
        (self.ws / "ok.txt").write_text("x")
        c = self.entra(self.app())
        self.assertEqual([f["path"] for f in c.get("/api/files").get_json()["files"]], ["ok.txt"])

    def test_token_giusto_entra_anche_dopo_errori(self):
        app = self.app()
        c = app.test_client()
        for _ in range(6):
            c.post("/login", data={"token": "sbagliato"})
        self.assertEqual(c.post("/login", data={"token": "ancora-sbagliato"}).status_code, 429)
        self.assertEqual(c.post("/login", data={"token": self.TOKEN}).status_code, 303)       # il proprietario NON resta fuori

    def test_il_blocco_e_per_utente_non_globale(self):
        c = self.app().test_client()
        for _ in range(6):
            c.post("/login", data={"token": "x"}, headers={"Tailscale-User-Login": "attaccante@x"})
        r = c.post("/login", data={"token": "x"}, headers={"Tailscale-User-Login": "io@gmail"})
        self.assertEqual(r.status_code, 401)            # non 429: un altro utente non e' bloccato

    def test_gli_errori_decadono(self):
        c = self.app().test_client()
        for _ in range(4):
            c.post("/login", data={"token": "x"})
        chi = next(iter(web.TENTATIVI))
        e, f, u = web.TENTATIVI[chi]
        web.TENTATIVI[chi] = (e, f, u - 700)            # 11 minuti fa
        c.post("/login", data={"token": "x"})
        self.assertEqual(web.TENTATIVI[chi][0], 1)

    def test_cambio_token_invalida_i_cookie(self):
        c = self.entra(self.app())
        cookie = c.get_cookie("sid").value
        vecchio = self.TOKEN
        TestWeb.TOKEN = "un-altro-token-completamente-diverso-99"
        try:
            c2 = self.app().test_client()
            c2.set_cookie("sid", cookie)
            self.assertEqual(c2.get("/api/stato").status_code, 401)
        finally:
            TestWeb.TOKEN = vecchio

    def test_logout_sopravvive_al_riavvio(self):
        app = self.app()
        c = self.entra(app)
        cookie = c.get_cookie("sid").value
        c.post("/logout", json={})
        self.assertEqual(c.get("/api/stato").status_code, 401)
        web.REVOCATE.clear()                                   # "riavvio": la memoria si perde, il file no
        c2 = self.app().test_client()
        c2.set_cookie("sid", cookie)
        self.assertEqual(c2.get("/api/stato").status_code, 401)

    def test_il_cookie_non_dipende_dal_token(self):
        # la chiave di firma e' un file casuale distinto dal token
        self.entra(self.app())
        self.assertTrue(web.FILE_CHIAVE.exists())
        self.assertEqual(oct(web.FILE_CHIAVE.stat().st_mode)[-3:], "600")

    def test_ripresa_di_sessione_corrotta_o_gia_aperta(self):
        c = self.entra(self.app([testo("uno"), testo("due")]))
        (web.BASE / "sessions").mkdir(exist_ok=True)
        (web.BASE / "sessions" / "20260101-000000.json").write_text('{"contents": [{"ro')
        self.assertEqual(c.post("/api/nuovo", json={"obiettivo": "x", "sid": "20260101-000000"}).status_code, 422)
        c.post("/api/nuovo", json={"obiettivo": "primo"})
        st = self.aspetta(c, lambda s: s["runs"] and not s["runs"][0]["attivo"])
        self.assertEqual(c.post("/api/nuovo", json={"obiettivo": "x", "sid": st["runs"][0]["sid"]}).status_code, 409)

    def test_nonce_interno_solo_con_la_chiave_e_non_da_tailscale(self):
        import hashlib
        app = self.app()
        c = app.test_client()
        chiave = hashlib.sha256(web.FILE_CHIAVE.read_bytes()).hexdigest()
        self.assertEqual(c.post("/interno/nonce").status_code, 403)                                         # senza chiave
        self.assertEqual(c.post("/interno/nonce", headers={"X-Interno": "sbagliata"}).status_code, 403)
        self.assertEqual(c.post("/interno/nonce", headers={"X-Interno": chiave, "X-Forwarded-For": "1.2.3.4"}).status_code, 403)   # da proxy
        self.assertEqual(c.post("/interno/nonce", headers={"X-Interno": chiave, "Tailscale-User-Login": "x@y"}).status_code, 403)
        nonce = c.post("/interno/nonce", headers={"X-Interno": chiave}).get_json()["nonce"]
        r = c.get("/entra?c=" + nonce)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(c.get("/api/stato").status_code, 200)                                              # ora e' autenticato
        self.assertEqual(app.test_client().get("/entra?c=" + nonce).status_code, 403)                       # monouso

    def _audio(self):
        f = self.tmp / "voce.webm"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=300:duration=1", "-c:a", "libopus", str(f)], check=True)
        return f.read_bytes()

    def test_voce_endpoint_sicurezza_e_successo(self):
        app = self.app([testo("Crea il file prova")])
        anon = app.test_client()
        self.assertEqual(anon.post("/api/voce", data=b"x", headers={"X-Requested-With": "gemini-agent"}).status_code, 401)
        c = self.entra(app)
        self.assertEqual(c.post("/api/voce", data=b"x").status_code, 400)                    # senza header: niente (difesa CSRF)
        self.assertEqual(c.post("/api/voce", data=b"non audio", headers={"X-Requested-With": "gemini-agent"}).status_code, 422)
        self.assertEqual(c.post("/api/voce", data=b"a" * (12 * 1024 * 1024), headers={"X-Requested-With": "gemini-agent"}).status_code, 413)
        r = c.post("/api/voce", data=self._audio(), headers={"X-Requested-With": "gemini-agent"})
        self.assertEqual((r.status_code, r.get_json()["testo"]), (200, "Crea il file prova"))
        self.assertEqual(audit.ultime(1)[0]["strumento"], "voce")
        audit.imposta_blocco(True)
        self.assertEqual(c.post("/api/voce", data=self._audio(), headers={"X-Requested-With": "gemini-agent"}).status_code, 423)

    def test_voce_quota_finita_messaggio_chiaro(self):
        from test_deterministici import quota_giornaliera
        c = self.entra(self.app([quota_giornaliera(), quota_giornaliera()]))
        r = c.post("/api/voce", data=self._audio(), headers={"X-Requested-With": "gemini-agent"})
        self.assertEqual(r.status_code, 422)
        self.assertIn("Scrivi a mano", r.get_json()["errore"])

    def test_il_blocco_non_va_in_crash_con_run_concorrenti(self):
        c = self.entra(self.app([testo("x")] * 50))
        errori = []

        def lancia():
            for _ in range(10):
                try:
                    c.post("/api/nuovo", json={"obiettivo": "x"})
                except Exception as e:
                    errori.append(e)
        th = threading.Thread(target=lancia)
        th.start()
        for _ in range(20):
            self.assertEqual(c.post("/api/blocco", json={"attivo": True}).status_code, 200)
            c.post("/api/blocco", json={"attivo": False})
        th.join()
        self.assertEqual(errori, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
