import hashlib
import io
import os
import queue
import re
import shutil
import threading
import time
import tkinter as tk
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import messagebox, scrolledtext
from urllib.parse import urljoin, urlparse

import requests
try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None
    ImageOps = None

# Eski Pillow sürümlerinde AVIF desteğini kaydeder; yeni sürümlerde gerekmez.
try:
    import pillow_avif  # noqa: F401
except ImportError:
    pass

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait


gorev_kuyrugu = queue.Queue()
tarayici_driver = None


class _SessizUygulama:
    """app parametresi verilmediğinde log_yaz çağrılarını sessizce yutar."""

    def log_yaz(self, mesaj):
        pass


def metni_normallestir(metin):
    metin = unicodedata.normalize("NFKD", metin or "")
    metin = "".join(harf for harf in metin if not unicodedata.combining(harf))
    return " ".join(metin.casefold().split())


def guvenli_klasor_adi(admin_url):
    parca = urlparse(admin_url).path.rstrip("/").split("/")[-1] or "otel"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", parca)


WINDOWS_YASAK_DOSYA_ADLARI = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{sira}" for sira in range(1, 10)),
    *(f"LPT{sira}" for sira in range(1, 10)),
}


def guvenli_gorsel_adi(otel_adi, azami_uzunluk=120):
    """Otel adini Windows'ta guvenli bir dosya adi parcasina donusturur."""
    ad = unicodedata.normalize("NFKC", str(otel_adi or ""))
    ad = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", ad)
    ad = " ".join(ad.split()).strip(" .")
    if not ad:
        ad = "Otel"

    # CON.jpg ve COM1.png gibi adlar Windows'ta daima gecersizdir.
    if ad.upper() in WINDOWS_YASAK_DOSYA_ADLARI:
        ad = f"Otel {ad}"

    azami_uzunluk = max(1, int(azami_uzunluk))
    ad = ad[:azami_uzunluk].rstrip(" .")
    if ad.upper() in WINDOWS_YASAK_DOSYA_ADLARI:
        ad = (f"Otel {ad}")[:azami_uzunluk].rstrip(" .")
    return ad or "Otel"


def gorsel_dosya_yolu(klasor_adi, otel_adi, sira, uzanti):
    """`Otel Adi 1.jpg` biciminde, makul uzunlukta mutlak yol uretir."""
    klasor = os.path.abspath(klasor_adi)
    uzanti = str(uzanti or ".jpg").lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,5}", uzanti):
        uzanti = ".jpg"

    son_ek = f" {max(1, int(sira))}{uzanti}"
    # Eski Windows araclari ve tarayici dosya yukleme alanlari icin toplam yolu
    # 240 karakter civarinda tut. Klasor cok uzunsa otel adini en az 1 karaktere indir.
    kullanilabilir = max(1, min(120, 240 - len(klasor) - 1 - len(son_ek)))
    guvenli_ad = guvenli_gorsel_adi(otel_adi, kullanilabilir)
    return os.path.abspath(os.path.join(klasor, f"{guvenli_ad}{son_ek}"))


def kaynak_site_turu(kaynak_url):
    host = urlparse(kaynak_url).netloc.lower().split(":")[0]
    if host == "hotels.com" or host.endswith(".hotels.com"):
        return "hotels"
    if host == "etstur.com" or host.endswith(".etstur.com"):
        return "ets"
    if host == "obilet.com" or host.endswith(".obilet.com"):
        return "obilet"
    if host == "trivago.com" or ".trivago." in host:
        return "trivago"
    return "genel"


def tarayiciyi_hazirla(app):
    global tarayici_driver

    acik = False
    if tarayici_driver is not None:
        try:
            _ = tarayici_driver.current_url
            acik = True
        except Exception:
            acik = False

    if not acik:
        app.log_yaz("Chrome açılıyor...")
        secenekler = webdriver.ChromeOptions()
        secenekler.add_argument("--disable-notifications")
        secenekler.add_experimental_option(
            "prefs",
            {"profile.default_content_setting_values.notifications": 2},
        )
        tarayici_driver = webdriver.Chrome(options=secenekler)
        tarayici_driver.maximize_window()
    else:
        # Uzun calismalarda her otel icin acilan sekmeler RAM'i tuketiyordu.
        # En yeni kontrol sekmeleri tutulur, daha eskileri otomatik kapatilir.
        sekme_limiti = max(2, int(os.getenv("KEP_CHROME_SEKME_LIMITI", "2")))
        while len(tarayici_driver.window_handles) >= sekme_limiti:
            eski_sekme = tarayici_driver.window_handles[0]
            tarayici_driver.switch_to.window(eski_sekme)
            tarayici_driver.close()
            tarayici_driver.switch_to.window(tarayici_driver.window_handles[-1])

        # Önceki otelin Kep Atlas sayfasını açık bırak. Her yeni görev kendi
        # sekmesinde çalışsın; kullanıcı tamamlanan otelleri kontrol edebilsin.
        try:
            tarayici_driver.switch_to.new_window("tab")
            app.log_yaz("Yeni otel için yeni bir Chrome sekmesi açıldı.")
        except Exception as hata:
            raise RuntimeError(f"Yeni otel sekmesi açılamadı: {hata}") from hata

    return tarayici_driver


def gorunen_metne_tikla(driver, arananlar, sadece_ust_kisim=False):
    """Görünür bağlantı/buton/sekme öğeleri arasından en iyi eşleşmeye tıklar."""
    arananlar = [metni_normallestir(x) for x in arananlar]
    script = r"""
        const terms = arguments[0];
        const onlyTop = arguments[1];
        const normalize = (value) => (value || '')
            .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
            .toLocaleLowerCase('tr-TR').replace(/\s+/g, ' ').trim();

        const selector = 'button, a, [role="tab"], [role="button"], [onclick]';
        const candidates = [];
        for (const el of document.querySelectorAll(selector)) {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            if (rect.width < 8 || rect.height < 8 || style.display === 'none' ||
                style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
            if (onlyTop && (rect.top < -20 || rect.top > innerHeight * 0.55)) continue;

            const text = normalize(el.innerText || el.textContent ||
                el.getAttribute('aria-label') || el.getAttribute('title'));
            if (!text) continue;
            for (const term of terms) {
                if (text === term || text.includes(term)) {
                    let score = text === term ? 100 : 50;
                    if (el.getAttribute('role') === 'tab') score += 20;
                    if (el.tagName === 'BUTTON' || el.tagName === 'A') score += 10;
                    score -= Math.max(0, rect.top) / 10000;
                    candidates.push({el, score, text});
                    break;
                }
            }
        }
        candidates.sort((a, b) => b.score - a.score);
        if (!candidates.length) return null;
        const winner = candidates[0];
        winner.el.scrollIntoView({block: 'center', inline: 'center'});
        winner.el.click();
        return winner.text;
    """
    try:
        return driver.execute_script(script, arananlar, sadece_ust_kisim)
    except Exception:
        return None


def cerez_uyarisini_kapat(driver):
    gorunen_metne_tikla(
        driver,
        [
            "Tümünü kabul et",
            "Kabul et",
            "Reddet",
            "Çerezleri kabul et",
            "Accept all",
            "Reject all",
            "Allow all",
        ],
    )


def acilis_takvimini_kapat(driver, app):
    """Trivago açılışta tarih takvimini gösterirse kapatır."""
    app.log_yaz("Açılıştaki tarih penceresi kapatılıyor...")

    # Trivago akışı: yalnızca bir kez Escape gönder; başka öğeye tıklama.
    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
    except Exception:
        try:
            driver.switch_to.active_element.send_keys(Keys.ESCAPE)
        except Exception:
            pass
    time.sleep(1.0)


def hotels_takvimini_kapat(driver, app):
    """Hotels.com açılış tarih penceresini, tarih düğmesine ESC göndererek kapatır."""
    takvim_scripti = r"""
        const normalize = (value) => (value || '')
            .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
            .toLocaleLowerCase('tr-TR').replace(/\s+/g, ' ').trim();
        const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 20 && r.height > 20 && r.bottom > 0 &&
                   r.top < innerHeight && s.display !== 'none' &&
                   s.visibility !== 'hidden' && Number(s.opacity) !== 0;
        };

        return [...document.querySelectorAll('[role="dialog"], [aria-modal="true"]')]
            .some(el => {
                if (!visible(el)) return false;
                const text = normalize([
                    el.getAttribute('aria-label'), el.innerText, el.textContent
                ].filter(Boolean).join(' '));
                return el.matches('.uitk-date-selector-popover') ||
                       text.includes('bir tarih araligi secin') ||
                       text.includes('select a date range');
            });
    """
    dugme_scripti = r"""
        const normalize = (value) => (value || '')
            .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
            .toLocaleLowerCase('tr-TR').replace(/\s+/g, ' ').trim();
        const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 20 && r.height > 20 && r.bottom > 0 &&
                   r.top < innerHeight && s.display !== 'none' &&
                   s.visibility !== 'hidden' && Number(s.opacity) !== 0;
        };

        const expanded = [...document.querySelectorAll('button[aria-expanded="true"]')]
            .filter(visible);
        const dateButtons = [...document.querySelectorAll('button')].filter(el => {
            if (!visible(el)) return false;
            const text = normalize([
                el.getAttribute('aria-label'), el.getAttribute('title'),
                el.innerText, el.textContent
            ].filter(Boolean).join(' '));
            return text.includes('tarihler') || text.includes('dates') ||
                   text.includes('check-in') || text.includes('check in');
        });
        return expanded[0] || dateButtons[0] || null;
    """

    try:
        if not driver.execute_script(takvim_scripti):
            return False
        tarih_dugmesi = driver.execute_script(dugme_scripti)
        if tarih_dugmesi is None:
            return False
        driver.execute_script("arguments[0].focus();", tarih_dugmesi)
        tarih_dugmesi.send_keys(Keys.ESCAPE)
        bitis = time.time() + 2
        while time.time() < bitis:
            if not driver.execute_script(takvim_scripti):
                app.log_yaz("Hotels.com açılış tarih penceresi kapatıldı.")
                return True
            time.sleep(0.2)
        return False
    except Exception:
        return False


