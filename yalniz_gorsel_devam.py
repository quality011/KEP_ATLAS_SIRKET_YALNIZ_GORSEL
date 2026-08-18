import argparse
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import kimlik
from veritabani import (
    baglan,
    gorsel_dogrulandi,
    kesilen_gorsel_islemini_yeniden_kuyruga_al,
)
from yalniz_gorsel_botu import (
    CALISMA_KLASORU,
    calisma_kilidi_al,
    calisma_kilidi_birak,
    calismayi_surdur,
    ozet_yaz,
    sonucu_yaz,
    sorunlu_yaz,
)


def argumanlari_oku():
    parser = argparse.ArgumentParser(
        description="Kesilen yalniz-gorsel calismasini guvenle surdurur."
    )
    parser.add_argument("--db", default="")
    parser.add_argument("--min-genislik", type=int, default=600)
    parser.add_argument("--min-yukseklik", type=int, default=400)
    parser.add_argument("--kabul-esigi", type=float, default=0.78)
    parser.add_argument("--gorsel-isci", type=int, default=3)
    parser.add_argument("--gorsel-parti", type=int, default=8)
    parser.add_argument("--kaynak-tur", type=int, default=4)
    parser.add_argument("--gorsel-hata-deneme", type=int, default=4)
    parser.add_argument("--onayla", action="store_true")
    return parser.parse_args()


def en_yeni_veritabani():
    adaylar = sorted(
        CALISMA_KLASORU.glob("*/gorevler.db"),
        key=lambda yol: yol.stat().st_mtime,
        reverse=True,
    )
    if not adaylar:
        raise FileNotFoundError(
            "Devam edilecek yalniz_gorsel_calismalari/*/gorevler.db bulunamadi."
        )
    return adaylar[0].resolve()


def yarim_gorsel_gorevleri(db_yolu):
    with closing(baglan(db_yolu)) as db:
        return db.execute(
            """
            SELECT * FROM otel_gorevleri
            WHERE durum='ISLENIYOR' AND gorsel_durum='ISLENIYOR'
            ORDER BY id
            """
        ).fetchall()


def kesilen_gorselleri_kurtar(db_yolu, eposta, sifre):
    gorevler = yarim_gorsel_gorevleri(db_yolu)
    if not gorevler:
        print("Yarim kalmis gorsel gorevi yok.", flush=True)
        return

    print(
        f"KESINTI KURTARMA: {len(gorevler)} yarim otel Atlas'ta kontrol edilecek.",
        flush=True,
    )
    import gorsel_motoru as motor
    from gorsel_kontrol import atlas_gorsel_sayisini_oku

    class Uygulama:
        @staticmethod
        def log_yaz(mesaj):
            print(f"[KURTARMA] {mesaj}", flush=True)

    driver = None
    try:
        driver = motor.tarayiciyi_hazirla(Uygulama())
        for gorev in gorevler:
            print(
                f"KESINTI KONTROLU: {gorev['otel_id']} | {gorev['otel_adi']}",
                flush=True,
            )
            motor.panele_giris_yap(
                driver, gorev["duzenleme_url"], eposta, sifre, Uygulama()
            )
            motor.panel_gorseller_sekmesini_ac(driver, Uygulama())
            sayi = None
            for _ in range(3):
                sayi = atlas_gorsel_sayisini_oku(driver)
                if sayi is not None:
                    break
                time.sleep(2)
                driver.refresh()
            if sayi is None:
                raise RuntimeError(
                    f"{gorev['otel_adi']} icin gorsel sayaci okunamadi. "
                    "Cift yukleme riskine karsi devam durduruldu."
                )
            if int(sayi) > 0:
                gorsel_dogrulandi(db_yolu, gorev["id"], int(sayi))
                print(
                    f"KURTARILDI: Atlas'ta {sayi} gorsel var; tekrar yuklenmeyecek.",
                    flush=True,
                )
            else:
                kesilen_gorsel_islemini_yeniden_kuyruga_al(
                    db_yolu,
                    gorev["id"],
                    "Kesinti sonrasi Atlas gorsel sayaci 0; yeniden kuyrukta.",
                )
                print("YENIDEN KUYRUKTA: Atlas gorsel sayaci 0.", flush=True)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def yedekle(db_yolu):
    yedek = Path(db_yolu).with_name(
        f"gorevler_kesinti_yedegi_{datetime.now():%Y%m%d_%H%M%S}.db"
    )
    with closing(sqlite3.connect(db_yolu)) as kaynak, closing(
        sqlite3.connect(yedek)
    ) as hedef:
        kaynak.backup(hedef)
    return yedek


def main():
    kimlik.ortami_hazirla()
    args = argumanlari_oku()
    if not 1 <= args.gorsel_isci <= 4:
        raise ValueError("--gorsel-isci 1 ile 4 arasinda olmalidir.")
    db_yolu = Path(args.db).resolve() if args.db else en_yeni_veritabani()
    if not db_yolu.exists():
        raise FileNotFoundError(db_yolu)
    print(f"DEVAM EDILECEK VERITABANI: {db_yolu}", flush=True)
    if not args.onayla:
        print("MOD: PLAN - Veritabani ve Atlas degistirilmedi.")
        ozet_yaz(db_yolu)
        return 0

    eposta = os.getenv("KEP_ATLAS_EMAIL", "").strip()
    sifre = os.getenv("KEP_ATLAS_PASSWORD", "")
    if not eposta or not sifre:
        raise RuntimeError("KEP_ATLAS_EMAIL ve KEP_ATLAS_PASSWORD gereklidir.")

    kilit = calisma_kilidi_al(db_yolu)
    try:
        yedek = yedekle(db_yolu)
        print(f"KESINTI YEDEGI: {yedek}", flush=True)
        kesilen_gorselleri_kurtar(db_yolu, eposta, sifre)
        calismayi_surdur(
            args, db_yolu, db_yolu.parent, [], mevcut_kilit=kilit
        )
        # calismayi_surdur kilidi kendi finally blogunda birakir.
        kilit = None
    finally:
        if kilit is not None:
            calisma_kilidi_birak(kilit)
    print(f"\nDevam raporlari: {db_yolu.parent}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
