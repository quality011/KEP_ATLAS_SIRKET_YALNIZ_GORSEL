import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from kaynak_agent import (
    KaynakAdayi,
    KaynakAramaSonucu,
    ad_benzerligi,
    adres_benzerligi,
    GENEL_KELIMELER,
    gorev_degeri,
    metni_normallestir,
    otel_sayfasi_mi,
    siteyi_bul,
    sonucu_degerlendir,
    tam_otel_adi_eslesmesi,
    temiz_kaynak_url,
)
import kimlik
from veritabani import (
    atlas_adresi_kaydet,
    kaynak_incelemeye_al,
    kaynak_kaydet,
    siradaki_gorevler,
)


VARSAYILAN_DB = Path(__file__).with_name("kep_atlas_gorevler.db")
IZINLI_ALANLAR = (
    "hotels.com",
    "trivago.com.tr",
    "trivago.com",
    "etstur.com",
    "obilet.com",
)
EXA_GORSEL_SITE_GRUPLARI = (
    ("hotels.com",),
    ("trivago.com.tr", "trivago.com"),
    ("etstur.com",),
    ("obilet.com",),
)
URL_DESENI = re.compile(r"https?://[^\s<>\]\[\"']+")
# Ad on filtresi esigi. Kayitli 14252 aday uzerinde olculdu: 0.65'e kadar
# kaybedilen tek kabul 'Ozukara Apartments 2' -> 'Zera Bodrum' ki o zaten
# yanlis eslesme. 0.70'ten sonra dogru eslesmeler de elenmeye basliyor.
AD_ON_FILTRE_ESIGI = 0.65
ADRES_ISARETLERI = (
    "mah", "mahalle", "cad", "cadde", "sok", "sokak", "bulvar",
    "no:", "no ", "adres", "address", "antalya", "alanya", "kemer",
    "manavgat", "muratpasa", "konyaalti", "serik", "kas",
)
RAPOR_ALANLARI = (
    "otel_id", "otel_adi", "bolge", "atlas_adres", "karar", "site",
    "kaynak_url", "sayfa_otel_adi", "kaynak_adres", "toplam_puan",
    "ad_puani", "adres_puani", "aday_sayisi", "ham_aday_sayisi", "exa_suresi_sn",
    "okuma_suresi_sn", "dogrulama_turu", "inceleme_nedeni", "yazma_durumu",
    "hata",
)
AD_KANONIK_ATILACAK = {"hotel", "otel", "hotels", "apart", "turkiye", "turkey"}
GECERSIZ_BASLIK_PARÇALARI = (
    "bir hata olustu",
    "page not found",
    "sayfa bulunamadi",
    "otelleri icin ucuz rezervasyon",
)


class PilotHatasi(RuntimeError):
    pass


class KimlikVeyaBakiyeHatasi(PilotHatasi):
    """Anahtar gecersiz ya da Exa bakiyesi bitti; calisma yurutulemiyor.

    Otel bazli bir sorun olmadigi icin gorevler kuyrukta korunur. Ayrica ust
    dongu bu hatayi gorunce calismayi durdurur; aksi halde bakiye bittiginde
    1000 otel bosuna dolasilip log siskinlesiyordu.
    """


class GeciciAramaHatasi(PilotHatasi):
    """Kota/ağ sorunu; otel yanlış değildir ve kuyrukta bırakılmalıdır."""


class AramaKotasiHatasi(GeciciAramaHatasi):
    def __init__(self, mesaj, retry_after=None):
        super().__init__(mesaj)
        self.retry_after = retry_after


def argumanlari_oku():
    parser = argparse.ArgumentParser(
        description="Exa Search + Contents ile otel kaynak doğrulama pilotu"
    )
    parser.add_argument("--db", default=str(VARSAYILAN_DB))
    parser.add_argument("--adet", type=int, default=20)
    parser.add_argument(
        "--atla",
        type=int,
        default=0,
        help="KAYNAK_BEKLIYOR listesinin basindan kac otelin atlanacagi.",
    )
    parser.add_argument("--sonuc-adedi", type=int, default=8)
    parser.add_argument(
        "--yildiz",
        type=int,
        choices=(0, 1, 2, 3, 4, 5),
        default=0,
        help="0 tüm oteller; 1-5 yalnız belirtilen yıldız kuyruğu.",
    )
    parser.add_argument("--kabul-esigi", type=float, default=0.78)
    parser.add_argument("--exa-timeout", type=int, default=75)
    parser.add_argument("--okuma-timeout", type=int, default=45)
    parser.add_argument("--bekleme", type=float, default=0.4)
    parser.add_argument(
        "--adres-topla",
        action="store_true",
        help="Atlas adresi okunmamissa Genel sekmesinden salt-okunur toplar.",
    )
    parser.add_argument(
        "--atlas-goster",
        action="store_true",
        help="Atlas adresi okunurken Chrome penceresini gorunur acar.",
    )
    parser.add_argument(
        "--rapor",
        default="",
        help="CSV cikti yolu. Bos birakilirsa zaman damgali dosya olusur.",
    )
    parser.add_argument(
        "--onayla",
        action="store_true",
        help=(
            "KABUL sonucunu HAZIR'a yazar; diger tum sonuclari sorunlu "
            "INSAN_KONTROLU durumuna ayirir."
        ),
    )
    return parser.parse_args()


def _eski_atlas_konum_ipuclari(gorev):
    ipuclari = []
    bolge = gorev_degeri(gorev, "bolge", "").strip()
    if bolge:
        ipuclari.append(bolge)

    atlas_adres = gorev_degeri(gorev, "atlas_adres", "")
    parcalar = re.split(r"[,/|]", atlas_adres)
    genel = {
        "mah", "mahalle", "mahallesi", "cad", "cadde", "caddesi",
        "sok", "sokak", "sokagi", "bulvar", "bulvari", "no",
    }
    for parca in reversed(parcalar):
        norm = metni_normallestir(parca)
        kelimeler = [
            kelime for kelime in re.findall(r"[a-zA-ZçğıöşüÇĞİÖŞÜ]+", norm)
            if kelime not in genel and len(kelime) > 1
        ]
        if not kelimeler:
            continue
        ipucu = " ".join(kelimeler[:3]).strip()
        if ipucu and ipucu not in ipuclari:
            ipuclari.append(ipucu)
        if len(ipuclari) >= 3:
            break
    return ipuclari


