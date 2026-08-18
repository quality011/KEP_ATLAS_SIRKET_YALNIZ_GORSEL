import argparse
import json
import os
import re
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from pydantic import BaseModel, Field

from veritabani import (
    atlas_adresi_kaydet,
    kaynak_hatasi_kaydet,
    kaynak_incelemeye_al,
    kaynak_kaydet,
    siradaki_gorevler,
)


VARSAYILAN_DB = Path(__file__).with_name("kep_atlas_gorevler.db")
IZINLI_SITELER = {
    "hotels.com": "HOTELS",
    "trivago.com": "TRIVAGO",
    "trivago.com.tr": "TRIVAGO",
    "etstur.com": "ETS",
    "obilet.com": "OBILET",
}
GORSEL_SITE_ONCELIGI = {
    "HOTELS": 0,
    "TRIVAGO": 1,
    "ETS": 2,
    "OBILET": 3,
}
GENEL_KELIMELER = {
    "hotel", "otel", "hotels", "resort", "spa", "the", "all", "inclusive",
    "apart", "pansiyon", "suites", "suite", "turkiye", "turkey",
}
ADRES_GENEL_KELIMELER = {
    "mah", "mahalle", "mahallesi", "cad", "cadde", "caddesi", "sok",
    "sokak", "sokagi", "no", "numara", "kat", "daire", "turkiye", "turkey",
}


class KaynakAdayi(BaseModel):
    url: str
    sayfa_otel_adi: str
    sayfa_bolge: str = ""
    sayfa_adresi: str = ""
    eslesme_aciklamasi: str = ""
    model_guveni: float = Field(ge=0, le=1)


class KaynakAramaSonucu(BaseModel):
    bulundu: bool
    adaylar: list[KaynakAdayi]


def gorev_degeri(gorev, anahtar, varsayilan=""):
    try:
        return gorev[anahtar]
    except (KeyError, IndexError):
        return varsayilan


def metni_normallestir(metin):
    metin = (metin or "").casefold().translate(str.maketrans({"ı": "i"}))
    metin = "".join(
        harf
        for harf in unicodedata.normalize("NFKD", metin)
        if not unicodedata.combining(harf)
    )
    return " ".join(re.findall(r"[a-z0-9]+", metin))


def anlamli_kelimeler(metin):
    return {
        kelime
        for kelime in metni_normallestir(metin).split()
        if kelime not in GENEL_KELIMELER and len(kelime) > 1
    }


def ad_benzerligi(beklenen, bulunan):
    beklenen_norm = metni_normallestir(beklenen)
    bulunan_norm = metni_normallestir(bulunan)
    if not beklenen_norm or not bulunan_norm:
        return 0.0
    dizi = SequenceMatcher(None, beklenen_norm, bulunan_norm).ratio()
    a = anlamli_kelimeler(beklenen)
    b = anlamli_kelimeler(bulunan)
    kelime = (2 * len(a & b) / (len(a) + len(b))) if a and b else 0.0
    kapsama = 1.0 if min(len(a), len(b)) >= 2 and (a <= b or b <= a) else 0.0
    return round(max(dizi, kelime, kapsama), 4)


def tam_otel_adi_eslesmesi(beklenen_adlar, bulunan_ad):
    bulunan_norm = metni_normallestir(bulunan_ad)
    bulunan_anlamli = anlamli_kelimeler(bulunan_ad)
    for beklenen in (ad for ad in beklenen_adlar if ad):
        if metni_normallestir(beklenen) == bulunan_norm:
            return True
        beklenen_anlamli = anlamli_kelimeler(beklenen)
        if beklenen_anlamli and beklenen_anlamli == bulunan_anlamli:
            return True
    return False


def otel_adi_cekirdegi_kapsaniyor(beklenen_adlar, bulunan_ad):
    """Genel otel ekleri atılınca beklenen ayırt edici ad kaynakta korunuyor mu?"""
    bulunan = anlamli_kelimeler(bulunan_ad)
    for beklenen_ad in (ad for ad in beklenen_adlar if ad):
        beklenen = anlamli_kelimeler(beklenen_ad)
        if not beklenen or not beklenen <= bulunan:
            continue
        # Tek sözcüklü marka adları kardeş tesisleri karıştırabilir (Martı gibi).
        # Tek sözcük ancak 4Reasons benzeri rakamlı ve ayırt edici ise yeterlidir.
        if len(beklenen) >= 2 or any(any(harf.isdigit() for harf in x) for x in beklenen):
            return True
    return False


def otel_adi_yaklasik_eslesiyor(beklenen_adlar, bulunan_ad, kelime_esigi=0.82):
    """Bodurm/Bodrum gibi tekil yazim farklarini, kelime kelime kontrol eder.

    Her beklenen ayirt edici kelimenin kaynak adinda ayri bir karsiligi olmalidir.
    Bu nedenle "Gural Afyon" ile "NG Afyon" gibi marka celiskileri kabul edilmez.
    """
    bulunan = list(anlamli_kelimeler(bulunan_ad))
    if not bulunan:
        return False
    for beklenen_ad in (ad for ad in beklenen_adlar if ad):
        beklenen = list(anlamli_kelimeler(beklenen_ad))
        if len(beklenen) < 2 or len(bulunan) < 2:
            continue
        kullanilan = set()
        hepsi_eslesti = True
        for kelime in sorted(beklenen, key=len, reverse=True):
            secenekler = [
                (SequenceMatcher(None, kelime, aday).ratio(), sira)
                for sira, aday in enumerate(bulunan)
                if sira not in kullanilan
            ]
            if not secenekler:
                hepsi_eslesti = False
                break
            puan, sira = max(secenekler)
            if puan < kelime_esigi:
                hepsi_eslesti = False
                break
            kullanilan.add(sira)
        if hepsi_eslesti:
            return True
    return False


