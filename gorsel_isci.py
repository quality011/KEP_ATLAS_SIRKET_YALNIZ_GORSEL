import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

from veritabani import (
    gorsel_dogrulandi,
    gorsel_hatasi_kaydet,
    gorsel_islemini_baslat,
    gorsel_kaynagini_degistir,
    hazir_gorsel_gorevleri,
)
from kaynak_agent import gorsel_kaynak_adaylarini_sirala


VARSAYILAN_DB = Path(__file__).with_name("kep_atlas_gorevler.db")


class KonsolUygulamasi:
    def log_yaz(self, mesaj):
        print(mesaj, flush=True)

    def kuyruk_guncelle(self):
        pass


def motoru_yukle():
    try:
        import gorsel_motoru
    except ImportError as hata:
        raise RuntimeError(
            "gorsel_motoru.py bulunamadı. Aşama paketini yeniden kopyalayın."
        ) from hata
    return gorsel_motoru


def gecici_klasor(gorev):
    otel = str(gorev["otel_id"] or gorev["id"])
    damga = int(time.time() * 1000)
    return Path(__file__).with_name("work") / f"temp_images_{otel}_{damga}"


def gorevin_gorsel_kaynaklari(gorev, azami_site=3):
    try:
        adaylar = json.loads(gorev["kaynak_adaylari_json"] or "[]")
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        adaylar = []
    return gorsel_kaynak_adaylarini_sirala(
        adaylar,
        aktif_url=gorev["kaynak_url"],
        aktif_site=gorev["kaynak_site"],
        azami_site=azami_site,
        otel_adi=gorev["otel_adi"],
        ham_adi=gorev["ham_adi"] if "ham_adi" in gorev.keys() else "",
    )


def yuklemenin_bitmesini_bekle(driver, beklenen, baslangic_sayisi=0, timeout=240):
    """Atlas yükleme özeti tamamlanmadan tarayıcının kapanmasını engeller."""
    baslangic = time.monotonic()
    son_artis = None
    son_sayi = baslangic_sayisi
    while time.monotonic() - baslangic < timeout:
        durum = driver.execute_script(
            r"""
            const norm = value => (value || '').normalize('NFD')
              .replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase('tr-TR')
              .replace(/\s+/g, ' ').trim();
            const text = norm(document.body.innerText || document.body.textContent);
            const all = [...text.matchAll(/(\d+)\s*gorsel\s*yuklendi\s*,\s*(\d+)\s*hata/g)];
            if (all.length) {
              const m = all[all.length - 1];
              return {yuklenen:Number(m[1]), hata:Number(m[2]), ozet:true};
            }
            const kuyruk = text.match(/yukleme\s*kuyrugu\s*\((\d+)\s*\/\s*(\d+)\)/);
            return kuyruk
              ? {yuklenen:Number(kuyruk[1]), toplam:Number(kuyruk[2]), ozet:false}
              : {ozet:false};
            """
        ) or {}
        if durum.get("ozet"):
            tamamlanan = int(durum.get("yuklenen", 0)) + int(durum.get("hata", 0))
            if tamamlanan >= beklenen:
                if int(durum.get("yuklenen", 0)) < 1:
                    raise RuntimeError(
                        f"Atlas yüklemesi tamamlandı ancak {durum.get('hata', 0)} dosyanın tamamı hata verdi."
                    )
                return int(durum["yuklenen"])

        # Bazı Atlas sürümlerinde özet kısa süre görünüp kaybolur. Toplam sayı
        # artmış ve 10 saniye sabit kalmışsa dosya aktarımı bitmiş kabul edilir.
        try:
            from gorsel_kontrol import atlas_gorsel_sayisini_oku

            sayi = atlas_gorsel_sayisini_oku(driver)
        except Exception:
            sayi = None
        if sayi is not None and sayi > baslangic_sayisi:
            if sayi != son_sayi:
                son_sayi = sayi
                son_artis = time.monotonic()
            elif son_artis is not None and time.monotonic() - son_artis >= 10:
                return max(1, sayi - baslangic_sayisi)
        time.sleep(1)

    raise RuntimeError(
        f"Atlas görsel yüklemesi {timeout} saniye içinde tamamlanmış olarak doğrulanamadı."
    )


