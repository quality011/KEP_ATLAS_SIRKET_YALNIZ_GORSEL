import csv
import sqlite3
from contextlib import closing
from pathlib import Path


SEMA = """
CREATE TABLE IF NOT EXISTS otel_gorevleri (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    otel_id TEXT NOT NULL,
    otel_adi TEXT NOT NULL,
    bolge TEXT NOT NULL DEFAULT '',
    ham_adi TEXT NOT NULL DEFAULT '',
    yildiz INTEGER NOT NULL DEFAULT 0,
    calisma_grubu TEXT NOT NULL DEFAULT '',
    atlas_adres TEXT NOT NULL DEFAULT '',
    adres_kontrol_edildi INTEGER NOT NULL DEFAULT 0,
    duzenleme_url TEXT NOT NULL UNIQUE,
    gorsel_sayisi INTEGER NOT NULL DEFAULT 0,
    icerik_tamamlanan INTEGER NOT NULL DEFAULT 0,
    icerik_toplam INTEGER NOT NULL DEFAULT 4,
    karar TEXT NOT NULL,
    icerik_gerekli INTEGER NOT NULL DEFAULT 0,
    gorsel_gerekli INTEGER NOT NULL DEFAULT 0,
    kaynak_url TEXT NOT NULL DEFAULT '',
    kaynak_site TEXT NOT NULL DEFAULT '',
    kaynak_guven REAL NOT NULL DEFAULT 0,
    kaynak_adres TEXT NOT NULL DEFAULT '',
    adres_puani REAL NOT NULL DEFAULT 0,
    kaynak_adaylari_json TEXT NOT NULL DEFAULT '',
    gorsel_durum TEXT NOT NULL DEFAULT '',
    icerik_durum TEXT NOT NULL DEFAULT '',
    genel_durum TEXT NOT NULL DEFAULT '',
    gorsel_yuklenen INTEGER NOT NULL DEFAULT 0,
    gorsel_deneme INTEGER NOT NULL DEFAULT 0,
    icerik_deneme INTEGER NOT NULL DEFAULT 0,
    genel_deneme INTEGER NOT NULL DEFAULT 0,
    durum TEXT NOT NULL,
    deneme_sayisi INTEGER NOT NULL DEFAULT 0,
    son_hata TEXT NOT NULL DEFAULT '',
    olusturulma TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    guncellenme TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_otel_gorevleri_durum
ON otel_gorevleri(durum);

CREATE INDEX IF NOT EXISTS idx_otel_gorevleri_karar
ON otel_gorevleri(karar);

CREATE TABLE IF NOT EXISTS gorev_gecmisi (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gorev_id INTEGER NOT NULL,
    olay TEXT NOT NULL,
    aciklama TEXT NOT NULL DEFAULT '',
    tarih TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(gorev_id) REFERENCES otel_gorevleri(id)
);
"""


