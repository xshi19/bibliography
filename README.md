# bibliography

Shared BibTeX file for the public math notes ([xshi19/xshi-math](https://github.com/xshi19/xshi-math)) and the private quant notes. Both sites cite this file, so a work is added once, here.

`bibliography.bib` is the canonical file. `scripts/check_bib.py` checks keys, required fields, and duplicate works. GitHub Actions runs that check on every push and pull request.

## Key rules

New keys are `authorYEARkeyword`, entirely lowercase:

- `author` is the first author's family name in ASCII letters. Drop accents, apostrophes, and hyphens, and keep surname particles attached: `vandervaart`, `obrien`, `jorgensen`, `barndorff`. For an organization, use a short lowercase source name (`fred`, `scipy`, `nycdohmh`).
- `YEAR` is the four-digit `year` field.
- `keyword` is a short lowercase description starting with a letter. Words are run together (`powerlaw`, `doglicenses`, `pointforecasts`).

`taleb2020scoft` is Taleb, 2020, *Statistical Consequences of Fat Tails*.

Two keys were already cited before this rule. They stay as they are, listed in [`scripts/key_allowlist.txt`](scripts/key_allowlist.txt):

- `tongStatisticalPhysics`
- `fitzpatrickEffusion`

Add a key to that file only when an existing note already cites it and renaming would break the citation. New entries follow `authorYEARkeyword`.

## Adding an entry

1. Search `bibliography.bib` for the DOI, and for the title plus year, before adding a work.
2. Pick a key and an entry type. Put a comma after the last field.
3. Run the checker from the repository root:

```bash
python -m pip install -r requirements.txt
python scripts/check_bib.py
```

The checker exits nonzero when it finds an error. Warnings are printed and the command still exits 0.

Required fields:

| Type | Required |
| --- | --- |
| `@article` | `author`, `title`, `journal`, `year` |
| `@book` | `author` or `editor`, `title`, `year`, and `publisher` |
| `@misc` | `author`, `title`, and `url` or `howpublished` |

A `@book` preprint or author manuscript may omit `publisher` when it has a `url` or `doi`. The checker warns about that. Other standard BibTeX types (`inproceedings`, `incollection`, `phdthesis`, `techreport`, `unpublished`, and the rest of the classic set) are checked too. `@online` is treated like `@misc`.

`@book` entries that share a normalized title and differ only by year are reported as a warning. `taleb2025scoftv4` and `taleb2020scoft` are that case: the same book, a later arXiv version and the 2020 printing. The 2020 entry stays in the file for chapter-level reconciliation.

## Web pages and data

Web pages, datasets, blog posts, API docs, and posts are `@misc`, with a `url` and usually `howpublished` so the citation says what the link is (FRED series, Kaggle dataset, lecture notes, X post). `note` is the access date or the version that was read. `tongStatisticalPhysics` and `fitzpatrickEffusion` are the lecture-note pattern; `fred2026sp500` and `nycdohmh2026doglicenses` are the data-source pattern.

## Using this repo from a MyST site

Add the repository as a submodule at `bib/`:

```bash
git submodule add https://github.com/xshi19/bibliography.git bib
```

Point the MyST project at the submodule file:

```yaml
project:
  bibliography: [bib/bibliography.bib]
```

This repository is private. A site build needs a credential that can read it. The public math site still carries its own copy at `content/bibliography.bib`; this file was copied from there and is the copy to edit from now on.

## Checker

```bash
python scripts/check_bib.py
python scripts/check_bib.py --fetch
python scripts/check_bib.py --fetch --apply
```

Without `--fetch`, the checker looks for:

- duplicate citation keys
- the same DOI on more than one entry
- near-identical works: titles that match after case, accents, punctuation, and LaTeX are stripped, in the same year (exact match, or sequence similarity at least 0.92)
- keys outside `authorYEARkeyword`, except the allowlist
- a key year that differs from the `year` field
- missing required fields

`--fetch` downloads Crossref records for entries that have a DOI and stores them in `cache/crossref.json` (gitignored). Later runs reuse the cache. `--refresh` downloads again. The comparison covers title (including a Crossref subtitle), issued year, author family names, journal, publisher, volume, issue, and pages. A disagreement is an error. Fields Crossref has and the entry omits are printed as notes.

`--apply` writes those missing `publisher`, `volume`, `number`, and `pages` values into `bibliography.bib`. It leaves existing values in place. Review the diff before committing.

Set `CROSSREF_MAILTO` if you want that address included in the Crossref User-Agent. The checker runs without it.

## Copied without edits

`bibliography.bib` is the `xshi-math` file `content/bibliography.bib`, byte for byte. The checker reports the following and leaves every entry as it was:

- No duplicate keys, no shared DOIs, and no near-identical title+year pairs.
- `tongStatisticalPhysics` and `fitzpatrickEffusion` are legacy keys on the allowlist. Neither has a `year`.
- `taleb2025scoftv4` is a `@book` with an arXiv `url` and no `publisher`. Its normalized title matches `taleb2020scoft`, whose year is 2020. The file comment keeps the 2020 entry for chapter-level reconciliation.
- Crossref's issued date for `amari2000methods` (`10.1090/mmono/191`) is 2007-04-13. The entry year is 2000, which is the year in the citation key. Crossref's publisher is American Mathematical Society; the entry also names Oxford University Press.
- Crossref pages for `davison1990models` are 393-425. The entry says `393--442`. Crossref's journal string uses the later series name, Statistical Methodology; the entry uses Series B (Methodological).
- Twelve `@article` entries have no `publisher`. Crossref has one for each. Those values are notes from `--fetch`, and they are not written into the file.

`python scripts/check_bib.py` exits 0. `python scripts/check_bib.py --fetch` exits 1 because of the Amari year and the Davison pages.
