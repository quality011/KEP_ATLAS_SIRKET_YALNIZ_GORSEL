import argparse
import csv
import getpass
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import kimlik
from karar_motoru import icerik_var_mi, karar_ver


VARSAYILAN_LISTE_URL = (
    "https://atlas.kepguide.io/admin/oteller?"
    "sort=content&limit=25&q=&cityId=&stars=&published="
)


def metni_normallestir(metin):
    metin = unicodedata.normalize("NFKD", metin or "")
    metin = "".join(harf for harf in metin if not unicodedata.combining(harf))
    return " ".join(metin.casefold().split())


@dataclass(frozen=True)
class OtelDurumu:
    otel_adi: str
    otel_id: str
    bolge: str
    ham_adi: str
    yildiz: int
    duzenleme_url: str
    gorsel_sayisi: int
    icerik_tamamlanan: int
    icerik_toplam: int

    @property
    def gorsel_var(self):
        return self.gorsel_sayisi > 0

    @property
    def icerik_var(self):
        return icerik_var_mi(self.icerik_tamamlanan)

    @property
    def karar(self):
        return karar_ver(self.icerik_var, self.gorsel_var)


def tarayiciyi_ac(headless=False):
    secenekler = webdriver.ChromeOptions()
    secenekler.add_argument("--disable-notifications")
    secenekler.add_argument("--start-maximized")
    if headless:
        secenekler.add_argument("--headless=new")
        secenekler.add_argument("--window-size=1600,1000")
    return webdriver.Chrome(options=secenekler)


def giris_gerekli_mi(driver):
    if "/giris" in driver.current_url:
        return True
    return bool(driver.find_elements(By.CSS_SELECTOR, "input[type='password']"))


def giris_yap(driver, liste_url, eposta, sifre):
    driver.get(liste_url)
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    if not giris_gerekli_mi(driver):
        return
    if not eposta or not sifre:
        raise RuntimeError("Kep Atlas giriş bilgileri verilmedi.")

    email_input = WebDriverWait(driver, 15).until(
        EC.presence_of_element_located(
            (By.XPATH, "//input[@type='email' or contains(@name,'email')]")
        )
    )
    sifre_input = driver.find_element(
        By.XPATH, "//input[@type='password' or contains(@name,'password')]"
    )
    email_input.clear()
    email_input.send_keys(eposta)
    sifre_input.clear()
    sifre_input.send_keys(sifre)
    sifre_input.send_keys(Keys.RETURN)

    WebDriverWait(driver, 25).until(lambda d: "/giris" not in d.current_url)
    driver.get(liste_url)
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))


def baslik_haritasi(driver):
    basliklar = driver.find_elements(By.CSS_SELECTOR, "table thead th")
    if not basliklar:
        basliklar = driver.find_elements(By.CSS_SELECTOR, "[role='columnheader']")

    sonuc = {}
    for indeks, baslik in enumerate(basliklar):
        metin = metni_normallestir(baslik.text)
        if "gorsel" in metin:
            sonuc["gorsel"] = indeks
        elif "icerik" in metin:
            sonuc["icerik"] = indeks
        elif "bolge" in metin:
            sonuc["bolge"] = indeks
        elif "yildiz" in metin:
            sonuc["yildiz"] = indeks
        elif metin == "otel" or "otel adi" in metin:
            sonuc["otel"] = indeks
    return sonuc


def satirlari_bul(driver):
    satirlar = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
    if satirlar:
        return satirlar

    satirlar = driver.find_elements(By.CSS_SELECTOR, "[role='row']")
    return [
        satir
        for satir in satirlar
        if satir.find_elements(By.CSS_SELECTOR, "a[href*='/admin/otel/']")
    ]


def hucreleri_bul(satir):
    hucreler = satir.find_elements(By.CSS_SELECTOR, ":scope > td")
    if not hucreler:
        hucreler = satir.find_elements(By.CSS_SELECTOR, ":scope > [role='cell']")
    if not hucreler:
        hucreler = satir.find_elements(By.CSS_SELECTOR, "td, [role='cell']")
    return hucreler


def hucre_metni(hucreler, harita, anahtar):
    indeks = harita.get(anahtar)
    if indeks is None or indeks >= len(hucreler):
        return ""
    return hucreler[indeks].text.strip()


def ilk_eslesme(metin, desen, varsayilan=""):
    eslesme = re.search(desen, metin or "", flags=re.IGNORECASE)
    return eslesme.group(1) if eslesme else varsayilan