def otel_adi_guclu_ortak_cekirdek(beklenen_adlar, bulunan_ad):
    """Ayni marka kokunu koruyan kisaltilmis/uzatilmis otel adlarini tanir."""
    bulunan = [
        kelime
        for kelime in metni_normallestir(bulunan_ad).split()
        if kelime not in GENEL_KELIMELER and len(kelime) > 1
    ]
    if not bulunan:
        return False
    bulunan_set = set(bulunan)
    for beklenen_ad in (ad for ad in beklenen_adlar if ad):
        beklenen = [
            kelime
            for kelime in metni_normallestir(beklenen_ad).split()
            if kelime not in GENEL_KELIMELER and len(kelime) > 1
        ]
        if not beklenen:
            continue
        # Kefaluka / Highlight gibi tek ve ayirt edici bir marka adi.
        if len(beklenen) == 1:
            if len(beklenen[0]) >= 6 and beklenen[0] in bulunan_set:
                return True
            continue
        ortak = set(beklenen) & bulunan_set
        ilk_marka_uyusuyor = any(
            SequenceMatcher(None, beklenen[0], aday).ratio() >= 0.90
            for aday in bulunan[:2]
        )
        if (
            ilk_marka_uyusuyor
            and len(ortak) >= 2
            and len(ortak) / min(len(set(beklenen)), len(bulunan_set)) >= 0.80
        ):
            return True
    return False


def otel_adi_ilce_kurali_icin_ayni(beklenen_adlar, bulunan_ad):
    """Ilce kuralinda yalniz gercekten ayni tesis adini kabul eder.

    Kaynak basligina eklenen "+12", konum veya benzeri kisa rozetler tolere
    edilir. Fakat "Gural Afyon" / "NG Afyon" gibi ayni ilcedeki farkli marka
    adlari bu kuralla kabul edilmez.
    """
    if tam_otel_adi_eslesmesi(beklenen_adlar, bulunan_ad):
        return True
    bulunan = [
        kelime
        for kelime in metni_normallestir(bulunan_ad).split()
        if kelime not in GENEL_KELIMELER and len(kelime) > 1
    ]
    if not bulunan:
        return False
    bulunan_set = set(bulunan)
    for beklenen_ad in (ad for ad in beklenen_adlar if ad):
        beklenen = [
            kelime
            for kelime in metni_normallestir(beklenen_ad).split()
            if kelime not in GENEL_KELIMELER and len(kelime) > 1
        ]
        if not beklenen or ad_benzerligi(beklenen_ad, bulunan_ad) < 0.88:
            continue
        ilk_marka_uyusuyor = any(
            SequenceMatcher(None, beklenen[0], aday).ratio() >= 0.90
            for aday in bulunan[:2]
        )
        kapsama = len(set(beklenen) & bulunan_set) / len(set(beklenen))
        if ilk_marka_uyusuyor and kapsama >= 0.80:
            return True
    return False


def otel_adi_marka_koku_uyusuyor(beklenen_adlar, bulunan_ad):
    """Kaynak basligina konum/pazarlama eki gelse de ayirt edici markayi tanir.

    Bu daha esnek kural tek basina kabul vermez; sonucu_degerlendir ayrica ayni
    ilce/sehir destegi ister. Ilk marka kelimesi farkli olan tesisler bu nedenle
    ayni sehirde bulunsalar bile gecemez.
    """
    bulunan = [
        kelime
        for kelime in metni_normallestir(bulunan_ad).split()
        if kelime not in GENEL_KELIMELER and len(kelime) > 1
    ]
    if not bulunan:
        return False
    bulunan_set = set(bulunan)
    for beklenen_ad in (ad for ad in beklenen_adlar if ad):
        beklenen = [
            kelime
            for kelime in metni_normallestir(beklenen_ad).split()
            if kelime not in GENEL_KELIMELER and len(kelime) > 1
        ]
        if not beklenen:
            continue
        ortak = set(beklenen) & bulunan_set
        if len(beklenen) == 1:
            if len(beklenen[0]) >= 4 and beklenen[0] in bulunan_set:
                return True
            continue
        ilk_beklenen = beklenen[0]
        ilk_bulunan = bulunan[0]
        ilk_puan = SequenceMatcher(None, ilk_beklenen, ilk_bulunan).ratio()
        kok_uzantisi = (
            min(len(ilk_beklenen), len(ilk_bulunan)) >= 3
            and (
                ilk_beklenen.startswith(ilk_bulunan)
                or ilk_bulunan.startswith(ilk_beklenen)
            )
        )
        gereken_ortak = max(1, len(set(beklenen)) - 1)
        if (
            (ilk_puan >= 0.90 or kok_uzantisi)
            and len(ortak) >= gereken_ortak
            and ad_benzerligi(beklenen_ad, bulunan_ad) >= 0.60
        ):
            return True
    return False


def bolge_benzerligi(beklenen, bulunan):
    if not beklenen:
        return 0.5
    if not bulunan:
        return 0.0
    return ad_benzerligi(beklenen, bulunan)


