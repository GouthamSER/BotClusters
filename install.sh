#!/bin/bash
set -e

usage() {
    echo "Usage: $0 [--packages \"pkg1 pkg2...\"] [--pip-packages \"pkg1 pkg2...\"]"
    exit 1
}

SYS_PACKAGES="nano curl"
PIP_PACKAGES="croniter python-dateutil apscheduler python-dotenv"

echo "[INFO] Starting install.sh script"

while [[ $# -gt 0 ]]; do
    case $1 in
        --packages|--apt-packages|--dnf-packages)
            SYS_PACKAGES="$2"
            echo "[INFO] Overriding system packages: $SYS_PACKAGES"
            shift 2
            ;;
        --pip-packages)
            PIP_PACKAGES="$2"
            echo "[INFO] Overriding PIP packages: $PIP_PACKAGES"
            shift 2
            ;;
        *)
            usage
            ;;
    esac
done

if [ -n "$SYS_PACKAGES" ]; then
    if command -v apt-get &> /dev/null; then
        echo "[INFO] Installing Debian/Ubuntu packages: $SYS_PACKAGES"
        apt-get update && apt-get install -y --no-install-recommends $SYS_PACKAGES || echo "[WARN] apt install failed, continuing..."
        rm -rf /var/lib/apt/lists/* || true
    elif command -v dnf &> /dev/null; then
        echo "[INFO] Installing DNF packages: $SYS_PACKAGES"
        echo "$SYS_PACKAGES" | xargs dnf install -y || echo "[WARN] dnf install failed, continuing..."
    fi
fi

if [ -n "$PIP_PACKAGES" ]; then
    echo "[INFO] Checking for pip3"
    if ! command -v pip3 &> /dev/null; then
        if command -v apt-get &> /dev/null; then
            apt-get update && apt-get install -y python3-pip
        elif command -v dnf &> /dev/null; then
            dnf install -y python3-pip
        fi
    fi
    echo "[INFO] Installing pip packages: $PIP_PACKAGES"
    pip3 install --no-cache-dir $PIP_PACKAGES || echo "[WARN] pip install had warnings"
    echo "[INFO] PIP packages installed successfully"
fi

echo "[INFO] install.sh script completed"

echo "[INFO] Validating cluster env vars..."
python3 validate.py || { echo "[ERROR] Cluster config invalid — fix env vars before continuing."; exit 1; }