def satiri_oku(satir, harita, filtre_yildiz=0):
    hucreler = hucreleri_bul(satir)
    satir_metni = satir.text.strip()
    if not satir_metni:
        return None

    linkler = satir.find_elements(By.CSS_SELECTOR, "a[href*='/admin/otel/']")
    if not linkler:
        return None
    duzenleme_linki = next(
        (link for link in reversed(linkler) if "duzenle" in metni_normallestir(link.text)),
        linkler[-1],
    )
    duzenleme_url = duzenleme_linki.get_attribute("href") or ""

    otel_metni = hucre_metni(hucreler, harita, "otel")
    if not otel_metni and hucreler:
        otel_metni = hucreler[0].text.strip()
    otel_satirlari = [satir for satir in otel_metni.splitlines() if satir.strip()]
    otel_adi = otel_satirlari[0].strip() if otel_satirlari else "Bilinmeyen otel"
    bolge = hucre_metni(hucreler, harita, "bolge")
    ham_eslesmesi = re.search(r"(?:^|\n)\s*ham:\s*(.+)", otel_metni, flags=re.IGNORECASE)
    ham_adi = ham_eslesmesi.group(1).strip() if ham_eslesmesi else ""

    yildiz_metni = hucre_metni(hucreler, harita, "yildiz")
    yildiz_eslesmesi = re.search(r"\b([1-5])\b", yildiz_metni)
    yildiz = int(yildiz_eslesmesi.group(1)) if yildiz_eslesmesi else int(filtre_yildiz or 0)

    otel_id = ilk_eslesme(otel_metni, r"\b(\d{4,})\b")
    if not otel_id:
        otel_id = urlparse(duzenleme_url).path.rstrip("/").split("/")[-1]

    icerik_metni = hucre_metni(hucreler, harita, "icerik") or satir_metni
    icerik_eslesmesi = re.search(r"\b(\d+)\s*/\s*(\d+)\b", icerik_metni)
    if not icerik_eslesmesi:
        return None
    icerik_tamamlanan = int(icerik_eslesmesi.group(1))
    icerik_toplam = int(icerik_eslesmesi.group(2))

    gorsel_metni = hucre_metni(hucreler, harita, "gorsel")
    if not gorsel_metni:
        return None
    gorsel_eslesmesi = re.search(r"\b(\d+)\b", gorsel_metni)
    if not gorsel_eslesmesi:
        return None
    gorsel_sayisi = int(gorsel_eslesmesi.group(1))

    return OtelDurumu(
        otel_adi=otel_adi,
        otel_id=otel_id,
        bolge=bolge,
        ham_adi=ham_adi,
        yildiz=yildiz,
        duzenleme_url=duzenleme_url,
        gorsel_sayisi=gorsel_sayisi,
        icerik_tamamlanan=icerik_tamamlanan,
        icerik_toplam=icerik_toplam,
    )


def listeyi_oku(driver):
    try:
        WebDriverWait(driver, 20).until(
            lambda d: bool(satirlari_bul(d))
            or bool(d.find_elements(By.CSS_SELECTOR, "a[href*='/admin/otel/']"))
        )
    except TimeoutException as hata:
        raise RuntimeError("Kep Atlas otel tablosu bulunamadı.") from hata

    harita = baslik_haritasi(driver)
    if "gorsel" not in harita or "icerik" not in harita:
        raise RuntimeError("Görsel veya İçerik sütunu bulunamadı.")

    oteller = []
    sorgu = dict(parse_qsl(urlparse(driver.current_url).query, keep_blank_values=True))
    try:
        filtre_yildiz = int(str(sorgu.get("stars", "0")).split(",", 1)[0] or 0)
    except ValueError:
        filtre_yildiz = 0
    for satir in satirlari_bul(driver):
        try:
            otel = satiri_oku(satir, harita, filtre_yildiz=filtre_yildiz)
            if otel:
                oteller.append(otel)
        except Exception:
            continue
    if not oteller:
        raise RuntimeError("Tablodan okunabilir otel kaydı çıkarılamadı.")
    return oteller


def sayfa_urlsi(liste_url, sayfa, limit):
    parcalar = urlparse(liste_url)
    sorgu = dict(parse_qsl(parcalar.query, keep_blank_values=True))
    sorgu["limit"] = str(limit)
    if sayfa > 1:
        sorgu["page"] = str(sayfa)
    else:
        sorgu.pop("page", None)
    return urlunparse(parcalar._replace(query=urlencode(sorgu)))


def son_sayfa_numarasi(driver, mevcut_sayfa):
    sayfalar = [mevcut_sayfa]
    for link in driver.find_elements(By.CSS_SELECTOR, "nav[aria-label='Sayfalama'] a[href*='page=']"):
        href = link.get_attribute("href") or ""
        eslesme = re.search(r"[?&]page=(\d+)", href)
        if eslesme:
            sayfalar.append(int(eslesme.group(1)))
    return max(sayfalar)