def hotels_oneri_penceresini_kapat(driver, app, bekleme_suresi=0.0):
    """Yalnızca Hotels.com'un benzer tesis öneri penceresini güvenle kapatır."""
    modal_scripti = r"""
        const normalize = (value) => (value || '')
            .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
            .toLocaleLowerCase('tr-TR').replace(/\s+/g, ' ').trim();
        const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 200 && r.height > 150 && r.bottom > 0 &&
                   r.top < innerHeight && s.display !== 'none' &&
                   s.visibility !== 'hidden' && Number(s.opacity) !== 0;
        };
        const isOfferModal = (el) => {
            if (!visible(el)) return false;
            const text = normalize(el.innerText || el.textContent);
            const turkish = text.includes('gitmeden once') &&
                text.includes('benzer konaklama yerlerine');
            const english = text.includes('before you go') &&
                (text.includes('similar properties') || text.includes('similar stays'));
            return turkish || english;
        };
        const roots = [...document.querySelectorAll([
            '[role="dialog"]', '[aria-modal="true"]',
            '.uitk-dialog-layer.layer-overlay-active', '.uitk-centered-sheet'
        ].join(','))].filter(isOfferModal);
        if (!roots.length) return {open: false, close: null};

        for (const root of roots) {
            const buttons = [...root.querySelectorAll('button')].filter(button => {
                const r = button.getBoundingClientRect();
                const s = getComputedStyle(button);
                return r.width > 10 && r.height > 10 && s.display !== 'none' &&
                       s.visibility !== 'hidden' && Number(s.opacity) !== 0;
            });
            const exact = buttons.find(button => {
                const label = normalize([
                    button.getAttribute('aria-label'), button.getAttribute('title'),
                    button.innerText, button.textContent
                ].filter(Boolean).join(' '));
                return label.includes('kapat dugmesi') || label === 'kapat' ||
                       label.includes('close button') || label === 'close' ||
                       label.includes('dismiss');
            });
            if (exact) return {open: true, close: exact};

            const dataClose = root.querySelector(
                '[data-stid*="close" i], .uitk-dialog-close, button[aria-label*="close" i]'
            );
            if (dataClose) return {open: true, close: dataClose};
        }
        return {open: true, close: null};
    """

    bitis = time.time() + max(0.0, bekleme_suresi)
    modal_goruldu = False
    while True:
        try:
            durum = driver.execute_script(modal_scripti) or {}
            if not durum.get("open"):
                if modal_goruldu:
                    return True
                if time.time() >= bitis:
                    return False
                time.sleep(0.2)
                continue
            modal_goruldu = True
            kapat = durum.get("close")
            if kapat is None:
                return False
            try:
                kapat.click()
            except Exception:
                driver.execute_script("arguments[0].click();", kapat)

            kontrol_bitis = time.time() + 2
            while time.time() < kontrol_bitis:
                if not (driver.execute_script(modal_scripti) or {}).get("open"):
                    app.log_yaz("Hotels.com benzer konaklama öneri penceresi kapatıldı.")
                    return True
                time.sleep(0.2)
        except Exception:
            pass

        if time.time() >= bitis:
            return False
        time.sleep(0.2)


def hotels_galerisini_ac(driver, app):
    """Hotels.com üst fotoğraf mozaiğindeki ilk gerçek otel fotoğrafına tıklar."""
    aday_scripti = r"""
        const normalize = (value) => (value || '')
            .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
            .toLocaleLowerCase('tr-TR').replace(/\s+/g, ' ').trim();
        const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 20 && r.height > 20 && r.bottom > 0 &&
                   r.top < innerHeight && s.display !== 'none' &&
                   s.visibility !== 'hidden' && Number(s.opacity) !== 0;
        };

        const main = document.querySelector('main');
        if (!main) return [];

        // Oda kartlarına veya önerilen otellere değil, yalnızca sayfanın üstündeki
        // "... için fotoğraf galerisi" başlığının ait olduğu mozaiğe gir.
        const galleryHeading = [...main.querySelectorAll('h2, [role="heading"]')]
            .find(el => {
                const text = normalize(el.innerText || el.textContent);
                return text.includes('icin fotograf galerisi') ||
                       text.includes('icin resim galerisi') ||
                       text.includes('photo gallery for') ||
                       text.includes('image gallery for');
            });
        if (!galleryHeading || galleryHeading.closest(
            '[role="dialog"], [aria-modal="true"], .uitk-dialog-layer, .uitk-centered-sheet'
        )) return [];

        const tabTexts = [
            'genel bakis', 'overview', 'odalar', 'rooms',
            'konum', 'location', 'politikalar', 'policies'
        ];
        const tabTops = [...main.querySelectorAll('a, button, [role="tab"]')]
            .filter(el => tabTexts.includes(normalize(el.innerText || el.textContent)))
            .map(el => el.getBoundingClientRect().top + scrollY);
        const firstTabTop = tabTops.length ? Math.min(...tabTops) : Infinity;

        const lodgingImage = (img) => {
            if (!img) return false;
            try {
                const url = new URL(img.currentSrc || img.src || '', document.baseURI);
                return url.hostname === 'images.trvl-media.com' &&
                       url.pathname.includes('/lodging/');
            } catch (_) {
                return false;
            }
        };

        let root = galleryHeading.parentElement;
        for (let level = 0; level < 7 && root && root !== main; level++, root = root.parentElement) {
            const mosaicImages = [...root.querySelectorAll('img')]
                .filter(img => {
                    if (!lodgingImage(img)) return false;
                    const rect = img.getBoundingClientRect();
                    return rect.width * rect.height >= 12000 &&
                           rect.top + scrollY < firstTabTop;
                });
            // Hotels.com görseli <figure> içinde, tıklama düğmesini ise onun
            // üstünde ayrı bir kardeş katman olarak tutabiliyor. Bu yüzden
            // DOM ebeveynliği yerine ekrandaki geometrik çakışmayı doğrula.
            const candidates = [...root.querySelectorAll('button, [role="button"]')]
                .filter(button => {
                    if (!visible(button)) return false;
                    if (button.closest(
                        '[role="dialog"], [aria-modal="true"], .uitk-dialog-layer, .uitk-centered-sheet'
                    )) return false;
                    const rect = button.getBoundingClientRect();
                    const documentTop = rect.top + scrollY;
                    if (rect.width * rect.height < 20000 || documentTop >= firstTabTop) {
                        return false;
                    }
                    return mosaicImages.some(img => {
                        const imageRect = img.getBoundingClientRect();
                        const overlapWidth = Math.max(
                            0, Math.min(rect.right, imageRect.right) -
                               Math.max(rect.left, imageRect.left)
                        );
                        const overlapHeight = Math.max(
                            0, Math.min(rect.bottom, imageRect.bottom) -
                               Math.max(rect.top, imageRect.top)
                        );
                        const overlapArea = overlapWidth * overlapHeight;
                        const smallerArea = Math.min(
                            rect.width * rect.height,
                            imageRect.width * imageRect.height
                        );
                        return smallerArea > 0 && overlapArea / smallerArea >= 0.7;
                    });
                });

            // Bazı Hotels.com düzenlerinde birkaç fotoğraf tek bir düğmenin içinde
            // olabilir. Bu yüzden fotoğraf sayısını ve düğme sayısını ayrı doğrula.
            if (mosaicImages.length >= 3 && candidates.length >= 1) {
                candidates.sort((a, b) => {
                    const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
                    return (br.width * br.height) - (ar.width * ar.height) || ar.top - br.top;
                });
                return candidates;
            }
        }
        return [];
    """

    bitis = time.time() + 15
    denenmis = set()
    while time.time() < bitis:
        if hotels_oneri_penceresini_kapat(driver, app):
            denenmis.clear()
        hotels_takvimini_kapat(driver, app)
        if galeri_penceresi_acik_mi(driver, "hotels"):
            return True
        try:
            adaylar = driver.execute_script(aday_scripti) or []
        except Exception:
            adaylar = []
        for dugme in adaylar:
            try:
                if dugme.id in denenmis:
                    continue
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'nearest', inline:'nearest'});",
                    dugme,
                )
                time.sleep(0.2)

                # Gecikmeli öneri penceresi açıldıysa önce onu kapatıp mozaiği
                # yeniden bul. Arkadaki fotoğrafa kör JavaScript tıklaması yapma.
                if hotels_oneri_penceresini_kapat(driver, app):
                    denenmis.clear()
                    hotels_takvimini_kapat(driver, app)
                    break

                tiklanabilir = driver.execute_script(
                    """
                    const el = arguments[0];
                    const r = el.getBoundingClientRect();
                    const x = Math.max(0, Math.min(innerWidth - 1, r.left + r.width / 2));
                    const y = Math.max(0, Math.min(innerHeight - 1, r.top + r.height / 2));
                    const top = document.elementFromPoint(x, y);
                    return !!top && (top === el || el.contains(top));
                    """,
                    dugme,
                )
                if not tiklanabilir:
                    continue

                denenmis.add(dugme.id)
                try:
                    dugme.click()
                except Exception:
                    # Yalnız merkez noktasında hâlâ bu fotoğraf bulunuyorsa güvenli
                    # JS tıklaması yap; modal arkasındaki öğeye tıklama.
                    hala_tiklanabilir = driver.execute_script(
                        """
                        const el = arguments[0], r = el.getBoundingClientRect();
                        const top = document.elementFromPoint(
                            r.left + r.width / 2, r.top + r.height / 2
                        );
                        return !!top && (top === el || el.contains(top));
                        """,
                        dugme,
                    )
                    if not hala_tiklanabilir:
                        continue
                    driver.execute_script("arguments[0].click();", dugme)

                kontrol_bitis = time.time() + 8
                while time.time() < kontrol_bitis:
                    if galeri_penceresi_acik_mi(driver, "hotels"):
                        app.log_yaz(
                            "Hotels.com üst mozaiğindeki ilk fotoğrafa tıklandı; galeri açıldı."
                        )
                        return True
                    time.sleep(0.25)
            except Exception:
                continue
        time.sleep(0.4)
    return False


