"""Kimlik bilgisi yonetimi: ortam degiskeni -> .env -> macOS Keychain.

Bot su ucu kullanir: KEP_ATLAS_EMAIL, KEP_ATLAS_PASSWORD, EXA_API_KEY.

Oncelik sirasi (ilk bulunan kazanir):
  1. Zaten tanimli ortam degiskeni (launcher/surec tarafindan verilmis)
  2. Proje klasorundeki .env dosyasi
  3. macOS Anahtar Zinciri (Keychain)  -- yalniz macOS

Boylece sifreler diske duz metin yazilmadan (Keychain) saklanabilir.
Windows'ta Keychain yoktur; orada .env veya ortam degiskeni kullanilir.

Kurulum (macOS):   python3 kimlik.py --kur
Durum:             python3 kimlik.py --durum
Temizle:           python3 kimlik.py --sil
"""
import os
import subprocess
import sys
from pathlib import Path

KLASOR = Path(__file__).resolve().parent
ENV_DOSYASI = KLASOR / ".env"

# Botun kullandigi kimlik anahtarlari
ANAHTARLAR = ("KEP_ATLAS_EMAIL", "KEP_ATLAS_PASSWORD", "EXA_API_KEY")

# Keychain'de bu servis adi altinda saklanir
KEYCHAIN_SERVISI = "kep-atlas-gorsel-botu"

# Kullaniciya gosterilecek etiket ve degerin gizli olup olmadigi
ETIKETLER = {
    "KEP_ATLAS_EMAIL": ("Kep Atlas e-posta", False),
    "KEP_ATLAS_PASSWORD": ("Kep Atlas sifre", True),
    "EXA_API_KEY": ("Exa API anahtari", True),
}


# --------------------------------------------------------------------------- #
# .env destegi
# --------------------------------------------------------------------------- #
def _env_satirlarini_coz(metin):
    """Basit .env cozumleyici (harici bagimlilik yok)."""
    for ham in metin.splitlines():
        satir = ham.strip()
        if not satir or satir.startswith("#"):
            continue
        if satir.lower().startswith("export "):
            satir = satir[7:].lstrip()
        if "=" not in satir:
            continue
        ad, _, deger = satir.partition("=")
        ad = ad.strip()
        deger = deger.strip()
        # Cevreleyen tirnaklari soy
        if len(deger) >= 2 and deger[0] == deger[-1] and deger[0] in ("'", '"'):
            deger = deger[1:-1]
        if ad:
            yield ad, deger


def env_dosyasini_yukle(yol=ENV_DOSYASI, ustune_yazma=False):
    """`.env` dosyasindaki degerleri ortama yukler. Var olanlari ezmez."""
    try:
        metin = Path(yol).read_text(encoding="utf-8-sig")
    except OSError:
        return False
    for ad, deger in _env_satirlarini_coz(metin):
        if ustune_yazma or not os.environ.get(ad):
            os.environ[ad] = deger
    return True


# --------------------------------------------------------------------------- #
# macOS Keychain destegi (yerlesik `security` araci; harici paket yok)
# --------------------------------------------------------------------------- #
def keychain_kullanilabilir():
    return sys.platform == "darwin"


def keychainden_oku(anahtar, servis=KEYCHAIN_SERVISI):
    """macOS Keychain'den tek bir degeri okur; yoksa None."""
    if not keychain_kullanilabilir():
        return None
    try:
        sonuc = subprocess.run(
            ["security", "find-generic-password",
             "-s", servis, "-a", anahtar, "-w"],
            capture_output=True, text=True, check=False,
        )
    except (OSError, ValueError):
        return None
    if sonuc.returncode != 0:
        return None
    deger = sonuc.stdout.rstrip("\n")
    return deger or None


def keychaine_yaz(anahtar, deger, servis=KEYCHAIN_SERVISI):
    """Degeri Keychain'e kaydeder (varsa ustune yazar)."""
    if not keychain_kullanilabilir():
        raise RuntimeError("Keychain yalniz macOS'ta kullanilabilir.")
    subprocess.run(
        ["security", "add-generic-password",
         "-s", servis, "-a", anahtar, "-w", deger, "-U"],
        check=True, capture_output=True, text=True,
    )