def baglan(veritabani_yolu):
    yol = Path(veritabani_yolu).resolve()
    yol.parent.mkdir(parents=True, exist_ok=True)
    baglanti = sqlite3.connect(yol, timeout=30.0)
    baglanti.row_factory = sqlite3.Row
    baglanti.execute("PRAGMA busy_timeout = 30000")
    baglanti.execute("PRAGMA journal_mode = WAL")
    baglanti.execute("PRAGMA synchronous = NORMAL")
    baglanti.execute("PRAGMA foreign_keys = ON")
    baglanti.executescript(SEMA)
    mevcut_sutunlar = {
        satir[1] for satir in baglanti.execute("PRAGMA table_info(otel_gorevleri)")
    }
    for sutun, tanim in {
        "bolge": "TEXT NOT NULL DEFAULT ''",
        "ham_adi": "TEXT NOT NULL DEFAULT ''",
        "yildiz": "INTEGER NOT NULL DEFAULT 0",
        "calisma_grubu": "TEXT NOT NULL DEFAULT ''",
        "atlas_adres": "TEXT NOT NULL DEFAULT ''",
        "adres_kontrol_edildi": "INTEGER NOT NULL DEFAULT 0",
        "kaynak_guven": "REAL NOT NULL DEFAULT 0",
        "kaynak_adres": "TEXT NOT NULL DEFAULT ''",
        "adres_puani": "REAL NOT NULL DEFAULT 0",
        "kaynak_adaylari_json": "TEXT NOT NULL DEFAULT ''",
        "gorsel_durum": "TEXT NOT NULL DEFAULT ''",
        "icerik_durum": "TEXT NOT NULL DEFAULT ''",
        "genel_durum": "TEXT NOT NULL DEFAULT ''",
        "gorsel_yuklenen": "INTEGER NOT NULL DEFAULT 0",
        "gorsel_deneme": "INTEGER NOT NULL DEFAULT 0",
        "icerik_deneme": "INTEGER NOT NULL DEFAULT 0",
        "genel_deneme": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if sutun not in mevcut_sutunlar:
            baglanti.execute(f"ALTER TABLE otel_gorevleri ADD COLUMN {sutun} {tanim}")
    baglanti.execute(
        """
        UPDATE otel_gorevleri
        SET gorsel_durum = CASE
                WHEN gorsel_gerekli = 1 THEN 'BEKLIYOR'
                ELSE 'GEREKMIYOR'
            END
        WHERE gorsel_durum = ''
        """
    )
    baglanti.execute(
        """
        UPDATE otel_gorevleri
        SET genel_durum = CASE
                WHEN karar = 'ATLA' OR durum = 'TAMAMLANDI' THEN 'GEREKMIYOR'
                ELSE 'BEKLIYOR'
            END
        WHERE genel_durum = ''
        """
    )
    baglanti.execute(
        """
        UPDATE otel_gorevleri
        SET icerik_durum = CASE
                WHEN icerik_gerekli = 1 THEN 'BEKLIYOR'
                ELSE 'GEREKMIYOR'
            END
        WHERE icerik_durum = ''
        """
    )
    baglanti.commit()
    return baglanti


def icerik_durumunu_ayir(deger):
    tamamlanan, toplam = (deger or "0/4").split("/", maxsplit=1)
    return int(tamamlanan.strip()), int(toplam.strip())


def karar_bayraklari(karar):
    return {
        "ATLA": (0, 0),
        "SADECE_GORSEL": (0, 1),
        "SADECE_ICERIK": (1, 0),
        "ICERIK_VE_GORSEL": (1, 1),
    }[karar]


def ilk_durum(karar, kaynak_url=""):
    if karar == "ATLA":
        return "TAMAMLANDI"
    return "HAZIR" if kaynak_url else "KAYNAK_BEKLIYOR"


def raporu_aktar(rapor_yolu, veritabani_yolu):
    rapor_yolu = Path(rapor_yolu).resolve()
    if not rapor_yolu.exists():
        raise FileNotFoundError(f"Rapor bulunamadı: {rapor_yolu}")

    eklenen = 0
    guncellenen = 0
    with closing(baglan(veritabani_yolu)) as db, db, rapor_yolu.open(
        "r", encoding="utf-8-sig", newline=""
    ) as dosya:
        for satir in csv.DictReader(dosya):
            karar = satir["karar"].strip()
            icerik_gerekli, gorsel_gerekli = karar_bayraklari(karar)
            icerik_tamamlanan, icerik_toplam = icerik_durumunu_ayir(
                satir["icerik_durumu"]
            )
            mevcut = db.execute(
                "SELECT id, durum, kaynak_url FROM otel_gorevleri WHERE duzenleme_url = ?",
                (satir["duzenleme_url"].strip(),),
            ).fetchone()

            if mevcut is None:
                imlec = db.execute(
                    """
                    INSERT INTO otel_gorevleri (
                        otel_id, otel_adi, bolge, ham_adi, yildiz, duzenleme_url, gorsel_sayisi,
                        icerik_tamamlanan, icerik_toplam, karar,
                        icerik_gerekli, gorsel_gerekli, genel_durum, durum
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        satir["otel_id"].strip(),
                        satir["otel_adi"].strip(),
                        satir.get("bolge", "").strip(),
                        satir.get("ham_adi", "").strip(),
                        int(satir.get("yildiz", 0) or 0),
                        satir["duzenleme_url"].strip(),
                        int(satir["gorsel_sayisi"]),
                        icerik_tamamlanan,
                        icerik_toplam,
                        karar,
                        icerik_gerekli,
                        gorsel_gerekli,
                        "GEREKMIYOR" if karar == "ATLA" else "BEKLIYOR",
                        ilk_durum(karar),
                    ),
                )
                db.execute(
                    "INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama) VALUES (?, ?, ?)",
                    (imlec.lastrowid, "RAPORDAN_EKLENDI", karar),
                )
                eklenen += 1
                continue

            if karar == "ATLA":
                yeni_durum = "TAMAMLANDI"
            elif mevcut["durum"] == "TAMAMLANDI":
                yeni_durum = "KONTROL_BEKLIYOR"
            elif mevcut["durum"] in ("ISLENIYOR", "INSAN_KONTROLU", "HATA"):
                yeni_durum = mevcut["durum"]
            else:
                yeni_durum = ilk_durum(karar, mevcut["kaynak_url"])

            db.execute(
                """
                UPDATE otel_gorevleri
                SET otel_id = ?, otel_adi = ?, bolge = ?, ham_adi = ?, yildiz = ?, gorsel_sayisi = ?,
                    icerik_tamamlanan = ?, icerik_toplam = ?, karar = ?,
                    icerik_gerekli = ?, gorsel_gerekli = ?,
                    genel_durum = CASE
                        WHEN ? = 'ATLA' THEN 'GEREKMIYOR'
                        WHEN genel_durum IN ('', 'GEREKMIYOR') THEN 'BEKLIYOR'
                        ELSE genel_durum
                    END,
                    durum = ?,
                    guncellenme = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    satir["otel_id"].strip(),
                    satir["otel_adi"].strip(),
                    satir.get("bolge", "").strip(),
                    satir.get("ham_adi", "").strip(),
                    int(satir.get("yildiz", 0) or 0),
                    int(satir["gorsel_sayisi"]),
                    icerik_tamamlanan,
                    icerik_toplam,
                    karar,
                    icerik_gerekli,
                    gorsel_gerekli,
                    karar,
                    yeni_durum,
                    mevcut["id"],
                ),
            )
            guncellenen += 1

    return {"eklenen": eklenen, "guncellenen": guncellenen}


