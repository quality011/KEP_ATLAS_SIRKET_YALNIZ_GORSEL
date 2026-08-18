import argparse
import os
import re
import sys
import time
from pathlib import Path

from veritabani import (
    gorsel_dogrulandi,
    kontrol_bekleyen_gorsel_gorevleri,
)


VARSAYILAN_DB = Path(__file__).with_name("kep_atlas_gorevler.db")


class KonsolUygulamasi:
    def log_yaz(self, mesaj):
        print(mesaj, flush=True)


def atlas_gorsel_sayisini_oku(driver):
    script = r"""
        const normalize = (value) => (value || '')
            .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
            .toLocaleLowerCase('tr-TR').replace(/\s+/g, ' ').trim();
        for (const el of document.querySelectorAll('button')) {
            const text = normalize(el.innerText || el.textContent);
            if (!text.startsWith('gorseller')) continue;
            const match = text.match(/gorseller\s*(\d+)/);
            if (match) return Number(match[1]);
        }
        for (const el of document.querySelectorAll('main *')) {
            const text = normalize(el.innerText || el.textContent);
            const match = text.match(/^(\d+)\s*gorsel$/);
            if (match) return Number(match[1]);
        }
        return null;
    """
    sonuc = driver.execute_script(script)
    return None if sonuc is None else int(sonuc)


def argumanlari_oku():
    parser = argparse.ArgumentParser(description="Kep Atlas görsel doğrulama işçisi")
    parser.add_argument("--db", default=str(VARSAYILAN_DB))
    parser.add_argument("--adet", type=int, default=1)
    parser.add_argument("--yildiz", type=int, choices=(0, 1, 2, 3, 4, 5), default=0)
    parser.add_argument("--grup", default="")
    parser.add_argument("--onayla", action="store_true")
    parser.add_argument("--bekleme", type=float, default=3.0)
    return parser.parse_args()


def main():
    args = argumanlari_oku()
    gorevler = kontrol_bekleyen_gorsel_gorevleri(
        args.db, args.adet, yildiz=args.yildiz, grup=args.grup
    )
    print(f"KONTROL_BEKLIYOR görsel görevi: {len(gorevler)}")
    for sira, gorev in enumerate(gorevler, 1):
        print(f"[{sira}/{len(gorevler)}] {gorev['otel_id']} | {gorev['otel_adi']}")

    if not args.onayla:
        print("MOD: PLANLAMA - Atlas okunmadı, veritabanı değiştirilmedi.")
        return 0
    if not gorevler:
        return 0

    eposta = os.getenv("KEP_ATLAS_EMAIL", "").strip()
    sifre = os.getenv("KEP_ATLAS_PASSWORD", "")
    if not eposta or not sifre:
        raise RuntimeError("KEP_ATLAS_EMAIL ve KEP_ATLAS_PASSWORD bulunamadı.")

    import gorsel_motoru as motor

    app = KonsolUygulamasi()
    driver = None
    try:
        for gorev in gorevler:
            driver = motor.tarayiciyi_hazirla(app)
            motor.panele_giris_yap(
                driver, gorev["duzenleme_url"], eposta, sifre, app
            )
            sayi = None
            for _ in range(3):
                sayi = atlas_gorsel_sayisini_oku(driver)
                if sayi is not None and sayi > 0:
                    break
                time.sleep(args.bekleme)
                driver.refresh()
            if sayi is None or sayi < 1:
                print(
                    f"BEKLEMEDE: {gorev['otel_adi']} için Atlas'ta görsel "
                    "henüz doğrulanamadı. Durum değiştirilmedi."
                )
                continue
            gorsel_dogrulandi(args.db, gorev["id"], sayi)
            print(f"DOĞRULANDI: {gorev['otel_adi']} | Atlas görsel={sayi}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
