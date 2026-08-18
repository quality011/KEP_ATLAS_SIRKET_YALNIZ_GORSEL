import csv
import re
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


VARSAYILAN_LISTE_URL = (
    "https://atlas.kepguide.io/admin/oteller?"
    "sort=content&limit=25&q=&cityId=&stars=&published="
)


def idleri_ayikla(metin):
    parcalar = re.findall(r"\d+", str(metin or ""))
    gecersiz = [parca for parca in parcalar if len(parca) != 6]
    if gecersiz:
        raise ValueError(
            "Butun Kep Atlas ID'leri tam 6 haneli olmalidir. Gecersiz: "
            + ", ".join(gecersiz)
        )
    sonuc = []
    gorulen = set()
    for parca in parcalar:
        if parca not in gorulen:
            gorulen.add(parca)
            sonuc.append(parca)
    if not sonuc:
        raise ValueError("En az bir adet 6 haneli Kep Atlas ID'si girilmelidir.")
    return sonuc


def arama_urlsi(otel_id, liste_url=VARSAYILAN_LISTE_URL):
    parcalar = urlparse(liste_url)
    sorgu = dict(parse_qsl(parcalar.query, keep_blank_values=True))
    sorgu.update({"q": str(otel_id), "limit": "25", "page": "1"})
    return urlunparse(parcalar._replace(query=urlencode(sorgu)))


def tam_eslesen_oteli_bul(oteller, otel_id):
    eslesenler = [
        otel for otel in oteller if str(otel.otel_id).strip() == str(otel_id)
    ]
    return eslesenler[0] if len(eslesenler) == 1 else None


def secili_otelleri_atlastan_oku(idler, eposta, sifre, headless=False):
    from durum_tarama import giris_yap, listeyi_oku, tarayiciyi_ac

    bulunanlar = []
    bulunamayanlar = []
    driver = tarayiciyi_ac(headless=headless)
    try:
        giris_yap(driver, arama_urlsi(idler[0]), eposta, sifre)
        for sira, otel_id in enumerate(idler, 1):
            print(f"[{sira}/{len(idler)}] Atlas ID araniyor: {otel_id}", flush=True)
            driver.get(arama_urlsi(otel_id))
            time.sleep(1)
            try:
                oteller = listeyi_oku(driver)
            except Exception as hata:
                bulunamayanlar.append(
                    (otel_id, f"Atlas aramasi okunamadi: {hata}")
                )
                continue
            otel = tam_eslesen_oteli_bul(oteller, otel_id)
            if otel is None:
                bulunamayanlar.append(
                    (
                        otel_id,
                        "Arama sonucunda tam ID eslesmesi olan tek bir otel bulunamadi.",
                    )
                )
                continue
            bulunanlar.append(otel)
            print(
                f"  BULUNDU: {otel.otel_adi} | gorsel={otel.gorsel_sayisi}",
                flush=True,
            )
    finally:
        driver.quit()
    return bulunanlar, bulunamayanlar


def secili_raporunu_yaz(oteller, rapor_yolu):
    rapor_yolu = Path(rapor_yolu).resolve()
    with rapor_yolu.open("w", encoding="utf-8-sig", newline="") as dosya:
        yazici = csv.DictWriter(
            dosya,
            fieldnames=(
                "otel_id", "otel_adi", "bolge", "ham_adi", "yildiz",
                "gorsel_sayisi", "icerik_durumu", "karar", "duzenleme_url",
            ),
        )
        yazici.writeheader()
        for otel in oteller:
            yazici.writerow(
                {
                    "otel_id": otel.otel_id,
                    "otel_adi": otel.otel_adi,
                    "bolge": otel.bolge,
                    "ham_adi": otel.ham_adi,
                    "yildiz": otel.yildiz,
                    "gorsel_sayisi": otel.gorsel_sayisi,
                    "icerik_durumu": (
                        f"{otel.icerik_tamamlanan}/{otel.icerik_toplam}"
                    ),
                    "karar": otel.karar,
                    "duzenleme_url": otel.duzenleme_url,
                }
            )
    return rapor_yolu