def trivago_ust_fotografina_tikla(driver, app):
    """Yalnızca Trivago'nun üst fotoğraf mozaiğindeki gerçek butonlara tıklar."""
    aday_scripti = r"""
        const normalize = (value) => (value || '')
            .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
            .toLocaleLowerCase('tr-TR').replace(/\s+/g, ' ').trim();
        const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width >= 120 && r.height >= 80 &&
                   r.top >= 120 && r.top < innerHeight && r.bottom > 0 &&
                   s.display !== 'none' && s.visibility !== 'hidden' &&
                   Number(s.opacity) !== 0;
        };

        const trivagoImage = (img) => {
            try {
                const r = img.getBoundingClientRect();
                const url = new URL(img.currentSrc || img.src || '', document.baseURI);
                return url.hostname === 'imgcy.trivago.com' &&
                       r.width >= 120 && r.height >= 80;
            } catch (_) {
                return false;
            }
        };

        const candidates = [...document.querySelectorAll('button, [role="button"]')].filter(button => {
            if (!visible(button)) return false;
            const aria = normalize(
                button.getAttribute('aria-label') || button.getAttribute('title')
            );
            const text = normalize(button.innerText || button.textContent);
            const galleryLabel = aria.includes('tam ekran galeri') ||
                aria.includes('full screen gallery') ||
                aria.includes('fullscreen gallery');
            const showAllText = text.includes('tum fotograflari goster') ||
                text.includes('show all photos') || text.includes('view all photos');
            // Bazi Trivago otellerinde butonun aria/metni yoktur; ana mozaikteki
            // imgcy resmi yine de gercek ve tiklanabilir galeri girisidir.
            const hotelImageButton = [...button.querySelectorAll('img')].some(trivagoImage);
            return galleryLabel || (showAllText && !!button.querySelector('img')) ||
                   hotelImageButton;
        });

        candidates.sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            const at = normalize(a.innerText || a.textContent);
            const bt = normalize(b.innerText || b.textContent);
            const aAll = at.includes('tum fotograflari goster') || at.includes('show all photos');
            const bAll = bt.includes('tum fotograflari goster') || bt.includes('show all photos');
            if (aAll !== bAll) return bAll - aAll;
            // Metinsiz yedekte sayfanin en ustundeki ve en buyuk otel resmi
            // onerilen otel kartlarindan once denenir.
            if (Math.abs(br.top - ar.top) > 25) return ar.top - br.top;
            return (br.width * br.height) - (ar.width * ar.height);
        });
        return candidates;
    """

    bitis = time.time() + 15
    denenmis = set()
    while time.time() < bitis:
        try:
            adaylar = driver.execute_script(aday_scripti) or []
        except Exception:
            adaylar = []

        for fotograf_butonu in adaylar:
            try:
                if fotograf_butonu.id in denenmis:
                    continue
                denenmis.add(fotograf_butonu.id)
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'nearest', inline:'nearest'});",
                    fotograf_butonu,
                )
                time.sleep(0.25)
                try:
                    fotograf_butonu.click()
                except Exception:
                    ActionChains(driver).move_to_element(fotograf_butonu).click().perform()

                kontrol_bitis = time.time() + 8
                while time.time() < kontrol_bitis:
                    if galeri_penceresi_acik_mi(driver, "trivago"):
                        app.log_yaz("Üst bölümdeki fotoğraf butonuna tıklandı.")
                        return True
                    time.sleep(0.25)
            except Exception:
                continue
        time.sleep(0.5)
    return False


def fotograf_mozayigi_sag_alt_kutuya_tikla(driver):
    """Metin DOM'da yoksa galerideki sağ-alt fotoğraf kutusuna tıklar."""
    script = r"""
        const visibleImages = [...document.querySelectorAll('img')].filter(img => {
            const r = img.getBoundingClientRect();
            const s = getComputedStyle(img);
            return r.width >= 150 && r.height >= 100 && r.top >= 220 &&
                   r.top < innerHeight && r.left >= 0 && r.left < innerWidth &&
                   s.display !== 'none' && s.visibility !== 'hidden';
        });

        // Fotoğraf mozaiğinde önce en aşağıdaki, eşitse en sağdaki görseli seç.
        visibleImages.sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            if (Math.abs(br.top - ar.top) > 30) return br.top - ar.top;
            return br.left - ar.left;
        });
        if (!visibleImages.length) return false;

        const img = visibleImages[0];
        let target = img.closest('button, a, [role="button"], [onclick]');
        if (!target) {
            let parent = img.parentElement;
            for (let i = 0; i < 6 && parent; i++, parent = parent.parentElement) {
                const r = parent.getBoundingClientRect();
                const s = getComputedStyle(parent);
                if ((s.cursor === 'pointer' || parent.hasAttribute('tabindex')) &&
                    r.width < innerWidth * 0.7 && r.height < innerHeight * 0.7) {
                    target = parent;
                    break;
                }
            }
        }
        target = target || img.parentElement || img;
        target.scrollIntoView({block: 'center', inline: 'center'});
        target.click();
        return true;
    """
    try:
        return bool(driver.execute_script(script))
    except Exception:
        return False


def galeri_penceresi_acik_mi(driver, site_turu):
    """Kaynak sitenin gerçek fotoğraf galerisinin açıldığını doğrular."""
    script = r"""
        const site = arguments[0];
        let selector;
        if (site === 'ets') {
            selector = '.modal-container, [class*="MuiModal-root"]';
        } else if (site === 'obilet') {
            selector = '.gallery-modal.open, .ob-modal-full.open, .ob-modal-overlay.open';
        } else if (site === 'hotels') {
            selector = '[role="dialog"], .uitk-dialog-layer.layer-overlay-active, .uitk-centered-sheet';
        } else if (site === 'trivago') {
            selector = '[role="dialog"], dialog, [aria-modal="true"]';
        } else {
            return false;
        }

        const normalCandidates = [...document.querySelectorAll(selector)];
        const fixedOverlays = site === 'trivago'
            ? [...document.querySelectorAll('div')].filter(el => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return s.position === 'fixed' &&
                       r.width >= innerWidth * 0.7 && r.height >= innerHeight * 0.7;
            })
            : [];
        return [...new Set([...normalCandidates, ...fixedOverlays])].some(el => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            const visible = r.width > 200 && r.height > 150 &&
                s.display !== 'none' && s.visibility !== 'hidden' &&
                Number(s.opacity) !== 0;
            if (!visible) return false;

            const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
            const etsGallery = site === 'ets' && /^Galeri(?:\s|Tümü|Genel)/i.test(text) &&
                /Tümü\s*\(\d+\)/i.test(text);
            const hotelsImages = site === 'hotels'
                ? [...el.querySelectorAll('img')].filter(img => {
                    try {
                        const url = new URL(img.currentSrc || img.src || '', document.baseURI);
                        return url.hostname === 'images.trvl-media.com' &&
                               url.pathname.includes('/lodging/');
                    } catch (_) {
                        return false;
                    }
                }).length
                : 0;
            const hotelsBackButton = site === 'hotels' &&
                [...el.querySelectorAll('button')].some(button => {
                    const aria = (button.getAttribute('aria-label') || '')
                        .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
                        .toLocaleLowerCase('tr-TR');
                    return aria.includes('konaklama detaylarina geri don') ||
                           aria.includes('konaklama detaylarina don') ||
                           aria.includes('property details');
                });
            const hotelsGallery = site === 'hotels' && hotelsImages >= 3 &&
                (new URLSearchParams(location.search).get('pwaThumbnailDialog') ===
                    'thumbnail-gallery' ||
                 hotelsBackButton);
            if (site === 'hotels') return hotelsGallery;
            if (site === 'trivago') {
                const trivagoImages = [...el.querySelectorAll('img')].filter(img => {
                    try {
                        const url = new URL(img.currentSrc || img.src || '', document.baseURI);
                        return url.hostname === 'imgcy.trivago.com';
                    } catch (_) {
                        return false;
                    }
                }).length;
                return trivagoImages >= 2;
            }
            return etsGallery || el.querySelectorAll('img').length >= 5;
        });
    """
    try:
        return bool(driver.execute_script(script, site_turu))
    except Exception:
        return False


