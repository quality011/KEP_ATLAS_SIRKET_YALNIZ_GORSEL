#!/bin/bash
# KEP ATLAS - KALDIGI YERDEN DEVAM (macOS)
# Windows'taki 3_KALDIGI_YERDEN_DEVAM.cmd + DEVAM.ps1 karsiligidir.
set -euo pipefail
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

KLASOR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$KLASOR"

PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
    echo "Kurulum bulunamadi. Once mac_kur.command dosyasini calistirin." >&2
    exit 1
fi
"$PY" -c "import selenium, requests, PIL, pydantic" 2>/dev/null || {
    echo "Python paketleri eksik. mac_kur.command dosyasini yeniden calistirin." >&2
    exit 1
}

mkdir -p logs
LOG="logs/yalniz_gorsel_devam_$(date +%Y%m%d_%H%M%S).log"

echo "============================================================"
echo "KEP ATLAS - KALDIGI YERDEN DEVAM"
echo "============================================================"

# Once .env + macOS Keychain denenir, yalniz EKSIK olanlar sorulur.
EKSIKLER="$("$PY" -c "import kimlik,os;kimlik.ortami_hazirla();print(' '.join(k for k in kimlik.ANAHTARLAR if not os.environ.get(k)))")"
TEMIZLENECEK=""
if [ -z "$EKSIKLER" ]; then
    echo "Kimlik bilgileri .env / Keychain uzerinden okundu (soru sorulmadi)."
else
    for anahtar in $EKSIKLER; do
        case "$anahtar" in
            KEP_ATLAS_EMAIL)
                read -r -p "Kep Atlas e-posta: " KEP_ATLAS_EMAIL
                export KEP_ATLAS_EMAIL ;;
            KEP_ATLAS_PASSWORD)
                read -r -s -p "Kep Atlas sifre (ekranda gorunmez): " KEP_ATLAS_PASSWORD; echo
                export KEP_ATLAS_PASSWORD ;;
            EXA_API_KEY)
                read -r -s -p "Exa API anahtari (ekranda gorunmez): " EXA_API_KEY; echo
                export EXA_API_KEY ;;
        esac
        TEMIZLENECEK="$TEMIZLENECEK $anahtar"
    done
fi
temizle() { for a in $TEMIZLENECEK; do unset "$a"; done; }
trap temizle EXIT

export KEP_CHROME_SEKME_LIMITI=2
"$PY" yalniz_gorsel_devam.py --gorsel-isci 3 --gorsel-parti 8 --kaynak-tur 4 \
    --gorsel-hata-deneme 4 --min-genislik 600 --min-yukseklik 400 \
    --kabul-esigi 0.78 --onayla 2>&1 | tee "$LOG"

echo ""
echo "Islem tamamlandi."
echo "Log: $LOG"