def ilce_tam_eslesiyor(beklenen_bolge, *kaynak_metinleri):
    """Atlas ilce/mevki adinin kaynak konumunda tam kelime olarak gectigini denetler."""
    beklenen = [
        kelime
        for kelime in metni_normallestir(beklenen_bolge).split()
        if kelime not in ADRES_GENEL_KELIMELER and len(kelime) > 1
    ]
    if not beklenen:
        return False
    for kaynak in kaynak_metinleri:
        bulunan = metni_normallestir(kaynak).split()
        if not bulunan:
            continue
        uzunluk = len(beklenen)
        if any(
            bulunan[sira:sira + uzunluk] == beklenen
            for sira in range(len(bulunan) - uzunluk + 1)
        ):
            return True
    return False


def adres_kelimeleri(adres):
    return {
        kelime
        for kelime in metni_normallestir(adres).split()
        if kelime not in ADRES_GENEL_KELIMELER and len(kelime) > 1
    }


def adres_benzerligi(atlas_adres, kaynak_adres):
    """Adres benzerliği ve posta/bina numarası çelişkisini birlikte döndürür."""
    if not atlas_adres or not kaynak_adres:
        return 0.0, False
    atlas_kelimeler = adres_kelimeleri(atlas_adres)
    kaynak_kelimeler = adres_kelimeleri(kaynak_adres)
    if not atlas_kelimeler or not kaynak_kelimeler:
        return 0.0, False

    kelime_puani = 2 * len(atlas_kelimeler & kaynak_kelimeler) / (
        len(atlas_kelimeler) + len(kaynak_kelimeler)
    )
    atlas_norm = metni_normallestir(atlas_adres)
    kaynak_norm = metni_normallestir(kaynak_adres)
    dizi_puani = SequenceMatcher(None, atlas_norm, kaynak_norm).ratio()

    atlas_sayilar = set(re.findall(r"\b\d+\b", atlas_norm))
    kaynak_sayilar = set(re.findall(r"\b\d+\b", kaynak_norm))
    atlas_posta = {sayi for sayi in atlas_sayilar if len(sayi) == 5}
    kaynak_posta = {sayi for sayi in kaynak_sayilar if len(sayi) == 5}
    posta_celiski = bool(atlas_posta and kaynak_posta and not atlas_posta & kaynak_posta)

    atlas_bina = atlas_sayilar - atlas_posta
    kaynak_bina = kaynak_sayilar - kaynak_posta
    bina_celiski = bool(atlas_bina and kaynak_bina and not atlas_bina & kaynak_bina)
    kritik_celiski = posta_celiski or bina_celiski
    return round(max(kelime_puani, dizi_puani), 4), kritik_celiski


def adres_ortak_kelime_sayisi(atlas_adres, kaynak_adres):
    return len(adres_kelimeleri(atlas_adres) & adres_kelimeleri(kaynak_adres))


def adres_posta_kodlari(adres):
    return set(re.findall(r"\b\d{5}\b", metni_normallestir(adres)))


def adres_sayilari(adres):
    sayilar = set(re.findall(r"\b\d+\b", metni_normallestir(adres)))
    postalar = {sayi for sayi in sayilar if len(sayi) == 5}
    return postalar, sayilar - postalar


def kaynak_adresi_ayrintili_mi(adres):
    """Arama ozeti/yorum metnini gercek acik adresten ayirir."""
    norm = metni_normallestir(adres)
    if not norm:
        return False
    adres_isaretleri = {
        "mah", "mahalle", "mahallesi", "cad", "cadde", "caddesi",
        "sok", "sokak", "sokagi", "mevkii", "mevki", "koyu", "bulvar",
        "bulvari", "road", "street", "avenue", "district",
    }
    return bool(re.search(r"\d", norm)) or bool(
        adres_isaretleri & set(norm.split())
    ) or adres.count(",") >= 2


def adres_tam_eslesiyor(atlas_adres, kaynak_adres, adres_puani, ortak_sayisi):
    """Isim farkli olsa da ayni fiziksel tesisi gosteren guclu adresi tanir."""
    if not atlas_adres or not kaynak_adres:
        return False
    atlas_norm = metni_normallestir(atlas_adres)
    kaynak_norm = metni_normallestir(kaynak_adres)
    if atlas_norm == kaynak_norm:
        return True
    atlas_kelimeler = adres_kelimeleri(atlas_adres)
    kaynak_kelimeler = adres_kelimeleri(kaynak_adres)
    if len(atlas_kelimeler) >= 3 and atlas_kelimeler == kaynak_kelimeler:
        return True
    ayni_posta = bool(
        adres_posta_kodlari(atlas_adres) & adres_posta_kodlari(kaynak_adres)
    )
    return (
        adres_puani >= 0.92
        and ortak_sayisi >= 5
    ) or (
        ayni_posta
        and adres_puani >= 0.82
        and ortak_sayisi >= 4
    )


def siteyi_bul(url):
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""
    for alan_adi, site in IZINLI_SITELER.items():
        if host == alan_adi or host.endswith("." + alan_adi):
            return site
    return ""


