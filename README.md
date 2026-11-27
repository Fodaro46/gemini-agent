# gemini-agent

Agente locale che usa le API di Gemini (piano gratuito di Google AI Studio) per lavorare su una cartella del tuo PC:
scrive e modifica file, esegue comandi (con la tua conferma), cerca sul web, legge pagine, e puo' lanciare
2-3 sotto-agenti in parallelo. Si usa da terminale o dal telefono (pagina web sulla tua Wi-Fi).
Tutti i segreti stanno in **Infisical** e vengono letti ad ogni avvio, solo in memoria.

## Installazione

```bash
npm install -g @infisical/cli        # CLI di Infisical
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Segreti in Infisical (Cloud EU), passo passo

1. Accedi a https://eu.infisical.com, apri il progetto e l'ambiente `dev`.
2. Crea questi segreti:
   | Nome | Cosa |
   |---|---|
   | `GEMINI_API_KEY` | chiave di Google AI Studio |
   | `BRAVE_API_KEY` | *(facoltativo)* chiave Brave Search API: se c'e', `web_search` la usa per prima |
   | `AGENT_WEB_TOKEN` | stringa casuale lunga (>=16 caratteri), la password della pagina web |
3. Accedi dalla CLI (il token resta nel keyring del sistema, mai in file):
   `infisical login --domain=https://eu.infisical.com`
