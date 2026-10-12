"""Tests de la grille d'audit des sources (python -m unittest discover tests)."""
import json
import sys
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402


def pubmed(pubtypes, mesh=(), abstract="", grants=(), coi=None, title="Titre"):
    pt = "".join(f"<PublicationType>{p}</PublicationType>" for p in pubtypes)
    mh = "".join(f"<MeshHeading><DescriptorName>{m}</DescriptorName></MeshHeading>" for m in mesh)
    gr = "".join(f"<Grant><Agency>{g}</Agency></Grant>" for g in grants)
    co = f"<CoiStatement>{coi}</CoiStatement>" if coi is not None else ""
    xml = (
        "<PubmedArticle><MedlineCitation><PMID>1</PMID><Article>"
        f"<ArticleTitle>{title}</ArticleTitle><Abstract><AbstractText>{abstract}</AbstractText></Abstract>"
        f"<PublicationTypeList>{pt}</PublicationTypeList><GrantList>{gr}</GrantList></Article>"
        f"<MeshHeadingList>{mh}</MeshHeadingList>{co}</MedlineCitation></PubmedArticle>"
    )
    return ET.fromstring(xml)


class AuditPubMed(unittest.TestCase):
    def audit(self, *args, **kw):
        art = pubmed(*args, **kw)
        return main.audit_pubmed(art, kw.get("abstract", ""), kw.get("title", "Titre"), 100)

    def test_gros_essai_randomise(self):
        a, v = self.audit(["Randomized Controlled Trial"], ["Humans"],
                          abstract="We randomized 240 participants with type 1 diabetes to drug or placebo.",
                          grants=["NIDDK NIH HHS"], coi="The authors declare no competing interests.")
        self.assertIn("Essai randomisé contrôlé", a)
        self.assertIn("240 participants", a)
        self.assertIn("financement : NIDDK NIH HHS", a)
        self.assertIn("aucun conflit d'intérêts déclaré", a)
        self.assertTrue(v.startswith("À retenir — confiance élevée"), v)

    def test_petit_essai(self):
        _, v = self.audit(["Randomized Controlled Trial"], ["Humans"],
                          abstract="Forty-two adults (n = 42) were randomized versus placebo.")
        self.assertTrue(v.startswith("À retenir avec réserves — confiance moyenne"), v)
        self.assertIn("moins de 100 participants", v)

    def test_industrie_plafonne(self):
        _, v = self.audit(["Randomized Controlled Trial"], ["Humans"],
                          abstract="A total of 1,203 patients were randomized.",
                          coi="J.D. is an employee of Novo Nordisk and holds stock.")
        self.assertTrue(v.startswith("À retenir — confiance moyenne"), v)
        self.assertIn("industrie", v)

    def test_animal(self):
        a, v = self.audit(["Journal Article"], ["Animals", "Mice, Inbred NOD"],
                          abstract="NOD mice treated with the antibody showed delayed onset.")
        self.assertIn("population : animal", a)
        self.assertIn("effectif non indiqué", a)
        self.assertTrue(v.startswith("À retenir avec réserves — confiance faible"), v)
        self.assertIn("chez l'animal", v)

    def test_animal_sans_mesh(self):
        a, _ = self.audit(["Journal Article"], [], abstract="In NOD mice, islet grafts survived.")
        self.assertIn("population : animal", a)

    def test_editorial(self):
        a, v = self.audit(["Editorial"], ["Humans"], abstract="")
        self.assertIn("avis (source secondaire)", a)
        self.assertTrue(v.startswith("À écarter comme preuve"), v)

    def test_meta_analyse(self):
        a, v = self.audit(["Meta-Analysis", "Review"], ["Humans"], abstract="We pooled 12 trials.")
        self.assertIn("source secondaire", a)
        self.assertIn("groupe de comparaison : sans objet", a)
        self.assertTrue(v.startswith("À retenir — confiance élevée"), v)

    def test_rien_d_invente(self):
        a, _ = self.audit(["Journal Article"], [], abstract="")
        self.assertIn("financement : non indiqué", a)
        self.assertIn("conflits d'intérêts : non indiqués", a)
        self.assertIn("population : non indiquée", a)

    def test_annee_pas_prise_pour_effectif(self):
        self.assertIsNone(main._largest_sample("In 2024 participants were enrolled."))