def temiz_kaynak_url(url):
    """Takip parametrelerini atar ve Trivago'yu Turkce galeri DOM'una cevirir."""
    parca = urlparse(url)
    site = siteyi_bul(url)
    sorgu = ""
    netloc = parca.netloc.lower()
    yol = parca.path
    if site == "TRIVAGO":
        arama = [
            (anahtar, deger)
            for anahtar, deger in parse_qsl(parca.query, keep_blank_values=False)
            if anahtar == "search"
        ]
        sorgu = urlencode(arama)
        # Exa ar.trivago.com/en-145 gibi yerel alan adlari dondurebiliyor.
        # Gorsel motoru Turkce sayfa DOM'una gore calistigi icin ayni tesis
        # kimligini/search parametresini koruyarak Turkce URL'ye donustur.
        parcalar = [parca for parca in yol.split("/") if parca]
        try:
            oar_sirasi = next(
                sira for sira, deger in enumerate(parcalar)
                if deger.casefold() == "oar"
            )
            slug = parcalar[oar_sirasi + 1]
        except (StopIteration, IndexError):
            slug = ""
        if slug:
            if slug.casefold().startswith("hotel-"):
                slug = "otel-" + slug[6:]
            netloc = "www.trivago.com.tr"
            yol = f"/tr/oar/{slug}"
    return urlunparse(
        (parca.scheme or "https", netloc, yol, "", sorgu, "")
    )


def gorsel_kaynak_adaylarini_sirala(
    adaylar,
    aktif_url="",
    aktif_site="",
    azami_site=3,
    otel_adi="",
    ham_adi="",
):
    """Guvenli adaylardan site basina bir tane, en fazla uc kaynak dondurur.

    Hotels.com, Trivago ve ETS, galerisi bulunmayan Obilet kayitlarindan once
    denenir. Eski veritabanlarinda yalniz aktif URL'nin kabul bilgisi bulunmasa
    bile o URL korunur; diger alternatiflerin mutlaka guvenli kabul yolundan
    gecmis olmasi gerekir.
    """
    azami_site = max(1, min(3, int(azami_site)))
    aktif_temiz = temiz_kaynak_url(aktif_url) if aktif_url else ""
    benzersiz = {}

    for ham_aday in adaylar or []:
        if not isinstance(ham_aday, dict):
            continue
        url = temiz_kaynak_url(str(ham_aday.get("url", "") or ""))
        site = str(ham_aday.get("site", "") or siteyi_bul(url)).upper()
        if not url or site not in GORSEL_SITE_ONCELIGI:
            continue
        if not otel_sayfasi_mi(url, site):
            continue
        aktif_mi = bool(aktif_temiz and url == aktif_temiz)
        eski_kayit_guvenli = (
            not ham_aday.get("adres_celiskili", False)
            and (
                tam_otel_adi_eslesmesi(
                    [ad for ad in (otel_adi, ham_adi) if ad],
                    str(ham_aday.get("sayfa_otel_adi", "") or ""),
                )
                or (
                    float(ham_aday.get("adres_puani", 0.0) or 0.0) >= 0.82
                    and int(ham_aday.get("adres_ortak_sayisi", 0) or 0) >= 4
                    and not ham_aday.get("bina_celiskili", False)
                )
            )
        )
        if not aktif_mi and not (
            ham_aday.get("kabul_yolu") or eski_kayit_guvenli
        ):
            continue
        aday = dict(ham_aday)
        aday["url"] = url
        aday["site"] = site
        aday["aktif_mi"] = aktif_mi
        guven = float(
            aday.get("kabul_guveni", aday.get("toplam_puan", 0.0)) or 0.0
        )
        mevcut = benzersiz.get(site)
        mevcut_guven = float(
            (mevcut or {}).get(
                "kabul_guveni", (mevcut or {}).get("toplam_puan", 0.0)
            )
            or 0.0
        )
        if mevcut is None or guven > mevcut_guven:
            benzersiz[site] = aday

    if aktif_temiz:
        site = str(aktif_site or siteyi_bul(aktif_temiz)).upper()
        if site in GORSEL_SITE_ONCELIGI and site not in benzersiz:
            benzersiz[site] = {
                "url": aktif_temiz,
                "site": site,
                "aktif_mi": True,
            }

    sirali = sorted(
        benzersiz.values(),
        key=lambda aday: (
            GORSEL_SITE_ONCELIGI.get(aday["site"], 99),
            -float(
                aday.get("kabul_guveni", aday.get("toplam_puan", 0.0)) or 0.0
            ),
        ),
    )
    return sirali[:azami_site]


def url_anahtari(url):
    """Aynı otel sayfasının ülke alt alan adlarını aynı anahtara indirger."""
    parca = urlparse(url)
    site = siteyi_bul(url)
    yol = re.sub(r"/+$", "", parca.path.lower())
    return site, yol


def nesne_alani(nesne, alan, varsayilan=None):
    """OpenAI SDK nesneleriyle ham sözlükleri aynı biçimde okur."""
    if isinstance(nesne, dict):
        return nesne.get(alan, varsayilan)
    return getattr(nesne, alan, varsayilan)


def alinti_urllerini_topla(yanit):
    """Web aramasının kaynak ve url_citation URL'lerini birlikte toplar."""
    bulunan = []
    for cikti in nesne_alani(yanit, "output", []) or []:
        cikti_tipi = nesne_alani(cikti, "type", "")

        # include=["web_search_call.action.sources"] ile dönen eksiksiz kaynak listesi.
        if cikti_tipi == "web_search_call":
            action = nesne_alani(cikti, "action", {}) or {}
            for kaynak in nesne_alani(action, "sources", []) or []:
                url = nesne_alani(kaynak, "url", "")
                if url:
                    bulunan.append(url)
            continue

        # Son metinde kullanılan kaynaklar; eski ve yeni SDK biçimleri desteklenir.
        if cikti_tipi != "message":
            continue
        for icerik in nesne_alani(cikti, "content", []) or []:
            for alinti in nesne_alani(icerik, "annotations", []) or []:
                if nesne_alani(alinti, "type", "") != "url_citation":
                    continue
                url = nesne_alani(alinti, "url", "")
                if not url:
                    url_citation = nesne_alani(alinti, "url_citation", {}) or {}
                    url = nesne_alani(url_citation, "url", "")
                if url:
                    bulunan.append(url)
    return list(dict.fromkeys(bulunan))