KONUM_ISARETLERI = {
    "mah", "mahalle", "mahallesi", "mevki", "mevkii", "kasaba",
    "kasabasi", "belde", "beldesi", "koy", "koyu",
}
SOKAK_ISARETLERI = {
    "cad", "cadde", "caddesi", "cd", "sok", "sokak", "sokagi",
    "sk", "bulvar", "bulvari", "blv", "no",
}
KONUMDA_ATILACAK = {"turkiye", "turkey", "pk", "posta", "kodu"}


def _adres_parcasindan_konum(parca):
    """Sokak/caddeyi değil, mahalle-mevki-ilçe parçasını döndürür."""
    kelimeler = [
        kelime
        for kelime in metni_normallestir(parca).split()
        if len(kelime) > 1 and not kelime.isdigit()
    ]
    if not kelimeler:
        return ""

    konum_indeksleri = [
        sira for sira, kelime in enumerate(kelimeler) if kelime in KONUM_ISARETLERI
    ]
    if konum_indeksleri:
        # "Kumluörencik Mevkii" -> "kumluorencik",
        # "Bağarası Mahallesi Tepecik Caddesi" -> "bagarasi".
        kelimeler = kelimeler[:konum_indeksleri[0]]
    elif any(kelime in SOKAK_ISARETLERI for kelime in kelimeler):
        return ""

    kelimeler = [
        kelime
        for kelime in kelimeler
        if kelime not in KONUM_ISARETLERI
        and kelime not in SOKAK_ISARETLERI
        and kelime not in KONUMDA_ATILACAK
    ]
    return " ".join(kelimeler[:3]).strip()


def atlas_konum_ipuclari(gorev):
    """Atlas bölgesi ve adresinden ilçe/mevki ipuçlarını öncelik sırasıyla çıkarır."""
    ipuclari = []
    gorulenler = set()

    def ekle(ipucu):
        ipucu = (ipucu or "").strip()
        anahtar = metni_normallestir(ipucu)
        if not anahtar or anahtar in gorulenler:
            return
        gorulenler.add(anahtar)
        ipuclari.append(ipucu)

    ekle(gorev_degeri(gorev, "bolge", ""))

    atlas_adres = gorev_degeri(gorev, "atlas_adres", "")
    # 124/1 gibi kapı numaralarını parçalamamak için yalnız boşluklu / ayraçtır.
    parcalar = re.split(r"[,|;]|\s+/\s+", atlas_adres)

    # Mahalle/mevki/kasaba belirtilmiş parçaları önce ekle.
    for parca in parcalar:
        norm = set(metni_normallestir(parca).split())
        if norm & KONUM_ISARETLERI:
            ekle(_adres_parcasindan_konum(parca))

    # Ardından Beldibi, Kemer, Marmaris gibi idari/yerleşim parçalarını ekle.
    for parca in parcalar:
        ekle(_adres_parcasindan_konum(parca))
        if len(ipuclari) >= 4:
            break
    return ipuclari[:4]


def exa_sorgusu(gorev, alanlari_ekle=True):
    """Exa icin dogal dilli, tesisi ayirt eden arama sorgusu olusturur.

    Exa'nin ``includeDomains`` alani site filtresini zaten uygular. Google'a
    ozgu ``OR`` ve ``site:`` sozdizimi Exa'da sorguyu genisletip ayni bolgedeki
    baska otelleri one cikardigi icin burada kullanilmaz.
    """
    adlar = [gorev["otel_adi"]]
    ham_adi = gorev_degeri(gorev, "ham_adi", "")
    if ham_adi and ham_adi not in adlar:
        adlar.append(ham_adi)

    ad_ifadesi = ", diger adi ".join(f'"{ad}"' for ad in adlar)
    parcalar = [
        "Turkiye'deki su otelin dogrudan tesis detay sayfasini bul:",
        f"otel adi {ad_ifadesi}.",
    ]
    konumlar = atlas_konum_ipuclari(gorev)
    if konumlar:
        parcalar.append("Konum ipuclari: " + ", ".join(konumlar) + ".")
    atlas_adres = gorev_degeri(gorev, "atlas_adres", "").strip()
    if atlas_adres:
        parcalar.append(f"Kep Atlas'taki bilinen adres: {atlas_adres}.")
    parcalar.append(
        "Yalniz bu tesisin dogrudan otel sayfasini dondur; yakindaki, benzer "
        "isimli veya onerilen baska otelleri dondurme."
    )
    if alanlari_ekle:
        parcalar.append(
            "Kaynak yalniz Hotels.com, Trivago, Etstur veya Obilet olsun."
        )
    return " ".join(parcalar)


def exa_anahtar_sorgusu(gorev):
    """Tesisi kimligiyle bulan kisa, anahtar kelime tabanli sorgu uretir.

    Uzun dogal dilli sorgu Exa'yi anlamsal aramaya itiyor ve "ayni bolgedeki
    herhangi bir otel" sonuclarini one cikariyordu; Marmaris'teki Nerium icin
    Turunc Resort donmesinin sebebi buydu. Kimlik aramasinda otel adi ve tek
    bir konum ipucu yeterlidir.
    """
    ad = (gorev["otel_adi"] or "").strip()
    parcalar = [f'"{ad}"' if ad else ""]
    konumlar = atlas_konum_ipuclari(gorev)
    if konumlar:
        parcalar.append(konumlar[0])
    parcalar.append("otel")
    return " ".join(parca for parca in parcalar if parca)


def aday_ad_havuzu(kayit):
    """Aday kaydinin basligi ve URL slugundan normallestirilmis kelime havuzu."""
    havuz = set(metni_normallestir(kayit.get("title", "") or "").split())
    yol = unquote(urlparse(kayit.get("url", "") or "").path or "").replace("-", " ")
    return havuz | set(metni_normallestir(yol).split())


def aday_adi_ilgili_mi(gorev, kayit, esik=AD_ON_FILTRE_ESIGI):
    """Arama gurultusunu, sayfa icerigi okunmadan once ad kanitiyla eler.

    Tek basina benzerlik esigi yetmez: "Nese" -> "Nese Otel Cesme, Turkiye -
    www.trivago.com.tr" gibi basliklarda site eki puani dusuruyor. Bu yuzden
    ayirt edici ad kelimesinin baslikta veya URL slugunda gecmesi de kabul
    gerekcesi sayilir.
    """
    baslik = kayit.get("title", "") or ""
    havuz = aday_ad_havuzu(kayit)
    for ad in (gorev["otel_adi"], gorev_degeri(gorev, "ham_adi", "")):
        if not ad:
            continue
        if ad_benzerligi(ad, baslik) >= esik:
            return True
        ayirt_edici = [
            kelime
            for kelime in metni_normallestir(ad).split()
            if kelime not in GENEL_KELIMELER and len(kelime) >= 4
        ]
        if ayirt_edici and any(kelime in havuz for kelime in ayirt_edici):
            return True
    return False