4. Collega la cartella al progetto: `infisical init --project-id <ID> --domain https://eu.infisical.com`
   (l'ID: `infisical secrets-manager projects get -o json`). Crea `.infisical.yaml`, che contiene solo l'ID.
5. Nomi, ambiente e dominio sono in `config.json` (sezione `infisical`, `brave_secret`, `web`).

Se Infisical non risponde o un segreto manca, il programma si ferma con un messaggio chiaro e **non chiede mai la chiave in chiaro**.
Alternativa a `infisical init`: `infisical secrets-manager run -- python main.py` (la CLI inietta i segreti come variabili
d'ambiente; qui non lo usiamo perche' un comando shell dell'agente potrebbe leggerle da `/proc`).

## Avvio

```bash
.venv/bin/python main.py                      # terminale
.venv/bin/python main.py --ruolo ricercatore  # con un ruolo (vedi config.json)
.venv/bin/python main.py --resume <ID>        # riprende una sessione (sessions/)
.venv/bin/python web.py                       # pagina web: stampa l'indirizzo da aprire dal telefono
```

Esempi di obiettivi utili: "riassumi tutti i PDF/markdown nel workspace in un indice", "scrivi i test per
questo codice e fai passare la suite", "confronta 3 librerie per X cercando sul web e scrivi report.md"
(usa `delega_agenti`: un sotto-agente per libreria).

## Pagina web (telefono)

- **Agenti**: scrivi un obiettivo, scegli un ruolo (o un esempio) e premi Avvia. Ogni agente e' una scheda con i passi in italiano ("Scrive report.md", "Esegue: python3 x.py"), il risultato formattato e i pulsanti Ferma / Continua / Copia.
- **Conferme**: quando serve (shell, sovrascrittura, cancellazione) compare una barra fissa in basso con il comando e due grandi pulsanti Approva / Rifiuta; il titolo della scheda diventa "(1) Conferma" e il telefono vibra. Se non rispondi entro `confirm_timeout_s` (10 min) viene rifiutata. Se rifiuti, l'agente non ritenta lo stesso comando.
- **Cronologia**: le sessioni salvate; "Riprendi" continua una sessione da dove era rimasta.
- **File prodotti**: elenco dei file del workspace con anteprima di testo (sola lettura, solo dentro il workspace).
- Login con il token `AGENT_WEB_TOKEN` (da Infisical); poi cookie di sessione casuale. 5 errori = blocco di un minuto. Nell'app desktop il login e' automatico (codice monouso valido 30 s, solo da localhost). Se riavvii `web.py` devi rifare il login e gli agenti in memoria spariscono (le sessioni restano in `sessions/`).
- Il server ascolta **solo su questo PC** (127.0.0.1): nessuno sulla tua rete puo' raggiungerlo. Per usarlo dal telefono fuori casa vedi "Accesso da fuori".

## Verifica automatica (anti-allucinazioni) e test
Ogni risposta viene confrontata con quello che gli strumenti hanno DAVVERO fatto (`verifica.py`, regole fisse, nessun altro modello): se il modello dice "ho creato il file X" ma non risulta nessuna scrittura riuscita, o il file non esiste, l'agente riceve una contestazione e deve eseguire l'azione o correggersi (max 2 volte); se resta una menzogna la pagina mostra "Non verificato" con i motivi. Una risposta vuota dopo l'uso di strumenti viene contestata. Dentro gli strumenti: dopo ogni scrittura/modifica/cancellazione il file viene riletto ("verificato").
- Test deterministici (modello finto, senza rete): `.venv/bin/python -m unittest discover -s tests -v`
- Prove sul modello di riserva con esiti oggettivi: `.venv/bin/python evals/qwen_eval.py --rep 3` (risultati in `evals/ultimo_risultato.json`)

## Allegati (immagini, PDF, file di testo)
Graffetta nella casella di scrittura, oppure incolla (Ctrl+V) o trascina i file sulla casella. Fino a 5 file per messaggio, 10 MB ciascuno, 20 MB in tutto. Il tipo si riconosce dai byte del file, non dal nome. Le immagini vengono ricodificate in JPEG (via EXIF e posizione GPS) e ridotte a 2048 px; HEIC non e' supportato (l'iPhone di solito converte in JPEG da solo). Gemini legge immagini e PDF; Qwen locale legge le immagini (ha la visione) e i PDF come testo (`pdftotext`), con le prime pagine come immagini se il PDF e' scansionato. I file restano nella cronologia della conversazione per le domande successive, vengono copiati in `allegati/` del workspace (tranne con "home" e "tutto il PC") e si cancellano dopo 14 giorni. Lo strumento `estrai_testo` legge un PDF del workspace.

## Voce
Microfono nella casella di scrittura: registra, trascrive (ffmpeg + Gemini, 1 richiesta per messaggio) e mette il testo nella casella. "Voce: invia subito" nel menu fa partire il messaggio da solo. "Ascolta" sotto una risposta la legge a voce (sintesi del browser). Serve HTTPS (l'indirizzo Tailscale va bene) e il permesso del microfono per il sito. L'audio va ai server del provider (Google, nel piano gratuito).

## Installare pacchetti (sudo controllato)
`sudo` nella shell resta rifiutato. Per installare programmi (per esempio `xdotool`, che serve a mouse e tastiera) c'e' lo strumento `installa_pacchetto`: chiede SEMPRE la tua conferma dalla pagina, anche in modalita' "Mai". Si abilita UNA volta sul PC:
```bash
sudo bash scripts/abilita_installazione_pacchetti.sh
```
Crea `/usr/local/sbin/gemini-agent-apt` (accetta solo `apt install <nome>` o `update`, nomi validati, niente opzioni) e una regola in `/etc/sudoers.d/gemini-agent` che permette solo al tuo utente di lanciare quello script senza password. Per annullare: lo stesso comando con `--rimuovi`.

## Controllo del PC (occhi e mani)
Strumenti: `screenshot` (lo schermo, che Gemini legge), `finestre`, `stato_pc` (carico, memoria, dischi, batteria, GPU), `apri` (URL o app) e `controlla_input` (mouse e tastiera; serve `sudo apt install xdotool`). Con la cartella "tutto il PC (/)" l'agente puo' lavorare ovunque; restano protetti `.ssh`, `.config`, chiavi, `.env`, `/proc`, `/root`, `/dev`... e `sudo` e' sempre bloccato.
Screenshot, apri e input chiedono conferma in "Chiedi sempre" e "Se rischioso"; in "Mai" partono da soli. Gli screenshot vengono inviati al provider del modello (Google, nel piano gratuito).
**Controlli dal telefono:** *Registro* (ogni azione degli agenti, con orario e esito), *Blocca tutto* (ferma subito gli agenti e impedisce di avviarne altri, anche dopo un riavvio; si toglie dallo stesso menu) e la scelta delle conferme.

## Avvio automatico (servizio)

```bash
.venv/bin/python web.py --servizio     # installa e avvia il servizio utente "gemini-agent"
systemctl --user status gemini-agent   # stato
systemctl --user restart gemini-agent  # riavvio (serve dopo aver cambiato il token in Infisical)
journalctl --user -u gemini-agent -f   # log
```
Parte da solo all'accesso (e all'avvio, con il "linger" attivo), senza finestra, e si riavvia se cade. Funziona anche con Claude chiuso. Il PC deve essere acceso e non in sospensione.

## Modalita' di conferma
Selettore sotto il campo di scrittura (si ricorda l'ultima scelta): **Chiedi sempre**, **Comodo** (comandi innocui e script nel workspace partono da soli), **Tutto automatico**. In ogni modalita' restano rifiutati `sudo`, `rm` su percorsi radice, `mkfs`, `dd`, `curl | sh` ecc.; il `git push` chiede SEMPRE conferma.

## Altre cartelle di lavoro e git
In `config.json`, sezione `cartelle`: `{"mio-progetto": "/percorso/assoluto"}`. Appare un selettore nella pagina; il browser puo' scegliere solo tra i nomi elencati, mai percorsi liberi. Lo strumento `git` (status, diff, log, add, commit, push) lavora sulla cartella scelta e usa le tue credenziali git solo dentro il processo git: il modello non le vede.

## App desktop

```bash
.venv/bin/python web.py --installa    # crea "Gemini Agent" nel menu delle applicazioni (con icona)
.venv/bin/python web.py --finestra    # oppure avvia subito la finestra
```
Apre una finestra senza barra del browser, gia' collegata. Usa un profilo separato (`~/.local/share/gemini-agent/profilo`), quindi non tocca il tuo browser. Chiudendo la finestra l'app si ferma. Il progetto deve stare in una cartella permanente: la voce del menu punta li'.

## Accesso da fuori (telefono), senza aprire il PC a nessuno

Non aprire porte sul router e non usare `--host 0.0.0.0`. Soluzione consigliata: **Tailscale** (rete privata tra i *tuoi* dispositivi: il PC non e' raggiungibile da internet, solo dai tuoi telefoni/PC con il tuo account). Verifica tu piano e condizioni su tailscale.com.
1. Installa Tailscale sul PC e sul telefono, accedi con lo stesso account (anche con Google: `sudo tailscale up --operator=$USER` stampa un link da aprire nel browser, e il login si fa li').
2. Con l'app avviata: `tailscale serve --bg 8765` (pubblica la pagina in HTTPS **solo** nella tua rete Tailscale; non usare `funnel`, che la aprirebbe a internet).
3. Dal telefono apri l'indirizzo `https://<nome-pc>.<tuo-tailnet>.ts.net` e fai il login con il token. Il token e le conferme restano la seconda barriera.
Tre livelli: rete privata (solo i tuoi dispositivi) + token + conferma esplicita per shell/cancellazioni.

## Sotto-agenti e quota

Un solo limitatore globale (`requests_per_minute`) e' condiviso da tutti gli agenti; errori 429 -> retry con backoff;
a quota giornaliera finita si passa a `fallback_model` e, se finisce anche quella, l'agente si ferma con un messaggio.
`max_steps`, `sub_max_steps`, `max_subagents`, `max_runs_parallel` limitano il consumo.

## Sicurezza

- Strumenti sui file: **solo** dentro il workspace. Shell, sovrascrittura, modifica e cancellazione chiedono conferma (default: no).
- Comandi che nominano Infisical, `/proc`, `.config`, `.ssh` sono bloccati; la shell parte con ambiente minimo; `fetch_url` rifiuta indirizzi locali/privati.
- Il testo di file e pagine web e' dato, non istruzioni (il prompt lo ricorda al modello), ma una prompt injection resta possibile: leggi i comandi prima di approvare.
- **Limite onesto:** la shell non e' un vero sandbox. Un comando che approvi puo' fare tutto cio' che puo' il tuo utente.
- Piano gratuito Google: i contenuti inviati possono essere usati per migliorare i prodotti Google.
- Le sessioni salvate hanno i segreti sostituiti da `[SEGRETO]`.

## Aggiungere uno strumento nuovo

In `tools.py`: funzione `def nome(ctx, arg) -> str`, schema in `DECLARATIONS`, registrazione in `TOOLS`.
Se serve un segreto: `from secrets_infisical import get_secret` e `get_secret("NOME", ctx.cfg)` dentro la funzione
(oppure, per un segreto obbligatorio, caricalo all'avvio come fa `carica_brave`); crea il segreto in Infisical con quel nome.

## Limiti del piano gratuito (verificati il 2026-11-26)

- `gemini-3.6-flash`: 20 richieste/giorno (e' la riserva). `gemini-3.5-flash-lite` e' il default. `gemini-2.5-flash-lite` non e' piu' disponibile ai nuovi utenti.
- Google Search grounding: non disponibile nel piano gratuito (429). La ricerca usa Bing (senza chiave), DuckDuckGo come riserva, e Brave API se aggiungi `BRAVE_API_KEY`. Sono pagine HTML: se cambiano formato, `web_search` torna un errore chiaro.
- Nomi dei modelli e limiti cambiano: controlla `config.json` e aistudio.google.com/rate-limit.
