import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_bib  # noqa: E402


ALLOW = {"tongStatisticalPhysics", "fitzpatrickEffusion"}


def issues_for(text: str, records=None, allowlist=None):
    _entries, issues = check_bib.check_text(text, allowlist if allowlist is not None else ALLOW, records)
    return issues


def levels(issues, level):
    return [issue for issue in issues if issue.level == level]


class BibliographyTests(unittest.TestCase):
    def test_shared_bibliography_passes(self):
        text = (ROOT / "bibliography.bib").read_text(encoding="utf-8")
        entries, issues = check_bib.check_text(text, ALLOW)
        self.assertEqual(len(entries), 49)
        self.assertEqual(levels(issues, "error"), [])
        warnings = levels(issues, "warning")
        by_key: dict[str, list[str]] = {}
        for issue in warnings:
            by_key.setdefault(issue.key, []).append(issue.message)
        self.assertEqual(
            set(by_key),
            {"tongStatisticalPhysics", "fitzpatrickEffusion", "taleb2025scoftv4"},
        )
        self.assertIn("no year", by_key["tongStatisticalPhysics"][0])
        self.assertIn("no year", by_key["fitzpatrickEffusion"][0])
        self.assertTrue(any("no publisher" in message for message in by_key["taleb2025scoftv4"]))
        self.assertTrue(any("taleb2020scoft" in message for message in by_key["taleb2025scoftv4"]))

    def test_duplicate_key_and_bad_format(self):
        text = """
@article{Doe2020paper,
  author = {Doe, Jane},
  title = {A Paper},
  journal = {Journal},
  year = {2020},
}
@article{Doe2020paper,
  author = {Doe, Jane},
  title = {Another Paper},
  journal = {Journal},
  year = {2020},
}
@misc{BadKey,
  author = {Doe, Jane},
  title = {Note},
  year = {2020},
  url = {https://example.com},
}
"""
        found = issues_for(text)
        errors = levels(found, "error")
        self.assertTrue(any("duplicate key" in issue.message for issue in errors))
        self.assertTrue(any(issue.key == "BadKey" for issue in errors))

    def test_allowlist_skips_format_only(self):
        text = """
@misc{tongStatisticalPhysics,
  author = {Tong, David},
  title = {Notes},
  url = {https://example.com},
}
"""
        found = issues_for(text)
        self.assertEqual(levels(found, "error"), [])
        self.assertTrue(any("no year" in issue.message for issue in found))

    def test_key_year_must_match(self):
        text = """
@article{doe2020paper,
  author = {Doe, Jane},
  title = {A Paper},
  journal = {Journal},
  year = {2021},
}
"""
        found = issues_for(text)
        self.assertTrue(any("does not match year field" in issue.message for issue in found))

    def test_duplicate_doi_and_near_title(self):
        text = """
@article{ada2020one,
  author = {Ada, A},
  title = {Shared Result},
  journal = {Journal},
  year = {2020},
  doi = {10.1000/xyz},
}
@article{bee2020two,
  author = {Bee, B},
  title = {Shared Result},
  journal = {Journal},
  year = {2020},
  doi = {https://doi.org/10.1000/XYZ},
}
"""
        found = issues_for(text)
        messages = " ".join(issue.message for issue in levels(found, "error"))
        self.assertIn("duplicate DOI", messages)
        self.assertIn("near-duplicate", messages)

    def test_same_title_different_year_warns(self):
        text = """
@book{taleb2020scoft,
  author = {Taleb, Nassim Nicholas},
  title = {Statistical Consequences of Fat Tails},
  publisher = {STEM Academic Press},
  year = {2020},
}
@book{taleb2025scoftv4,
  author = {Taleb, Nassim Nicholas},
  title = {Statistical Consequences of Fat Tails},
  year = {2025},
  url = {https://arxiv.org/abs/2001.10488v4},
}
"""
        found = issues_for(text)
        self.assertEqual(levels(found, "error"), [])
        warnings = levels(found, "warning")
        self.assertTrue(any("same normalized title" in issue.message for issue in warnings))
        self.assertTrue(any("no publisher" in issue.message for issue in warnings))

    def test_book_without_any_locator_fails(self):
        text = """
@book{doe2020book,
  author = {Doe, Jane},
  title = {A Book},
  year = {2020},
}
"""
        found = issues_for(text)
        self.assertTrue(any("publisher, url, or doi" in issue.message for issue in found))

    def test_misc_needs_a_locator(self):
        text = """
@misc{doe2020note,
  author = {Doe, Jane},
  title = {A Note},
  year = {2020},
}
"""
        found = issues_for(text)
        self.assertTrue(any("url or howpublished" in issue.message for issue in found))

    def test_names_and_crossref_subtitle(self):
        self.assertEqual(
            check_bib.normalize_name(check_bib.bib_surnames("van der Vaart, A. W.")[0]),
            "vandervaart",
        )
        self.assertEqual(
            check_bib.normalize_name(
                check_bib.crossref_family({"family": "Vaart", "given": "A. W. van der"})
            ),
            "vandervaart",
        )
        self.assertEqual(
            check_bib.normalize_name(
                check_bib.crossref_family({"family": "III", "given": "James Pickands"})
            ),
            "pickands",
        )
        self.assertEqual(
            check_bib.normalize_name(check_bib.bib_surnames("O'Brien, George L.")[0]),
            check_bib.normalize_name(check_bib.crossref_family({"family": "O'brien", "given": "G. L."})),
        )
        self.assertEqual(check_bib.normalize_name("Jørgensen"), "jorgensen")
        self.assertEqual(
            check_bib.normalize_name(check_bib.bib_surnames(r"J{\o}rgensen, Bent")[0]),
            "jorgensen",
        )
        self.assertEqual(
            check_bib.normalize_name(
                check_bib.crossref_family({"family": "Lê", "given": "Hông Vân"})
            ),
            "le",
        )
        record = check_bib.CrossrefRecord(
            doi="10.1000/example",
            title="Heavy-Tail Phenomena",
            subtitle="Probabilistic and Statistical Modeling",
            year="2007",
            authors=["Resnick"],
            publisher="Springer New York",
        )
        text = """
@book{resnick2007heavy,
  author = {Resnick, Sidney I.},
  title = {Heavy-Tail Phenomena: Probabilistic and Statistical Modeling},
  year = {2007},
  publisher = {Springer},
  doi = {10.1000/example},
}
"""
        found = issues_for(text, records={"10.1000/example": record})
        self.assertEqual(levels(found, "error"), [])

    def test_crossref_disagreements(self):
        record = check_bib.CrossrefRecord(
            doi="10.1000/year",
            title="Methods of Information Geometry",
            year="2007",
            authors=["Amari", "Nagaoka"],
            publisher="American Mathematical Society",
            page="393-425",
        )
        text = """
@book{amari2000methods,
  author = {Amari, Shun-ichi and Nagaoka, Hiroshi},
  title = {Methods of Information Geometry},
  publisher = {American Mathematical Society and Oxford University Press},
  year = {2000},
  doi = {10.1000/year},
  pages = {393--442},
}
"""
        found = issues_for(text, records={"10.1000/year": record})
        messages = " ".join(issue.message for issue in levels(found, "error"))
        self.assertIn("issued year 2007", messages)
        self.assertIn("pages", messages)
        self.assertNotIn("publisher", messages)

    def test_apply_inserts_only_missing_fields(self):
        text = (
            "@article{ada2020one,\n"
            "  author = {Ada, A},\n"
            "  title = {A Paper},\n"
            "  journal = {Journal},\n"
            "  year = {2020},\n"
            "  doi = {10.1000/fill},\n"
            "}\n"
        )
        record = check_bib.CrossrefRecord(
            doi="10.1000/fill",
            title="A Paper",
            year="2020",
            authors=["Ada"],
            publisher="A & B Press",
            volume="3",
            issue="1",
            page="10-20",
        )
        entries = check_bib.parse_bibliography(text)
        updated, applied = check_bib.apply_fills(text, entries, {"10.1000/fill": record})
        self.assertEqual(applied, 4)
        self.assertIn(r"publisher = {A \& B Press}", updated)
        self.assertIn("volume = {3}", updated)
        self.assertIn("number = {1}", updated)
        self.assertIn("pages = {10-20}", updated)
        self.assertIn("title = {A Paper}", updated)
        again, applied_again = check_bib.apply_fills(
            updated, check_bib.parse_bibliography(updated), {"10.1000/fill": record}
        )
        self.assertEqual(applied_again, 0)
        self.assertEqual(again, updated)

    def test_cli_on_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            bib = Path(directory) / "sample.bib"
            allow = Path(directory) / "allow.txt"
            bib.write_text(
                "@article{doe2020paper,\n"
                "  author = {Doe, Jane},\n"
                "  title = {A Paper},\n"
                "  journal = {Journal},\n"
                "  year = {2020},\n"
                "}\n",
                encoding="utf-8",
            )
            allow.write_text("", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                status = check_bib.main([str(bib), "--allowlist", str(allow)])
            self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