def adaylari_alintilarla_dogrula(sonuc, alinti_urlleri):
    """Modelin yazdığı URL yerine web aramasının alıntıladığı gerçek URL'yi kullanır."""
    alinti_haritasi = {}
    for url in alinti_urlleri:
        if siteyi_bul(url) and otel_sayfasi_mi(url, siteyi_bul(url)):
            alinti_haritasi.setdefault(url_anahtari(url), temiz_kaynak_url(url))

    dogrulanan = []
    for aday in sonuc.adaylar:
        gercek_url = alinti_haritasi.get(url_anahtari(aday.url))
        if gercek_url:
            dogrulanan.append(aday.model_copy(update={"url": gercek_url}))
    return KaynakAramaSonucu(bulundu=bool(dogrulanan), adaylar=dogrulanan)


def otel_sayfasi_mi(url, site):
    yol = (urlparse(url).path or "").lower()
    if site == "OBILET":
        return yol.startswith("/otel/") and len(yol.split("/")) >= 3
    if site == "TRIVAGO":
        return "/oar/" in yol
    if site == "HOTELS":
        return bool(re.search(r"/(?:ho\d+|hotel/)", yol))
    if site == "ETS":
        return len(yol.strip("/")) >= 4
    return False


def adayi_puanla(gorev, aday):
    site = siteyi_bul(aday.url)
    if not site or not otel_sayfasi_mi(aday.url, site):
        return None
    beklenen_adlar = [gorev["otel_adi"], gorev["ham_adi"]]
    ad_puani = max(
        ad_benzerligi(ad, aday.sayfa_otel_adi) for ad in beklenen_adlar if ad
    )
    bolge_puani = bolge_benzerligi(gorev["bolge"], aday.sayfa_bolge)
    atlas_adres = gorev_degeri(gorev, "atlas_adres", "")
    adres_kontrol_edildi = bool(gorev_degeri(gorev, "adres_kontrol_edildi", 0))
    adres_puani, adres_celiskili = adres_benzerligi(atlas_adres, aday.sayfa_adresi)
    atlas_postalar, atlas_binalar = adres_sayilari(atlas_adres)
    kaynak_postalar, kaynak_binalar = adres_sayilari(aday.sayfa_adresi)
    posta_celiskili = bool(
        atlas_postalar and kaynak_postalar and not atlas_postalar & kaynak_postalar
    )
    bina_celiskili = bool(
        atlas_binalar and kaynak_binalar and not atlas_binalar & kaynak_binalar
    )
    ayni_bina_numarasi = bool(atlas_binalar & kaynak_binalar)
    adres_ortak_sayisi = adres_ortak_kelime_sayisi(
        atlas_adres, aday.sayfa_adresi
    )
    ilce_adreste_ayni = ilce_tam_eslesiyor(
        gorev["bolge"], aday.sayfa_adresi
    )
    ilce_bolgede_ayni = ilce_tam_eslesiyor(
        gorev["bolge"], aday.sayfa_bolge, aday.sayfa_otel_adi, aday.url
    )
    kaynak_adresi_ayrintili = kaynak_adresi_ayrintili_mi(aday.sayfa_adresi)
    # Ayrintili bir adres varsa ilceyi bizzat adres metninde gormeliyiz.
    # Boylece sayfa bolgesi Alanya, adresi Istanbul olan celiskili aday gecemez.
    ilce_ayni = ilce_adreste_ayni or (
        not kaynak_adresi_ayrintili and ilce_bolgede_ayni
    )

    if not adres_kontrol_edildi:
        adres_durumu = "ATLAS_ADRESI_OKUNMADI"
        toplam = 0.0
    elif atlas_adres and not aday.sayfa_adresi:
        adres_durumu = "KAYNAK_ADRESI_YOK"
        toplam = 0.0
    elif atlas_adres:
        adres_durumu = "CELISKI" if adres_celiskili else "KARSILASTIRILDI"
        toplam = (
            0.62 * adres_puani
            + 0.23 * ad_puani
            + 0.08 * bolge_puani
            + 0.07 * aday.model_guveni
        )
    else:
        adres_durumu = "ATLAS_ADRESI_YOK_FALLBACK"
        toplam = 0.68 * ad_puani + 0.17 * bolge_puani + 0.15 * aday.model_guveni
    return {
        "site": site,
        "url": aday.url,
        "sayfa_otel_adi": aday.sayfa_otel_adi,
        "sayfa_bolge": aday.sayfa_bolge,
        "sayfa_adresi": aday.sayfa_adresi,
        "aciklama": aday.eslesme_aciklamasi,
        "model_guveni": round(aday.model_guveni, 4),
        "ad_puani": round(ad_puani, 4),
        "bolge_puani": round(bolge_puani, 4),
        "adres_puani": round(adres_puani, 4),
        "adres_ortak_sayisi": adres_ortak_sayisi,
        "adres_durumu": adres_durumu,
        "adres_celiskili": adres_celiskili,
        "posta_celiskili": posta_celiskili,
        "bina_celiskili": bina_celiskili,
        "ayni_bina_numarasi": ayni_bina_numarasi,
        "ilce_ayni": ilce_ayni,
        "kaynak_adresi_ayrintili": kaynak_adresi_ayrintili,
        "toplam_puan": round(toplam, 4),
    }