def adaylari_ad_ile_on_filtrele(gorev, kayitlar):
    """Ad kaniti tasimayan adaylari havuzdan cikarir."""
    return [kayit for kayit in kayitlar if aday_adi_ilgili_mi(gorev, kayit)]


def _json_adaylarini_cikar(nesne, bulunan):
    if isinstance(nesne, dict):
        url = nesne.get("url") or nesne.get("link")
        if isinstance(url, str):
            bulunan.append(url)
        for deger in nesne.values():
            _json_adaylarini_cikar(deger, bulunan)
    elif isinstance(nesne, list):
        for deger in nesne:
            _json_adaylarini_cikar(deger, bulunan)
    elif isinstance(nesne, str):
        bulunan.extend(URL_DESENI.findall(nesne))
        metin = nesne.strip()
        if metin[:1] in ("{", "["):
            try:
                _json_adaylarini_cikar(json.loads(metin), bulunan)
            except json.JSONDecodeError:
                pass


def exa_urllerini_cikar(cikti):
    bulunan = []
    try:
        _json_adaylarini_cikar(json.loads(cikti), bulunan)
    except json.JSONDecodeError:
        bulunan.extend(URL_DESENI.findall(cikti))

    sonuc = []
    anahtarlar = set()
    for ham_url in bulunan:
        url = ham_url.rstrip(".,);}")
        site = siteyi_bul(url)
        if not site or not otel_sayfasi_mi(url, site):
            continue
        temiz = temiz_kaynak_url(url)
        parca = urlparse(temiz)
        anahtar = (site, parca.path.casefold().rstrip("/"), parca.query)
        if anahtar in anahtarlar:
            continue
        anahtarlar.add(anahtar)
        sonuc.append(temiz)
    return sonuc


def _retry_after_saniyesi(hata):
    basliklar = getattr(hata, "headers", None)
    if not basliklar:
        return None
    try:
        deger = basliklar.get("Retry-After")
        if deger is None:
            return None
        return max(0.0, float(deger))
    except (TypeError, ValueError):
        return None


def _exa_api_json(api_anahtari, endpoint, govde, timeout=60, azami_deneme=3):
    """Exa REST istegi; gecici kota ve ag hatalarinda kontrollu tekrar yapar."""
    son_hata = None
    for deneme in range(max(1, azami_deneme)):
        istek = Request(
            f"https://api.exa.ai/{endpoint}",
            data=json.dumps(govde, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {api_anahtari}",
                "x-api-key": api_anahtari,
                "Content-Type": "application/json",
                "User-Agent": "KepAtlasOtelBotu/2.0",
            },
        )
        try:
            with urlopen(istek, timeout=timeout) as yanit:
                return json.loads(yanit.read().decode("utf-8", errors="replace"))
        except HTTPError as hata:
            son_hata = hata
            # Anahtar ve bakiye hatalari otel hakkinda hicbir sey soylemez;
            # calismanin kendisi yurumuyor demektir. Kalici PilotHatasi olarak
            # birakilirsa her otel "AGENT_REACH_SORUNLU: bakiye yetersiz" diye
            # INSAN_KONTROLU'ne yaziliyor ve kredi eklendiginde bir daha hic
            # denenmiyordu. GeciciAramaHatasi otelleri KAYNAK_BEKLIYOR birakir.
            if hata.code in (401, 403):
                raise KimlikVeyaBakiyeHatasi(
                    "Exa API anahtari gecersiz veya yetkisiz."
                ) from hata
            if hata.code == 402:
                raise KimlikVeyaBakiyeHatasi(
                    "Exa API bakiyesi yetersiz. Exa hesabina kredi ekleyin."
                ) from hata
            if hata.code == 429:
                retry_after = _retry_after_saniyesi(hata)
                if deneme + 1 < azami_deneme:
                    bekleme = retry_after if retry_after is not None else 2 ** deneme
                    time.sleep(min(30.0, bekleme))
                    continue
                raise AramaKotasiHatasi(
                    "Exa API hiz sinirina ulasildi; otel daha sonra yeniden denenecek.",
                    retry_after=retry_after,
                ) from hata
            if 500 <= hata.code <= 599:
                if deneme + 1 < azami_deneme:
                    time.sleep(min(10.0, 2 ** deneme))
                    continue
                raise GeciciAramaHatasi(
                    f"Exa API gecici sunucu hatasi verdi (HTTP {hata.code})."
                ) from hata
            raise PilotHatasi(
                f"Exa API istegi basarisiz (HTTP {hata.code})."
            ) from hata
        except (URLError, TimeoutError, OSError) as hata:
            son_hata = hata
            if deneme + 1 < azami_deneme:
                time.sleep(min(10.0, 2 ** deneme))
                continue
            raise GeciciAramaHatasi(
                f"Exa API ag/zaman asimi: {type(hata).__name__}"
            ) from hata
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as hata:
            raise GeciciAramaHatasi(
                "Exa API gecersiz JSON yaniti dondurdu."
            ) from hata
    raise GeciciAramaHatasi(
        f"Exa API istegi tamamlanamadi: {type(son_hata).__name__}"
    )


def _exa_sonuc_kayitlari(veri):
    """Exa yanitini izinli ve benzersiz otel sayfasi kayitlarina cevirir."""
    kayitlar = []
    gorulenler = set()
    for sonuc in veri.get("results", []):
        if not isinstance(sonuc, dict):
            continue
        url = sonuc.get("url", "")
        site = siteyi_bul(url)
        if not site or not otel_sayfasi_mi(url, site):
            continue
        temiz_url = temiz_kaynak_url(url)
        parca = urlparse(temiz_url)
        anahtar = (site, parca.path.casefold().rstrip("/"), parca.query)
        if anahtar in gorulenler:
            continue
        gorulenler.add(anahtar)
        baslik = sonuc.get("title", "")
        metin = sonuc.get("text", "")
        kayitlar.append(
            {
                "url": temiz_url,
                "title": baslik if isinstance(baslik, str) else "",
                "text": metin if isinstance(metin, str) else "",
            }
        )
    return kayitlar


