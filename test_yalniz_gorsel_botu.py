import csv
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from veritabani import baglan, gorsel_dogrulandi, raporu_aktar
from yalniz_gorsel_botu import (
    gorsel_isci_planlari,
    secili_gorsel_kuyrugunu_hazirla,
)


class YalnizGorselBotuTesti(unittest.TestCase):
    def setUp(self):
        self.gecici = tempfile.TemporaryDirectory()
        self.klasor = Path(self.gecici.name)
        self.db = self.klasor / "gorevler.db"
        self.rapor = self.klasor / "secili.csv"
        alanlar = (
            "otel_id", "otel_adi", "bolge", "ham_adi", "yildiz",
            "gorsel_sayisi", "icerik_durumu", "karar", "duzenleme_url",
        )
        satirlar = [
            ("100001", "Dolu Otel", "Antalya", "", 5, 12, "0/4", "SADECE_ICERIK"),
            ("100002", "Bos Otel 1", "Kemer", "", 5, 0, "0/4", "ICERIK_VE_GORSEL"),
            ("100003", "Bos Otel 2", "Alanya", "", 4, 0, "1/4", "SADECE_GORSEL"),
            ("100004", "Bos Otel 3", "Side", "", 5, 0, "4/4", "SADECE_GORSEL"),
        ]
        with self.rapor.open("w", encoding="utf-8-sig", newline="") as dosya:
            yazici = csv.DictWriter(dosya, fieldnames=alanlar)
            yazici.writeheader()
            for otel_id, ad, bolge, ham, yildiz, gorsel, icerik, karar in satirlar:
                yazici.writerow(
                    {
                        "otel_id": otel_id,
                        "otel_adi": ad,
                        "bolge": bolge,
                        "ham_adi": ham,
                        "yildiz": yildiz,
                        "gorsel_sayisi": gorsel,
                        "icerik_durumu": icerik,
                        "karar": karar,
                        "duzenleme_url": f"https://atlas.example/admin/otel/{otel_id}",
                    }
                )
        raporu_aktar(self.rapor, self.db)
        secili_gorsel_kuyrugunu_hazirla(self.db)

    def tearDown(self):
        self.gecici.cleanup()

    def test_dolu_otel_atlanir_ve_genel_icerik_kapanir(self):
        with closing(baglan(self.db)) as db:
            gorev = db.execute(
                "SELECT * FROM otel_gorevleri WHERE otel_id='100001'"
            ).fetchone()
        self.assertEqual(gorev["durum"], "TAMAMLANDI")
        self.assertEqual(gorev["karar"], "ATLA")
        self.assertEqual(gorev["gorsel_durum"], "GEREKMIYOR")
        self.assertEqual(gorev["icerik_durum"], "GEREKMIYOR")
        self.assertEqual(gorev["genel_durum"], "GEREKMIYOR")
        self.assertEqual(gorev["icerik_gerekli"], 0)
        self.assertEqual(gorev["gorsel_gerekli"], 0)

    def test_bos_otel_yalniz_gorsel_kuyruguna_girer(self):
        with closing(baglan(self.db)) as db:
            gorev = db.execute(
                "SELECT * FROM otel_gorevleri WHERE otel_id='100002'"
            ).fetchone()
        self.assertEqual(gorev["durum"], "KAYNAK_BEKLIYOR")
        self.assertEqual(gorev["karar"], "SADECE_GORSEL")
        self.assertEqual(gorev["gorsel_durum"], "BEKLIYOR")
        self.assertEqual(gorev["icerik_durum"], "GEREKMIYOR")
        self.assertEqual(gorev["genel_durum"], "GEREKMIYOR")
        self.assertEqual(gorev["icerik_gerekli"], 0)
        self.assertEqual(gorev["gorsel_gerekli"], 1)

    def test_uc_gorsel_iscisi_disjoint_planlanir_icerik_iscisi_yoktur(self):
        with closing(baglan(self.db)) as db, db:
            db.execute(
                """
                UPDATE otel_gorevleri
                SET durum='HAZIR', kaynak_url='https://www.trivago.com/hotel',
                    kaynak_site='TRIVAGO'
                WHERE gorsel_gerekli=1
                """
            )
        args = SimpleNamespace(
            gorsel_isci=3,
            gorsel_parti=8,
            min_genislik=600,
            min_yukseklik=400,
        )
        planlar = gorsel_isci_planlari(args, self.db)
        self.assertEqual([etiket for etiket, _ in planlar], [
            "GORSEL-1", "GORSEL-2", "GORSEL-3"
        ])
        tum_komut = " ".join(str(parca) for _, komut in planlar for parca in komut)
        self.assertIn("gorsel_isci.py", tum_komut)
        self.assertNotIn("icerik_isci.py", tum_komut)

    def test_gorsel_bittiginde_icerik_ve_genel_acilmaz(self):
        with closing(baglan(self.db)) as db, db:
            gorev = db.execute(
                "SELECT id FROM otel_gorevleri WHERE otel_id='100002'"
            ).fetchone()
            db.execute(
                """
                UPDATE otel_gorevleri
                SET durum='ISLENIYOR', gorsel_durum='ISLENIYOR'
                WHERE id=?
                """,
                (gorev["id"],),
            )
        gorsel_dogrulandi(self.db, gorev["id"], 37)
        with closing(baglan(self.db)) as db:
            tamam = db.execute(
                "SELECT * FROM otel_gorevleri WHERE id=?", (gorev["id"],)
            ).fetchone()
        self.assertEqual(tamam["durum"], "TAMAMLANDI")
        self.assertEqual(tamam["gorsel_durum"], "TAMAMLANDI")
        self.assertEqual(tamam["gorsel_sayisi"], 37)
        self.assertEqual(tamam["icerik_durum"], "GEREKMIYOR")
        self.assertEqual(tamam["genel_durum"], "GEREKMIYOR")


if __name__ == "__main__":
    unittest.main()
