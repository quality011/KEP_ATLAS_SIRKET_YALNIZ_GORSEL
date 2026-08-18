import json
import time


ATLAS_LISTE_URL = (
    "https://atlas.kepguide.io/admin/oteller?"
    "sort=content&limit=100&q=&cityId=&stars=&published="
)


ADRES_ADAYLARI_JS = r"""
const norm = (value) => String(value || "")
  .normalize("NFD")
  .replace(/[\u0300-\u036f]/g, "")
  .toLocaleLowerCase("tr-TR")
  .replace(/\s+/g, " ")
  .trim();
const adresSozcuguVar = (value) => /(adres|address)/.test(norm(value));
const gorunur = (el) => {
  if (!el || !(el instanceof Element)) return false;
  const style = getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== "none" && style.visibility !== "hidden" &&
    rect.width > 0 && rect.height > 0;
};
const kontrolDegeri = (el) => {
  if (!el) return "";
  if ("value" in el && typeof el.value === "string" && el.value.trim()) {
    return el.value.trim();
  }
  for (const ad of ["data-value", "aria-valuetext", "value"]) {
    const deger = el.getAttribute && el.getAttribute(ad);
    if (deger && deger.trim()) return deger.trim();
  }
  if (el.isContentEditable) return (el.innerText || el.textContent || "").trim();
  return "";
};
const yakinMetin = (el) => {
  let dugum = el;
  for (let i = 0; dugum && i < 5; i += 1, dugum = dugum.parentElement) {
    const metin = (dugum.innerText || "").replace(/\s+/g, " ").trim();
    if (adresSozcuguVar(metin) && metin.length <= 700) return metin;
  }
  return "";
};
const etiketMetni = (el) => {
  const metinler = [];
  if (el.labels) {
    for (const label of el.labels) metinler.push(label.innerText || label.textContent || "");
  }
  const aria = el.getAttribute && el.getAttribute("aria-labelledby");
  if (aria) {
    for (const id of aria.split(/\s+/)) {
      const label = document.getElementById(id);
      if (label) metinler.push(label.innerText || label.textContent || "");
    }
  }
  return metinler.join(" ").replace(/\s+/g, " ").trim();
};
const adaylar = [];
const eklenen = new Set();
const ekle = (el, kaynak, ekPuan = 0) => {
  if (!el || eklenen.has(el)) return;
  const tag = (el.tagName || "").toLowerCase();
  const tur = norm(el.getAttribute && el.getAttribute("type"));
  if (tur === "password" || tur === "email") return;
  const nitelikler = ["name", "id", "placeholder", "aria-label", "data-testid"]
    .map((ad) => (el.getAttribute && el.getAttribute(ad)) || "")
    .join(" ");
  const etiket = etiketMetni(el);
  const yakin = yakinMetin(el);
  let puan = ekPuan;
  if (adresSozcuguVar(nitelikler)) puan += 12;
  if (adresSozcuguVar(etiket)) puan += 14;
  if (adresSozcuguVar(yakin)) puan += 6;
  if (tag === "textarea") puan += 2;
  if (!gorunur(el)) puan -= 4;
  const deger = kontrolDegeri(el);
  if (/\b\d{5}\b/.test(deger)) puan += 3;
  if (/(mah|mahalle|cad|cadde|sok|sokak|bulvar|no[: ]|antalya|alanya|kemer)/i.test(deger)) puan += 2;
  if (puan < 5) return;
  eklenen.add(el);
  adaylar.push({
    puan,
    deger,
    kaynak,
    tag,
    etiket: etiket.slice(0, 160),
    yakin: yakin.slice(0, 500),
    nitelikler: nitelikler.slice(0, 240),
    gorunur: gorunur(el),
  });
};
const secici = "input, textarea, select, [contenteditable='true'], [role='textbox'], [role='combobox']";
const adresMetniGibi = (value) => {
  const ham = String(value || "").replace(/\s+/g, " ").trim();
  const n = norm(ham);
  if (ham.length < 6 || ham.length > 400) return false;
  if (/(bos birakilirsa|kullanilir|maximum|placeholder)/.test(n)) return false;
  return /\b\d{5}\b/.test(ham) ||
    /(mah|mahalle|cad|cadde|sok|sokak|bulvar|no[: ]|antalya|alanya|kemer|marmaris|bodrum|cesme|izmir|mugla)/.test(n) ||
    (ham.includes(",") && ham.length >= 12);
};
const paximumAdresiniAyikla = (value) => {
  const ham = String(value || "").replace(/\s+/g, " ").trim();
  const eslesme = ham.match(/paximum\s*:\s*(\[[\s\S]*\]|.+)$/i);
  return eslesme ? eslesme[1].trim() : "";
};
const kapsamAdresMetniniEkle = (etiket, kapsam) => {
  const ham = (kapsam && kapsam.innerText) || "";
  const satirlar = ham.split(/\r?\n/).map((s) => s.replace(/\s+/g, " ").trim()).filter(Boolean);
  const adresIndeksi = satirlar.findIndex((s) => {
    const n = norm(s).replace(/:$/, "");
    return n === "adres" || n === "address";
  });
  const olasiliklar = [];
  if (adresIndeksi >= 0) {
    olasiliklar.push(...satirlar.slice(adresIndeksi + 1, adresIndeksi + 4));
  }
  const tekSatir = ham.replace(/\s+/g, " ").trim();
  const etiketsiz = tekSatir.replace(/^\s*(adres|address)\s*:?[\s-]*/i, "").trim();
  if (etiketsiz && etiketsiz !== tekSatir) olasiliklar.push(etiketsiz);
  const paximumDegeri = olasiliklar.map(paximumAdresiniAyikla).find(Boolean);
  const deger = paximumDegeri || olasiliklar.find(adresMetniGibi);
  if (!deger) return;
  adaylar.push({
    puan: paximumDegeri ? 60 : 40,
    deger,
    kaynak: paximumDegeri ? "adres_paximum_metni" : "adres_kapsam_metni",
    tag: (kapsam.tagName || "").toLowerCase(),
    etiket: (etiket.innerText || etiket.textContent || "").trim(),
    yakin: deger.slice(0, 500),
    nitelikler: "yalniz_adres_kapsami",
    gorunur: gorunur(kapsam),
  });
};
const metinDugumleri = Array.from(document.querySelectorAll(
  "label, legend, span, p, div, h1, h2, h3, h4"
)).filter((el) => {
  if ((el.tagName || "").toLowerCase() !== "label" &&
      el.querySelector && el.querySelector(secici)) return false;
  const metin = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
  const n = norm(metin).replace(/:$/, "");
  return (n === "adres" || n === "address" || n.startsWith("adres ") || n.startsWith("address ")) && metin.length <= 100;
});
for (const etiket of metinDugumleri) {
  const htmlFor = etiket.getAttribute && etiket.getAttribute("for");
  if (htmlFor) ekle(document.getElementById(htmlFor), "label_for", 20);
  ekle(etiket.querySelector && etiket.querySelector(secici), "label_ici", 18);
  let dugum = etiket;
  for (let i = 0; dugum && i < 5; i += 1, dugum = dugum.parentElement) {
    const bulunanlar = dugum.querySelectorAll ? Array.from(dugum.querySelectorAll(secici)) : [];
    if (bulunanlar.length) {
      const bulunan = bulunanlar.find(gorunur) || bulunanlar[0];
      ekle(bulunan, "etiket_kapsami", Math.max(8, 18 - i * 2));
      kapsamAdresMetniniEkle(etiket, dugum);
      break;
    }
  }
  const kardes = etiket.nextElementSibling;
  if (kardes) {
    if (kardes.matches && kardes.matches(secici)) ekle(kardes, "etiket_kardesi", 18);
    const bulunanlar = kardes.querySelectorAll ? Array.from(kardes.querySelectorAll(secici)) : [];
    const bulunan = bulunanlar.find(gorunur) || bulunanlar[0];
    if (bulunan) ekle(bulunan, "etiket_kardes_kapsami", 16);
    kapsamAdresMetniniEkle(etiket, kardes);
  }
}
return adaylar.sort((a, b) => b.puan - a.puan);
"""