def obilet_reklamini_kapat(driver, app):
    """Galerinin üstünü örten Obilet kampanya/indirim penceresini kapatır.

    Obilet farklı zamanlarda farklı pazarlama araçları kullanabiliyor: bilinen
    Insider widget'ı (`.ins-preview-wrapper`) yanında, örn. "Bugüne özel %20
    net indirim" gibi kendi kupon kartı da galeri üstünü kaplayıp tıklamaları
    engelleyebiliyor. Bu ikinci tür sabit bir CSS sınıfı taşımadığından, önce
    bilinen Insider seçicisi denenir; bulunamazsa ekranın ortasında duran,
    indirim/kupon içerikli herhangi bir sabit/mutlak konumlu kart içinde
    sağ-üst köşeye yakın küçük bir kapatma kontrolü aranır.
    """
    kapatma_scripti = r"""
        const visible = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 10 && r.height > 10 && r.bottom > 0 &&
                   r.right > 0 && r.top < innerHeight && r.left < innerWidth &&
                   s.display !== 'none' && s.visibility !== 'hidden' &&
                   Number(s.opacity) !== 0;
        };
        const normalize = (value) => (value || '')
            .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
            .toLocaleLowerCase('tr-TR').replace(/\s+/g, ' ').trim();

        for (const popup of document.querySelectorAll('.ins-preview-wrapper')) {
            if (!visible(popup)) continue;
            const close = popup.querySelector([
                '.ins-element-close-button',
                '[id^="wrap-close-button-"]',
                '[id^="close-button-"]'
            ].join(','));
            if (visible(close)) return close;
        }

        // Insider disinda, sabit CSS sinifi bilinmeyen kupon/indirim kartlari
        // icin genel tespit: ekran ortasinda duran, indirim/kupon anahtar
        // kelimeleri gecen sabit/mutlak konumlu bir kok bul.
        const ANAHTAR_KELIMELER = [
            'indirim', 'kupon', 'firsat', 'net discount', 'discount code',
            'indirim kodu', 'bugune ozel', 'today only', 'sms ile al',
            'valid for today', 'discount', 'kesfet, katil, kazan',
        ];
        const genisEkranOrtasi = (r) => r.width >= 200 && r.height >= 150 &&
            r.width <= innerWidth * 0.97 && r.height <= innerHeight * 0.97;

        const kokAdaylari = [...document.querySelectorAll('div, section, aside')]
            .filter((el) => {
                if (!visible(el)) return false;
                const s = getComputedStyle(el);
                if (s.position !== 'fixed' && s.position !== 'absolute') return false;
                const r = el.getBoundingClientRect();
                if (!genisEkranOrtasi(r)) return false;
                const metin = normalize(el.innerText || el.textContent || '');
                if (!metin) return false;
                return ANAHTAR_KELIMELER.some((kelime) => metin.includes(kelime)) ||
                    !!el.querySelector('input[readonly], input[disabled]');
            })
            // Tam sayfayi kaplayan seffaf arka plan degil, asil kupon karti
            // genelde en kucuk eslesen kapsayicidir.
            .sort((a, b) => {
                const ar = a.getBoundingClientRect();
                const br = b.getBoundingClientRect();
                return (ar.width * ar.height) - (br.width * br.height);
            });

        for (const kok of kokAdaylari) {
            const kokRect = kok.getBoundingClientRect();
            const adaylar = [...kok.querySelectorAll(
                'button, a, [role="button"], svg, span, div, i'
            )];
            let enIyi = null;
            let enIyiPuan = 0;
            for (const el of adaylar) {
                if (!visible(el)) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 6 || r.width > 72 || r.height < 6 || r.height > 72) continue;
                const aria = normalize(
                    el.getAttribute('aria-label') || el.getAttribute('title') || ''
                );
                const metin = normalize(el.textContent || '');
                const classAd = normalize(
                    (el.className && el.className.toString) ? el.className.toString() : ''
                );
                let puan = 0;
                if (aria.includes('kapat') || aria.includes('close') || aria.includes('dismiss')) puan += 50;
                if (classAd.includes('close') || classAd.includes('kapat')) puan += 40;
                if (['x', '×', '✕', '✖'].includes(metin)) puan += 30;
                const sagOrani = (r.left - kokRect.left) / Math.max(1, kokRect.width);
                const yukariOrani = (r.top - kokRect.top) / Math.max(1, kokRect.height);
                if (sagOrani >= 0.72 && yukariOrani <= 0.30) puan += 25;
                if (puan > enIyiPuan) {
                    enIyiPuan = puan;
                    enIyi = el;
                }
            }
            if (enIyi && enIyiPuan >= 25) return enIyi;
        }
        return null;
    """

    # Kampanya birkaç saniye gecikmeli gösterilebildiği için kısa süre izle.
    # Hızlı sürüm ilk açılışta kısa kontrol yapar; gecikmeli reklamlar galeri
    # arama döngüsünde ayrıca kapatılır.
    bitis = time.time() + 1.2
    while time.time() < bitis:
        try:
            kapat = driver.execute_script(kapatma_scripti)
            if kapat is not None:
                # Koordinat tıklaması alttaki galeriye gidebildiğinden kampanyanın
                # kendi kapatma olayını doğrudan çalıştır.
                driver.execute_script("arguments[0].click();", kapat)
                time.sleep(1)
                app.log_yaz("Obilet kampanya penceresi kapatıldı.")
                return True
        except Exception:
            pass
        time.sleep(0.4)

    # Kapatma kontrolü hiç bulunamadıysa, kartın kendisi hâlâ ekranı
    # kaplıyor olabilir. Son çare olarak Escape dener; birçok kupon/modal
    # bileşeni klavye ile kapanacak şekilde yazılır ve bu galeriyi
    # engelleyen katmanı temizleyebilir.
    try:
        if driver.execute_script(
            "return !!document.querySelector('input[readonly], input[disabled]')"
        ):
            ActionChains(driver).send_keys(Keys.ESCAPE).perform()
            time.sleep(0.5)
    except Exception:
        pass
    return False


class KaynakGorselsizHatasi(RuntimeError):
    """Kaynak sayfasi acildi ama tesise ait hic fotograf yok.

    Obilet, tasimadigi oteller icin otel adi ve adresi dogru olan fakat
    fotografsiz SEO sayfalari uretiyor. Bu sayfalar kaynak dogrulamasini
    gectigi icin gorsel asamasina kadar geliyor, orada da galeri
    acilamadigi icin 'galeri acilamadi' hatasina donusuyordu. Iki durum
    ayni kutuya dustugu surece, tekrar denemekle asla cozulmeyecek
    oteller sonsuza kadar yeniden deneniyor. Bu hata onlari ayirir.
    """


GORSELSIZ_TESPIT_SCRIPTI = r"""
    const normalize = (value) => (value || '')
        .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
        .toLocaleLowerCase('tr-TR').replace(/\s+/g, ' ').trim();

    // 1) '+N Fotograf' / 'Galeri (N)' rozeti: fotograf VARSA gorunur.
    let rozet = false;
    for (const el of document.querySelectorAll('p, span, div, button, a')) {
        if (el.children.length) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 8 || r.height < 8) continue;
        const t = normalize(el.textContent);
        if (/^\+?\d+\s*fotograf(?:lar)?$/.test(t) || /^galeri\s*\(\d+\)$/.test(t)) {
            rozet = true; break;
        }
    }

    // 2) Kapak gorseli placeholder mi?
    const kaynaklar = [];
    for (const i of document.querySelectorAll('img')) {
        const u = i.currentSrc || i.src || '';
        if (u) kaynaklar.push(u);
    }
    for (const e of document.querySelectorAll('*')) {
        const v = getComputedStyle(e).backgroundImage;
        if (v && v !== 'none' && v.includes('http')) {
            kaynaklar.push(v.slice(v.indexOf('http')).replace(/["')].*$/, ''));
        }
    }
    const placeholder = kaynaklar.some(u => /placeholder-cover|placeholder\/placeholder/i.test(u));

    return { rozet: rozet, placeholder: placeholder, gorsel_adedi: kaynaklar.length };
"""


def kaynak_gorselsiz_mi(driver, site_turu, app=None):
    """Sayfada tesise ait hic fotograf olmadigini POZITIF kanitla belirler.

    Yalniz olculmus isaretlere guvenir: fotograf sayisi rozetinin yoklugu ve
    kapak gorselinin placeholder olmasi. Ikisi birden saglanmadikca False
    doner; yani emin olunmayan sayfa gorselsiz sayilmaz ve normal galeri
    akisi denenmeye devam eder.

    Not: 'Bu tesisin bilgileri sistemlerimizde dogrulanamamistir' uyarisi
    ayirt edici DEGILDIR; fotografi olan otellerde de gorunuyor.
    """
    if site_turu not in ("obilet", "ets"):
        return False
    try:
        sonuc = driver.execute_script(GORSELSIZ_TESPIT_SCRIPTI)
    except Exception:
        return False
    if not isinstance(sonuc, dict):
        return False
    gorselsiz = not sonuc.get("rozet") and bool(sonuc.get("placeholder"))
    if gorselsiz and app is not None:
        app.log_yaz(
            "Kaynakta fotograf rozeti yok ve kapak placeholder: "
            "bu sayfada otel gorseli bulunmuyor."
        )
    return gorselsiz