def gorevi_isle(
    motor,
    app,
    db_yolu,
    gorev,
    eposta,
    sifre,
    min_genislik,
    min_yukseklik,
    yukleme_timeout=240,
):
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    if not gorsel_islemini_baslat(db_yolu, gorev["id"]):
        app.log_yaz(f"ATLANDI: {gorev['otel_adi']} artık HAZIR durumda değil.")
        return False

    klasor = gecici_klasor(gorev).resolve()
    yukleme_basladi = False
    yukleme_tamamlandi = False
    try:
        app.log_yaz(f"\nİŞLENİYOR: {gorev['otel_id']} | {gorev['otel_adi']}")
        driver = motor.tarayiciyi_hazirla(app)

        kaynaklar = gorevin_gorsel_kaynaklari(gorev, azami_site=3)
        if not kaynaklar:
            raise RuntimeError("Denenebilecek güvenli bir görsel kaynağı bulunamadı.")

        app.log_yaz(
            "1/3: Doğrulanmış kaynak galerileri deneniyor: "
            + " -> ".join(kaynak["site"] for kaynak in kaynaklar)
        )
        indirilenler = []
        secilen_kaynak = None
        kaynak_hatalari = []
        for kaynak_no, kaynak in enumerate(kaynaklar, 1):
            kaynak_url = kaynak["url"]
            site = kaynak["site"]
            try:
                app.log_yaz(
                    f"Kaynak {kaynak_no}/{len(kaynaklar)} deneniyor: {site} | {kaynak_url}"
                )
                driver.get(kaynak_url)
                WebDriverWait(driver, 25).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                time.sleep(0.5)
                motor.cerez_uyarisini_kapat(driver)
                motor.kaynak_galerisini_ac(driver, app)
                image_urls = motor.galeri_gorsellerini_topla(driver, app)

                if klasor.exists():
                    shutil.rmtree(klasor)
                indirilenler = motor.gorselleri_indir(
                    driver,
                    kaynak_url,
                    image_urls,
                    str(klasor),
                    min_genislik,
                    min_yukseklik,
                    app,
                    otel_adi=gorev["otel_adi"],
                )
                if not indirilenler:
                    raise RuntimeError("Galeriden geçerli bir görsel indirilemedi.")
                secilen_kaynak = kaynak
                break
            except Exception as kaynak_hatasi:
                if klasor.exists():
                    shutil.rmtree(klasor, ignore_errors=True)
                ozet = f"{site}: {kaynak_hatasi}"
                kaynak_hatalari.append(ozet)
                app.log_yaz(f"KAYNAK BAŞARISIZ: {ozet}")
                if kaynak_no < len(kaynaklar):
                    app.log_yaz("Sıradaki güvenli alternatif siteye geçiliyor...")

        if not indirilenler or secilen_kaynak is None:
            raise RuntimeError(
                "En fazla 3 güvenli kaynak denendi, görsel alınamadı: "
                + " | ".join(kaynak_hatalari)
            )
        if secilen_kaynak["url"] != gorev["kaynak_url"]:
            gorsel_kaynagini_degistir(
                db_yolu,
                gorev["id"],
                secilen_kaynak["url"],
                secilen_kaynak["site"],
                secilen_kaynak.get(
                    "kabul_guveni", secilen_kaynak.get("toplam_puan", 0.0)
                ),
                secilen_kaynak.get("sayfa_adresi", ""),
                secilen_kaynak.get("adres_puani", 0.0),
            )
            app.log_yaz(
                f"AKTİF KAYNAK DEĞİŞTİRİLDİ: {secilen_kaynak['site']} | "
                f"{secilen_kaynak['url']}"
            )
        app.log_yaz(f"{len(indirilenler)} benzersiz görsel indirildi.")

        app.log_yaz("2/3: Kep Atlas paneline geçiliyor...")
        motor.panele_giris_yap(
            driver, gorev["duzenleme_url"], eposta, sifre, app
        )
        motor.panel_gorseller_sekmesini_ac(driver, app)

        app.log_yaz("3/3: Görseller yükleme kuyruğuna gönderiliyor...")
        try:
            from gorsel_kontrol import atlas_gorsel_sayisini_oku

            baslangic_sayisi = atlas_gorsel_sayisini_oku(driver) or 0
        except Exception:
            baslangic_sayisi = 0
        motor.dosyalari_yukle(driver, indirilenler, app)
        yukleme_basladi = True
        app.log_yaz("Atlas yükleme kuyruğunun gerçekten bitmesi bekleniyor...")
        yuklenen = yuklemenin_bitmesini_bekle(
            driver,
            len(indirilenler),
            baslangic_sayisi=baslangic_sayisi,
            timeout=yukleme_timeout,
        )
        yukleme_tamamlandi = True
        # Yükleme sayacı bu tarayıcı oturumunda tamamlandı. Kullanıcının istediği
        # hızlı akışta ayrı bir son kontrol tarayıcısı açmadan görevi tamamla.
        gorsel_dogrulandi(db_yolu, gorev["id"], yuklenen)
        app.log_yaz(
            f"YÜKLEME BİTTİ: {yuklenen} görsel kabul edildi; "
            "ayrı son kontrol aşaması çalıştırılmadı."
        )
        return True
    except Exception as hata:
        gorsel_hatasi_kaydet(db_yolu, gorev["id"], str(hata))
        app.log_yaz(f"HATA: {hata}")
        return False
    finally:
        if klasor.exists() and (not yukleme_basladi or yukleme_tamamlandi):
            shutil.rmtree(klasor, ignore_errors=True)
        elif klasor.exists():
            app.log_yaz(f"Geçici görseller kontrol bitene kadar korundu: {klasor}")


