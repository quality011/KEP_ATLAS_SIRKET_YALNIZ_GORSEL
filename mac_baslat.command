#!/bin/bash
# KEP ATLAS - SIRKET YALNIZ GORSEL BOTU (macOS baslatici)
# Windows'taki 2_ID_LISTESINI_CALISTIR.cmd + BASLAT.ps1 karsiligidir.
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
LOG="logs/yalniz_gorsel_$(date +%Y%m%d_%H%M%S).log"

echo "============================================================"
echo "KEP ATLAS - SIRKET YALNIZ GORSEL BOTU"
echo "DOGRUDAN 6 HANELI ID LISTESIYLE CALISIR"
echo "MERKEZI KUMANDA VE OPENAI KULLANMAZ"
echo "============================================================"
echo ""
echo "6 haneli Kep Atlas ID'lerini yapistirin."
echo "Boslukla, virgulle veya alt alta girebilirsiniz."
echo "Tum ID'leri girdikten sonra BOS SATIRDA bir kez daha Enter'a basin."

HAM=""
while IFS= read -r satir; do
    [ -z "$satir" ] && break
    HAM="$HAM $satir"
done

# Benzersiz 6 haneli ID'leri ayikla (sirayi koruyarak)
IDLER="$(printf '%s' "$HAM" | grep -oE '[0-9]{6}' | awk '!g[$0]++' | tr '\n' ' ')"
IDLER="$(echo "$IDLER" | xargs)"
if [ -z "$IDLER" ]; then
    echo "Gecerli 6 haneli Kep Atlas ID'si bulunamadi." >&2
    exit 1
fi
ADET="$(echo "$IDLER" | wc -w | xargs)"

echo ""
echo "Hazirlanan benzersiz ID sayisi: $ADET"
echo "Ayni ID listesini baska bir bilgisayarda ayni anda calistirmayin."
read -r -p "Canli yuklemeyi baslatmak icin BASLAT yazin: " ONAY
[ "$ONAY" = "BASLAT" ] || exit 0

# Kimlik bilgileri: once .env + macOS Keychain denenir, yalniz EKSIK olanlar sorulur.
# Sorulan degerler yalniz bu oturumda bellekte tutulur, diske yazilmaz.
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
    echo "Ipucu: python3 kimlik.py --kur  ile bu bilgileri Keychain'e kaydedip"
    echo "       bir daha sorulmasini engelleyebilirsiniz."
fi

temizle() { for a in $TEMIZLENECEK; do unset "$a"; done; }
trap temizle EXIT

export KEP_CHROME_SEKME_LIMITI=2
# Cikti hem ekrana hem log dosyasina (Start-Transcript karsiligi)
"$PY" yalniz_gorsel_botu.py --idler $IDLER --gorsel-isci 3 --gorsel-parti 8 \
    --kaynak-tur 4 --gorsel-hata-deneme 4 --min-genislik 600 \
    --min-yukseklik 400 --kabul-esigi 0.78 --onayla 2>&1 | tee "$LOG"

echo ""
echo "Islem tamamlandi."
echo "Log: $LOG"