class AuditClinicalTrials(unittest.TestCase):
    def study(self, has_results=False, sponsor="NIH", allocation="RANDOMIZED", count=300, ctype="ESTIMATED"):
        return {
            "hasResults": has_results,
            "protocolSection": {
                "designModule": {
                    "studyType": "INTERVENTIONAL",
                    "designInfo": {"allocation": allocation, "maskingInfo": {"masking": "DOUBLE"}},
                    "enrollmentInfo": {"count": count, "type": ctype},
                },
                "sponsorCollaboratorsModule": {"leadSponsor": {"name": "X", "class": sponsor}},
            },
        }

    def test_protocole_sans_resultats(self):
        a, v = main.audit_clinicaltrials(self.study(), 100)
        self.assertIn("Essai interventionnel randomisé, double aveugle", a)
        self.assertIn("300 participants (prévus)", a)
        self.assertTrue(v.startswith("À suivre"), v)

    def test_resultats_industrie(self):
        a, v = main.audit_clinicaltrials(self.study(True, "INDUSTRY", ctype="ACTUAL"), 100)
        self.assertIn("promoteur industriel", a)
        self.assertTrue(v.startswith("À retenir avec réserves — confiance moyenne"), v)
        self.assertIn("industrie", v)


class AuditRss(unittest.TestCase):
    def test_communique(self):
        self.assertTrue(main.audit_rss(False)[1].startswith("À écarter comme preuve"))
        self.assertTrue(main.audit_rss(True)[1].startswith("À retenir avec réserves"))


class Restitution(unittest.TestCase):
    def test_tri_des_verdicts(self):
        vs = ["À écarter comme preuve — x", "À suivre — x", "À retenir avec réserves — x", "À retenir — x", ""]
        self.assertEqual([main.verdict_rank(v) for v in vs], [3, 2, 1, 0, 4])

    def test_markdown(self):
        e = main.Entry(datetime(2026, 10, 5, tzinfo=timezone.utc), "France", "Titre", "Biologique",
                       "Article", "Phase 2", "Résumé.", "Élevé", "", "PubMed", "https://doi.org/10.1/x",
                       audit="Essai randomisé contrôlé · population : humains", verdict="À retenir — confiance élevée : x")
        md = main.format_entry_md(e, "%d/%m/%Y")
        lines = md.splitlines()
        self.assertEqual(lines[-3], "- **Audit** : Essai randomisé contrôlé · population : humains")
        self.assertEqual(lines[-2], "- **Verdict** : À retenir — confiance élevée : x")
        self.assertTrue(lines[-1].startswith("- **Source** :"))

    def test_email_avec_ancien_resume(self):
        # Un résumé d'exécution réel, antérieur à l'audit (sans champs audit/verdict),
        # doit toujours produire un email, avec l'avertissement médical en tête.
        summary = json.loads((ROOT / "logs" / "last_run_summary.json").read_text(encoding="utf-8"))
        summary["error"] = None
        txt = main._build_email_body(summary)
        htm = main._build_email_body_html(summary)
        self.assertIn(main.DISCLAIMER, txt)
        self.assertIn("pas un avis médical", htm)

    def test_email_trie(self):
        base = {"date": "05/10/2026", "country": "France", "axis": "Biologique", "doc_type": "Article",
                "maturity": "Phase 2", "confidence": "Élevé", "confidence_note": "", "summary": "R.",
                "source_name": "PubMed", "source_url": "https://x"}
        summary = {"window_start": "a", "window_end": "b", "error": None, "fiches_updated": [],
                   "new_entries": [dict(base, title="B", verdict="À écarter comme preuve — x", audit="a"),
                                   dict(base, title="A", verdict="À retenir — confiance élevée : x", audit="a")]}
        txt = main._build_email_body(summary)
        self.assertLess(txt.index("] A"), txt.index("] B"))
        self.assertIn("Verdict : À retenir", txt)
        self.assertIn("🔎 <b>Audit</b>", main._build_email_body_html(summary))


if __name__ == "__main__":
    unittest.main()