def durum_ozeti(veritabani_yolu):
    with closing(baglan(veritabani_yolu)) as db:
        satirlar = db.execute(
            "SELECT durum, COUNT(*) AS adet FROM otel_gorevleri GROUP BY durum ORDER BY durum"
        ).fetchall()
        toplam = db.execute("SELECT COUNT(*) FROM otel_gorevleri").fetchone()[0]
    return toplam, {satir["durum"]: satir["adet"] for satir in satirlar}


def karar_ozeti(veritabani_yolu):
    with closing(baglan(veritabani_yolu)) as db:
        satirlar = db.execute(
            "SELECT karar, COUNT(*) AS adet FROM otel_gorevleri GROUP BY karar ORDER BY karar"
        ).fetchall()
    return {satir["karar"]: satir["adet"] for satir in satirlar}


def siradaki_gorevler(veritabani_yolu, adet=20, durum="KAYNAK_BEKLIYOR", yildiz=0):
    with closing(baglan(veritabani_yolu)) as db:
        return db.execute(
            """
            SELECT * FROM otel_gorevleri
            WHERE durum = ?
              AND (? = 0 OR yildiz = ?)
            ORDER BY
                deneme_sayisi ASC,
                CASE karar
                    WHEN 'SADECE_ICERIK' THEN 0
                    WHEN 'ICERIK_VE_GORSEL' THEN 1
                    WHEN 'SADECE_GORSEL' THEN 2
                    ELSE 3
                END ASC,
                id ASC
            LIMIT ?
            """,
            (durum, int(yildiz), int(yildiz), adet),
        ).fetchall()


def kaynak_kaydet(
    veritabani_yolu,
    otel_id,
    kaynak_url,
    kaynak_site,
    kaynak_guven=1.0,
    kaynak_adaylari_json="",
    kaynak_adres="",
    adres_puani=0.0,
):
    with closing(baglan(veritabani_yolu)) as db, db:
        gorev = db.execute(
            "SELECT id, karar FROM otel_gorevleri WHERE otel_id = ?", (otel_id,)
        ).fetchone()
        if gorev is None:
            raise KeyError(f"Otel bulunamadı: {otel_id}")
        yeni_durum = "TAMAMLANDI" if gorev["karar"] == "ATLA" else "HAZIR"
        db.execute(
            """
            UPDATE otel_gorevleri
            SET kaynak_url = ?, kaynak_site = ?, kaynak_guven = ?,
                kaynak_adaylari_json = ?, kaynak_adres = ?, adres_puani = ?,
                durum = ?,
                son_hata = '', guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                kaynak_url,
                kaynak_site,
                float(kaynak_guven),
                kaynak_adaylari_json,
                kaynak_adres,
                float(adres_puani),
                yeni_durum,
                gorev["id"],
            ),
        )
        db.execute(
            "INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama) VALUES (?, ?, ?)",
            (gorev["id"], "KAYNAK_BULUNDU", kaynak_url),
        )


def atlas_adresi_kaydet(veritabani_yolu, otel_id, atlas_adres):
    with closing(baglan(veritabani_yolu)) as db, db:
        gorev = db.execute(
            "SELECT id FROM otel_gorevleri WHERE otel_id = ?", (otel_id,)
        ).fetchone()
        if gorev is None:
            raise KeyError(f"Otel bulunamadı: {otel_id}")
        db.execute(
            """
            UPDATE otel_gorevleri
            SET atlas_adres = ?, adres_kontrol_edildi = 1,
                guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (atlas_adres, gorev["id"]),
        )
        db.execute(
            "INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama) VALUES (?, ?, ?)",
            (gorev["id"], "ATLAS_ADRES_OKUNDU", atlas_adres or "ADRES_YOK"),
        )