def adres_tarayicisini_ac(eposta, sifre, headless=True):
    from durum_tarama import giris_yap, tarayiciyi_ac

    driver = tarayiciyi_ac(headless=headless)
    try:
        giris_yap(driver, ATLAS_LISTE_URL, eposta, sifre)
        return driver
    except Exception:
        driver.quit()
        raise


def adres_degerini_temizle(deger):
    deger = (deger or "").strip()
    if not deger:
        return ""
    if deger.casefold().startswith("paximum:"):
        deger = deger.split(":", 1)[1].strip()
    try:
        veri = json.loads(deger)
        if isinstance(veri, list):
            return ", ".join(str(parca).strip() for parca in veri if str(parca).strip())
        if isinstance(veri, str):
            return veri.strip()
    except (json.JSONDecodeError, TypeError):
        pass
    return deger


def en_iyi_adres_adayini_sec(adaylar):
    """DOM taramasindan gelen adaylardan gercek adres alanini secer."""
    adaylar = [aday for aday in (adaylar or []) if isinstance(aday, dict)]
    dolu = [
        aday
        for aday in adaylar
        if adres_degerini_temizle(aday.get("deger"))
        and float(aday.get("puan", 0)) >= 10
    ]
    if dolu:
        en_iyi = max(dolu, key=lambda aday: float(aday.get("puan", 0)))
        return adres_degerini_temizle(en_iyi.get("deger")), en_iyi

    kesin_bos_alan = [
        aday
        for aday in adaylar
        if float(aday.get("puan", 0)) >= 18
        and (
            "adres" in (aday.get("etiket") or "").casefold()
            or "address" in (aday.get("etiket") or "").casefold()
            or "adres" in (aday.get("nitelikler") or "").casefold()
            or "address" in (aday.get("nitelikler") or "").casefold()
            or str(aday.get("kaynak", "")).startswith("label_")
            or str(aday.get("kaynak", ""))
            in {
                "etiket_kapsami",
                "etiket_kardesi",
                "etiket_kardes_kapsami",
            }
        )
    ]
    if kesin_bos_alan:
        en_iyi = max(kesin_bos_alan, key=lambda aday: float(aday.get("puan", 0)))
        return "", en_iyi
    return None, None


