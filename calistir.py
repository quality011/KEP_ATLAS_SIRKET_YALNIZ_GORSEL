#!/usr/bin/env python3
"""KEP ATLAS YALNIZ GORSEL BOTU - tek capraz-platform baslatici.

macOS ve Windows'ta ayni sekilde calisir; .cmd/.ps1/.command dosyalarinin
yerini alir. Sanal ortami (.venv) kurar, paketleri yukler, kimlik bilgilerini
(.env / macOS Keychain) hazirlar ve botu calistirir.

Kullanim:
  python3 calistir.py            -> menu
  python3 calistir.py kur        -> kurulum / guncelleme
  python3 calistir.py baslat     -> yeni ID listesiyle calistir
  python3 calistir.py devam      -> kaldigi yerden devam
  python3 calistir.py durum      -> kimlik bilgisi durumu
"""
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

KLASOR = Path(__file__).resolve().parent
if str(KLASOR) not in sys.path:
    sys.path.insert(0, str(KLASOR))

# Botun BASLAT/DEVAM ile ayni varsayilan parametreleri
ORTAK_ARGS = [
    "--gorsel-isci", "3",
    "--gorsel-parti", "8",
    "--kaynak-tur", "4",
    "--gorsel-hata-deneme", "4",
    "--min-genislik", "600",
    "--min-yukseklik", "400",
    "--kabul-esigi", "0.78",
    "--onayla",
]


# --------------------------------------------------------------------------- #
# Yardimcilar
# --------------------------------------------------------------------------- #
def venv_python():
    """Bu klasordeki sanal ortamin python yolu (platforma gore)."""
    if os.name == "nt":
        return KLASOR / ".venv" / "Scripts" / "python.exe"
    return KLASOR / ".venv" / "bin" / "python"


def _uygun_temel_python():
    """venv olusturmak icin 3.11+ bir yorumlayici bulur."""
    if sys.version_info >= (3, 11):
        return sys.executable
    for ad in ("python3.13", "python3.12", "python3.11", "python3", "python"):
        try:
            r = subprocess.run(
                [ad, "-c", "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"],
                capture_output=True,
            )
        except (OSError, ValueError):
            continue
        if r.returncode == 0:
            return ad
    return None


def _paketler_hazir(py):
    r = subprocess.run(
        [str(py), "-c", "import selenium, requests, PIL, pydantic"],
        capture_output=True,
    )
    return r.returncode == 0


