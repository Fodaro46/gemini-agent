"""Lettura dei segreti da Infisical.

La chiave NON viene mai scritta su disco e NON passa da variabili d'ambiente:
usiamo la CLI `infisical` (gia' loggata col tuo account, token nel keyring)
e leggiamo il valore dallo stdout, solo in memoria.

Per aggiungere un nuovo strumento che richiede un segreto basta:
    chiave = get_secret("NOME_SEGRETO")
"""
import subprocess
import shutil


class SecretError(RuntimeError):
    """Errore chiaro quando Infisical non risponde o il segreto manca."""


def get_secret(name: str, cfg: dict) -> str:
    """Legge un segreto da Infisical e lo restituisce (solo in memoria)."""
    if not shutil.which("infisical"):
        raise SecretError("CLI 'infisical' non trovata nel PATH. Vedi README (installazione).")
    inf = cfg["infisical"]
    cmd = [
        "infisical", "secrets-manager", "secrets", "get", name,
        "--plain", "--silent",
        "--domain", inf["domain"],
        "--env", inf["env"],
        "--path", inf.get("path", "/"),
    ]
    try:
        # Il progetto viene da .infisical.yaml (creato da `infisical init`).
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise SecretError("Infisical non risponde (timeout). Mi fermo: controlla la rete.")
    value = r.stdout.strip()
    if r.returncode != 0 or not value:
        # Mostriamo solo l'errore (stderr), mai il valore.
        err = (r.stderr or "").strip().splitlines()[-1:] or ["errore sconosciuto"]
        raise SecretError(
            f"Impossibile leggere '{name}' da Infisical: {err[0]}\n"
            "Controlla: `infisical login --domain=...`, `infisical init`, nome segreto/ambiente."
        )
    return value
