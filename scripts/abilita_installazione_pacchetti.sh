#!/bin/bash
# Abilita l'installazione di pacchetti apt per l'agente, in modo CONTROLLATO.
#
#   Abilitare:   sudo bash scripts/abilita_installazione_pacchetti.sh
#   Disabilitare: sudo bash scripts/abilita_installazione_pacchetti.sh --rimuovi
#
# Cosa fa (leggilo prima di lanciarlo):
#  1. crea /usr/local/sbin/gemini-agent-apt, uno script di root che accetta SOLO "install <pacchetto>" o "update",
#     controlla che il nome del pacchetto sia fatto di lettere minuscole, numeri e + . - (niente opzioni), e poi chiama apt-get.
#  2. crea /etc/sudoers.d/gemini-agent che permette al TUO utente di lanciare SOLO quello script, senza password.
#  Nient'altro: l'agente NON ottiene "sudo" libero. Ogni installazione chiede comunque la tua conferma dalla pagina.
set -eu
if [ "$(id -u)" -ne 0 ]; then
  echo "Serve root: lancia  sudo bash $0" >&2
  exit 1
fi
UTENTE="${SUDO_USER:-}"
SCRIPT=/usr/local/sbin/gemini-agent-apt
SUDOERS=/etc/sudoers.d/gemini-agent

if [ "${1:-}" = "--rimuovi" ]; then
  rm -f "$SCRIPT" "$SUDOERS"
  echo "Disabilitato: rimossi $SCRIPT e $SUDOERS"
  exit 0
fi
if [ -z "$UTENTE" ] || [ "$UTENTE" = "root" ]; then
  echo "Lancia lo script con 'sudo' da un utente normale (serve sapere per chi abilitare)." >&2
  exit 1
fi

cat > "$SCRIPT" <<'EOF'
#!/bin/sh
# Installa UN pacchetto apt dopo aver validato il nome. Chiamato dall'agente tramite sudo (vedi /etc/sudoers.d/gemini-agent).
set -eu
export DEBIAN_FRONTEND=noninteractive
if [ "$#" -eq 1 ] && [ "$1" = "update" ]; then
  exec /usr/bin/apt-get update
fi
if [ "$#" -ne 2 ] || [ "$1" != "install" ]; then
  echo "uso: gemini-agent-apt install <pacchetto> | update" >&2
  exit 2
fi
case "$2" in
  ""|-*|*-|*+|*.|*[!a-z0-9+.-]*) echo "nome pacchetto non valido" >&2; exit 2 ;;
esac
if [ "${#2}" -gt 64 ]; then
  echo "nome pacchetto troppo lungo" >&2
  exit 2
fi
# il nome deve essere ESATTO: apt tratta alcuni nomi (con . o +) come espressioni regolari e un '-' finale come rimozione
if ! /usr/bin/apt-cache policy -- "$2" 2>/dev/null | grep -q '^  Candidate: [^(]'; then
  echo "pacchetto non trovato" >&2
  exit 2
fi
exec /usr/bin/apt-get install -y --no-install-recommends -- "$2"
EOF
chown root:root "$SCRIPT"
chmod 0755 "$SCRIPT"

TMP="$(mktemp)"
echo "$UTENTE ALL=(root) NOPASSWD: $SCRIPT" > "$TMP"
if ! visudo -cf "$TMP" >/dev/null; then
  rm -f "$TMP"
  echo "Errore: la regola sudoers non e' valida, non installata." >&2
  exit 1
fi
install -m 0440 -o root -g root "$TMP" "$SUDOERS"
rm -f "$TMP"
echo "Abilitato per l'utente '$UTENTE': l'agente puo' chiedere 'installa pacchetto' (con la tua conferma dalla pagina)."
echo "Per annullare: sudo bash $0 --rimuovi"