def _chrome_kontrol():
    if sys.platform == "darwin":
        if not Path("/Applications/Google Chrome.app").exists():
            print("UYARI: Google Chrome bulunamadi. https://www.google.com/chrome/")
    elif os.name == "nt":
        kokler = [
            os.environ.get("ProgramFiles", ""),
            os.environ.get("ProgramFiles(x86)", ""),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        varmi = any(
            kok and (Path(kok) / "Google" / "Chrome" / "Application" / "chrome.exe").exists()
            for kok in kokler
        )
        if not varmi:
            print("UYARI: Google Chrome bulunamadi. Botu baslatmadan once kurun.")


def _kimlik_hazirla_ve_sor():
    """Kimlik bilgilerini .env/Keychain'den yukler; yalniz eksik olanlari sorar."""
    import getpass

    import kimlik

    kimlik.ortami_hazirla()
    eksik = [a for a in kimlik.ANAHTARLAR if not os.environ.get(a)]
    if not eksik:
        print("Kimlik bilgileri .env / Keychain uzerinden okundu (soru sorulmadi).")
        return
    for anahtar in eksik:
        etiket, gizli = kimlik.ETIKETLER[anahtar]
        if gizli:
            deger = getpass.getpass(f"{etiket} (ekranda gorunmez): ").strip()
        else:
            deger = input(f"{etiket}: ").strip()
        if deger:
            os.environ[anahtar] = deger
    print("Ipucu: 'python3 calistir.py durum' veya menu 4 ile bilgileri")
    print("       macOS Keychain'e kaydedip bir daha sorulmasini engelleyebilirsiniz.")


def _calistir_ve_logla(komut, etiket):
    """Alt sureci canli ekrana yansitir ve ayni anda log dosyasina yazar."""
    (KLASOR / "logs").mkdir(exist_ok=True)
    log = KLASOR / "logs" / f"{etiket}_{datetime.now():%Y%m%d_%H%M%S}.log"
    ortam = os.environ.copy()
    ortam["PYTHONUNBUFFERED"] = "1"
    ortam["PYTHONUTF8"] = "1"
    ortam["PYTHONIOENCODING"] = "utf-8"
    ortam.setdefault("KEP_CHROME_SEKME_LIMITI", "2")
    print(f"\nLog: {log}\n")
    surec = subprocess.Popen(
        [str(p) for p in komut],
        cwd=str(KLASOR),
        env=ortam,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    try:
        with open(log, "w", encoding="utf-8") as lf:
            if surec.stdout is not None:
                for satir in surec.stdout:
                    sys.stdout.write(satir)
                    sys.stdout.flush()
                    lf.write(satir)
                    lf.flush()
        kod = surec.wait()
    except KeyboardInterrupt:
        surec.terminate()
        print("\nDurduruldu. 'python3 calistir.py devam' ile surdurebilirsiniz.")
        return 130
    if kod != 0:
        print(f"\nSurec {kod} hata koduyla durdu. Log: {log}")
    else:
        print(f"\nIslem tamamlandi. Log: {log}")
    return kod


def _id_topla():
    print("6 haneli Kep Atlas ID'lerini yapistirin.")
    print("Boslukla, virgulle veya alt alta girebilirsiniz.")
    print("Tum ID'leri girdikten sonra BOS SATIRDA bir kez daha Enter'a basin.")
    satirlar = []
    while True:
        try:
            satir = input("ID: ")
        except EOFError:
            break
        if not satir.strip():
            break
        satirlar.append(satir)
    ham = " ".join(satirlar)
    benzersiz = []
    for eslesme in re.finditer(r"(?<!\d)\d{6}(?!\d)", ham):
        deger = eslesme.group()
        if deger not in benzersiz:
            benzersiz.append(deger)
    return benzersiz


# --------------------------------------------------------------------------- #
# Komutlar
# --------------------------------------------------------------------------- #
def kurulum():
    py = venv_python()
    if not py.exists():
        temel = _uygun_temel_python()
        if not temel:
            print("HATA: Python 3.11 veya ustu bulunamadi.")
            if sys.platform == "darwin":
                print("  Kurmak icin:  brew install python@3.12")
            else:
                print("  https://www.python.org/downloads/ adresinden kurun.")
            return 2
        print("Bu klasore ozel Python ortami (.venv) olusturuluyor...")
        subprocess.run([temel, "-m", "venv", str(KLASOR / ".venv")], check=True)

    print("Gerekli paketler kuruluyor/guncelleniyor...")
    subprocess.run(
        [str(py), "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", "pip"],
        check=True,
    )
    subprocess.run(
        [str(py), "-m", "pip", "install", "--disable-pip-version-check",
         "-r", str(KLASOR / "requirements.txt")],
        check=True,
    )
    print("Paketler dogrulaniyor...")
    subprocess.run(
        [str(py), "-c", "import selenium, requests, PIL, pydantic; print('Python paketleri hazir.')"],
        check=True,
    )
    print("Kod ve testler kontrol ediliyor...")
    subprocess.run(
        [str(py), "-m", "unittest", "test_yalniz_gorsel_botu.py"],
        cwd=str(KLASOR), check=True,
    )
    _chrome_kontrol()
    print("\nKURULUM BASARILI")
    print("Simdi menuden 2 (yeni liste) veya 3 (devam) secebilirsiniz.")
    return 0


def baslat():
    py = venv_python()
    if not py.exists() or not _paketler_hazir(py):
        print("Once kurulum yapin (menu 1 veya 'python3 calistir.py kur').")
        return 1
    idler = _id_topla()
    if not idler:
        print("Gecerli 6 haneli Kep Atlas ID'si bulunamadi.")
        return 1
    print(f"\nHazirlanan benzersiz ID sayisi: {len(idler)}")
    print("Ayni ID listesini baska bir bilgisayarda ayni anda calistirmayin.")
    if input("Canli yuklemeyi baslatmak icin BASLAT yazin: ").strip() != "BASLAT":
        print("Iptal edildi.")
        return 0
    _kimlik_hazirla_ve_sor()
    komut = [py, KLASOR / "yalniz_gorsel_botu.py", "--idler", " ".join(idler), *ORTAK_ARGS]
    return _calistir_ve_logla(komut, "yalniz_gorsel")


def devam():
    py = venv_python()
    if not py.exists() or not _paketler_hazir(py):
        print("Once kurulum yapin (menu 1 veya 'python3 calistir.py kur').")
        return 1
    _kimlik_hazirla_ve_sor()
    komut = [py, KLASOR / "yalniz_gorsel_devam.py", *ORTAK_ARGS]
    return _calistir_ve_logla(komut, "yalniz_gorsel_devam")


def kimlik_menu():
    import kimlik

    print("\n1) Durumu goster")
    print("2) macOS Keychain'e kaydet")
    print("3) Keychain'den sil")
    print("4) Geri")
    secim = input("Seciminiz [1-4]: ").strip()
    if secim == "1":
        kimlik.main(["--durum"])
    elif secim == "2":
        kimlik.main(["--kur"])
    elif secim == "3":
        kimlik.main(["--sil"])


def menu():
    while True:
        print("\n" + "=" * 60)
        print("KEP ATLAS - SIRKET YALNIZ GORSEL BOTU")
        print("=" * 60)
        print("1) Kurulum / guncelleme")
        print("2) Yeni ID listesi ile calistir")
        print("3) Kaldigi yerden devam et")
        print("4) Kimlik bilgileri (kaydet / durum)")
        print("5) Cikis")
        secim = input("Seciminiz [1-5]: ").strip().lower()
        if secim == "1":
            kurulum()
        elif secim == "2":
            baslat()
        elif secim == "3":
            devam()
        elif secim == "4":
            kimlik_menu()
        elif secim in ("5", "q", "cikis", ""):
            return 0
        else:
            print("Gecersiz secim.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return menu()
    komut = argv[0].lower()
    if komut in ("kur", "kurulum", "setup"):
        return kurulum()
    if komut in ("baslat", "basla", "run", "start"):
        return baslat()
    if komut in ("devam", "continue"):
        return devam()
    if komut in ("durum", "status"):
        import kimlik
        return kimlik.main(["--durum"])
    if komut in ("-h", "--help", "help", "yardim"):
        print(__doc__)
        return 0
    print(f"Bilinmeyen komut: {komut}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