def sayili_galeri_kutusuna_tikla(driver, site_turu, app=None):
    """ETS 'Galeri (N)' veya Obilet '+N Fotoğraf' kutusunu güvenle açar."""
    aday_scripti = r"""
        const site = arguments[0];
        const normalize = (value) => (value || '')
            .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
            .toLocaleLowerCase('tr-TR').replace(/\s+/g, ' ').trim();
        const matchesTarget = (text) => {
            if (site === 'ets') return /^galeri\s*\(\d+\)$/.test(text);
            if (site === 'obilet') return /^\+\d+\s*fotograf(?:lar)?$/.test(text);
            return false;
        };

        const selector = site === 'ets'
            ? 'p, span, button, a, [role="button"]'
            : 'p, span, div, button, a, [role="button"]';
        const candidates = [];

        for (const el of document.querySelectorAll(selector)) {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            if (r.width < 8 || r.height < 8 || s.display === 'none' ||
                s.visibility === 'hidden' || Number(s.opacity) === 0) continue;

            const text = normalize(el.textContent);
            if (!matchesTarget(text)) continue;

            // Aynı metni taşıyan büyük üst kapsayıcıları alma; gerçek yazı/düğme
            // öğesi en alttaki (leaf) eşleşmedir.
            const matchingChild = [...el.children].some(child =>
                matchesTarget(normalize(child.textContent))
            );
            if (matchingChild) continue;
            // Obilet '+N Fotoğraf' yazısının kendisine yapılan tıklamayı yok
            // sayıyor. Olay, resmi taşıyan js-gallery-list-item karesine bağlı.
            // Selenium bu üst öğeyi tıklayınca karenin merkezine gerçek tıklama
            // gönderir ve galeri bütün Obilet otellerinde açılır.
            if (site === 'obilet') {
                candidates.push(el.closest('.js-gallery-list-item') || el);
            } else {
                candidates.push(el);
            }
        }

        candidates.sort((a, b) => {
            const ar = a.getBoundingClientRect();
            const br = b.getBoundingClientRect();
            return (ar.width * ar.height) - (br.width * br.height);
        });
        return candidates;
    """

    if galeri_penceresi_acik_mi(driver, site_turu):
        return "galeri zaten açık"

    bitis = time.time() + 20
    denenmis = set()

    while time.time() < bitis:
        if site_turu == "obilet":
            # Obilet kampanyası/indirim kartı galeriyi ararken gecikmeli
            # açılabilir. Uzun sabit bekleme yapmadan, her turda görünür
            # kapatma alanını kontrol et. Bilinen Insider widget'ı ve CSS
            # sınıfı olmayan kupon kartları için ortak algılayıcıyı kullanır.
            try:
                obilet_reklamini_kapat(driver, app or _SessizUygulama())
            except Exception:
                pass

        # ETS galerisi ağ hızına göre geç açılabiliyor. Önceki tıklamanın
        # sonucunu, düğmeyi tekrar bulamasak bile her turda yeniden kontrol et.
        if galeri_penceresi_acik_mi(driver, site_turu):
            return "galeri açıldı"

        try:
            adaylar = driver.execute_script(aday_scripti, site_turu) or []
        except Exception:
            adaylar = []

        for eleman in adaylar:
            try:
                anahtar = eleman.id
                if anahtar in denenmis:
                    continue
                denenmis.add(anahtar)

                gorunen_metin = " ".join((eleman.text or "").split())
                hedef = eleman

                # ETS'de gerçek tıklama alanı çoğunlukla Galeri yazısının kendisi
                # veya birkaç seviye üstündeki cursor:pointer kapsayıcıdır.
                for _ in range(5):
                    etiket = (hedef.tag_name or "").lower()
                    rol = (hedef.get_attribute("role") or "").lower()
                    imlec = (hedef.value_of_css_property("cursor") or "").lower()
                    if etiket in ("button", "a") or rol == "button" or imlec == "pointer":
                        break
                    hedef = hedef.find_element(By.XPATH, "..")

                # block:center büyük ETS kapsayıcılarında sayfayı aşağı atıyordu.
                # nearest yalnızca öğe ekran dışındaysa gereken kadar kaydırır.
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'nearest', inline:'nearest'});",
                    hedef,
                )
                time.sleep(0.25)

                try:
                    hedef.click()
                except Exception:
                    try:
                        ActionChains(driver).move_to_element(hedef).click().perform()
                    except Exception:
                        driver.execute_script("arguments[0].click();", hedef)

                kontrol_bitis = time.time() + (20 if site_turu == "ets" else 8)
                while time.time() < kontrol_bitis:
                    if galeri_penceresi_acik_mi(driver, site_turu):
                        return gorunen_metin or (
                            "Galeri" if site_turu == "ets" else "Fotoğraflar"
                        )
                    time.sleep(0.25)
            except Exception:
                continue

        time.sleep(0.5)

    return None


def tum_fotograflari_goster_tikla(driver):
    """Trivago fotoğraf kutusundaki yazıyı veya tıklanabilir üst öğesini bulur."""
    arananlar = [
        "Tüm fotoğrafları göster",
        "Tüm görselleri göster",
        "Fotoğrafların tümünü göster",
        "Tümünü gör",
        "Show all photos",
        "View all photos",
    ]
    arananlar = [metni_normallestir(metin) for metin in arananlar]
    script = r"""
        const terms = arguments[0];
        const normalize = (value) => (value || '')
            .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
            .toLocaleLowerCase('tr-TR').replace(/\s+/g, ' ').trim();
        const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 8 && r.height > 8 && r.bottom > 0 && r.top < innerHeight &&
                   s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) !== 0;
        };

        const candidates = [];
        const selector = 'button, a, [role="button"], [onclick], span, p, div';
        for (const el of document.querySelectorAll(selector)) {
            if (!visible(el)) continue;
            const text = normalize(el.innerText || el.textContent ||
                el.getAttribute('aria-label') || el.getAttribute('title'));
            if (!text) continue;

            for (const term of terms) {
                if (text !== term && !text.includes(term)) continue;
                // Büyük sayfa kapsayıcılarının tamamı da aynı metni içerir; onları ele.
                if (text.length > term.length + 80) continue;

                let target = el.closest('button, a, [role="button"], [onclick]');
                if (!target) {
                    let parent = el;
                    for (let i = 0; i < 5 && parent; i++, parent = parent.parentElement) {
                        const style = getComputedStyle(parent);
                        if (style.cursor === 'pointer' || parent.hasAttribute('tabindex')) {
                            target = parent;
                            break;
                        }
                    }
                }
                target = target || el;
                let score = text === term ? 100 : 50;
                if (target.matches('button, a, [role="button"]')) score += 30;
                score -= text.length / 1000;
                candidates.push({target, score, text});
                break;
            }
        }

        candidates.sort((a, b) => b.score - a.score);
        if (!candidates.length) return null;
        const winner = candidates[0];
        winner.target.scrollIntoView({block: 'center', inline: 'center'});
        winner.target.click();
        return winner.text;
    """

    bitis = time.time() + 8
    while time.time() < bitis:
        try:
            tiklanan = driver.execute_script(script, arananlar)
            if tiklanan:
                return tiklanan
        except Exception:
            pass
        time.sleep(0.5)
    # Bazı Trivago sürümlerinde yazı CSS katmanı olarak gösterilir ve DOM metni
    # olarak bulunmaz. Bu durumda ekrandaki fotoğraf mozaiğinin sağ-alt kutusunu
    # doğrudan görsel konumundan seç.
    if fotograf_mozayigi_sag_alt_kutuya_tikla(driver):
        return "fotoğraf mozaiğinin sağ-alt kutusu"
    return None


def kaynak_galerisini_ac(driver, app):
    """Kaynak siteyi algılar ve o sitenin otel galerisini açar."""
    site_turu = kaynak_site_turu(driver.current_url)

    if site_turu == "hotels":
        hotels_oneri_penceresini_kapat(driver, app, bekleme_suresi=2.0)
        hotels_takvimini_kapat(driver, app)
        # Öneri penceresi bazen tarih penceresi kapandıktan sonra gecikmeli açılır.
        hotels_oneri_penceresini_kapat(driver, app, bekleme_suresi=2.0)
        hotels_takvimini_kapat(driver, app)
        app.log_yaz("Hotels.com üst bölümündeki fotoğraf galerisi aranıyor...")
        if not hotels_galerisini_ac(driver, app):
            raise RuntimeError(
                "Hotels.com üst bölümündeki otel fotoğrafına tıklanarak "
                "fotoğraf galerisi açılamadı."
            )
        app.log_yaz("Hotels.com fotoğraf galerisi doğrulandı.")
        return

    if site_turu in ("ets", "obilet"):
        site_adi = "ETS" if site_turu == "ets" else "Obilet"
        if site_turu == "obilet":
            obilet_reklamini_kapat(driver, app)
        app.log_yaz(f"{site_adi} fotoğraf galerisi aranıyor...")
        tiklanan = sayili_galeri_kutusuna_tikla(driver, site_turu, app)
        if not tiklanan:
            raise RuntimeError(f"{site_adi} fotoğraf galerisi açılamadı.")
        app.log_yaz(f"{site_adi} fotoğraf galerisi açıldı ({tiklanan}).")
        time.sleep(0.5)
        return

    if site_turu == "trivago":
        acilis_takvimini_kapat(driver, app)
        app.log_yaz("Trivago üst bölümündeki fotoğraf butonları aranıyor...")
        if not trivago_ust_fotografina_tikla(driver, app):
            raise RuntimeError(
                "ESC ile tarih penceresi kapatıldı ancak üst bölümdeki "
                "fotoğraf butonuna tıklanarak galeri açılamadı."
            )
        app.log_yaz("Trivago fotoğraf galerisi doğrulandı.")
        return

    # Bilinmeyen siteler için genel metin tabanlı yöntem.
    acilis_takvimini_kapat(driver, app)
    app.log_yaz("Fotoğraf kutusundaki 'Tüm fotoğrafları göster' düğmesi aranıyor...")
    tiklanan = tum_fotograflari_goster_tikla(driver)
    if not tiklanan:
        raise RuntimeError("'Tüm fotoğrafları göster' düğmesi bulunamadı.")

    app.log_yaz("'Tüm fotoğrafları göster' düğmesine tıklandı; galeri açılıyor...")
    time.sleep(1)