def kaynak_incelemeye_al(
    veritabani_yolu, otel_id, aciklama, kaynak_adaylari_json=""
):
    with closing(baglan(veritabani_yolu)) as db, db:
        gorev = db.execute(
            "SELECT id FROM otel_gorevleri WHERE otel_id = ?", (otel_id,)
        ).fetchone()
        if gorev is None:
            raise KeyError(f"Otel bulunamadı: {otel_id}")
        db.execute(
            """
            UPDATE otel_gorevleri
            SET durum = 'INSAN_KONTROLU', son_hata = ?,
                kaynak_adaylari_json = ?, guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (aciklama, kaynak_adaylari_json, gorev["id"]),
        )
        db.execute(
            "INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama) VALUES (?, ?, ?)",
            (gorev["id"], "KAYNAK_INCELEME", aciklama),
        )


def kaynak_hatasi_kaydet(veritabani_yolu, otel_id, hata, azami_deneme=3):
    with closing(baglan(veritabani_yolu)) as db, db:
        gorev = db.execute(
            "SELECT id, deneme_sayisi FROM otel_gorevleri WHERE otel_id = ?",
            (otel_id,),
        ).fetchone()
        if gorev is None:
            raise KeyError(f"Otel bulunamadı: {otel_id}")
        deneme = gorev["deneme_sayisi"] + 1
        yeni_durum = "INSAN_KONTROLU" if deneme >= azami_deneme else "KAYNAK_BEKLIYOR"
        db.execute(
            """
            UPDATE otel_gorevleri
            SET durum = ?, deneme_sayisi = ?, son_hata = ?,
                guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (yeni_durum, deneme, hata, gorev["id"]),
        )
        db.execute(
            "INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama) VALUES (?, ?, ?)",
            (gorev["id"], "KAYNAK_HATASI", hata),
        )


def hazir_gorsel_gorevleri(
    veritabani_yolu, adet=3, yildiz=0, grup="", isci_no=0, isci_sayisi=1
):
    isci_no = int(isci_no)
    isci_sayisi = int(isci_sayisi)
    if isci_sayisi < 1 or not 0 <= isci_no < isci_sayisi:
        raise ValueError("Gorsel isci numarasi 0 <= isci_no < isci_sayisi olmalidir.")
    with closing(baglan(veritabani_yolu)) as db:
        return db.execute(
            """
            SELECT * FROM otel_gorevleri
            WHERE durum = 'HAZIR'
              AND gorsel_gerekli = 1
              AND gorsel_durum = 'BEKLIYOR'
              AND kaynak_url <> ''
              AND (? = 0 OR yildiz = ?)
              AND (? = '' OR calisma_grubu = ?)
              AND ((id - 1) % ? = ?)
            ORDER BY id ASC
            LIMIT ?
            """,
            (
                int(yildiz), int(yildiz), str(grup), str(grup),
                isci_sayisi, isci_no, adet,
            ),
        ).fetchall()


def gorsel_islemini_baslat(veritabani_yolu, gorev_id):
    """Görevi yalnız hâlâ hazırsa atomik biçimde görsel işçisine ayırır."""
    with closing(baglan(veritabani_yolu)) as db, db:
        sonuc = db.execute(
            """
            UPDATE otel_gorevleri
            SET durum = 'ISLENIYOR', gorsel_durum = 'ISLENIYOR',
                gorsel_deneme = gorsel_deneme + 1,
                son_hata = '', guncellenme = CURRENT_TIMESTAMP
            WHERE id = ? AND durum = 'HAZIR' AND gorsel_durum = 'BEKLIYOR'
            """,
            (gorev_id,),
        )
        if sonuc.rowcount != 1:
            return False
        db.execute(
            "INSERT INTO gorev_gecmisi (gorev_id, olay) VALUES (?, ?)",
            (gorev_id, "GORSEL_ISLEMI_BASLADI"),
        )
        return True


def kesilen_gorsel_islemini_yeniden_kuyruga_al(
    veritabani_yolu, gorev_id, aciklama="Program kesintisi sonrasi guvenli devam"
):
    """Atlas'ta gorsel bulunmadigi dogrulanan yarim gorevin kilidini acar."""
    with closing(baglan(veritabani_yolu)) as db, db:
        sonuc = db.execute(
            """
            UPDATE otel_gorevleri
            SET durum = 'HAZIR', gorsel_durum = 'BEKLIYOR',
                son_hata = ?, guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
              AND durum = 'ISLENIYOR'
              AND gorsel_durum = 'ISLENIYOR'
            """,
            (str(aciklama), gorev_id),
        )
        if sonuc.rowcount != 1:
            return False
        db.execute(
            """INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama)
               VALUES (?, ?, ?)""",
            (gorev_id, "KESILEN_GORSEL_YENIDEN_KUYRUKTA", str(aciklama)),
        )
        return True