def keychainden_sil(anahtar, servis=KEYCHAIN_SERVISI):
    if not keychain_kullanilabilir():
        return False
    sonuc = subprocess.run(
        ["security", "delete-generic-password", "-s", servis, "-a", anahtar],
        check=False, capture_output=True, text=True,
    )
    return sonuc.returncode == 0


def keychainden_yukle(anahtarlar=ANAHTARLAR):
    """Ortamda tanimli olmayan anahtarlari Keychain'den doldurur."""
    if not keychain_kullanilabilir():
        return
    for anahtar in anahtarlar:
        if os.environ.get(anahtar):
            continue
        deger = keychainden_oku(anahtar)
        if deger:
            os.environ[anahtar] = deger


# --------------------------------------------------------------------------- #
# Ana giris: ortami hazirla
# --------------------------------------------------------------------------- #
def ortami_hazirla():
    """Kimlik bilgilerini .env ve (macOS'ta) Keychain'den ortama yukler.

    Oncelik: mevcut ortam degiskeni > .env > Keychain.
    Idempotenttir; birden fazla kez cagirmak zarar vermez.
    """
    env_dosyasini_yukle()
    keychainden_yukle()


# --------------------------------------------------------------------------- #
# Komut satiri: kurulum / durum / silme
# --------------------------------------------------------------------------- #
def _durum():
    ortami_hazirla()
    print("Kimlik bilgisi durumu")
    print("-" * 40)
    print(f".env dosyasi : {'VAR' if ENV_DOSYASI.exists() else 'yok'} ({ENV_DOSYASI})")
    print(f"Keychain     : {'kullanilabilir' if keychain_kullanilabilir() else 'yok (macOS degil)'}")
    print("-" * 40)
    for anahtar in ANAHTARLAR:
        deger = os.environ.get(anahtar, "")
        etiket = ETIKETLER[anahtar][0]
        if not deger:
            durum = "TANIMSIZ"
        elif ETIKETLER[anahtar][1]:
            durum = "tanimli (gizli)"
        else:
            durum = deger
        print(f"{etiket:22}: {durum}")
    return 0


def _kur():
    if not keychain_kullanilabilir():
        print("Bu komut yalniz macOS Keychain icindir.")
        print("Windows/Linux'ta bunun yerine klasorde bir .env dosyasi kullanin.")
        return 2
    import getpass

    print("Kimlik bilgileri macOS Anahtar Zinciri'ne (Keychain) kaydedilecek.")
    print("Bos birakilan alan degistirilmez.\n")
    kaydedilen = 0
    for anahtar in ANAHTARLAR:
        etiket, gizli = ETIKETLER[anahtar]
        if gizli:
            deger = getpass.getpass(f"{etiket} (ekranda gorunmez): ").strip()
        else:
            deger = input(f"{etiket}: ").strip()
        if not deger:
            continue
        keychaine_yaz(anahtar, deger)
        print(f"  kaydedildi: {anahtar}")
        kaydedilen += 1
    print(f"\nTamam. {kaydedilen} deger Keychain'e kaydedildi.")
    print("Bot artik bu bilgileri sormadan calisabilir.")
    return 0


def _sil():
    if not keychain_kullanilabilir():
        print("Keychain yalniz macOS'ta kullanilabilir.")
        return 2
    silinen = 0
    for anahtar in ANAHTARLAR:
        if keychainden_sil(anahtar):
            print(f"  silindi: {anahtar}")
            silinen += 1
    print(f"\n{silinen} deger Keychain'den silindi.")
    return 0


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Kep Atlas bot kimlik bilgisi yonetimi (.env / macOS Keychain)."
    )
    grup = parser.add_mutually_exclusive_group()
    grup.add_argument("--kur", action="store_true",
                      help="Kimlik bilgilerini macOS Keychain'e kaydet")
    grup.add_argument("--durum", action="store_true",
                      help="Hangi kaynaktan hangi bilgi geliyor goster")
    grup.add_argument("--sil", action="store_true",
                      help="Kaydedilen bilgileri Keychain'den sil")
    args = parser.parse_args(argv)

    if args.kur:
        return _kur()
    if args.sil:
        return _sil()
    return _durum()


if __name__ == "__main__":
    sys.exit(main())