def argumanlari_oku():
    parser = argparse.ArgumentParser(description="Kep Atlas görsel kuyruk işçisi")
    parser.add_argument("--db", default=str(VARSAYILAN_DB))
    parser.add_argument("--adet", type=int, default=3)
    parser.add_argument("--isci-no", type=int, default=0)
    parser.add_argument("--isci-sayisi", type=int, default=1)
    parser.add_argument("--yildiz", type=int, choices=(0, 1, 2, 3, 4, 5), default=0)
    parser.add_argument("--grup", default="")
    parser.add_argument("--min-genislik", type=int, default=600)
    parser.add_argument("--min-yukseklik", type=int, default=400)
    parser.add_argument("--yukleme-timeout", type=int, default=240)
    parser.add_argument("--onayla", action="store_true")
    parser.add_argument("--beklet", action="store_true")
    return parser.parse_args()


def main():
    args = argumanlari_oku()
    gorevler = hazir_gorsel_gorevleri(
        args.db,
        args.adet,
        yildiz=args.yildiz,
        grup=args.grup,
        isci_no=args.isci_no,
        isci_sayisi=args.isci_sayisi,
    )
    print(
        f"HAZIR görsel görevi: {len(gorevler)} "
        f"(işçi {args.isci_no + 1}/{args.isci_sayisi})"
    )
    for sira, gorev in enumerate(gorevler, 1):
        print(
            f"[{sira}/{len(gorevler)}] {gorev['otel_id']} | {gorev['otel_adi']} | "
            f"{gorev['kaynak_site']} | {gorev['kaynak_url']}"
        )

    if not args.onayla:
        print("\nMOD: PLANLAMA - Atlas'a görsel yüklenmedi, görev durumu değiştirilmedi.")
        return 0
    if not gorevler:
        print("İşlenecek HAZIR görsel görevi yok.")
        return 0

    eposta = os.getenv("KEP_ATLAS_EMAIL", "").strip()
    sifre = os.getenv("KEP_ATLAS_PASSWORD", "")
    if not eposta or not sifre:
        raise RuntimeError("KEP_ATLAS_EMAIL ve KEP_ATLAS_PASSWORD bulunamadı.")

    motor = motoru_yukle()
    app = KonsolUygulamasi()
    try:
        for gorev in gorevler:
            gorevi_isle(
                motor,
                app,
                args.db,
                gorev,
                eposta,
                sifre,
                args.min_genislik,
                args.min_yukseklik,
                args.yukleme_timeout,
            )
    finally:
        driver = getattr(motor, "tarayici_driver", None)
        if driver is not None and args.beklet:
            input("\nYüklemeyi kontrol edin. Tarayıcıyı kapatmak için Enter'a basın...")
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