def kesilen_icerik_islemini_yeniden_kuyruga_al(
    veritabani_yolu, gorev_id, aciklama="Program kesintisi sonrasi guvenli devam"
):
    """Yarim kalan Genel/Icerik gorevini idempotent yeniden denemeye acar."""
    with closing(baglan(veritabani_yolu)) as db, db:
        sonuc = db.execute(
            """
            UPDATE otel_gorevleri
            SET durum = 'HAZIR',
                icerik_durum = CASE WHEN icerik_durum='ISLENIYOR'
                    THEN 'BEKLIYOR' ELSE icerik_durum END,
                genel_durum = CASE WHEN genel_durum='ISLENIYOR'
                    THEN 'BEKLIYOR' ELSE genel_durum END,
                son_hata = ?, guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
              AND durum='ISLENIYOR'
              AND (icerik_durum='ISLENIYOR' OR genel_durum='ISLENIYOR')
            """,
            (str(aciklama), gorev_id),
        )
        if sonuc.rowcount != 1:
            return False
        db.execute(
            """INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama)
               VALUES (?, ?, ?)""",
            (gorev_id, "KESILEN_ICERIK_YENIDEN_KUYRUKTA", str(aciklama)),
        )
        return True


def hatali_gorevleri_yeniden_kuyruga_al(veritabani_yolu, azami_deneme=3):
    """Yalniz isci hatalarini sinirli sayida yeniden dener.

    TAMAMLANDI ve INSAN_KONTROLU kayitlarina dokunulmaz. Gorsel hatasi ancak
    gorsel deneme limiti dolmadiysa; Genel/Icerik hatasi da gorsel asamasi
    tamamlanmissa yeniden acilir.
    """
    azami_deneme = max(1, int(azami_deneme))
    yeniden = []
    with closing(baglan(veritabani_yolu)) as db, db:
        gorevler = db.execute(
            """SELECT * FROM otel_gorevleri
               WHERE durum='HATA'
               ORDER BY id"""
        ).fetchall()
        for gorev in gorevler:
            gorsel_tekrar = (
                gorev["gorsel_durum"] == "HATA"
                and int(gorev["gorsel_deneme"] or 0) < azami_deneme
            )
            icerik_tekrar = (
                gorev["gorsel_durum"] in ("GEREKMIYOR", "TAMAMLANDI")
                and (
                    (
                        gorev["icerik_durum"] == "HATA"
                        and int(gorev["icerik_deneme"] or 0) < azami_deneme
                    )
                    or (
                        gorev["genel_durum"] == "HATA"
                        and int(gorev["genel_deneme"] or 0) < azami_deneme
                    )
                )
            )
            if not gorsel_tekrar and not icerik_tekrar:
                continue

            eski_hata = str(gorev["son_hata"] or "")
            yeni_gorsel = "BEKLIYOR" if gorsel_tekrar else gorev["gorsel_durum"]
            yeni_icerik = (
                "BEKLIYOR"
                if icerik_tekrar and gorev["icerik_durum"] == "HATA"
                else gorev["icerik_durum"]
            )
            yeni_genel = (
                "BEKLIYOR"
                if icerik_tekrar and gorev["genel_durum"] == "HATA"
                else gorev["genel_durum"]
            )
            db.execute(
                """UPDATE otel_gorevleri
                   SET durum='HAZIR', gorsel_durum=?, icerik_durum=?,
                       genel_durum=?, son_hata=?, guncellenme=CURRENT_TIMESTAMP
                   WHERE id=? AND durum='HATA'""",
                (
                    yeni_gorsel,
                    yeni_icerik,
                    yeni_genel,
                    f"OTOMATIK_YENIDEN_DENEME: {eski_hata}"[:2000],
                    gorev["id"],
                ),
            )
            db.execute(
                """INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama)
                   VALUES (?, ?, ?)""",
                (
                    gorev["id"],
                    "HATA_OTOMATIK_YENIDEN_KUYRUKTA",
                    f"azami_deneme={azami_deneme}; {eski_hata}"[:2000],
                ),
            )
            yeniden.append(
                {
                    "id": gorev["id"],
                    "otel_id": gorev["otel_id"],
                    "otel_adi": gorev["otel_adi"],
                    "asama": "GORSEL" if gorsel_tekrar else "GENEL_ICERIK",
                }
            )
    return yeniden