def sonucu_degerlendir(gorev, sonuc, kabul_esigi=0.82):
    puanlilar = [adayi_puanla(gorev, aday) for aday in sonuc.adaylar]
    puanlilar = [aday for aday in puanlilar if aday is not None]
    if not sonuc.bulundu or not puanlilar:
        return "BULUNAMADI", None, puanlilar

    atlas_adres = gorev_degeri(gorev, "atlas_adres", "")
    adres_kontrol_edildi = bool(gorev_degeri(gorev, "adres_kontrol_edildi", 0))
    if not adres_kontrol_edildi:
        en_iyi = max(puanlilar, key=lambda aday: aday["ad_puani"])
        return "INCELE", en_iyi, puanlilar

    beklenen_adlar = [gorev["otel_adi"], gorev_degeri(gorev, "ham_adi", "")]
    if atlas_adres:
        kabul_edilenler = []
        for aday in puanlilar:
            tam_ad = tam_otel_adi_eslesmesi(
                beklenen_adlar, aday["sayfa_otel_adi"]
            )
            cekirdek_ad = otel_adi_cekirdegi_kapsaniyor(
                beklenen_adlar, aday["sayfa_otel_adi"]
            )
            ortak_cekirdek = otel_adi_guclu_ortak_cekirdek(
                beklenen_adlar, aday["sayfa_otel_adi"]
            )
            yaklasik_ad = otel_adi_yaklasik_eslesiyor(
                beklenen_adlar, aday["sayfa_otel_adi"]
            )
            guclu_ad = tam_ad or cekirdek_ad or ortak_cekirdek
            tam_adres = adres_tam_eslesiyor(
                atlas_adres,
                aday["sayfa_adresi"],
                aday["adres_puani"],
                aday["adres_ortak_sayisi"],
            )
            bolge_destegi = aday["bolge_puani"] >= 0.82
            ilce_ayni = aday["ilce_ayni"]
            ad_ilce_icin_yeterli = otel_adi_ilce_kurali_icin_ayni(
                beklenen_adlar, aday["sayfa_otel_adi"]
            )
            marka_koku_uyusuyor = otel_adi_marka_koku_uyusuyor(
                beklenen_adlar, aday["sayfa_otel_adi"]
            )
            kismi_adres_destegi = (
                aday["adres_puani"] >= 0.28
                and aday["adres_ortak_sayisi"] >= 1
            )

            kabul_yolu = ""
            oncelik = 0
            posta_farki_tolere_edilebilir = (
                not aday["posta_celiskili"]
                or (
                    guclu_ad
                    and aday["ayni_bina_numarasi"]
                    and aday["adres_ortak_sayisi"] >= 4
                )
            )
            # Tam adres, yeniden adlandirilmis tesisi de guvenle tanir.
            if tam_adres:
                kabul_yolu, oncelik = "TAM_ADRES", 100
            # Tam ad veya ayirt edici ad cekirdegi; kismi adres ya da bolge yeterlidir.
            elif (
                aday["sayfa_adresi"]
                and not aday["bina_celiskili"]
                and guclu_ad
                and posta_farki_tolere_edilebilir
                and (
                kismi_adres_destegi
                or (
                    not aday["kaynak_adresi_ayrintili"]
                    and (bolge_destegi or ilce_ayni)
                )
                )
            ):
                kabul_yolu, oncelik = "GUCLU_AD_KONUM", 90
            # Bodurm/Bodrum gibi yazim farki mutlaka adres destegi ister.
            elif yaklasik_ad and (
                not aday["adres_celiskili"]
                and
                aday["adres_puani"] >= 0.35
                and aday["adres_ortak_sayisi"] >= 1
            ):
                kabul_yolu, oncelik = "YAKLASIK_AD_ADRES", 80
            # Isim tamamen ayni olmasa da hem isim hem adres gucluyse kabul edilir.
            elif (
                not aday["adres_celiskili"]
                and
                aday["ad_puani"] >= 0.68
                and aday["adres_puani"] >= 0.68
                and aday["adres_ortak_sayisi"] >= 3
            ):
                kabul_yolu, oncelik = "AD_VE_ADRES", 70
            # Adres yazimi/mahalle bilgisi farkli olsa da otel adi ve ilce ayniysa
            # ayni tesistir. Tam posta kodu celiskisi guvenlik icin engel kalir.
            elif (
                aday["sayfa_adresi"]
                and ad_ilce_icin_yeterli
                and ilce_ayni
                and not aday["posta_celiskili"]
            ):
                kabul_yolu, oncelik = "GUCLU_AD_ILCE", 65
            elif (
                aday["sayfa_adresi"]
                and marka_koku_uyusuyor
                and ilce_ayni
                and not aday["posta_celiskili"]
            ):
                kabul_yolu, oncelik = "MARKA_ADI_ILCE", 64
            # Yukarıdaki adlandırılmış kurallardan hiçbiri tam uymasa da, adres ve
            # ad benzerliğinin ağırlıklı bileşimi (toplam_puan) kullanıcının
            # belirlediği kabul eşiğini geçiyorsa ve hiçbir posta/bina numarası
            # çelişkisi yoksa aday güvenle kabul edilir. Böylece --kabul-esigi
            # parametresi gerçekten devreye girer ve yalnızca dar kalıplara
            # uymadığı için insan incelemesine düşen sınırdaki eşleşmeler de
            # otomatik işlenebilir.
            elif (
                not aday["adres_celiskili"]
                and not aday["bina_celiskili"]
                and aday["ad_puani"] >= 0.40
                and aday["toplam_puan"] >= kabul_esigi
            ):
                kabul_yolu, oncelik = "ESIK_PUANI", 55

            if kabul_yolu:
                aday["kabul_yolu"] = kabul_yolu
                aday["kabul_guveni"] = round(
                    oncelik + aday["adres_puani"] + aday["ad_puani"] / 10,
                    4,
                )
                kabul_edilenler.append(aday)

        if kabul_edilenler:
            # Görsel bulunabilirliği daha yüksek olan kaynakları önce kullan. Her aday
            # zaten yukarıdaki güvenli otel/adres kurallarını geçtiği için site önceliği
            # eşleşme güvenliğini düşürmez; yalnız Obilet'in gereksiz yere ilk kaynak
            # olmasını engeller.
            en_iyi = min(
                kabul_edilenler,
                key=lambda aday: (
                    GORSEL_SITE_ONCELIGI.get(aday["site"], 99),
                    -aday["kabul_guveni"],
                ),
            )
            return "KABUL", en_iyi, puanlilar

    # Atlas'ta veya kaynak sayfasinda karsilastirilabilir adres yoksa,
    # yalniz tamamen ayni (otel/Hotel gibi genel ekler haric) ad kabul edilir.
    tam_adaylar = []
    for aday in puanlilar:
        tam_ad = tam_otel_adi_eslesmesi(
            beklenen_adlar, aday["sayfa_otel_adi"]
        )
        guclu_cekirdek = (
            otel_adi_cekirdegi_kapsaniyor(
                beklenen_adlar, aday["sayfa_otel_adi"]
            )
            or otel_adi_guclu_ortak_cekirdek(
                beklenen_adlar, aday["sayfa_otel_adi"]
            )
        )
        if not (tam_ad or guclu_cekirdek) or aday["adres_celiskili"]:
            continue
        if atlas_adres and aday["kaynak_adresi_ayrintili"]:
            continue
        # Kaynakta acik adres yoksa tamamen ayni ad tek basina yeterlidir.
        # Daha esnek marka/cekirdek eslesmesi ise mutlaka Atlas ilcesini
        # baslikta veya URL'de tasimalidir; farkli sehirdeki adaş otel gecmez.
        if not tam_ad and gorev["bolge"] and not aday["ilce_ayni"]:
            continue
        tam_adaylar.append(aday)
    if tam_adaylar:
        for aday in tam_adaylar:
            aday["kabul_yolu"] = "GUCLU_AD_ADRES_YOK"
            aday["kabul_guveni"] = round(
                60 + aday["ad_puani"] + aday["bolge_puani"] / 10,
                4,
            )
        en_iyi = min(
            tam_adaylar,
            key=lambda aday: (
                GORSEL_SITE_ONCELIGI.get(aday["site"], 99),
                -aday["kabul_guveni"],
            ),
        )
        return "KABUL", en_iyi, puanlilar
    en_iyi = max(puanlilar, key=lambda aday: aday["ad_puani"])
    return "INCELE", en_iyi, puanlilar