def exa_api_ara_ve_icerik(
    api_anahtari, sorgu, sonuc_adedi=10, timeout=60, icerik_getir=True,
    alanlar=None, arama_turu="auto",
):
    """Exa Search ile URL adaylarini ve sayfa metnini tek istekte getirir.

    ``arama_turu`` Exa'nin arama kipini secer. ``keyword`` tesisi adiyla tekil
    olarak arar; ``auto`` noral aramaya dusebildigi icin ayni bolgedeki baska
    otelleri one cikarabilir.
    """
    govde = {
        "query": sorgu,
        "numResults": max(1, min(20, sonuc_adedi)),
        "type": arama_turu,
        "includeDomains": list(alanlar or IZINLI_ALANLAR),
        "userLocation": "TR",
    }
    if icerik_getir:
        govde["contents"] = {
            "text": {"maxCharacters": 20000, "verbosity": "compact"},
            "livecrawlTimeout": min(15000, max(5000, int(timeout * 1000))),
        }
    veri = _exa_api_json(api_anahtari, "search", govde, timeout)
    return _exa_sonuc_kayitlari(veri)


def exa_api_oncelikli_gorsel_adaylari(
    api_anahtari, sorgu, sonuc_adedi=10, timeout=60, azami_site=4,
    arama_turu="auto", icerik_getir=True,
):
    """Siteleri ayri ayri arar; adres/isim eslesmesi icin butun izinli siteleri tarar.

    Onceden yalniz ilk uc site bulununca arama durduruluyordu; bu da Obilet'i
    (ve bazen ETS'i) aday havuzundan tamamen dislayabiliyor, hem eslesme
    ihtimalini hem de gorsel indirme asamasindaki gercek yedek sayisini
    dusuruyordu. Varsayilan artik butun IZINLI_ALANLAR gruplarini tarar;
    siralama/secim asamasinda site onceligi (Hotels > Trivago > ETS > Obilet)
    ve "en fazla 3 kaynak dene" kurali degismeden korunur.
    """
    azami_site = max(1, min(len(EXA_GORSEL_SITE_GRUPLARI), int(azami_site)))
    site_basina = max(4, min(20, int(sonuc_adedi)))
    kayitlar = []
    bulunan_siteler = set()
    gorulen_urller = set()
    for alan_grubu in EXA_GORSEL_SITE_GRUPLARI:
        if len(bulunan_siteler) >= azami_site:
            break
        grup_kayitlari = exa_api_ara_ve_icerik(
            api_anahtari,
            sorgu,
            site_basina,
            timeout,
            icerik_getir=icerik_getir,
            alanlar=alan_grubu,
            arama_turu=arama_turu,
        )
        for kayit in grup_kayitlari:
            url = temiz_kaynak_url(kayit.get("url", ""))
            site = siteyi_bul(url)
            if not url or not site or url in gorulen_urller:
                continue
            gorulen_urller.add(url)
            bulunan_siteler.add(site)
            kayitlar.append(kayit)
    return kayitlar


def exa_api_ara(api_anahtari, sorgu, sonuc_adedi=10, timeout=60):
    """Geriye uyumlu yardimci; yalniz dogrudan otel URL'lerini dondurur."""
    return [
        kayit["url"]
        for kayit in exa_api_ara_ve_icerik(
            api_anahtari, sorgu, sonuc_adedi, timeout, icerik_getir=False
        )
    ]


EXA_ICERIK_YIGIN_BOYUTU = 10


def exa_api_icerikleri_oku(api_anahtari, urller, timeout=30):
    """Search metin dondurmezse bilinen URL'leri Exa Contents ile okur.

    Liste eskiden ilk 10 URL'de sessizce kesiliyordu. Noral gecis icerik
    getirmeden calistigi icin okunacak URL sayisi artik duzenli olarak 10'u
    asiyor; kesilen adaylar "metin dondurmedi" hatasina donusup oteli
    gereksiz yere ERTELENDI yapiyordu. Bu yuzden istek yiginlara bolunur.
    """
    temiz_urller = exa_urllerini_cikar(chr(10).join(urller))
    if not temiz_urller:
        return []
    kayitlar = []
    for bas in range(0, len(temiz_urller), EXA_ICERIK_YIGIN_BOYUTU):
        yigin = temiz_urller[bas:bas + EXA_ICERIK_YIGIN_BOYUTU]
        govde = {
            "urls": yigin,
            "text": {"maxCharacters": 20000, "verbosity": "compact"},
            "livecrawlTimeout": min(15000, max(5000, int(timeout * 1000))),
        }
        kayitlar.extend(
            _exa_sonuc_kayitlari(
                _exa_api_json(api_anahtari, "contents", govde, timeout)
            )
        )
    return kayitlar


def sayfa_basligini_cikar(metin):
    for satir in metin.splitlines()[:120]:
        temiz = satir.strip().strip("#* ")
        if temiz.casefold().startswith("title:"):
            temiz = temiz.split(":", 1)[1].strip()
        elif not satir.lstrip().startswith("#"):
            continue
        if 2 <= len(temiz) <= 180:
            temiz = re.split(
                r"\s+[|–—-]\s+(?:Hotels\.com|trivago|Etstur|obilet).*$",
                temiz,
                flags=re.I,
            )[0].strip()
            temiz = re.split(
                r",\s*[^,]{0,80}(?:Ucuz Rezervasyon Fırsatları|Hotel Reviews|Rooms & Prices|Otel Yorumları).*$",
                temiz,
                flags=re.I,
            )[0].strip()
            return temiz
    return ""


def url_otel_adini_cikar(url):
    """Dogrudan otel URL'sindeki ad slugini, baslik bozuksa ikinci kanit yapar."""
    yol = [parca for parca in urlparse(url).path.split("/") if parca]
    if not yol:
        return ""
    site = siteyi_bul(url)
    aday = ""
    if site == "HOTELS":
        aday = next((x for x in yol if not re.fullmatch(r"ho\d+", x, re.I)), "")
    elif site == "TRIVAGO":
        if "oar" in [x.casefold() for x in yol]:
            sira = [x.casefold() for x in yol].index("oar")
            aday = yol[sira + 1] if sira + 1 < len(yol) else ""
    elif site == "OBILET":
        aday = yol[yol.index("otel") + 1] if "otel" in yol and yol.index("otel") + 1 < len(yol) else ""
    elif site == "ETS":
        aday = yol[-1]
    aday = re.sub(r"^(?:otel|hotel|tatil-koyu)-", "", aday, flags=re.I)
    aday = re.sub(r"[-_]+", " ", aday)
    aday = re.sub(r"\b\d{4,}\b", " ", aday)
    return " ".join(aday.split())[:180]


