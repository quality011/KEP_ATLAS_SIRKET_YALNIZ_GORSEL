#!/bin/bash
# KEP ATLAS SIRKET YALNIZ GORSEL BOTU - MAC KURULUM
# Windows'taki 1_KURULUM_YAP.cmd + KURULUM.ps1 karsiligidir.
set -euo pipefail
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

KLASOR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$KLASOR"

echo "============================================================"
echo "KEP ATLAS SIRKET YALNIZ GORSEL BOTU - KURULUM (macOS)"
echo "============================================================"

# 1) Python 3.11+ bul
PY=""
for aday in python3.13 python3.12 python3.11 python3; do
    if command -v "$aday" >/dev/null 2>&1; then
        if "$aday" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
            PY="$aday"; break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo "HATA: Python 3.11 veya ustu bulunamadi." >&2
    echo "  Kurmak icin:  brew install python@3.12" >&2
    echo "  Alternatif:   https://www.python.org/downloads/macos/" >&2
    exit 2
fi
echo "Python bulundu: $($PY --version)"

# 2) Google Chrome kontrolu
if [ ! -d "/Applications/Google Chrome.app" ]; then
    echo "UYARI: Google Chrome bulunamadi. Botu baslatmadan once Chrome kurun:" >&2
    echo "  https://www.google.com/chrome/" >&2
fi

# 3) Sanal ortam
if [ ! -x ".venv/bin/python" ]; then
    echo "Bu klasore ozel Python ortami olusturuluyor..."
    "$PY" -m venv .venv
fi
VENV_PY=".venv/bin/python"

# 4) Paketler
echo "Gerekli paketler kuruluyor/guncelleniyor..."
"$VENV_PY" -m pip install --disable-pip-version-check --upgrade pip
"$VENV_PY" -m pip install --disable-pip-version-check -r requirements.txt

# 5) Dogrulama
echo "Kurulum ve kodlar test ediliyor..."
"$VENV_PY" -c "import selenium, requests, PIL, pydantic; print('Python paketleri hazir.')"
"$VENV_PY" -m py_compile *.py
"$VENV_PY" -m unittest test_yalniz_gorsel_botu.py

echo ""
echo "KURULUM BASARILI"
echo "Simdi mac_baslat.command dosyasini calistirabilirsiniz."
echo "Sifreler ve API anahtarlari bu pakete kaydedilmez."
