#!/usr/bin/env bash
# Instalacja bota na serwerze Ubuntu 24.04 (Scaleway). Uruchamiaj jako root:
#   bash setup_server.sh
# Skrypt można uruchamiać wielokrotnie: pomija kroki już wykonane, a przy kolejnym
# uruchomieniu pobiera najnowszy kod z GitHuba.
set -euo pipefail

REPO_SSH="git@github.com:Buberq/Tradingbot.git"
REPO_SETTINGS_URL="https://github.com/Buberq/Tradingbot/settings/keys/new"
APP_USER="krakenbot"
APP_HOME="/opt/krakenbot"
APP_DIR="$APP_HOME/app"
DEPLOY_KEY="$APP_HOME/.ssh/github_deploy"
# Oficjalny klucz hosta GitHub (ed25519), przypięty, żeby nie ufać pierwszemu połączeniu.
GITHUB_HOSTKEY="github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"

say()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m    OK: %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mBŁĄD: %s\033[0m\n' "$*" >&2; exit 1; }
as_app() { runuser -u "$APP_USER" -- "$@"; }

[[ $(id -u) -eq 0 ]] || die "Uruchom jako root (na Scaleway logujesz się jako root)."
. /etc/os-release
[[ ${ID:-} == ubuntu ]] || die "Skrypt jest przygotowany pod Ubuntu, wykryto: ${PRETTY_NAME:-?}"

say "1/7 Pakiety systemowe i automatyczne poprawki bezpieczeństwa"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip git unattended-upgrades sqlite3 >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
  || die "Potrzebny Python 3.12+, jest $(python3 --version)"
ok "$(python3 --version), git $(git --version | cut -d' ' -f3)"

say "2/7 Zegar systemowy (nonce Krakena zależy od czasu)"
timedatectl set-ntp true || true
case "$(timedatectl show -p NTPSynchronized --value 2>/dev/null || true)" in
  yes) ok "zegar zsynchronizowany (NTP)" ;;
  *)   ok "synchronizacja NTP włączona, w toku" ;;
esac

say "3/7 Użytkownik systemowy $APP_USER (bot nie działa jako root)"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$APP_HOME" --shell /usr/sbin/nologin "$APP_USER"
fi
chmod 750 "$APP_HOME"
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$APP_HOME/.ssh"
ok "użytkownik $APP_USER, katalog $APP_HOME"

say "4/7 Klucz do pobierania kodu z GitHuba (tylko odczyt, tylko to repozytorium)"
if [[ ! -f $DEPLOY_KEY ]]; then
  as_app ssh-keygen -q -t ed25519 -N "" -C "krakenbot-deploy@$(hostname)" -f "$DEPLOY_KEY"
fi
echo "$GITHUB_HOSTKEY" > "$APP_HOME/.ssh/known_hosts"
cat > "$APP_HOME/.ssh/config" <<EOF
Host github.com
  IdentityFile $DEPLOY_KEY
  IdentitiesOnly yes
  StrictHostKeyChecking yes
EOF
chown "$APP_USER:$APP_USER" "$APP_HOME/.ssh/known_hosts" "$APP_HOME/.ssh/config"
chmod 600 "$APP_HOME/.ssh/config" "$APP_HOME/.ssh/known_hosts"

# GitHub odpowiada na `ssh -T` kodem 1 także przy poprawnym kluczu, więc liczy się treść.
github_ok() {
  local out
  out=$(as_app ssh -T git@github.com 2>&1 || true)
  grep -q "successfully authenticated" <<<"$out"
}
if ! github_ok; then
  cat <<EOF

    Dodaj ten klucz w GitHubie jako Deploy key repozytorium:
      $REPO_SETTINGS_URL
    Title: krakenbot-server, NIE zaznaczaj "Allow write access", kliknij "Add key".

$(cat "$DEPLOY_KEY.pub")

EOF
  until github_ok; do
    read -r -p "    Naciśnij Enter, gdy klucz będzie dodany w GitHubie... " _
    github_ok || echo "    GitHub jeszcze nie przyjmuje klucza. Sprawdź, czy wkleiłeś całą linię."
  done
fi
ok "GitHub przyjmuje klucz serwera"

say "5/7 Kod bota"
if [[ -d $APP_DIR/.git ]]; then
  as_app git -C "$APP_DIR" pull --ff-only -q
else
  as_app git clone -q "$REPO_SSH" "$APP_DIR"
fi
ok "commit $(as_app git -C "$APP_DIR" log -1 --format='%h %s')"

say "6/7 Środowisko Python i testy"
[[ -d $APP_DIR/.venv ]] || as_app python3 -m venv "$APP_DIR/.venv"
as_app "$APP_DIR/.venv/bin/pip" install -q --upgrade pip
as_app "$APP_DIR/.venv/bin/pip" install -q -e "$APP_DIR[dev]"
(cd "$APP_DIR" && as_app "$APP_DIR/.venv/bin/python" -m pytest -q -p no:cacheprovider) | tail -1
ok "testy przeszły na serwerze"

say "7/7 Konfiguracja i klucz Kraken tylko do odczytu"
if [[ ! -f $APP_DIR/config/config.yaml ]]; then
  as_app cp "$APP_DIR/config/config.example.yaml" "$APP_DIR/config/config.yaml"
fi
ENV_FILE="$APP_DIR/.env"
if [[ ! -f $ENV_FILE ]] || ! grep -q '^KRAKEN_READ_API_KEY=.' "$ENV_FILE"; then
  echo "    Wklej klucz z Krakena. Wpisywany tekst nie będzie widoczny (to celowe)."
  read -r -s -p "    KRAKEN_READ_API_KEY (API Key): " KEY; echo
  read -r -s -p "    KRAKEN_READ_API_SECRET (Private Key): " SECRET; echo
  [[ -n $KEY && -n $SECRET ]] || die "Klucz i sekret nie mogą być puste."
  (
    umask 077
    {
      echo "# Sekrety bota. Uprawnienia 600, tylko użytkownik $APP_USER."
      echo "KRAKEN_READ_API_KEY=$KEY"
      echo "KRAKEN_READ_API_SECRET=$SECRET"
    } > "$ENV_FILE"
  )
  unset KEY SECRET
fi
chown "$APP_USER:$APP_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "plik .env z uprawnieniami $(stat -c %a "$ENV_FILE")"

say "Publiczny adres IPv4 tego serwera (ma być na whiteliście klucza)"
as_app "$APP_DIR/.venv/bin/python" - <<'EOF' || true
from krakenbot.kraken_client import make_transport
try:
    r = make_transport(4)("GET", "https://api.ipify.org", {"User-Agent": "krakenbot"}, None, 10)
    print("    " + r.body.decode())
except Exception as exc:
    print(f"    nie udało się ustalić ({exc})")
EOF

say "Test Krakena: saldo z serwera + kontrola, że inny adres jest odrzucany"
cd "$APP_DIR"
as_app .venv/bin/krakenbot check-config
set +e
as_app .venv/bin/krakenbot check-kraken --ipv6-negative-test
rc=$?
as_app .venv/bin/krakenbot portfolio
set -e

cat <<EOF

Gotowe. Wynik testów jest powyżej i w $APP_DIR/logs/krakenbot.log.
Ponowne komendy (z katalogu $APP_DIR):
  sudo -u $APP_USER .venv/bin/krakenbot check-kraken --ipv6-negative-test
  sudo -u $APP_USER .venv/bin/krakenbot portfolio
EOF
exit $rc