def tum_sayfalari_oku(driver, liste_url, limit=100, max_sayfa=0):
    tum_oteller = []
    gorulen_url = set()
    sayfa = 1
    son_sayfa = 1

    while True:
        hedef_url = sayfa_urlsi(liste_url, sayfa, limit)
        driver.get(hedef_url)
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        oteller = listeyi_oku(driver)

        yeni_oteller = []
        for otel in oteller:
            if otel.duzenleme_url in gorulen_url:
                continue
            gorulen_url.add(otel.duzenleme_url)
            yeni_oteller.append(otel)
        tum_oteller.extend(yeni_oteller)

        son_sayfa = max(son_sayfa, son_sayfa_numarasi(driver, sayfa))
        print(
            f"Sayfa {sayfa}/{son_sayfa}: {len(yeni_oteller)} yeni otel "
            f"(toplam {len(tum_oteller)})"
        )

        if max_sayfa and sayfa >= max_sayfa:
            break
        if sayfa >= son_sayfa:
            break
        sayfa += 1

    return tum_oteller


def rapor_yaz(oteller, rapor_yolu):
    rapor_yolu = Path(rapor_yolu).resolve()
    rapor_yolu.parent.mkdir(parents=True, exist_ok=True)
    with rapor_yolu.open("w", newline="", encoding="utf-8-sig") as dosya:
        yazici = csv.writer(dosya)
        yazici.writerow(
            [
                "otel_id",
                "otel_adi",
                "bolge",
                "ham_adi",
                "yildiz",
                "gorsel_sayisi",
                "icerik_durumu",
                "karar",
                "duzenleme_url",
            ]
        )
        for otel in oteller:
            yazici.writerow(
                [
                    otel.otel_id,
                    otel.otel_adi,
                    otel.bolge,
                    otel.ham_adi,
                    otel.yildiz,
                    otel.gorsel_sayisi,
                    f"{otel.icerik_tamamlanan}/{otel.icerik_toplam}",
                    otel.karar,
                    otel.duzenleme_url,
                ]
            )
    return rapor_yolu


def ozet_yaz(oteller):
    sayac = {
        "ATLA": 0,
        "SADECE_GORSEL": 0,
        "SADECE_ICERIK": 0,
        "ICERIK_VE_GORSEL": 0,
    }
    for otel in oteller:
        sayac[otel.karar] += 1
        print(
            f"[{otel.karar:18}] {otel.otel_adi} | "
            f"görsel={otel.gorsel_sayisi} | "
            f"içerik={otel.icerik_tamamlanan}/{otel.icerik_toplam}"
        )
    print("\nÖZET")
    print(f"Okunan otel: {len(oteller)}")
    for karar, adet in sayac.items():
        print(f"{karar}: {adet}")


def argumanlari_oku():
    parser = argparse.ArgumentParser(
        description="Kep Atlas otel listesini değiştirmeden tarar ve yapılacak işi belirler."
    )
    parser.add_argument("--list-url", default=VARSAYILAN_LISTE_URL)
    parser.add_argument("--rapor", default="otel_durum_raporu.csv")
    parser.add_argument("--limit", type=int, choices=(25, 50, 100), default=100)
    parser.add_argument(
        "--max-sayfa",
        type=int,
        default=0,
        help="0 bütün sayfaları, pozitif sayı belirtilen kadar sayfayı tarar.",
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--beklet",
        action="store_true",
        help="Tarama sonunda Chrome penceresini açık bırakır.",
    )
    return parser.parse_args()


def main():
    kimlik.ortami_hazirla()
    args = argumanlari_oku()
    eposta = os.getenv("KEP_ATLAS_EMAIL", "").strip()
    sifre = os.getenv("KEP_ATLAS_PASSWORD", "")
    if not eposta:
        eposta = input("Kep Atlas e-posta: ").strip()
    if not sifre:
        sifre = getpass.getpass("Kep Atlas şifre: ")

    driver = tarayiciyi_ac(args.headless)
    try:
        print("Kep Atlas listesi açılıyor (salt-okunur tarama)...")
        giris_yap(driver, args.list_url, eposta, sifre)
        time.sleep(1)
        oteller = tum_sayfalari_oku(
            driver,
            args.list_url,
            limit=args.limit,
            max_sayfa=max(0, args.max_sayfa),
        )
        ozet_yaz(oteller)
        rapor = rapor_yaz(oteller, args.rapor)
        print(f"\nRapor oluşturuldu: {rapor}")
        if args.beklet:
            input("Tarayıcıyı kapatmak için Enter'a basın...")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