def _adres_adaylarini_oku(driver, timeout=15):
    son_adaylar = []
    bitis = time.monotonic() + timeout
    while time.monotonic() < bitis:
        son_adaylar = driver.execute_script(ADRES_ADAYLARI_JS) or []
        deger, secilen = en_iyi_adres_adayini_sec(son_adaylar)
        if secilen is not None:
            return deger, secilen, son_adaylar
        time.sleep(0.25)
    return None, None, son_adaylar


def atlas_adresini_oku(driver, duzenleme_url):
    from selenium.common.exceptions import TimeoutException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    driver.get(duzenleme_url)
    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located((By.TAG_NAME, "body"))
    )

    try:
        genel = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    "//main//*[self::button or self::a or @role='tab']"
                    "[normalize-space()='Genel']",
                )
            )
        )
        try:
            genel.click()
        except Exception:
            driver.execute_script("arguments[0].click()", genel)
    except TimeoutException:
        # Bazi surumlerde Genel varsayilan aciktir ve baslik tiklanabilir degildir.
        pass

    deger, secilen, adaylar = _adres_adaylarini_oku(driver, timeout=15)
    if secilen is None:
        ozet = [
            {
                "puan": aday.get("puan"),
                "kaynak": aday.get("kaynak"),
                "tag": aday.get("tag"),
                "etiket": aday.get("etiket"),
                "nitelikler": aday.get("nitelikler"),
            }
            for aday in adaylar[:5]
        ]
        raise RuntimeError(
            "Atlas Genel sekmesindeki gercek Adres alani guvenle belirlenemedi. "
            f"Aday ozeti: {json.dumps(ozet, ensure_ascii=False)}"
        )
    return adres_degerini_temizle(deger)