def galerideki_url_adayi(driver):
    """O anda yüklenmiş img, picture/source ve arka plan görsellerini döndürür."""
    script = r"""
        const found = new Set();
        const pageHost = location.hostname.toLowerCase();
        const add = (raw) => {
            if (!raw || typeof raw !== 'string') return;
            raw = raw.trim().replace(/^['"]|['"]$/g, '');
            if (!raw || raw.startsWith('data:') || raw.startsWith('blob:')) return;
            try {
                const url = new URL(raw, document.baseURI);

                // Açılan galeriye ait olmayan reklam, logo, harita ve oda kartı
                // görsellerini siteye göre CDN/path üzerinden ele.
                if (pageHost === 'etstur.com' || pageHost.endsWith('.etstur.com')) {
                    const odamaxHotelImage = url.hostname === 'images.odamax.com' &&
                        url.pathname.includes('/odamax/image/upload/');
                    const etsHotelImage = url.hostname === 'images.etstur.com' &&
                        url.pathname.includes('/imgproxy/files/images/hotelImages/');

                    if (!odamaxHotelImage && !etsHotelImage) return;

                    // ETS'nin yeni galerisi görselleri 300x300 imgproxy adresiyle
                    // veriyor. Aynı dosyanın yüksek çözünürlüklü sürümünü iste.
                    if (odamaxHotelImage) {
                        url.pathname = url.pathname.replace(
                            /\/imgproxy\/img\/\d+x\d+\//,
                            '/imgproxy/img/2160x1440/'
                        );
                    }

                    // Yeni ETS sayfaları galeride 240 px /s/ küçük resimlerini kullanıyor.
                    // Aynı dosyanın /l/ sürümü 2160x1440 olarak sunuluyor. /m/ ve /s/
                    // adreslerini tek bir yüksek çözünürlüklü adreste birleştir.
                    if (etsHotelImage) {
                        const parts = url.pathname.split('/');
                        const hotelImagesIndex = parts.indexOf('hotelImages');
                        const sizeIndex = hotelImagesIndex + 3;
                        if (hotelImagesIndex >= 0 && ['s', 'm'].includes(parts[sizeIndex])) {
                            parts[sizeIndex] = 'l';
                            url.pathname = parts.join('/');
                        }
                    }
                }
                if ((pageHost === 'obilet.com' || pageHost.endsWith('.obilet.com')) &&
                    !(url.hostname === 'd3m404n3ahyqc3.cloudfront.net' && url.pathname.includes('/images/product/'))) {
                    return;
                }
                if (pageHost.includes('trivago.') && url.hostname !== 'imgcy.trivago.com') {
                    return;
                }
                if ((pageHost === 'hotels.com' || pageHost.endsWith('.hotels.com'))) {
                    const hotelsImage = url.hostname === 'images.trvl-media.com' &&
                        url.pathname.includes('/lodging/');
                    if (!hotelsImage) return;

                    // Galerideki küçük/kırpılmış Hotels.com görselini aynı dosyanın
                    // yüksek çözünürlüklü, oranı korunmuş sürümüne dönüştür.
                    url.searchParams.set('impolicy', 'resizecrop');
                    url.searchParams.set('rw', '1800');
                    url.searchParams.set('ra', 'fit');
                    for (const key of ['w', 'h', 'width', 'height', 'p', 'q']) {
                        url.searchParams.delete(key);
                    }
                }

                // Trivago galerisi küçük resimleri 400x267 olarak verir. Aynı
                // görselin yüksek çözünürlüklü CDN adresini oluştur.
                if (url.hostname === 'imgcy.trivago.com' && url.pathname.includes('/partner-images/')) {
                    const partnerPart = url.pathname.slice(url.pathname.indexOf('/partner-images/'));
                    url.pathname = '/c_limit,d_dummy.jpeg,f_auto,h_1200,q_auto,w_2000' + partnerPart;
                }
                found.add(url.href);
            } catch (_) {}
        };
        const fromSrcset = (value) => {
            if (!value) return;
            const parts = value.split(',').map(x => x.trim()).filter(Boolean);
            for (const part of parts) add(part.split(/\s+/)[0]);
        };

        // Tek bir en büyük dialog seçmek bazı Trivago galeri sürümlerinde yanlış
        // katmanı seçiyordu. Bütün görünür galeri/dialog köklerini birlikte tara.
        const modalSelector = [
            '[role="dialog"]',
            'dialog',
            '[aria-modal="true"]',
            '.modal-container',
            '[class*="MuiModal-root"]',
            '.gallery-modal',
            '.ob-modal-full.open',
            '.ob-modal-overlay.open',
            '.uitk-dialog-layer.layer-overlay-active',
            '.uitk-centered-sheet'
        ].join(',');
        const normalDialogs = [...document.querySelectorAll(modalSelector)];
        const fixedGalleryOverlays = pageHost.includes('trivago.')
            ? [...document.querySelectorAll('div')].filter(el => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return s.position === 'fixed' &&
                       r.width >= innerWidth * 0.7 && r.height >= innerHeight * 0.7 &&
                       el.querySelectorAll('img').length >= 5;
            })
            : [];
        const dialogs = [...new Set([...normalDialogs, ...fixedGalleryOverlays])].filter(el => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 100 && r.height > 100 && s.display !== 'none' && s.visibility !== 'hidden';
        });
        // Ana sayfaya düşmeyi engelleyen katı koruma yalnızca Trivago içindir.
        // ETS/Obilet galeri DOM'u değişirse kendi CDN filtreleriyle sayfadaki
        // otel görsellerini toplamaya devam edebilir.
        const strictGalleryOnly = pageHost.includes('trivago.') ||
            pageHost === 'hotels.com' || pageHost.endsWith('.hotels.com');

        // Trivago ve Hotels.com'da galeri açılmamışsa ana sayfaya ASLA düşme. Ana sayfadaki
        // önerilen otel kapaklarının alınmasını engelleyen koruma budur.
        const roots = dialogs.length
            ? dialogs
            : (strictGalleryOnly ? [] : [document.querySelector('main') || document.body]);

        for (const root of roots) {
            for (const img of root.querySelectorAll('img')) {
                const width = img.naturalWidth || img.width || 0;
                const height = img.naturalHeight || img.height || 0;
                if (width && height && (width < 100 || height < 70)) continue;
                add(img.currentSrc);
                for (const attr of ['src', 'data-src', 'data-original', 'data-lazy-src',
                                    'data-image-url', 'data-full-src', 'data-zoom-src']) {
                    add(img.getAttribute(attr));
                }
                fromSrcset(img.getAttribute('srcset'));
                fromSrcset(img.getAttribute('data-srcset'));
            }
            for (const source of root.querySelectorAll('picture source')) {
                fromSrcset(source.getAttribute('srcset'));
                fromSrcset(source.getAttribute('data-srcset'));
            }
            for (const el of root.querySelectorAll('[style*="background"], [class*="image"], [class*="photo"]')) {
                const bg = getComputedStyle(el).backgroundImage || '';
                for (const match of bg.matchAll(/url\((['"]?)(.*?)\1\)/g)) add(match[2]);
            }
        }
        return [...found];
    """
    return driver.execute_script(script) or []


def galeriyi_bir_adim_kaydir(driver):
    """Bilinen sitelerde yalnızca açık galeri/modal içini kaydırır."""
    script = r"""
        const visible = (el) => {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 80 && r.height > 80 && r.bottom > 0 && r.top < innerHeight &&
                   s.display !== 'none' && s.visibility !== 'hidden';
        };
        const pageHost = location.hostname.toLowerCase();
        const strictGalleryOnly = pageHost.includes('trivago.') ||
            pageHost === 'hotels.com' || pageHost.endsWith('.hotels.com');
        const modalSelector = [
            '[role="dialog"]', 'dialog', '[aria-modal="true"]', '.modal-container',
            '[class*="MuiModal-root"]', '.gallery-modal',
            '.ob-modal-full.open', '.ob-modal-overlay.open',
            '.uitk-dialog-layer.layer-overlay-active', '.uitk-centered-sheet'
        ].join(',');
        const roots = [...new Set(document.querySelectorAll(modalSelector))]
            .filter(visible);
        const pool = roots.flatMap(root => [root, ...root.querySelectorAll('*')]);
        const items = pool.filter(
            el => visible(el) && el.scrollHeight > el.clientHeight + 150
        );
        items.sort((a, b) => {
            const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
            return (br.width * br.height) - (ar.width * ar.height);
        });
        const target = items[0];
        if (target) {
            const before = target.scrollTop;
            target.scrollTop = Math.min(target.scrollTop + Math.max(500, target.clientHeight * 0.85),
                                        target.scrollHeight);
            target.dispatchEvent(new Event('scroll', {bubbles: true}));
            return {moved: target.scrollTop > before, end: target.scrollTop + target.clientHeight >= target.scrollHeight - 5};
        }
        if (strictGalleryOnly) return {moved: false, end: true};
        const before = window.scrollY;
        window.scrollBy(0, Math.max(600, innerHeight * 0.85));
        return {moved: window.scrollY > before, end: window.scrollY + innerHeight >= document.documentElement.scrollHeight - 5};
    """
    return driver.execute_script(script) or {"moved": False, "end": True}


