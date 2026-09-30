#!/usr/bin/env bash
# Set up NervLynx on Raspberry Pi OS (Bookworm or newer) in one go:
#
#   curl -fsSL https://raw.githubusercontent.com/vedantparnaik/nervlynx/main/deploy/pi/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --with ai,voice --hostname my-rover
#
# Installs the GPIO, I2C, serial, and camera libraries from apt, a virtualenv that can see
# them (~/.venv), and NervLynx; turns on I2C and SPI; adds you to the hardware groups;
# lets your services run at boot; and finishes with `nervlynx doctor`. Safe to run again
# to upgrade.
#
# Options:
#   --with ai,voice,mesh   extras: object detection, on-robot voice, Zenoh mesh
#   --hostname NAME        rename the Pi (reach it as NAME.local)
#   --ref REF              NervLynx branch, tag, or commit (default: main)
#   --source PATH_OR_URL   install NervLynx from here instead (a checkout, or a pip URL)
#   --no-interfaces        leave I2C/SPI settings alone
#   --image                building an SD image in a chroot: no services, no reboots
set -euo pipefail

WITH=""
HOSTNAME_NEW=""
REF="${NERVLYNX_REF:-main}"
SOURCE="${NERVLYNX_SOURCE:-}"
INTERFACES=1
IMAGE=0
TARGET_USER="${SUDO_USER:-${USER:-$(id -un)}}"

while [ $# -gt 0 ]; do
  case "$1" in
    --with) WITH="$2"; shift 2 ;;
    --hostname) HOSTNAME_NEW="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --source) SOURCE="$2"; shift 2 ;;
    --no-interfaces) INTERFACES=0; shift ;;
    --image) IMAGE=1; shift ;;
    --user) TARGET_USER="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
has() { command -v "$1" >/dev/null 2>&1; }
SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi
HOME_DIR="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
HOME_DIR="${HOME_DIR:-$HOME}"
VENV="$HOME_DIR/.venv"
as_user() { if [ "$(id -un)" = "$TARGET_USER" ]; then "$@"; else sudo -u "$TARGET_USER" -H "$@"; fi; }

if ! has apt-get; then
  echo "This installer is for Raspberry Pi OS (or another Debian-based system with apt)." >&2
  exit 1
fi
MODEL="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)"
case "$MODEL" in
  "Raspberry Pi"*) say "Setting up NervLynx on a $MODEL for $TARGET_USER" ;;
  *) warn "this does not look like a Raspberry Pi (${MODEL:-no device-tree model}); installing anyway, hardware steps may be skipped" ;;
esac

say "Installing system packages"
PACKAGES="git rsync python3-venv python3-pip alsa-utils"
for optional in python3-gpiozero python3-lgpio python3-smbus2 python3-serial python3-picamera2 python3-opencv; do
  if apt-cache show "$optional" >/dev/null 2>&1; then PACKAGES="$PACKAGES $optional"; fi
done
case ",$WITH," in *,voice,*) PACKAGES="$PACKAGES espeak-ng" ;; esac
$SUDO apt-get update -qq
# shellcheck disable=SC2086 # the package list is meant to split
DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq $PACKAGES

if [ "$INTERFACES" -eq 1 ] && has raspi-config; then
  say "Turning on I2C and SPI"
  $SUDO raspi-config nonint do_i2c 0 || warn "could not enable I2C"
  $SUDO raspi-config nonint do_spi 0 || warn "could not enable SPI"
fi

say "Adding $TARGET_USER to the hardware groups"
for group in gpio i2c spi dialout video audio; do
  if getent group "$group" >/dev/null; then $SUDO usermod -aG "$group" "$TARGET_USER"; fi
done

say "Creating $VENV (it can see the apt packages above)"
if [ ! -x "$VENV/bin/python" ]; then
  as_user python3 -m venv --system-site-packages "$VENV"
fi
as_user "$VENV/bin/python" -m pip install --quiet --upgrade pip

EXTRAS="$WITH"
case ",$WITH," in *,voice,*) EXTRAS="$(echo "$WITH" | sed -e 's/voice//' -e 's/,,*/,/g' -e 's/^,//' -e 's/,$//')" ;; esac
SUFFIX=""
if [ -n "$EXTRAS" ]; then SUFFIX="[$EXTRAS]"; fi
if [ -z "$SOURCE" ]; then
  SPEC="nervlynx$SUFFIX @ git+https://github.com/vedantparnaik/nervlynx@$REF"
elif [ -d "$SOURCE" ]; then
  SPEC="$(cd "$SOURCE" && pwd)$SUFFIX"
else
  SPEC="$SOURCE"
fi
say "Installing NervLynx ($SPEC)"
as_user "$VENV/bin/pip" install --quiet --upgrade "$SPEC"
case ",$WITH," in *,voice,*) as_user "$VENV/bin/pip" install --quiet --upgrade vosk || warn "vosk did not install; the voice node needs it" ;; esac

PROFILE="$HOME_DIR/.profile"
if ! grep -q 'NervLynx' "$PROFILE" 2>/dev/null; then
  printf '\n# NervLynx\nexport PATH="$HOME/.venv/bin:$PATH"\n' | as_user tee -a "$PROFILE" >/dev/null
fi

say "Letting $TARGET_USER's services start at boot"
if [ "$IMAGE" -eq 1 ]; then
  $SUDO mkdir -p /var/lib/systemd/linger && $SUDO touch "/var/lib/systemd/linger/$TARGET_USER"
elif has loginctl; then
  $SUDO loginctl enable-linger "$TARGET_USER" || warn "could not enable lingering; services will start at login instead of boot"
fi

if [ -n "$HOSTNAME_NEW" ]; then
  say "Renaming this computer to $HOSTNAME_NEW"
  if has raspi-config; then
    $SUDO raspi-config nonint do_hostname "$HOSTNAME_NEW"
  else
    echo "$HOSTNAME_NEW" | $SUDO tee /etc/hostname >/dev/null
    $SUDO sed -i "s/127.0.1.1.*/127.0.1.1\t$HOSTNAME_NEW/" /etc/hosts
  fi
fi

if [ "$IMAGE" -eq 1 ]; then
  say "NervLynx is installed in the image"
  exit 0
fi

say "Checking this machine"
as_user "$VENV/bin/nervlynx" doctor || true

NAME="${HOSTNAME_NEW:-$(hostname)}"
cat <<EOF

NervLynx is installed. Log out and back in (or reboot) so the new groups and PATH apply,
then:

  nervlynx new my-rover && cd my-rover
  nervlynx run --control            # dashboard: http://$NAME.local:9120/

Or from your laptop: nervlynx deploy $TARGET_USER@$NAME.local --remote-nervlynx '~/.venv/bin/nervlynx' --service
EOF
if [ "$INTERFACES" -eq 1 ] && has raspi-config; then
  echo "Reboot once so the I2C and SPI settings take effect: sudo reboot"
fi