def gorsel_yukleme_kuyruga_alindi(veritabani_yolu, gorev_id, gorsel_adedi):
    """Yükleme başlatıldıktan sonra görevi doğrulama kuyruğuna taşır."""
    with closing(baglan(veritabani_yolu)) as db, db:
        db.execute(
            """
            UPDATE otel_gorevleri
            SET durum = 'KONTROL_BEKLIYOR', gorsel_durum = 'KONTROL_BEKLIYOR',
                gorsel_yuklenen = ?, son_hata = '', guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(gorsel_adedi), gorev_id),
        )
        db.execute(
            "INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama) VALUES (?, ?, ?)",
            (gorev_id, "GORSELLER_YUKLEME_KUYRUGUNDA", str(int(gorsel_adedi))),
        )


def gorsel_hatasi_kaydet(veritabani_yolu, gorev_id, hata):
    with closing(baglan(veritabani_yolu)) as db, db:
        db.execute(
            """
            UPDATE otel_gorevleri
            SET durum = 'HATA', gorsel_durum = 'HATA', son_hata = ?,
                guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(hata), gorev_id),
        )
        db.execute(
            "INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama) VALUES (?, ?, ?)",
            (gorev_id, "GORSEL_HATASI", str(hata)),
        )


def gorsel_kaynagini_degistir(
    veritabani_yolu,
    gorev_id,
    kaynak_url,
    kaynak_site,
    kaynak_guven=0.0,
    kaynak_adres="",
    adres_puani=0.0,
):
    """Galerisi calisan alternatif kaynagi aktif yapar; gorev durumunu degistirmez."""
    with closing(baglan(veritabani_yolu)) as db, db:
        sonuc = db.execute(
            """
            UPDATE otel_gorevleri
            SET kaynak_url = ?, kaynak_site = ?, kaynak_guven = ?,
                kaynak_adres = ?, adres_puani = ?,
                guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                str(kaynak_url),
                str(kaynak_site),
                float(kaynak_guven or 0.0),
                str(kaynak_adres or ""),
                float(adres_puani or 0.0),
                gorev_id,
            ),
        )
        if sonuc.rowcount != 1:
            return False
        db.execute(
            """INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama)
               VALUES (?, ?, ?)""",
            (
                gorev_id,
                "GORSEL_ALTERNATIF_KAYNAK_SECILDI",
                f"{kaynak_site} | {kaynak_url}",
            ),
        )
        return True


def kontrol_bekleyen_gorsel_gorevleri(
    veritabani_yolu, adet=10, yildiz=0, grup=""
):
    with closing(baglan(veritabani_yolu)) as db:
        return db.execute(
            """
            SELECT * FROM otel_gorevleri
            WHERE durum = 'KONTROL_BEKLIYOR'
              AND gorsel_durum = 'KONTROL_BEKLIYOR'
              AND (? = 0 OR yildiz = ?)
              AND (? = '' OR calisma_grubu = ?)
            ORDER BY id ASC
            LIMIT ?
            """,
            (int(yildiz), int(yildiz), str(grup), str(grup), adet),
        ).fetchall()


def gorsel_dogrulandi(veritabani_yolu, gorev_id, gercek_gorsel_sayisi):
    """Atlas'ta en az bir görsel görüldüğünde görsel alt görevini tamamlar."""
    gercek_gorsel_sayisi = int(gercek_gorsel_sayisi)
    if gercek_gorsel_sayisi < 1:
        raise ValueError("Görsel doğrulaması için sayı en az 1 olmalıdır.")

    with closing(baglan(veritabani_yolu)) as db, db:
        gorev = db.execute(
            """SELECT icerik_gerekli, icerik_durum, genel_durum
               FROM otel_gorevleri WHERE id = ?""",
            (gorev_id,),
        ).fetchone()
        if gorev is None:
            raise KeyError(f"Görev bulunamadı: {gorev_id}")

        icerik_hazir = (
            not bool(gorev["icerik_gerekli"])
            or gorev["icerik_durum"] == "TAMAMLANDI"
        )
        genel_hazir = gorev["genel_durum"] in ("GEREKMIYOR", "TAMAMLANDI")
        yeni_durum = "TAMAMLANDI" if icerik_hazir and genel_hazir else "HAZIR"
        db.execute(
            """
            UPDATE otel_gorevleri
            SET durum = ?, gorsel_durum = 'TAMAMLANDI',
                gorsel_sayisi = ?, guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (yeni_durum, gercek_gorsel_sayisi, gorev_id),
        )
        db.execute(
            "INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama) VALUES (?, ?, ?)",
            (gorev_id, "GORSELLER_DOGRULANDI", str(gercek_gorsel_sayisi)),
        )


def hazir_icerik_gorevleri(veritabani_yolu, adet=10, yildiz=0, grup=""):
    """Görsel işi tamamlanmış veya gerekmeyen içerik görevlerini döndürür."""
    with closing(baglan(veritabani_yolu)) as db:
        return db.execute(
            """
            SELECT * FROM otel_gorevleri
            WHERE durum = 'HAZIR'
              AND (
                    (icerik_gerekli = 1 AND icerik_durum = 'BEKLIYOR')
                    OR genel_durum = 'BEKLIYOR'
                  )
              AND gorsel_durum IN ('GEREKMIYOR', 'TAMAMLANDI')
              AND kaynak_url <> ''
              AND (? = 0 OR yildiz = ?)
              AND (? = '' OR calisma_grubu = ?)
            ORDER BY id ASC
            LIMIT ?
            """,
            (int(yildiz), int(yildiz), str(grup), str(grup), adet),
        ).fetchall()


def icerik_islemini_baslat(veritabani_yolu, gorev_id):
    """Hazir gorevi atomik olarak icerik iscisine ayirir."""
    with closing(baglan(veritabani_yolu)) as db, db:
        sonuc = db.execute(
            """
            UPDATE otel_gorevleri
            SET durum = 'ISLENIYOR',
                icerik_durum = CASE
                    WHEN icerik_gerekli = 1 AND icerik_durum = 'BEKLIYOR'
                    THEN 'ISLENIYOR' ELSE icerik_durum END,
                genel_durum = CASE
                    WHEN genel_durum = 'BEKLIYOR' THEN 'ISLENIYOR'
                    ELSE genel_durum END,
                icerik_deneme = CASE
                    WHEN icerik_gerekli = 1 AND icerik_durum = 'BEKLIYOR'
                    THEN icerik_deneme + 1 ELSE icerik_deneme END,
                genel_deneme = CASE
                    WHEN genel_durum = 'BEKLIYOR' THEN genel_deneme + 1
                    ELSE genel_deneme END,
                son_hata = '', guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
              AND durum = 'HAZIR'
              AND (
                    (icerik_gerekli = 1 AND icerik_durum = 'BEKLIYOR')
                    OR genel_durum = 'BEKLIYOR'
                  )
              AND gorsel_durum IN ('GEREKMIYOR', 'TAMAMLANDI')
              AND kaynak_url <> ''
            """,
            (gorev_id,),
        )
        if sonuc.rowcount != 1:
            return False
        db.execute(
            "INSERT INTO gorev_gecmisi (gorev_id, olay) VALUES (?, ?)",
            (gorev_id, "ICERIK_ISLEMI_BASLADI"),
        )
        return True


def icerik_kontrol_bekliyor(veritabani_yolu, gorev_id):
    """Kaydet tiklandiktan sonra Atlas dogrulamasini zorunlu kilar."""
    with closing(baglan(veritabani_yolu)) as db, db:
        db.execute(
            """
            UPDATE otel_gorevleri
            SET durum = 'KONTROL_BEKLIYOR',
                icerik_durum = 'KONTROL_BEKLIYOR',
                son_hata = '', guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (gorev_id,),
        )
        db.execute(
            "INSERT INTO gorev_gecmisi (gorev_id, olay) VALUES (?, ?)",
            (gorev_id, "ICERIK_ATLAS_KONTROLUNDE"),
        )


