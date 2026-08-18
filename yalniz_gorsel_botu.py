import argparse
import csv
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import kimlik
from secili_atlas import (
    idleri_ayikla,
    secili_otelleri_atlastan_oku,
    secili_raporunu_yaz,
)
from veritabani import (
    baglan,
    hatali_gorevleri_yeniden_kuyruga_al,
    hazir_gorsel_gorevleri,
    raporu_aktar,
)


def konsol_kodlamasini_ayarla():
    """Windows konsolunda bozuk kaynak metni yuzunden isci thread'inin cokmesini onler."""
    for ad in ("stdout", "stderr"):
        akis = getattr(sys, ad, None)
        yeniden_ayarla = getattr(akis, "reconfigure", None)
        if callable(yeniden_ayarla):
            try:
                yeniden_ayarla(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


konsol_kodlamasini_ayarla()


def guvenli_konsol_metni(metin):
    """reconfigure desteklenmese bile konsol kodlamasinda yazilabilir metin uretir."""
    metin = str(metin)
    kodlama = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        return metin.encode(kodlama, errors="replace").decode(
            kodlama, errors="replace"
        )
    except (LookupError, UnicodeError):
        return metin.encode("ascii", errors="replace").decode("ascii")


KLASOR = Path(__file__).resolve().parent
CALISMA_KLASORU = KLASOR / "yalniz_gorsel_calismalari"


def argumanlari_oku():
    parser = argparse.ArgumentParser(
        description=(
            "Secilen Kep Atlas otellerinde sadece eksik gorselleri, "
            "kaynak aramasi ile paralel yukler."
        )
    )
    parser.add_argument("--idler", default="")
    parser.add_argument("--id-dosyasi", default="")
    parser.add_argument("--min-genislik", type=int, default=600)
    parser.add_argument("--min-yukseklik", type=int, default=400)
    parser.add_argument("--kabul-esigi", type=float, default=0.78)
    parser.add_argument("--gorsel-isci", type=int, default=3)
    parser.add_argument("--gorsel-parti", type=int, default=8)
    parser.add_argument("--kaynak-tur", type=int, default=4)
    parser.add_argument("--gorsel-hata-deneme", type=int, default=4)
    parser.add_argument("--headless-atlas-arama", action="store_true")
    parser.add_argument("--onayla", action="store_true")
    return parser.parse_args()


def secili_gorsel_kuyrugunu_hazirla(db_yolu):
    """Dolu gorselleri atlar; bos otellerde yalniz gorsel alt gorevini acar."""
    with closing(baglan(db_yolu)) as db, db:
        db.execute(
            """
            UPDATE otel_gorevleri
            SET durum = CASE
                    WHEN gorsel_sayisi > 0 THEN 'TAMAMLANDI'
                    ELSE 'KAYNAK_BEKLIYOR' END,
                karar = CASE
                    WHEN gorsel_sayisi > 0 THEN 'ATLA'
                    ELSE 'SADECE_GORSEL' END,
                icerik_gerekli = 0,
                gorsel_gerekli = CASE WHEN gorsel_sayisi > 0 THEN 0 ELSE 1 END,
                kaynak_url = '', kaynak_site = '', kaynak_guven = 0,
                kaynak_adaylari_json = '', kaynak_adres = '', adres_puani = 0,
                atlas_adres = '', adres_kontrol_edildi = 0,
                deneme_sayisi = 0, gorsel_deneme = 0,
                icerik_deneme = 0, genel_deneme = 0,
                son_hata = CASE
                    WHEN gorsel_sayisi > 0 THEN 'GORSEL_ZATEN_VAR_ATLANDI'
                    ELSE '' END,
                calisma_grubu = '',
                gorsel_durum = CASE
                    WHEN gorsel_sayisi > 0 THEN 'GEREKMIYOR'
                    ELSE 'BEKLIYOR' END,
                icerik_durum = 'GEREKMIYOR',
                genel_durum = 'GEREKMIYOR',
                guncellenme = CURRENT_TIMESTAMP
            """
        )
        db.execute(
            """
            INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama)
            SELECT id, 'YALNIZ_GORSEL_KUYRUGU',
                   CASE WHEN gorsel_sayisi > 0
                        THEN 'Mevcut gorseller korundu; otel atlandi.'
                        ELSE 'Icerik ve Genel kapali; yalniz gorsel acildi.' END
            FROM otel_gorevleri
            """
        )


def sayi(db_yolu, kosul, parametreler=()):
    with closing(baglan(db_yolu)) as db:
        return int(
            db.execute(
                f"SELECT COUNT(*) FROM otel_gorevleri WHERE {kosul}",
                tuple(parametreler),
            ).fetchone()[0]
        )


def kaynak_bekleyen_sayisi(db_yolu):
    return sayi(db_yolu, "durum='KAYNAK_BEKLIYOR' AND gorsel_gerekli=1")


def hazir_gorsel_sayisi(db_yolu):
    return sayi(
        db_yolu,
        "durum='HAZIR' AND gorsel_gerekli=1 AND gorsel_durum='BEKLIYOR' "
        "AND kaynak_url<>''",
    )


def islenen_gorsel_sayisi(db_yolu):
    return sayi(db_yolu, "durum='ISLENIYOR' AND gorsel_durum='ISLENIYOR'")


def kuyruk_ozeti(db_yolu):
    with closing(baglan(db_yolu)) as db:
        return dict(
            db.execute(
                "SELECT durum, COUNT(*) FROM otel_gorevleri GROUP BY durum"
            ).fetchall()
        )


def pid_calisiyor(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        tanitici = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not tanitici:
            return False
        try:
            cikis_kodu = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(tanitici, ctypes.byref(cikis_kodu)):
                return False
            return int(cikis_kodu.value) == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(tanitici)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def calisma_kilidi_al(db_yolu):
    kilit = Path(db_yolu).with_suffix(Path(db_yolu).suffix + ".yalniz-gorsel.lock")
    for _ in range(2):
        try:
            tanitici = os.open(str(kilit), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(tanitici, str(os.getpid()).encode("ascii"))
            os.close(tanitici)
            return kilit
        except FileExistsError:
            try:
                eski_pid = kilit.read_text(encoding="ascii").strip()
            except OSError:
                eski_pid = ""
            if eski_pid and pid_calisiyor(eski_pid):
                raise RuntimeError(
                    f"Bu calisma zaten acik (PID {eski_pid}). Ayni veritabaninda "
                    "ikinci bot baslatilmadi."
                )
            try:
                kilit.unlink()
            except OSError:
                pass
    raise RuntimeError(f"Calisma kilidi alinamadi: {kilit}")


def calisma_kilidi_birak(kilit):
    try:
        if (
            Path(kilit).exists()
            and Path(kilit).read_text(encoding="ascii").strip() == str(os.getpid())
        ):
            Path(kilit).unlink()
    except OSError:
        pass


def surec_baslat(etiket, komut, aktif_surecler):
    ortam = os.environ.copy()
    ortam["PYTHONUNBUFFERED"] = "1"
    ortam.setdefault("KEP_CHROME_SEKME_LIMITI", "2")
    surec = subprocess.Popen(
        [str(parca) for parca in komut],
        cwd=KLASOR,
        env=ortam,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    aktif_surecler.append(surec)

    def aktar():
        if surec.stdout is None:
            return
        for satir in surec.stdout:
            print(
                guvenli_konsol_metni(f"[{etiket}] {satir.rstrip()}"),
                flush=True,
            )

    okuyucu = threading.Thread(target=aktar, daemon=True)
    okuyucu.start()
    return etiket, surec, okuyucu


def sureci_bekle(calisan, aktif_surecler):
    etiket, surec, okuyucu = calisan
    kod = surec.wait()
    okuyucu.join(timeout=10)
    if surec in aktif_surecler:
        aktif_surecler.remove(surec)
    if kod != 0:
        raise RuntimeError(f"{etiket} sureci {kod} hata koduyla durdu.")
    return kod


def kaynak_komutu(args, db_yolu, rapor_yolu, adet):
    return [
        sys.executable,
        KLASOR / "agent_reach_kaynak_pilotu.py",
        "--db", db_yolu,
        "--adet", adet,
        "--adres-topla",
        "--sonuc-adedi", 20,
        "--kabul-esigi", args.kabul_esigi,
        "--rapor", rapor_yolu,
        "--onayla",
    ]


def gorsel_isci_planlari(args, db_yolu):
    planlar = []
    for isci_no in range(args.gorsel_isci):
        if not hazir_gorsel_gorevleri(
            db_yolu,
            1,
            isci_no=isci_no,
            isci_sayisi=args.gorsel_isci,
        ):
            continue
        planlar.append(
            (
                f"GORSEL-{isci_no + 1}",
                [
                    sys.executable,
                    KLASOR / "gorsel_isci.py",
                    "--db", db_yolu,
                    "--adet", args.gorsel_parti,
                    "--isci-no", isci_no,
                    "--isci-sayisi", args.gorsel_isci,
                    "--min-genislik", args.min_genislik,
                    "--min-yukseklik", args.min_yukseklik,
                    "--onayla",
                ],
            )
        )
    return planlar


def bir_gorsel_turu_calistir(args, db_yolu, aktif_surecler):
    planlar = gorsel_isci_planlari(args, db_yolu)
    if not planlar:
        return 0
    print(
        "\nPARALEL GORSEL TURU: " + ", ".join(etiket for etiket, _ in planlar),
        flush=True,
    )
    calisanlar = [
        surec_baslat(etiket, komut, aktif_surecler)
        for etiket, komut in planlar
    ]
    for calisan in calisanlar:
        sureci_bekle(calisan, aktif_surecler)
    return len(planlar)


def hazir_gorselleri_bitir(args, db_yolu, aktif_surecler):
    tur = 0
    while hazir_gorsel_sayisi(db_yolu) > 0:
        tur += 1
        if tur > 1000:
            raise RuntimeError("Gorsel kuyrugu 1000 turde bitmedi; dongu durduruldu.")
        if bir_gorsel_turu_calistir(args, db_yolu, aktif_surecler) == 0:
            break


def kaynak_ve_gorseli_paralel_calistir(args, db_yolu, calisma, aktif_surecler):
    """Kaynak bulunurken HAZIR olanlari uc Chrome iscisina dagitir."""
    for kaynak_turu in range(1, args.kaynak_tur + 1):
        bekleyen = kaynak_bekleyen_sayisi(db_yolu)
        if bekleyen == 0:
            break
        if kaynak_turu > 1:
            print(
                f"\nGecici kaynak sorunlari icin yeniden arama "
                f"{kaynak_turu}/{args.kaynak_tur}...",
                flush=True,
            )
            time.sleep(8)
        kaynak = surec_baslat(
            f"KAYNAK-{kaynak_turu}",
            kaynak_komutu(
                args,
                db_yolu,
                calisma / f"kaynak_dogrulama_{kaynak_turu}.csv",
                bekleyen,
            ),
            aktif_surecler,
        )

        # Kaynak arayicisi bir yandan yeni HAZIR gorevler uretirken, burada uc
        # bagimsiz Chrome sureci mevcut HAZIR gorevleri yukler.
        while kaynak[1].poll() is None:
            if hazir_gorsel_sayisi(db_yolu) > 0:
                bir_gorsel_turu_calistir(args, db_yolu, aktif_surecler)
            else:
                time.sleep(2)
        sureci_bekle(kaynak, aktif_surecler)
        hazir_gorselleri_bitir(args, db_yolu, aktif_surecler)

    hazir_gorselleri_bitir(args, db_yolu, aktif_surecler)

    # Galeri veya ag kaynakli gecici gorsel hatalarini sinirli sayida dener.
    for _ in range(args.gorsel_hata_deneme):
        yeniden = hatali_gorevleri_yeniden_kuyruga_al(
            db_yolu, azami_deneme=args.gorsel_hata_deneme
        )
        if not yeniden:
            break
        for gorev in yeniden:
            print(
                f"GORSEL YENIDEN KUYRUKTA: {gorev['otel_id']} | "
                f"{gorev['otel_adi']}",
                flush=True,
            )
        hazir_gorselleri_bitir(args, db_yolu, aktif_surecler)


def sonucu_yaz(db_yolu, bulunamayanlar, rapor_yolu):
    alanlar = (
        "otel_id", "otel_adi", "atlas_url", "kaynak_url", "kaynak_site",
        "kaynak_guven", "atlas_adres", "kaynak_adres", "adres_puani",
        "baslangic_gorseli", "guncel_gorsel", "gorsel_durum", "durum",
        "son_hata",
    )
    with closing(baglan(db_yolu)) as db, Path(rapor_yolu).open(
        "w", encoding="utf-8-sig", newline=""
    ) as dosya:
        yazici = csv.DictWriter(dosya, fieldnames=alanlar)
        yazici.writeheader()
        for gorev in db.execute("SELECT * FROM otel_gorevleri ORDER BY id"):
            zaten_vardi = gorev["son_hata"] == "GORSEL_ZATEN_VAR_ATLANDI"
            yazici.writerow(
                {
                    "otel_id": gorev["otel_id"],
                    "otel_adi": gorev["otel_adi"],
                    "atlas_url": gorev["duzenleme_url"],
                    "kaynak_url": gorev["kaynak_url"],
                    "kaynak_site": gorev["kaynak_site"],
                    "kaynak_guven": gorev["kaynak_guven"],
                    "atlas_adres": gorev["atlas_adres"],
                    "kaynak_adres": gorev["kaynak_adres"],
                    "adres_puani": gorev["adres_puani"],
                    "baslangic_gorseli": gorev["gorsel_sayisi"] if zaten_vardi else 0,
                    "guncel_gorsel": gorev["gorsel_sayisi"],
                    "gorsel_durum": gorev["gorsel_durum"],
                    "durum": gorev["durum"],
                    "son_hata": gorev["son_hata"],
                }
            )
        for otel_id, hata in bulunamayanlar:
            yazici.writerow(
                {
                    "otel_id": otel_id,
                    "durum": "ATLAS_ID_BULUNAMADI",
                    "son_hata": hata,
                }
            )


def kontrol_linklerini_yaz(db_yolu, rapor_yolu):
    with closing(baglan(db_yolu)) as db, Path(rapor_yolu).open(
        "w", encoding="utf-8-sig", newline=""
    ) as dosya:
        dosya.write("# ID | Otel | Kep Atlas duzenleme URL | Kaynak URL | Durum\n")
        for gorev in db.execute(
            "SELECT * FROM otel_gorevleri WHERE kaynak_url<>'' ORDER BY id"
        ):
            dosya.write(
                f"{gorev['otel_id']} | {gorev['otel_adi']} | "
                f"{gorev['duzenleme_url']} | {gorev['kaynak_url']} | "
                f"{gorev['durum']}\n"
            )


def sorunlu_yaz(db_yolu, bulunamayanlar, rapor_yolu):
    alanlar = ("otel_id", "otel_adi", "atlas_url", "durum", "neden")
    with closing(baglan(db_yolu)) as db, Path(rapor_yolu).open(
        "w", encoding="utf-8-sig", newline=""
    ) as dosya:
        yazici = csv.DictWriter(dosya, fieldnames=alanlar)
        yazici.writeheader()
        for gorev in db.execute(
            """SELECT * FROM otel_gorevleri
               WHERE durum NOT IN ('TAMAMLANDI') ORDER BY id"""
        ):
            yazici.writerow(
                {
                    "otel_id": gorev["otel_id"],
                    "otel_adi": gorev["otel_adi"],
                    "atlas_url": gorev["duzenleme_url"],
                    "durum": gorev["durum"],
                    "neden": gorev["son_hata"],
                }
            )
        for otel_id, hata in bulunamayanlar:
            yazici.writerow(
                {
                    "otel_id": otel_id,
                    "durum": "ATLAS_ID_BULUNAMADI",
                    "neden": hata,
                }
            )


def ozet_yaz(db_yolu, bulunamayan_sayisi=0):
    with closing(baglan(db_yolu)) as db:
        toplam = int(db.execute("SELECT COUNT(*) FROM otel_gorevleri").fetchone()[0])
        atlanan = int(
            db.execute(
                "SELECT COUNT(*) FROM otel_gorevleri "
                "WHERE son_hata='GORSEL_ZATEN_VAR_ATLANDI'"
            ).fetchone()[0]
        )
        yuklenen = int(
            db.execute(
                "SELECT COUNT(*) FROM otel_gorevleri "
                "WHERE gorsel_durum='TAMAMLANDI'"
            ).fetchone()[0]
        )
        yuklenen_gorsel = int(
            db.execute(
                "SELECT COALESCE(SUM(gorsel_sayisi),0) FROM otel_gorevleri "
                "WHERE gorsel_durum='TAMAMLANDI'"
            ).fetchone()[0]
        )
        sorunlu = int(
            db.execute(
                "SELECT COUNT(*) FROM otel_gorevleri "
                "WHERE durum IN ('HATA','INSAN_KONTROLU')"
            ).fetchone()[0]
        )
        bekleyen = int(
            db.execute(
                "SELECT COUNT(*) FROM otel_gorevleri "
                "WHERE durum IN ('KAYNAK_BEKLIYOR','HAZIR','ISLENIYOR')"
            ).fetchone()[0]
        )
    print("\n" + "=" * 64, flush=True)
    print("YALNIZ GORSEL SISTEMI - SON OZET", flush=True)
    print("=" * 64, flush=True)
    print(f"Atlas'ta bulunan       : {toplam}", flush=True)
    print(f"Gorseli vardi, atlandi : {atlanan}", flush=True)
    print(f"Yeni gorsel yuklenen   : {yuklenen} otel / {yuklenen_gorsel} gorsel", flush=True)
    print(f"Sorunluya ayrilan      : {sorunlu}", flush=True)
    print(f"Gecici olarak bekleyen : {bekleyen}", flush=True)
    print(f"Atlas ID bulunamayan   : {int(bulunamayan_sayisi)}", flush=True)
    return {
        "toplam": toplam,
        "atlanan": atlanan,
        "yuklenen": yuklenen,
        "yuklenen_gorsel": yuklenen_gorsel,
        "sorunlu": sorunlu,
        "bekleyen": bekleyen,
        "bulunamayan": int(bulunamayan_sayisi),
    }


def yedekle(db_yolu):
    yedek = Path(db_yolu).with_name(
        f"gorevler_yedek_{datetime.now():%Y%m%d_%H%M%S}.db"
    )
    with closing(sqlite3.connect(db_yolu)) as kaynak, closing(
        sqlite3.connect(yedek)
    ) as hedef:
        kaynak.backup(hedef)
    return yedek


def calismayi_surdur(
    args, db_yolu, calisma, bulunamayanlar=None, mevcut_kilit=None
):
    aktif_surecler = []
    kilit = mevcut_kilit or calisma_kilidi_al(db_yolu)
    try:
        kaynak_ve_gorseli_paralel_calistir(
            args, db_yolu, calisma, aktif_surecler
        )
    except KeyboardInterrupt:
        print(
            "\nKullanici durdurdu. Devam dosyasi ile guvenli sekilde surdurulebilir.",
            flush=True,
        )
        raise
    finally:
        for surec in aktif_surecler:
            if surec.poll() is None:
                surec.terminate()
        calisma_kilidi_birak(kilit)

    bulunamayanlar = bulunamayanlar or []
    sonucu_yaz(db_yolu, bulunamayanlar, calisma / "sonuc.csv")
    kontrol_linklerini_yaz(db_yolu, calisma / "kontrol_edilecek_atlas_linkleri.txt")
    sorunlu_yaz(db_yolu, bulunamayanlar, calisma / "sorunlu_ve_atlanan_oteller.csv")
    return ozet_yaz(db_yolu, len(bulunamayanlar))


def main():
    kimlik.ortami_hazirla()
    args = argumanlari_oku()
    ham_idler = args.idler
    if args.id_dosyasi:
        ham_idler += "\n" + Path(args.id_dosyasi).read_text(encoding="utf-8-sig")
    idler = idleri_ayikla(ham_idler)
    if args.min_genislik < 1 or args.min_yukseklik < 1:
        raise ValueError("Minimum gorsel olculeri pozitif olmalidir.")
    if not 0.0 < args.kabul_esigi <= 1.0:
        raise ValueError("--kabul-esigi 0 ile 1 arasinda olmalidir.")
    if not 1 <= args.gorsel_isci <= 4:
        raise ValueError("--gorsel-isci 1 ile 4 arasinda olmalidir.")
    if min(
        args.gorsel_parti, args.kaynak_tur, args.gorsel_hata_deneme
    ) < 1:
        raise ValueError("Parti ve deneme degerleri en az 1 olmalidir.")

    print(
        f"PLAN: {len(idler)} ID; sadece gorsel; "
        f"{args.gorsel_isci} paralel Chrome iscisi; "
        "Icerik ve Genel tamamen kapali.",
        flush=True,
    )
    if not args.onayla:
        print("MOD: PLAN - Atlas ve veritabani degistirilmedi.")
        return 0

    eposta = os.getenv("KEP_ATLAS_EMAIL", "").strip()
    sifre = os.getenv("KEP_ATLAS_PASSWORD", "")
    if not eposta or not sifre:
        raise RuntimeError("KEP_ATLAS_EMAIL ve KEP_ATLAS_PASSWORD gereklidir.")

    calisma = CALISMA_KLASORU / datetime.now().strftime("%Y%m%d_%H%M%S")
    calisma.mkdir(parents=True, exist_ok=False)
    (calisma / "girilen_kep_atlas_idleri.txt").write_text(
        "\n".join(idler) + "\n", encoding="utf-8-sig"
    )
    db_yolu = calisma / "gorevler.db"
    print(f"\nCALISMA KLASORU: {calisma}", flush=True)

    oteller, bulunamayanlar = secili_otelleri_atlastan_oku(
        idler, eposta, sifre, headless=args.headless_atlas_arama
    )
    if not oteller:
        raise RuntimeError("Verilen ID'lerden hicbiri Kep Atlas'ta bulunamadi.")

    secili_raporu = calisma / "secili_oteller.csv"
    secili_raporunu_yaz(oteller, secili_raporu)
    raporu_aktar(secili_raporu, db_yolu)
    secili_gorsel_kuyrugunu_hazirla(db_yolu)

    dolu = sayi(db_yolu, "gorsel_sayisi>0")
    bos = sayi(db_yolu, "gorsel_sayisi=0")
    print(
        f"\nATLAS TARAMASI: {dolu} otelde gorsel var ve atlandi; "
        f"{bos} otel kaynak+yukleme kuyruguna alindi.",
        flush=True,
    )
    if bos:
        calismayi_surdur(args, db_yolu, calisma, bulunamayanlar)
    else:
        sonucu_yaz(db_yolu, bulunamayanlar, calisma / "sonuc.csv")
        kontrol_linklerini_yaz(
            db_yolu, calisma / "kontrol_edilecek_atlas_linkleri.txt"
        )
        sorunlu_yaz(
            db_yolu, bulunamayanlar, calisma / "sorunlu_ve_atlanan_oteller.csv"
        )
        ozet_yaz(db_yolu, len(bulunamayanlar))

    print(f"\nButun raporlar: {calisma}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