def baslik_gecerli_mi(baslik):
    norm = metni_normallestir(baslik)
    return bool(norm) and not any(parca in norm for parca in GECERSIZ_BASLIK_PARÇALARI)


def kanonik_otel_adi(ad):
    return [
        kelime
        for kelime in metni_normallestir(ad).split()
        if kelime not in AD_KANONIK_ATILACAK
    ]


def tam_ad_eslesmesi(gorev, bulunan_ad):
    beklenenler = [gorev["otel_adi"], gorev_degeri(gorev, "ham_adi", "")]
    return tam_otel_adi_eslesmesi(beklenenler, bulunan_ad)


def guvenli_karari_uygula(gorev, karar, en_iyi):
    if en_iyi is None:
        return karar, "PUANLANABILIR_ADAY_YOK", ""
    if karar == "KABUL" and en_iyi.get("kabul_yolu"):
        return "KABUL", "", en_iyi["kabul_yolu"]
    atlas_adres = gorev_degeri(gorev, "atlas_adres", "")
    bolge = gorev_degeri(gorev, "bolge", "")
    if atlas_adres:
        if karar == "KABUL" and not en_iyi.get("sayfa_adresi"):
            return "KABUL", "", "TAM_OTEL_ADI"
        neden = "" if karar == "KABUL" else "ADRES_ESLESMESI_YETERSIZ"
        return karar, neden, "ATLAS_ADRESI"
    if bolge:
        neden = "" if karar == "KABUL" else "AD_VEYA_BOLGE_ESLESMESI_YETERSIZ"
        return karar, neden, "ATLAS_BOLGESI"
    if tam_ad_eslesmesi(gorev, en_iyi["sayfa_otel_adi"]):
        return "KABUL", "", "TAM_OTEL_ADI"
    return "INCELE", "ATLAS_ADRESI_VE_BOLGESI_YOK_TAM_AD_ESLESMEDI", "TAM_OTEL_ADI"


def kalici_sonucu_yaz(db_yolu, gorev, karar, en_iyi, adaylar, neden):
    aday_json = json.dumps(adaylar, ensure_ascii=False)
    if karar == "KABUL" and en_iyi is not None:
        kaynak_kaydet(
            db_yolu,
            gorev["otel_id"],
            en_iyi["url"],
            en_iyi["site"],
            en_iyi["toplam_puan"],
            aday_json,
            en_iyi["sayfa_adresi"],
            en_iyi["adres_puani"],
        )
        return "KAYNAK_KAYDEDILDI"

    # Eslesme reddi ile "dogru otel aday havuzunda hic yok" ayni kutuya
    # dusunce gercek darbogaz gizleniyordu. Durum degeri INSAN_KONTROLU olarak
    # kalir (diger araclar bu duruma bagli), fakat neden oneki ayrilir.
    if karar == "BULUNAMADI":
        aciklama = "KAYNAK_BULUNAMADI: " + (
            neden or "Ad kaniti tasiyan aday bulunamadi."
        )
    else:
        aciklama = "AGENT_REACH_SORUNLU: " + (
            neden or "Yuksek guvenli dogrudan otel eslesmesi bulunamadi."
        )
    kaynak_incelemeye_al(
        db_yolu,
        gorev["otel_id"],
        aciklama,
        aday_json,
    )
    return "SORUNLU_AYRILDI"


ADRES_OLMAYAN_ARAYUZ_IFADELERI = (
    "fotograf galerisi", "photo gallery", "resim galerisi", "image gallery",
    "daha fazlasini goruntule", "tum fotograflari goster", "show all photos",
    "genel bakis", "overview", "odalar", "rooms", "search", "kaydet",
    "save", "restaurant", "outdoor pool", "amenities", "facilities",
    "baska bir oda ekle", "add another room", "tamam", "done", "offers",
    "uygulamayi ac", "open app", "giris yap", "sign in",
    # Hotels.com tarih secici ve yorum ozeti metinleri adres sanilıyordu.
    "current months are", "ratings across the web", "based on",
    "check in", "check out", "giris tarihi", "cikis tarihi",
    "free cancellation", "ucretsiz iptal", "gecelik", "per night",
    # Olanak listeleri de virgullu ve sayili oldugu icin adres gibi gorunuyor.
    "coffee tea maker", "microwave", "stovetop", "fridge", "air conditioning",
    "free wifi", "ucretsiz wifi", "breakfast included", "kahvalti dahil",
)

# Ingilizce gun kisaltmalari ve 'May 14' bicimindeki ay+gun kaliplari.
# Ornek cop adresler:
#   "Dates, Wed, May 14Tue, May 20"
#   "your current months are June, 2025 and July, 2025."
#   "Sat, Aug 2Mon, Aug 4"
# Bu satirlar iki virgul ve rakam tasidigi icin eski kural onlari acik adres
# sanıyor, adres puani anlamsiz cikiyor ve gercek aday eleniyordu.
TARIH_METNI_DESENI = re.compile(
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s*\d{1,2}\b"
    r"|\bdates\b"
    , re.I,
)


def _adres_disi_gurultu_mu(norm):
    """Tarih secici, yorum ozeti ve olanak listelerini adres disi sayar."""
    return bool(TARIH_METNI_DESENI.search(norm)) or any(
        ifade in norm for ifade in ADRES_OLMAYAN_ARAYUZ_IFADELERI
    )


def _adres_satiri_mi(satir):
    """Exa metnindeki gezinme/olanak satirlarini acik adresten ayirir."""
    norm = metni_normallestir(satir)
    if not norm or len(norm) < 10:
        return False
    if "http://" in satir.casefold() or "https://" in satir.casefold():
        return False
    if "](" in satir or _adres_disi_gurultu_mu(norm):
        return False
    adres_isareti = bool(
        re.search(
            r"\b(?:mah|mahalle|mahallesi|cad|cadde|caddesi|sok|sokak|"
            r"bulvar|bulvari|mevki|mevkii|koyu|köyü|no|adres|address|"
            r"road|street|avenue|district)\b",
            norm,
        )
    )
    posta_kodu = bool(re.search(r"\b\d{5}\b", satir))
    numarali_adres = bool(re.search(r"\b(?:no\s*[:.]?\s*)?\d{1,4}(?:[/\\-]\d+)?\b", satir, re.I))
    cok_parcali_konum = satir.count(",") >= 2
    return adres_isareti or posta_kodu or (numarali_adres and cok_parcali_konum)