def kontrol_bekleyen_icerik_gorevleri(
    veritabani_yolu, adet=10, yildiz=0, grup=""
):
    with closing(baglan(veritabani_yolu)) as db:
        return db.execute(
            """
            SELECT * FROM otel_gorevleri
            WHERE durum = 'KONTROL_BEKLIYOR'
              AND icerik_durum = 'KONTROL_BEKLIYOR'
              AND (? = 0 OR yildiz = ?)
              AND (? = '' OR calisma_grubu = ?)
            ORDER BY id ASC
            LIMIT ?
            """,
            (int(yildiz), int(yildiz), str(grup), str(grup), adet),
        ).fetchall()


def icerik_dogrulandi(veritabani_yolu, gorev_id, tamamlanan, toplam=4):
    """Atlas'taki canli icerik sayaci en az 1/4 oldugunda gorevi tamamlar."""
    tamamlanan = int(tamamlanan)
    toplam = max(1, int(toplam))
    if tamamlanan < 1:
        raise ValueError("Icerik dogrulamasi icin tamamlanan sayisi en az 1 olmali.")

    with closing(baglan(veritabani_yolu)) as db, db:
        gorev = db.execute(
            """SELECT gorsel_gerekli, gorsel_durum, genel_durum
               FROM otel_gorevleri WHERE id = ?""",
            (gorev_id,),
        ).fetchone()
        if gorev is None:
            raise KeyError(f"Gorev bulunamadi: {gorev_id}")
        gorsel_hazir = (
            not bool(gorev["gorsel_gerekli"])
            or gorev["gorsel_durum"] == "TAMAMLANDI"
        )
        genel_hazir = gorev["genel_durum"] in ("GEREKMIYOR", "TAMAMLANDI")
        yeni_durum = "TAMAMLANDI" if gorsel_hazir and genel_hazir else "HAZIR"
        db.execute(
            """
            UPDATE otel_gorevleri
            SET durum = ?, icerik_durum = 'TAMAMLANDI',
                icerik_tamamlanan = ?, icerik_toplam = ?,
                son_hata = '', guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (yeni_durum, tamamlanan, toplam, gorev_id),
        )
        db.execute(
            "INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama) VALUES (?, ?, ?)",
            (gorev_id, "ICERIK_DOGRULANDI", f"{tamamlanan}/{toplam}"),
        )


def icerik_hatasi_kaydet(veritabani_yolu, gorev_id, hata):
    """Sorunlu icerik gorevini tekrar otomatik secilmeyecek sekilde ayirir."""
    with closing(baglan(veritabani_yolu)) as db, db:
        db.execute(
            """
            UPDATE otel_gorevleri
            SET durum = 'HATA',
                icerik_durum = CASE WHEN icerik_durum = 'ISLENIYOR'
                    THEN 'HATA' ELSE icerik_durum END,
                genel_durum = CASE WHEN genel_durum = 'ISLENIYOR'
                    THEN 'HATA' ELSE genel_durum END,
                son_hata = ?,
                guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(hata), gorev_id),
        )
        db.execute(
            "INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama) VALUES (?, ?, ?)",
            (gorev_id, "ICERIK_HATASI", str(hata)),
        )