def galeri_gorsellerini_topla(driver, app):
    bulunanlar = set()
    degismeyen_tur = 0
    onceki_sayi = -1

    for _ in range(30):
        for url in galerideki_url_adayi(driver):
            dusuk = url.lower()
            if any(kelime in dusuk for kelime in ("logo", "icon", "avatar", "favicon", "sprite")):
                continue
            if dusuk.startswith(("http://", "https://")):
                bulunanlar.add(url)

        if len(bulunanlar) == onceki_sayi:
            degismeyen_tur += 1
        else:
            degismeyen_tur = 0
            onceki_sayi = len(bulunanlar)

        durum = galeriyi_bir_adim_kaydir(driver)
        time.sleep(0.25)
        if degismeyen_tur >= 3 and (durum.get("end") or not durum.get("moved")):
            break

    # Son kaydırmadan sonra yüklenenleri de al.
    for url in galerideki_url_adayi(driver):
        if url.lower().startswith(("http://", "https://")):
            bulunanlar.add(url)

    if not bulunanlar:
        if kaynak_site_turu(driver.current_url) in ("trivago", "hotels"):
            raise RuntimeError(
                "Açılmış fotoğraf galerisinde görsel bulunamadı. Ana sayfadaki "
                "önerilen otel kapaklarının alınmaması için işlem durduruldu."
            )
        raise RuntimeError("Kaynak sayfada indirilebilir otel görseli bulunamadı.")

    app.log_yaz(f"Galeride {len(bulunanlar)} görsel bağlantısı bulundu.")
    return list(bulunanlar)


def uzanti_belirle(content_type, url):
    content_type = (content_type or "").split(";")[0].strip().lower()
    eslesmeler = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    if content_type in eslesmeler:
        return eslesmeler[content_type]
    uzanti = os.path.splitext(urlparse(url).path)[1].lower()
    return uzanti if uzanti in eslesmeler.values() else ".jpg"


def avif_verisini_jpg_yap(veri):
    """Kep Atlas'ın kabul etmediği AVIF verisini JPEG'e dönüştürür."""
    if Image is None:
        raise RuntimeError(
            "AVIF görselleri JPEG'e çevirmek için Pillow eksik. "
            "Komut satırında 'pip install -U pillow pillow-avif-plugin' çalıştırın."
        )

    try:
        with Image.open(io.BytesIO(veri)) as kaynak:
            kaynak.load()
            resim = ImageOps.exif_transpose(kaynak)

            if resim.mode in ("RGBA", "LA") or "transparency" in resim.info:
                rgba = resim.convert("RGBA")
                rgb = Image.new("RGB", rgba.size, (255, 255, 255))
                rgb.paste(rgba, mask=rgba.getchannel("A"))
                resim = rgb
            else:
                resim = resim.convert("RGB")

            cikti = io.BytesIO()
            resim.save(cikti, format="JPEG", quality=92, optimize=True)
            return cikti.getvalue()
    except Exception as hata:
        raise RuntimeError(
            "AVIF görsel JPEG'e dönüştürülemedi. "
            "'pip install -U pillow pillow-avif-plugin' komutunu çalıştırın. "
            f"Ayrıntı: {hata}"
        ) from hata


def gorselleri_indir(
    driver,
    kaynak_url,
    image_urls,
    klasor_adi,
    min_genislik,
    min_yukseklik,
    app,
    otel_adi=None,
):
    if Image is None:
        raise RuntimeError(
            "Çözünürlük filtresi için Pillow gerekli. "
            "'pip install -U pillow pillow-avif-plugin' komutunu çalıştırın."
        )

    istek_basliklari = {
        "User-Agent": driver.execute_script("return navigator.userAgent"),
        "Referer": kaynak_url,
        # AVIF Kep Atlas tarafından kabul edilmiyor; CDN'den WebP/JPEG iste.
        "Accept": "image/webp,image/jpeg,image/png,image/gif,image/*;q=0.5,*/*;q=0.1",
    }
    cerezler = requests.cookies.RequestsCookieJar()
    for cerez in driver.get_cookies():
        try:
            cerezler.set(
                cerez["name"], cerez["value"], domain=cerez.get("domain"), path=cerez.get("path", "/")
            )
        except Exception:
            cerezler.set(cerez["name"], cerez["value"])

    os.makedirs(klasor_adi, exist_ok=True)
    dosyalar = []
    gorulen_icerikler = set()
    donusturulen_avif = 0
    dusuk_cozunurluk = 0
    boyutu_okunamayan = 0

    app.log_yaz(
        f"Çözünürlük filtresi: en az {min_genislik}x{min_yukseklik} piksel."
    )

    def tek_gorseli_indir_ve_incele(url):
        """Bir görseli indirir ve bellekte doğrular; dosya yazımını ana işçi yapar."""
        try:
            cevap = requests.get(
                urljoin(kaynak_url, url),
                headers=istek_basliklari,
                cookies=cerezler,
                timeout=(7, 20),
                allow_redirects=True,
            )
            cevap.raise_for_status()
            veri = cevap.content
            content_type = cevap.headers.get("Content-Type", "")
            if len(veri) < 5_000:
                return "kucuk", None
            if "image/" not in content_type.lower() and veri[:20].lstrip().lower().startswith((b"<html", b"<!doctype")):
                return "html", None

            avif_mi = (
                "image/avif" in content_type.lower()
                or urlparse(cevap.url).path.lower().endswith(".avif")
            )
            if avif_mi:
                veri = avif_verisini_jpg_yap(veri)
                content_type = "image/jpeg"

            try:
                with Image.open(io.BytesIO(veri)) as resim:
                    genislik, yukseklik = resim.size
            except Exception:
                return "okunamadi", None

            if genislik < min_genislik or yukseklik < min_yukseklik:
                return "dusuk", None

            ozet = hashlib.sha256(veri).hexdigest()
            uzanti = uzanti_belirle(content_type, cevap.url)
            return "tamam", (veri, uzanti, ozet, avif_mi)
        except requests.RequestException as hata:
            return "hata", f"{url}: {hata}"
        except Exception as hata:
            return "hata", f"{url}: {hata}"

    isci_sayisi = min(8, max(1, len(image_urls)))
    app.log_yaz(
        f"{len(image_urls)} görsel {isci_sayisi} paralel bağlantıyla indiriliyor..."
    )

    with ThreadPoolExecutor(max_workers=isci_sayisi) as havuz:
        gelecekler = [havuz.submit(tek_gorseli_indir_ve_incele, url) for url in image_urls]
        for gelecek in as_completed(gelecekler):
            durum, sonuc = gelecek.result()
            if durum == "dusuk":
                dusuk_cozunurluk += 1
                continue
            if durum == "okunamadi":
                boyutu_okunamayan += 1
                continue
            if durum == "hata":
                app.log_yaz(f"Bir görsel indirilemedi: {sonuc}")
                continue
            if durum != "tamam":
                continue

            veri, uzanti, ozet, avif_mi = sonuc
            if ozet in gorulen_icerikler:
                continue
            gorulen_icerikler.add(ozet)
            if avif_mi:
                donusturulen_avif += 1

            dosya_yolu = gorsel_dosya_yolu(
                klasor_adi,
                otel_adi,
                len(dosyalar) + 1,
                uzanti,
            )
            with open(dosya_yolu, "wb") as dosya:
                dosya.write(veri)
            dosyalar.append(dosya_yolu)

    if donusturulen_avif:
        app.log_yaz(f"{donusturulen_avif} AVIF görsel JPEG formatına dönüştürüldü.")
    if dusuk_cozunurluk:
        app.log_yaz(
            f"{dusuk_cozunurluk} görsel {min_genislik}x{min_yukseklik} piksel "
            "sınırının altında olduğu için elendi."
        )
    if boyutu_okunamayan:
        app.log_yaz(f"{boyutu_okunamayan} görselin piksel boyutu okunamadığı için elendi.")
    return dosyalar


def panele_giris_yap(driver, admin_url, admin_email, admin_pass, app):
    driver.get(admin_url)
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    try:
        email_input = WebDriverWait(driver, 4).until(
            EC.presence_of_element_located(
                (By.XPATH, "//input[@type='email' or contains(translate(@name,'EMAIL','email'),'email')]")
            )
        )
    except TimeoutException:
        return

    sifre_input = driver.find_element(
        By.XPATH, "//input[@type='password' or contains(translate(@name,'PASSWORD','password'),'password')]"
    )
    email_input.clear()
    email_input.send_keys(admin_email)
    sifre_input.clear()
    sifre_input.send_keys(admin_pass)
    sifre_input.send_keys(Keys.RETURN)
    app.log_yaz("Kep Atlas girişi yapılıyor...")
    time.sleep(1.5)
    driver.get(admin_url)
    WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))


def panel_gorseller_sekmesini_ac(driver, app, sessiz=False):
    # Panel her yükleme grubundan sonra Atlas Puanı sekmesine dönebildiği için
    # Görseller sekmesine her çağrıda yeniden tıklanır.
    tiklanan = None
    for _ in range(3):
        tiklanan = gorunen_metne_tikla(driver, ["Görseller"])
        if tiklanan:
            time.sleep(0.4)
            break
        time.sleep(0.4)

    if not tiklanan:
        raise RuntimeError("Kep Atlas'ta Görseller sekmesi bulunamadı.")

    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.XPATH, "//input[@type='file']"))
        )
    except TimeoutException as hata:
        raise RuntimeError("Kep Atlas'ta Görseller sekmesi veya dosya seçme alanı bulunamadı.") from hata
    if not sessiz:
        app.log_yaz("Kep Atlas 'Görseller' sekmesi açıldı.")