def arama_promptu(gorev):
    adlar = [gorev["otel_adi"]]
    if gorev["ham_adi"] and gorev["ham_adi"] not in adlar:
        adlar.append(gorev["ham_adi"])
    return (
        "Aşağıdaki Türkiye otelinin doğrudan fotoğraf/otel detay sayfasını webde ara. "
        "Yalnız Hotels.com, Trivago, Etstur veya Obilet üzerindeki aynı otele ait "
        "doğrudan otel sayfalarını aday göster; arama/listeme sayfalarını gösterme. "
        "Her aday için kaynak sayfasında yazan TAM AÇIK ADRESİ sayfa_adresi alanına "
        "aynen kopyala; tahmin etme. Adres görünmüyorsa alanı boş bırak. Otel adı, "
        "adres ve konum aynı tesisi göstermiyorsa bulundu=false döndür. En fazla 4 aday ver.\n"
        f"Otel adları: {', '.join(adlar)}\n"
        f"Bölge: {gorev['bolge'] or 'bilinmiyor'}\n"
        f"Kep Atlas adresi: {gorev_degeri(gorev, 'atlas_adres', '') or 'adres yok'}\n"
        f"Kep Atlas otel kimliği: {gorev['otel_id']}"
    )


def kaynak_ara(client, gorev, model):
    yanit = client.responses.parse(
        model=model,
        tools=[{"type": "web_search"}],
        include=["web_search_call.action.sources"],
        input=[
            {
                "role": "system",
                "content": (
                    "Sen bir otel kimlik eşleştirme ajanısın. Öncelikli doğrulama "
                    "ölçütün açık adrestir. Kaynak adresini yalnız arama sonucundaki "
                    "otel sayfasından çıkar; asla uydurma. Benzer isimli farklı "
                    "otelleri aynı kabul etme. Sonuçları istenen yapıda döndür."
                ),
            },
            {"role": "user", "content": arama_promptu(gorev)},
        ],
        text_format=KaynakAramaSonucu,
    )
    if yanit.output_parsed is None:
        raise RuntimeError("Model yapılandırılmış bir sonuç döndürmedi.")
    alinti_urlleri = alinti_urllerini_topla(yanit)
    if not alinti_urlleri:
        raise RuntimeError("Web araması doğrulanabilir bir kaynak URL döndürmedi.")
    return adaylari_alintilarla_dogrula(yanit.output_parsed, alinti_urlleri)