def genel_dogrulandi(veritabani_yolu, gorev_id, aciklama=""):
    """Genel sekmesi kontrol edilip boş ve bulunabilen alanlar dolduruldu."""
    with closing(baglan(veritabani_yolu)) as db, db:
        gorev = db.execute(
            """SELECT gorsel_gerekli, gorsel_durum,
                      icerik_gerekli, icerik_durum
               FROM otel_gorevleri WHERE id = ?""",
            (gorev_id,),
        ).fetchone()
        if gorev is None:
            raise KeyError(f"Gorev bulunamadi: {gorev_id}")
        gorsel_hazir = (
            not bool(gorev["gorsel_gerekli"])
            or gorev["gorsel_durum"] == "TAMAMLANDI"
        )
        icerik_hazir = (
            not bool(gorev["icerik_gerekli"])
            or gorev["icerik_durum"] == "TAMAMLANDI"
        )
        icerik_devam = gorev["icerik_durum"] in (
            "ISLENIYOR", "KONTROL_BEKLIYOR"
        )
        if icerik_devam:
            yeni_durum = (
                "KONTROL_BEKLIYOR"
                if gorev["icerik_durum"] == "KONTROL_BEKLIYOR"
                else "ISLENIYOR"
            )
        else:
            yeni_durum = "TAMAMLANDI" if gorsel_hazir and icerik_hazir else "HAZIR"
        db.execute(
            """
            UPDATE otel_gorevleri
            SET durum = ?, genel_durum = 'TAMAMLANDI',
                son_hata = '', guncellenme = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (yeni_durum, gorev_id),
        )
        db.execute(
            "INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama) VALUES (?, ?, ?)",
            (gorev_id, "GENEL_DOGRULANDI", str(aciklama)),
        )


def hazir_gorevleri_partiye_ata(veritabani_yolu, grup, adet=20, yildiz=0):
    """En eski HAZIR görevleri belirli otomasyon partisine atomik olarak ayırır."""
    grup = str(grup).strip()
    if not grup:
        raise ValueError("Çalışma grubu boş olamaz.")
    with closing(baglan(veritabani_yolu)) as db, db:
        satirlar = db.execute(
            """
            SELECT id FROM otel_gorevleri
            WHERE durum = 'HAZIR'
              AND kaynak_url <> ''
              AND calisma_grubu = ''
              AND (? = 0 OR yildiz = ?)
            ORDER BY id ASC
            LIMIT ?
            """,
            (int(yildiz), int(yildiz), int(adet)),
        ).fetchall()
        ids = [satir["id"] for satir in satirlar]
        for gorev_id in ids:
            db.execute(
                """UPDATE otel_gorevleri
                   SET calisma_grubu = ?, guncellenme = CURRENT_TIMESTAMP
                   WHERE id = ? AND durum = 'HAZIR' AND calisma_grubu = ''""",
                (grup, gorev_id),
            )
            db.execute(
                "INSERT INTO gorev_gecmisi (gorev_id, olay, aciklama) VALUES (?, ?, ?)",
                (gorev_id, "PARTIYE_ATANDI", grup),
            )
        return parti_gorevleri_baglanti(db, grup)


def parti_gorevleri_baglanti(db, grup):
    return db.execute(
        """SELECT * FROM otel_gorevleri
           WHERE calisma_grubu = ? ORDER BY id ASC""",
        (str(grup),),
    ).fetchall()


def parti_gorevleri(veritabani_yolu, grup):
    with closing(baglan(veritabani_yolu)) as db:
        return parti_gorevleri_baglanti(db, grup)
