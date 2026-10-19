#!/usr/bin/env bash
# Configure le Mac mini pour faire tourner le bot 24/7.
# Usage (depuis la racine du projet):  ./deploy/setup_macmini.sh
set -euo pipefail

BOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BOT_USER="$(whoami)"
PLIST_DST="/Library/LaunchDaemons/com.polybot.plist"
PYTHON="${PYTHON:-python3}"

echo "==> Dossier du bot : $BOT_DIR (utilisateur $BOT_USER)"

# --- 1. Energie: ne jamais dormir, redemarrer apres coupure de courant ---
echo "==> Reglages d'alimentation (sudo)"
sudo pmset -a sleep 0             # pas de veille systeme
sudo pmset -a disksleep 0         # pas de mise en veille des disques
sudo pmset -a displaysleep 10     # l'ecran peut s'eteindre, sans impact
sudo pmset -a autorestart 1       # redemarrage automatique apres coupure de courant
sudo pmset -a womp 1              # wake on LAN
sudo pmset -a powernap 0
sudo systemsetup -setrestartfreeze on   # redemarrage auto si le systeme gele
sudo systemsetup -setcomputersleep Never >/dev/null

# --- 2. Environnement Python ---
echo "==> Creation du venv et installation des dependances"
"$PYTHON" -m venv "$BOT_DIR/.venv"
"$BOT_DIR/.venv/bin/pip" install --upgrade pip -q
"$BOT_DIR/.venv/bin/pip" install -r "$BOT_DIR/requirements.txt" -q
mkdir -p "$BOT_DIR/logs" "$BOT_DIR/state"

if [ ! -f "$BOT_DIR/.env" ]; then
    cp "$BOT_DIR/.env.example" "$BOT_DIR/.env"
    chmod 600 "$BOT_DIR/.env"
    echo "!! .env cree depuis .env.example : remplis-le puis relance ce script."
    exit 1
fi
chmod 600 "$BOT_DIR/.env"

# --- 3. Service launchd (demarrage au boot + relance en cas de crash) ---
echo "==> Installation du LaunchDaemon"
sed -e "s|__BOT_DIR__|$BOT_DIR|g" -e "s|__USER__|$BOT_USER|g" \
    "$BOT_DIR/deploy/com.polybot.plist" | sudo tee "$PLIST_DST" >/dev/null
sudo chown root:wheel "$PLIST_DST"
sudo chmod 644 "$PLIST_DST"
sudo launchctl bootout system "$PLIST_DST" 2>/dev/null || true
sudo launchctl bootstrap system "$PLIST_DST"
sudo launchctl enable system/com.polybot

echo
echo "OK. Commandes utiles :"
echo "  sudo launchctl print system/com.polybot | head -30   # etat du service"
echo "  tail -f $BOT_DIR/logs/bot.log                       # logs"
echo "  sudo launchctl kickstart -k system/com.polybot      # redemarrer le bot"
echo "  sudo launchctl bootout system $PLIST_DST             # arreter / desinstaller"
echo
echo "RAPPEL : si FileVault est actif, apres une coupure de courant le Mac reste bloque"
echo "sur l'ecran de deverrouillage et le bot ne redemarre PAS. Voir README."