def argumanlari_oku():
    parser = argparse.ArgumentParser(description="Kep Atlas kaynak bulma ajanı")
    parser.add_argument("--db", default=str(VARSAYILAN_DB))
    parser.add_argument("--adet", type=int, default=5)
    parser.add_argument("--otel-id", default="")
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5.6"))
    parser.add_argument("--kabul-esigi", type=float, default=0.82)
    parser.add_argument("--bekleme", type=float, default=1.0)
    parser.add_argument(
        "--adres-topla",
        action="store_true",
        help="Kaynak aramasından önce Atlas Genel sekmesindeki adresi salt-okunur okur.",
    )
    parser.add_argument(
        "--atlas-goster",
        action="store_true",
        help="Adres okunurken Chrome penceresini görünür açar.",
    )
    parser.add_argument(
        "--onayla",
        action="store_true",
        help="Kabul edilen kaynakları veritabanına yazar. Yoksa güvenli deneme modudur.",
    )
    return parser.parse_args()


def main():
    args = argumanlari_oku()
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit(
            "OPENAI_API_KEY bulunamadı. Anahtarı koda yazmayın; Windows ortam "
            "değişkeni olarak tanımlayın."
        )

    try:
        from openai import OpenAI
    except ImportError as hata:
        raise SystemExit(
            "OpenAI paketi eksik. Önce şu komutu çalıştırın: "
            "python -m pip install -r requirements.txt"
        ) from hata

    gorevler = siradaki_gorevler(args.db, max(args.adet, 1), "KAYNAK_BEKLIYOR")
    if args.otel_id:
        gorevler = [gorev for gorev in gorevler if gorev["otel_id"] == args.otel_id]
    if not gorevler:
        print("İşlenecek KAYNAK_BEKLIYOR görevi bulunamadı.")
        return

    print("MOD:", "VERİTABANINA YAZ" if args.onayla else "DENEME - YAZMA YOK")
    client = OpenAI()
    adres_driver = None
    if args.adres_topla:
        atlas_eposta = os.getenv("KEP_ATLAS_EMAIL", "").strip()
        atlas_sifre = os.getenv("KEP_ATLAS_PASSWORD", "")
        if not atlas_eposta or not atlas_sifre:
            raise SystemExit(
                "Adres toplamak için KEP_ATLAS_EMAIL ve KEP_ATLAS_PASSWORD "
                "bu işlem oturumunda tanımlı olmalı."
            )
        from atlas_adres import adres_tarayicisini_ac

        print("Atlas adres okuyucusu açılıyor...")
        adres_driver = adres_tarayicisini_ac(
            atlas_eposta, atlas_sifre, headless=not args.atlas_goster
        )

    try:
        for sira, ham_gorev in enumerate(gorevler, start=1):
            gorev = dict(ham_gorev)
            print(
                f"\n[{sira}/{len(gorevler)}] "
                f"{gorev['otel_id']} | {gorev['otel_adi']}"
            )
            try:
                if not bool(gorev_degeri(gorev, "adres_kontrol_edildi", 0)):
                    if adres_driver is None:
                        raise RuntimeError(
                            "Atlas adresi henüz okunmadı; --adres-topla olmadan "
                            "otomatik doğrulama yapılamaz."
                        )
                    from atlas_adres import atlas_adresini_oku

                    atlas_adres = atlas_adresini_oku(
                        adres_driver, gorev["duzenleme_url"]
                    )
                    gorev["atlas_adres"] = atlas_adres
                    gorev["adres_kontrol_edildi"] = 1
                    print(f"ATLAS ADRESİ: {atlas_adres or 'Adres bulunmuyor'}")
                    if args.onayla:
                        atlas_adresi_kaydet(
                            args.db, gorev["otel_id"], atlas_adres
                        )

                sonuc = kaynak_ara(client, gorev, args.model)
                karar, en_iyi, adaylar = sonucu_degerlendir(
                    gorev, sonuc, args.kabul_esigi
                )
                aday_json = json.dumps(adaylar, ensure_ascii=False)
                if en_iyi:
                    print(
                        f"{karar}: {en_iyi['site']} | {en_iyi['toplam_puan']:.3f} | "
                        f"adres={en_iyi['adres_puani']:.3f} | {en_iyi['url']}"
                    )
                    print(f"KAYNAK ADRESİ: {en_iyi['sayfa_adresi'] or 'Adres yok'}")
                else:
                    print("BULUNAMADI: doğrulanabilir aday yok.")

                if not args.onayla:
                    continue
                if karar == "KABUL":
                    kaynak_kaydet(
                        args.db,
                        gorev["otel_id"],
                        en_iyi["url"],
                        en_iyi["site"],
                        en_iyi["toplam_puan"],
                        aday_json,
                        en_iyi["sayfa_adresi"],
                        en_iyi["adres_puani"],
                    )
                elif karar == "INCELE":
                    kaynak_incelemeye_al(
                        args.db,
                        gorev["otel_id"],
                        "Adres veya kimlik eşleşmesi otomatik kabul eşiğini "
                        "geçmedi ya da belirsiz.",
                        aday_json,
                    )
                else:
                    kaynak_hatasi_kaydet(
                        args.db, gorev["otel_id"], "Doğrulanabilir kaynak bulunamadı."
                    )
            except Exception as hata:
                print(f"HATA: {type(hata).__name__}: {hata}")
                if args.onayla:
                    kaynak_hatasi_kaydet(args.db, gorev["otel_id"], str(hata))
            if sira < len(gorevler) and args.bekleme > 0:
                time.sleep(args.bekleme)
    finally:
        if adres_driver is not None:
            adres_driver.quit()


if __name__ == "__main__":
    main()