def _adres_satiri_puani(satir, atlas_adres):
    norm = metni_normallestir(satir)
    if not norm or len(norm) < 12 or len(norm) > 320 or not _adres_satiri_mi(satir):
        return -1.0
    isaret = sum(1 for kelime in ADRES_ISARETLERI if kelime in satir.casefold())
    posta = 1 if re.search(r"\b\d{5}\b", satir) else 0
    adres_puani, celiski = adres_benzerligi(atlas_adres, satir)
    if celiski:
        adres_puani *= 0.25
    virguller = min(3, satir.count(","))
    return adres_puani * 10 + isaret * 0.45 + posta * 0.5 + virguller * 0.10


def sayfa_adresini_cikar(metin, atlas_adres):
    satirlar = [
        re.sub(r"\s+", " ", satir.strip("#*| -\t"))
        for satir in metin.splitlines()
    ]
    adaylar = []
    for i, satir in enumerate(satirlar):
        if not satir:
            continue
        adaylar.append(satir)
        if i + 1 < len(satirlar) and satirlar[i + 1]:
            birlesik = f"{satir}, {satirlar[i + 1]}"
            if len(birlesik) <= 320:
                adaylar.append(birlesik)
    puanli = [(_adres_satiri_puani(satir, atlas_adres), satir) for satir in adaylar]
    puanli = [(puan, satir) for puan, satir in puanli if puan >= 1.35]
    if not puanli:
        return ""
    return max(puanli, key=lambda oge: oge[0])[1]


def adaylari_exa_iceriginden_oku(gorev, kayitlar):
    """Exa Search/Contents kayitlarini mevcut deterministik puanlayiciya hazirlar."""
    adaylar = []
    hatalar = []
    atlas_adres = gorev_degeri(gorev, "atlas_adres", "")
    for kayit in kayitlar:
        url = kayit.get("url", "")
        try:
            metin = kayit.get("text", "")
            ham_baslik = (kayit.get("title", "") or "").strip()
            if ham_baslik and not baslik_gecerli_mi(ham_baslik):
                hatalar.append(f"{siteyi_bul(url)}: gecersiz veya hata sayfasi basligi")
                continue
            baslik = sayfa_basligini_cikar(f"Title: {ham_baslik}") if ham_baslik else ""
            if not baslik_gecerli_mi(baslik):
                baslik = sayfa_basligini_cikar(metin)
            slug_adi = url_otel_adini_cikar(url)
            beklenen_adlar = [gorev["otel_adi"], gorev_degeri(gorev, "ham_adi", "")]
            if baslik_gecerli_mi(slug_adi) and max(
                (ad_benzerligi(ad, slug_adi) for ad in beklenen_adlar if ad),
                default=0.0,
            ) > max(
                (ad_benzerligi(ad, baslik) for ad in beklenen_adlar if ad),
                default=0.0,
            ):
                baslik = slug_adi
            if not baslik_gecerli_mi(baslik):
                hatalar.append(f"{siteyi_bul(url)}: gecersiz veya hata sayfasi basligi")
                continue
            if not metin or len(metin.strip()) < 80:
                hatalar.append(f"{siteyi_bul(url)}: Exa Contents yeterli metin dondurmedi")
                continue
            adres = sayfa_adresini_cikar(metin, atlas_adres)
            url_yolu = unquote(urlparse(url).path or "").replace("-", " ")
            bolge_kaniti = " | ".join(
                parca
                for parca in (adres, ham_baslik, baslik, slug_adi, url_yolu)
                if parca
            )
            adaylar.append(
                KaynakAdayi(
                    url=url,
                    sayfa_otel_adi=baslik,
                    sayfa_bolge=bolge_kaniti,
                    sayfa_adresi=adres,
                    eslesme_aciklamasi="Exa Search + Contents metninden deterministik okundu.",
                    model_guveni=0.90,
                )
            )
        except Exception as hata:
            hatalar.append(
                f"{siteyi_bul(url) or 'SITE'}: {type(hata).__name__}: {hata}"
            )
    return KaynakAramaSonucu(bulundu=bool(adaylar), adaylar=adaylar), hatalar


def adaylari_oku(gorev, urller, timeout=30, api_anahtari=None):
    """Eski cagri imzasi; artik URL'leri yalniz Exa Contents ile okur."""
    anahtar = (api_anahtari or os.getenv("EXA_API_KEY", "")).strip()
    if not anahtar:
        raise PilotHatasi("Exa Contents icin EXA_API_KEY gerekli.")
    kayitlar = exa_api_icerikleri_oku(anahtar, urller, timeout)
    return adaylari_exa_iceriginden_oku(gorev, kayitlar)


def bos_rapor_satiri(gorev):
    return {
        "otel_id": gorev["otel_id"],
        "otel_adi": gorev["otel_adi"],
        "bolge": gorev_degeri(gorev, "bolge", ""),
        "atlas_adres": gorev_degeri(gorev, "atlas_adres", ""),
        "karar": "HATA", "site": "", "kaynak_url": "",
        "sayfa_otel_adi": "", "kaynak_adres": "", "toplam_puan": "",
        "ad_puani": "", "adres_puani": "", "aday_sayisi": 0,
        "ham_aday_sayisi": 0,
        "exa_suresi_sn": 0, "okuma_suresi_sn": 0, "dogrulama_turu": "",
        "inceleme_nedeni": "", "yazma_durumu": "YAZMA_YOK", "hata": "",
    }