def dosyalari_yukle(driver, dosyalar, app):
    """İndirilen bütün görselleri tek seferde dosya alanına gönderir."""
    # Yükleme başlamadan önce Görseller sekmesinin açık olduğundan emin ol.
    panel_gorseller_sekmesini_ac(driver, app, sessiz=True)

    file_input = WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.XPATH, "//input[@type='file']"))
    )

    try:
        driver.execute_script("arguments[0].value = '';", file_input)
    except Exception:
        pass

    app.log_yaz(f"{len(dosyalar)} görsel tek seferde yükleme alanına gönderiliyor...")
    file_input.send_keys("\n".join(dosyalar))

    # Dosyaların tarayıcı yükleme kuyruğuna alınması için kısa süre yeterlidir.
    # Sekme açık bırakıldığı için yükleme arka planda devam eder.
    time.sleep(2)
    app.log_yaz(
        f"{len(dosyalar)} görsel yükleme kuyruğuna verildi; sıradaki otele geçiliyor."
    )


def klasoru_gecikmeli_temizle(klasor_adi, app):
    """Arka plan yüklemesini bozmamak için geçici görselleri 15 dakika sonra siler."""
    def temizle():
        try:
            if os.path.exists(klasor_adi):
                shutil.rmtree(klasor_adi)
        except Exception as hata:
            app.log_yaz(f"Geçici klasör daha sonra temizlenemedi: {hata}")

    zamanlayici = threading.Timer(900, temizle)
    zamanlayici.daemon = True
    zamanlayici.start()


def islem_dongusu(app):
    while True:
        (
            admin_url,
            kaynak_url,
            admin_email,
            admin_pass,
            min_genislik,
            min_yukseklik,
        ) = gorev_kuyrugu.get()
        otel_id = guvenli_klasor_adi(admin_url)
        benzersiz_islem = int(time.time() * 1000)
        klasor_adi = os.path.abspath(
            os.path.join("work", f"temp_images_{otel_id}_{benzersiz_islem}")
        )
        arka_plan_yuklemesi_basladi = False

        try:
            app.log_yaz(f"\nİŞLENİYOR: {otel_id}")
            driver = tarayiciyi_hazirla(app)

            app.log_yaz("1/3: Kaynak sayfa açılıyor...")
            driver.get(kaynak_url)
            WebDriverWait(driver, 25).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(0.5)
            cerez_uyarisini_kapat(driver)
            kaynak_galerisini_ac(driver, app)
            image_urls = galeri_gorsellerini_topla(driver, app)

            if os.path.exists(klasor_adi):
                shutil.rmtree(klasor_adi)
            indirilenler = gorselleri_indir(
                driver,
                kaynak_url,
                image_urls,
                klasor_adi,
                min_genislik,
                min_yukseklik,
                app,
                otel_adi=otel_id,
            )
            if not indirilenler:
                raise RuntimeError("Galeriden geçerli bir görsel indirilemedi.")
            app.log_yaz(f"{len(indirilenler)} benzersiz görsel indirildi.")

            app.log_yaz("2/3: Kep Atlas paneline geçiliyor...")
            panele_giris_yap(driver, admin_url, admin_email, admin_pass, app)
            panel_gorseller_sekmesini_ac(driver, app)

            app.log_yaz("3/3: Görseller yükleniyor...")
            dosyalari_yukle(driver, indirilenler, app)
            arka_plan_yuklemesi_basladi = True
            app.log_yaz("İŞLEM TAMAMLANDI.")
        except Exception as hata:
            app.log_yaz(f"HATA: {hata}")
        finally:
            try:
                if os.path.exists(klasor_adi):
                    if arka_plan_yuklemesi_basladi:
                        klasoru_gecikmeli_temizle(klasor_adi, app)
                    else:
                        shutil.rmtree(klasor_adi)
                        app.log_yaz("Geçici görseller silindi.")
            except Exception as hata:
                app.log_yaz(f"Geçici klasör temizlenemedi: {hata}")
            gorev_kuyrugu.task_done()
            app.kuyruk_guncelle()


class KepAtlasResimBot:
    def __init__(self, root):
        self.root = root
        self.root.title("Kep Atlas Hızlı Görsel Botu")
        self.root.geometry("760x760")
        self.root.configure(padx=20, pady=20)

        tk.Label(
            root,
            text="Kep Atlas Hızlı Görsel Yükleyici",
            font=("Helvetica", 14, "bold"),
            fg="#1976D2",
        ).pack(pady=(0, 15))

        frame = tk.Frame(root)
        frame.pack(fill=tk.X)

        self.admin_email_entry = self._alan_ekle(
            frame, "Kep Atlas e-posta:", os.getenv("KEP_ATLAS_EMAIL", "")
        )
        self.admin_pass_entry = self._alan_ekle(
            frame, "Kep Atlas şifre:", os.getenv("KEP_ATLAS_PASSWORD", ""), sifre=True
        )
        self.admin_url_entry = self._alan_ekle(frame, "Kep Atlas düzenleme URL:")
        self.kaynak_url_entry = self._alan_ekle(
            frame, "Görsel çekilecek kaynak URL (Trivago, ETS, Obilet, Hotels.com):"
        )
        self.min_genislik_entry = self._alan_ekle(
            frame, "Minimum görsel genişliği (px):", "600"
        )
        self.min_yukseklik_entry = self._alan_ekle(
            frame, "Minimum görsel yüksekliği (px):", "400"
        )

        tk.Button(
            root,
            text="Kuyruğa Ekle ve Başlat",
            bg="#008CBA",
            fg="white",
            font=("Helvetica", 10, "bold"),
            height=2,
            command=self.kuyruga_ekle,
        ).pack(pady=7, fill=tk.X)

        tk.Label(root, text="Bekleyen oteller:", font=("Helvetica", 10, "bold")).pack(
            anchor="w", pady=(12, 0)
        )
        self.kuyruk_listbox = tk.Listbox(root, height=5)
        self.kuyruk_listbox.pack(fill=tk.X, pady=5)

        tk.Label(root, text="Sistem durumu:", font=("Helvetica", 10, "bold")).pack(
            anchor="w", pady=(8, 0)
        )
        self.log_text = scrolledtext.ScrolledText(
            root, height=12, state="disabled", bg="#f4f4f4"
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, pady=5)

        threading.Thread(target=islem_dongusu, args=(self,), daemon=True).start()
        root.protocol("WM_DELETE_WINDOW", self.kapat)

    @staticmethod
    def _alan_ekle(parent, baslik, varsayilan="", sifre=False):
        tk.Label(parent, text=baslik, font=("Helvetica", 10, "bold")).pack(anchor="w")
        entry = tk.Entry(parent, width=100, show="*" if sifre else "")
        entry.pack(fill=tk.X, pady=(0, 9))
        if varsayilan:
            entry.insert(0, varsayilan)
        return entry

    def kuyruga_ekle(self):
        admin_email = self.admin_email_entry.get().strip()
        admin_pass = self.admin_pass_entry.get()
        admin_url = self.admin_url_entry.get().strip()
        kaynak_url = self.kaynak_url_entry.get().strip()
        if not all((admin_email, admin_pass, admin_url, kaynak_url)):
            messagebox.showwarning("Uyarı", "Lütfen dört alanı da doldurun.")
            return
        if not admin_url.startswith(("http://", "https://")) or not kaynak_url.startswith(
            ("http://", "https://")
        ):
            messagebox.showwarning("Uyarı", "Lütfen geçerli http/https bağlantıları girin.")
            return

        try:
            min_genislik = int(self.min_genislik_entry.get().strip())
            min_yukseklik = int(self.min_yukseklik_entry.get().strip())
        except ValueError:
            messagebox.showwarning(
                "Uyarı", "Minimum genişlik ve yükseklik tam sayı olmalıdır."
            )
            return
        if not (1 <= min_genislik <= 10000 and 1 <= min_yukseklik <= 10000):
            messagebox.showwarning(
                "Uyarı", "Çözünürlük değerleri 1 ile 10000 arasında olmalıdır."
            )
            return

        gorev_kuyrugu.put(
            (
                admin_url,
                kaynak_url,
                admin_email,
                admin_pass,
                min_genislik,
                min_yukseklik,
            )
        )
        self.kuyruk_guncelle()
        self.admin_url_entry.delete(0, tk.END)
        self.kaynak_url_entry.delete(0, tk.END)
        self.log_yaz(f"Otel kuyruğa eklendi. Bekleyen: {gorev_kuyrugu.qsize()}")

    def kuyruk_guncelle(self):
        self.root.after(0, self._kuyruk_guncelle_gui)

    def _kuyruk_guncelle_gui(self):
        self.kuyruk_listbox.delete(0, tk.END)
        with gorev_kuyrugu.mutex:
            bekleyenler = list(gorev_kuyrugu.queue)
        for gorev in bekleyenler:
            self.kuyruk_listbox.insert(tk.END, "⏳ " + guvenli_klasor_adi(gorev[0]))

    def log_yaz(self, mesaj):
        self.root.after(0, self._log_yaz_gui, mesaj)

    def _log_yaz_gui(self, mesaj):
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, mesaj + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def kapat(self):
        global tarayici_driver
        try:
            if tarayici_driver is not None:
                tarayici_driver.quit()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    ana_pencere = tk.Tk()
    uygulama = KepAtlasResimBot(ana_pencere)
    ana_pencere.mainloop()