def main():
    kimlik.ortami_hazirla()
    args = argumanlari_oku()
    exa_api_anahtari = os.getenv("EXA_API_KEY", "").strip()
    if not exa_api_anahtari:
        raise SystemExit(
            "EXA_API_KEY gerekli. Program Jina/MCP kullanmaz; "
            "Exa Search + Contents anahtarinizi baslatma ekraninda girin."
        )
    atla = max(0, args.atla)
    secilecek = max(1, args.adet)
    gorevler = siradaki_gorevler(
        args.db, atla + secilecek, "KAYNAK_BEKLIYOR", yildiz=args.yildiz
    )[atla:]
    if not gorevler:
        print("Islenecek KAYNAK_BEKLIYOR gorevi bulunamadi.")
        return 0

    rapor = Path(args.rapor) if args.rapor else Path(__file__).with_name(
        f"agent_reach_kaynak_test_{datetime.now():%Y%m%d_%H%M%S}.csv"
    )
    rapor = rapor.resolve()
    rapor.parent.mkdir(parents=True, exist_ok=True)
    adres_driver = None
    if args.adres_topla:
        eposta = os.getenv("KEP_ATLAS_EMAIL", "").strip()
        sifre = os.getenv("KEP_ATLAS_PASSWORD", "")
        if not eposta or not sifre:
            raise SystemExit("KEP_ATLAS_EMAIL ve KEP_ATLAS_PASSWORD gerekli.")
        from atlas_adres import adres_tarayicisini_ac

        print("Atlas adres okuyucusu aciliyor...")
        adres_driver = adres_tarayicisini_ac(
            eposta, sifre, headless=not args.atlas_goster
        )

    sayac = Counter()
    baslangic = time.monotonic()
    if args.onayla:
        print(
            "MOD: CANLI - KABUL HAZIR'A, GERCEK UYUSMAZLIKLAR SORUNLUYA; "
            "KOTA/AG HATALARI KAYNAK_BEKLIYOR'A YAZILIR"
        )
    else:
        print("MOD: PILOT - OPENAI YOK - KAYNAK KAYDI YOK")
    print(f"Ilk {atla} bekleyen kayit atlandi; {len(gorevler)} otel test edilecek.")
    print(f"Rapor: {rapor}")
    try:
        with rapor.open("w", encoding="utf-8-sig", newline="") as dosya:
            yazici = csv.DictWriter(dosya, fieldnames=RAPOR_ALANLARI)
            yazici.writeheader()
            for sira, ham_gorev in enumerate(gorevler, 1):
                gorev = dict(ham_gorev)
                satir = bos_rapor_satiri(gorev)
                print(f"\n[{sira}/{len(gorevler)}] {gorev['otel_id']} | {gorev['otel_adi']}")
                try:
                    if not bool(gorev_degeri(gorev, "adres_kontrol_edildi", 0)):
                        if adres_driver is None:
                            raise PilotHatasi(
                                "Atlas adresi okunmamis; testi --adres-topla ile calistirin."
                            )
                        from atlas_adres import atlas_adresini_oku

                        atlas_adres = atlas_adresini_oku(
                            adres_driver, gorev["duzenleme_url"]
                        )
                        gorev["atlas_adres"] = atlas_adres
                        gorev["adres_kontrol_edildi"] = 1
                        satir["atlas_adres"] = atlas_adres
                        if args.onayla:
                            atlas_adresi_kaydet(
                                args.db, gorev["otel_id"], atlas_adres
                            )
                        print(
                            "ATLAS ADRESI (PAXIMUM): "
                            f"{atlas_adres or 'Adres bulunmuyor'}"
                        )

                    an = time.monotonic()
                    # Iki gecisin BIRLESIMI alinir. Anahtar kelime gecisi tesisi
                    # kimligiyle bulur; noral gecis ise Obilet'teki
                    # "oranj-ranch-orange-ranch" gibi farkli yazilmis sayfalari
                    # yakalar. Yedek gecis yalniz birincisi bos donunce
                    # calistirildiginda, birinciden gelen TEK bir yanlis aday
                    # yedegi bastirip dogru oteli havuz disinda birakiyordu.
                    # Eleme artik tek noktada, ad on filtresinde yapilir.
                    aramalar = (
                        ("ANAHTAR", exa_anahtar_sorgusu(gorev), "keyword", True),
                        (
                            "NORAL",
                            exa_sorgusu(gorev, alanlari_ekle=False),
                            "auto",
                            # Icerik bu geciste de arama ile birlikte gelir.
                            # Icerigi sonraya birakmak kotadan tasarruf ediyordu
                            # ama havuza metinsiz kayit sokuyor; tek bir
                            # taranamayan aday bile "okunamadi" korumasini
                            # tetikleyip oteli ERTELENDI yapiyordu.
                            True,
                        ),
                    )
                    ham_kayitlar = []
                    gorulen_urller = set()
                    for etiket, sorgu, arama_turu, icerik_getir in aramalar:
                        gecis_kayitlari = exa_api_oncelikli_gorsel_adaylari(
                            exa_api_anahtari,
                            sorgu,
                            max(1, args.sonuc_adedi),
                            args.exa_timeout,
                            azami_site=len(EXA_GORSEL_SITE_GRUPLARI),
                            arama_turu=arama_turu,
                            icerik_getir=icerik_getir,
                        )
                        yeni_adet = 0
                        for kayit in gecis_kayitlari:
                            anahtar = temiz_kaynak_url(kayit.get("url", ""))
                            if not anahtar or anahtar in gorulen_urller:
                                continue
                            gorulen_urller.add(anahtar)
                            ham_kayitlar.append(kayit)
                            yeni_adet += 1
                        print(
                            f"Exa {etiket} ({arama_turu}): "
                            f"{len(gecis_kayitlari)} aday, {yeni_adet} yeni"
                        )
                    ham_aday_sayisi = len(ham_kayitlar)
                    exa_kayitlari = adaylari_ad_ile_on_filtrele(gorev, ham_kayitlar)
                    print(
                        f"Ad ön filtresi: {ham_aday_sayisi} -> "
                        f"{len(exa_kayitlari)} aday"
                    )
                    urller = [kayit["url"] for kayit in exa_kayitlari]
                    arama_kaynagi = "Exa Search + Contents API"
                    satir["exa_suresi_sn"] = round(time.monotonic() - an, 2)
                    satir["ham_aday_sayisi"] = ham_aday_sayisi
                    print(f"{arama_kaynagi}: {len(urller)} doğrudan otel URL adayı")

                    an = time.monotonic()
                    eksik_urller = [
                        kayit["url"] for kayit in exa_kayitlari
                        if len((kayit.get("text") or "").strip()) < 80
                    ]
                    if eksik_urller:
                        tamamlayici = exa_api_icerikleri_oku(
                            exa_api_anahtari, eksik_urller, args.okuma_timeout
                        )
                        tamamlayici_haritasi = {
                            temiz_kaynak_url(kayit["url"]): kayit
                            for kayit in tamamlayici
                        }
                        exa_kayitlari = [
                            tamamlayici_haritasi.get(
                                temiz_kaynak_url(kayit["url"]), kayit
                            )
                            if len((kayit.get("text") or "").strip()) < 80
                            else kayit
                            for kayit in exa_kayitlari
                        ]
                    if exa_kayitlari and all(
                        len((kayit.get("text") or "").strip()) < 80
                        for kayit in exa_kayitlari
                    ):
                        raise GeciciAramaHatasi(
                            "Exa aday URL buldu ancak Contents bu turda okunabilir "
                            "sayfa metni dondurmedi. Otel yeniden denenecek."
                        )
                    sonuc, okuma_hatalari = adaylari_exa_iceriginden_oku(
                        gorev, exa_kayitlari
                    )
                    satir["okuma_suresi_sn"] = round(time.monotonic() - an, 2)
                    karar, en_iyi, adaylar = sonucu_degerlendir(
                        gorev, sonuc, args.kabul_esigi
                    )
                    karar, inceleme_nedeni, dogrulama_turu = guvenli_karari_uygula(
                        gorev, karar, en_iyi
                    )
                    if karar != "KABUL" and okuma_hatalari:
                        # Okunamayan adaylardan biri doğru otel olabilir. Exa'nın geçici
                        # içerik eksikliğini kalıcı eşleşme reddine çevirmeyelim.
                        raise GeciciAramaHatasi(
                            "Exa adaylarinin bir bölümü okunamadığı için güvenli karar "
                            "verilemedi; otel sonraki turda yeniden denenecek."
                        )
                    satir["karar"] = karar
                    satir["aday_sayisi"] = len(adaylar)
                    satir["inceleme_nedeni"] = inceleme_nedeni
                    satir["dogrulama_turu"] = dogrulama_turu
                    if en_iyi:
                        satir.update(
                            {
                                "site": en_iyi["site"],
                                "kaynak_url": en_iyi["url"],
                                "sayfa_otel_adi": en_iyi["sayfa_otel_adi"],
                                "kaynak_adres": en_iyi["sayfa_adresi"],
                                "toplam_puan": en_iyi["toplam_puan"],
                                "ad_puani": en_iyi["ad_puani"],
                                "adres_puani": en_iyi["adres_puani"],
                            }
                        )
                        print(
                            f"{karar}: {en_iyi['site']} | {en_iyi['toplam_puan']:.3f} | "
                            f"adres={en_iyi['adres_puani']:.3f} | {en_iyi['url']}"
                        )
                    else:
                        if ham_aday_sayisi and not urller:
                            inceleme_nedeni = "KAYNAK_BULUNAMADI_AD_ESLESMEDI"
                        elif not ham_aday_sayisi:
                            inceleme_nedeni = "KAYNAK_BULUNAMADI_ARAMA_BOS"
                        satir["inceleme_nedeni"] = inceleme_nedeni
                        print(
                            f"BULUNAMADI ({inceleme_nedeni}): "
                            f"{ham_aday_sayisi} ham aday, ad kaniti tasiyan yok."
                        )
                    if okuma_hatalari:
                        satir["hata"] = " | ".join(okuma_hatalari)[:1800]
                    if args.onayla:
                        satir["yazma_durumu"] = kalici_sonucu_yaz(
                            args.db,
                            gorev,
                            karar,
                            en_iyi,
                            adaylar,
                            inceleme_nedeni,
                        )
                        print(f"KUYRUK: {satir['yazma_durumu']}")
                except KimlikVeyaBakiyeHatasi as hata:
                    # Otel kuyrukta korunur ve calisma hemen durdurulur.
                    satir["karar"] = "ERTELENDI"
                    satir["hata"] = f"{type(hata).__name__}: {hata}"
                    satir["yazma_durumu"] = "KAYNAK_BEKLIYOR_KORUNDU"
                    print(f"DURDURULDU: {hata}")
                    print(
                        "KUYRUK: KAYNAK_BEKLIYOR olarak korundu; otel sorunlu "
                        "sayilmadi. Sorun giderilince kaldigi yerden devam eder."
                    )
                    sayac[satir["karar"]] += 1
                    yazici.writerow(satir)
                    dosya.flush()
                    break
                except GeciciAramaHatasi as hata:
                    # Kota/ağ kesintisi oteli yanlış yapmaz. Yeniden çalıştırılabilmesi
                    # için veritabanındaki KAYNAK_BEKLIYOR durumu aynen korunur.
                    satir["karar"] = "ERTELENDI"
                    satir["hata"] = f"{type(hata).__name__}: {hata}"
                    satir["yazma_durumu"] = "KAYNAK_BEKLIYOR_KORUNDU"
                    print(f"ERTELENDİ: {hata}")
                    print("KUYRUK: KAYNAK_BEKLIYOR olarak korundu; otel sorunlu sayılmadı.")
                except Exception as hata:
                    satir["karar"] = "HATA"
                    satir["hata"] = f"{type(hata).__name__}: {hata}"
                    print(f"HATA: {satir['hata']}")
                    if args.onayla:
                        try:
                            satir["yazma_durumu"] = kalici_sonucu_yaz(
                                args.db,
                                gorev,
                                "HATA",
                                None,
                                [],
                                satir["hata"],
                            )
                            print(f"KUYRUK: {satir['yazma_durumu']}")
                        except Exception as yazma_hatasi:
                            satir["hata"] += (
                                f" | KUYRUK_YAZMA_HATASI: {type(yazma_hatasi).__name__}: "
                                f"{yazma_hatasi}"
                            )
                sayac[satir["karar"]] += 1
                yazici.writerow(satir)
                dosya.flush()
                if sira < len(gorevler) and args.bekleme > 0:
                    time.sleep(args.bekleme)
    finally:
        if adres_driver is not None:
            adres_driver.quit()

    sure = time.monotonic() - baslangic
    print("\nOZET")
    for karar in ("KABUL", "INCELE", "BULUNAMADI", "ERTELENDI", "HATA"):
        print(f"{karar:12} {sayac[karar]}")
    print(f"Toplam sure: {sure:.1f} saniye")
    print("OpenAI API cagrisi: 0")
    print("Veritabani modu:", "CANLI" if args.onayla else "YAZMA YOK")
    print(f"CSV raporu: {rapor}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
