#!/usr/bin/env python3
"""Check a shared BibTeX bibliography.

Errors (nonzero exit): duplicate keys, duplicate DOIs, near-identical
normalized title+year, citation keys outside authorYEARkeyword (unless
allowlisted), and missing required fields.

Warnings stay in the report and do not fail the run. ``--fetch`` reads
Crossref into a local cache and compares DOI entries with that metadata.
``--apply`` inserts missing publisher, volume, number, and page values.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import bibtexparser
from bibtexparser.bparser import BibTexParser

ROOT = Path(__file__).resolve().parents[1]
KEY_RE = re.compile(r"^([a-z]+)(\d{4})([a-z][a-z0-9]*)$")
RAW_ENTRY_RE = re.compile(
    r"(?im)^@(?!string\b|preamble\b|comment\b)(\w+)\s*\{\s*([^,\s]+)\s*,"
)
TITLE_SIMILARITY = 0.92
JOURNAL_SIMILARITY = 0.80
USER_AGENT = "xshi19-bibliography-checker/1.0 (https://github.com/xshi19/bibliography)"
PARTICLES = {
    "van",
    "der",
    "de",
    "den",
    "von",
    "di",
    "da",
    "del",
    "della",
    "du",
    "le",
    "la",
    "st",
    "ter",
    "ten",
    "op",
    "zu",
    "zum",
    "zur",
    "dos",
    "das",
}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
FILL_FIELDS = {
    "article": ("publisher", "volume", "number", "pages"),
    "book": ("publisher",),
    "incollection": ("publisher", "volume", "pages"),
    "inproceedings": ("volume", "pages"),
    "conference": ("volume", "pages"),
}


@dataclass
class Issue:
    level: str
    key: str
    message: str

    def format(self) -> str:
        where = self.key or "bibliography"
        return f"{self.level.upper()} {where}: {self.message}"


@dataclass
class CrossrefRecord:
    doi: str
    title: str = ""
    subtitle: str = ""
    year: str = ""
    authors: list = field(default_factory=list)
    publisher: str = ""
    container_title: str = ""
    volume: str = ""
    issue: str = ""
    page: str = ""
    type: str = ""
    fetched_at: str = ""

    def author_names(self) -> list[str]:
        names: list[str] = []
        for author in self.authors:
            if isinstance(author, str):
                names.append(author)
            else:
                names.append(crossref_family(author))
        return [name for name in names if name]

    @property
    def full_title(self) -> str:
        title = self.title.strip()
        subtitle = self.subtitle.strip()
        if subtitle and subtitle.lower() not in title.lower():
            return f"{title}: {subtitle}"
        return title


def load_allowlist(path: Path) -> set[str]:
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            keys.add(line)
    return keys


def parse_bibliography(text: str) -> list[dict]:
    parser = BibTexParser(
        common_strings=False,
        ignore_nonstandard_types=False,
        interpolate_strings=False,
        customization=None,
    )
    library = bibtexparser.loads(text, parser=parser)
    return library.entries


_EXTRA_LATIN = str.maketrans(
    {
        "ø": "o",
        "Ø": "o",
        "æ": "ae",
        "Æ": "ae",
        "œ": "oe",
        "Œ": "oe",
        "ł": "l",
        "Ł": "l",
        "đ": "d",
        "Đ": "d",
        "ð": "d",
        "Ð": "d",
        "þ": "th",
        "Þ": "th",
        "ß": "ss",
        "ı": "i",
        "ŋ": "n",
    }
)


def latex_to_text(value: str) -> str:
    text = html.unescape(value or "")
    text = text.replace("\\&", " and ")
    text = text.replace("&", " and ")
    text = text.replace("\\%", " ")
    text = text.replace("\\_", " ")
    text = text.replace("~", " ")
    text = text.replace("---", " ")
    text = text.replace("--", " ")
    for command, replacement in (
        (r"\oe", "oe"),
        (r"\OE", "OE"),
        (r"\ae", "ae"),
        (r"\AE", "AE"),
        (r"\ss", "ss"),
        (r"\o", "o"),
        (r"\O", "O"),
        (r"\l", "l"),
        (r"\L", "L"),
        (r"\i", "i"),
        (r"\j", "j"),
        (r"\textasciicircum", " "),
    ):
        text = text.replace(command, replacement)
    text = re.sub(r"\\['\"`^~=.uvHckbdr]\s*\{?\\?([A-Za-z])\}?", r"\1", text)
    text = re.sub(r"\\[a-zA-Z]+\*?", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    return text


def fold_latin(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return text.translate(_EXTRA_LATIN)


def normalize_text(value: str) -> str:
    text = fold_latin(latex_to_text(value)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def normalize_name(value: str) -> str:
    text = fold_latin(latex_to_text(value)).lower()
    return re.sub(r"[^a-z]", "", text)


def normalize_doi(value: str) -> str:
    doi = (value or "").strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.I)
    return doi.lower()


def normalize_pages(value: str) -> str:
    pages = (value or "").strip().lower()
    pages = pages.replace("–", "-").replace("—", "-")
    pages = pages.replace("--", "-")
    return re.sub(r"\s+", "", pages)


def entry_field(entry: dict, name: str) -> str:
    return str(entry.get(name, "") or "").strip()


def split_authors(value: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    index = 0
    while index < len(value):
        char = value[index]
        if char == "{":
            depth += 1
            buffer.append(char)
            index += 1
            continue
        if char == "}":
            depth = max(0, depth - 1)
            buffer.append(char)
            index += 1
            continue
        if depth == 0 and value[index : index + 5].lower() == " and ":
            parts.append("".join(buffer))
            buffer = []
            index += 5
            continue
        buffer.append(char)
        index += 1
    parts.append("".join(buffer))
    return [part.strip() for part in parts if part.strip()]


def strip_outer_braces(value: str) -> str:
    text = value.strip()
    while len(text) >= 2 and text[0] == "{" and text[-1] == "}":
        inner = text[1:-1]
        depth = 0
        balanced = True
        for char in inner:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth < 0:
                    balanced = False
                    break
        if not balanced or depth != 0:
            break
        text = inner.strip()
    return text


def bib_surnames(author_field: str) -> list[str]:
    surnames: list[str] = []
    for part in split_authors(author_field):
        name = strip_outer_braces(part)
        if "," in name:
            surname = name.split(",", 1)[0].strip()
        else:
            tokens = name.split()
            while tokens and normalize_name(tokens[-1]) in SUFFIXES:
                tokens.pop()
            surname = tokens[-1] if tokens else name
        if surname:
            surnames.append(surname)
    return surnames


def _token_kind(token: str) -> str:
    return normalize_name(token)


def is_particle_token(token: str) -> bool:
    letters = "".join(char for char in token if char.isalpha())
    # "van" / "der" stay lowercase in Crossref given names. "Vân" does not.
    if not letters or letters != letters.lower():
        return False
    return _token_kind(token) in PARTICLES


def crossref_family(author: dict) -> str:
    family = str(author.get("family") or author.get("name") or "").strip()
    given = str(author.get("given") or "").strip()
    particle = str(author.get("particle") or "").strip()
    if particle and family:
        family = f"{particle} {family}"
    if _token_kind(family) in SUFFIXES and given:
        tokens = given.split()
        while tokens and (_token_kind(tokens[-1]) in SUFFIXES or is_particle_token(tokens[-1])):
            tokens.pop()
        family = tokens[-1] if tokens else family
        given = " ".join(tokens[:-1]) if tokens else ""
    given_tokens = given.split()
    prefix: list[str] = []
    while given_tokens and is_particle_token(given_tokens[-1]):
        prefix.insert(0, given_tokens.pop())
    if prefix:
        family = " ".join(prefix + [family])
    return family.strip()


def titles_near_identical(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    return difflib.SequenceMatcher(None, left, right).ratio() >= TITLE_SIMILARITY


def titles_compatible(left: str, right: str) -> bool:
    if titles_near_identical(left, right):
        return True
    if not left or not right:
        return False
    return left.startswith(right) or right.startswith(left)


def token_jaccard(left: str, right: str) -> float:
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def publishers_compatible(left: str, right: str) -> bool:
    normalized_left = normalize_text(left)
    normalized_right = normalize_text(right)
    if not normalized_left or not normalized_right:
        return True
    if (
        normalized_left == normalized_right
        or normalized_left in normalized_right
        or normalized_right in normalized_left
    ):
        return True
    left_tokens = normalized_left.split()
    right_tokens = normalized_right.split()
    return bool(left_tokens and right_tokens and left_tokens[0] == right_tokens[0] and len(left_tokens[0]) >= 5)


def missing_required(entry: dict) -> list[str]:
    entry_type = entry_field(entry, "ENTRYTYPE").lower()

    def has(name: str) -> bool:
        return bool(entry_field(entry, name))
    missing: list[str] = []

    def require(*names: str) -> None:
        for name in names:
            if not has(name):
                missing.append(name)

    if entry_type == "article":
        require("author", "title", "journal", "year")
    elif entry_type == "book":
        if not has("author") and not has("editor"):
            missing.append("author or editor")
        require("title", "year")
        if not has("publisher") and not has("url") and not has("doi"):
            missing.append("publisher, url, or doi")
    elif entry_type == "booklet":
        require("title")
    elif entry_type == "inbook":
        if not has("author") and not has("editor"):
            missing.append("author or editor")
        require("title", "publisher", "year")
        if not has("chapter") and not has("pages"):
            missing.append("chapter or pages")
    elif entry_type == "incollection":
        require("author", "title", "booktitle", "publisher", "year")
    elif entry_type in {"inproceedings", "conference"}:
        require("author", "title", "booktitle", "year")
    elif entry_type == "manual":
        require("title")
    elif entry_type in {"mastersthesis", "phdthesis"}:
        require("author", "title", "school", "year")
    elif entry_type in {"misc", "online", "electronic"}:
        require("author", "title")
        if not has("url") and not has("howpublished"):
            missing.append("url or howpublished")
    elif entry_type == "proceedings":
        require("title", "year")
    elif entry_type == "techreport":
        require("author", "title", "institution", "year")
    elif entry_type == "unpublished":
        require("author", "title", "note")
    else:
        missing.append(f"unsupported entry type @{entry_type}")
    return missing


def check_entries(entries: list[dict], raw_keys: list[str], allowlist: set[str]) -> list[Issue]:
    issues: list[Issue] = []
    if len(raw_keys) != len(entries):
        issues.append(
            Issue(
                "error",
                "",
                f"parsed {len(entries)} entries from {len(raw_keys)} citation keys",
            )
        )

    seen: dict[str, int] = defaultdict(int)
    for key in raw_keys:
        seen[key] += 1
    for key, count in sorted(seen.items()):
        if count > 1:
            issues.append(Issue("error", key, f"duplicate key ({count} entries)"))

    for entry in entries:
        key = entry_field(entry, "ID")
        year = entry_field(entry, "year")
        if key not in allowlist:
            match = KEY_RE.fullmatch(key)
            if not match:
                issues.append(
                    Issue(
                        "error",
                        key,
                        "key does not match authorYEARkeyword (lowercase); "
                        "legacy exceptions belong in scripts/key_allowlist.txt",
                    )
                )
            elif not year:
                issues.append(Issue("error", key, f"key embeds year {match.group(2)} but year is empty"))
            elif match.group(2) != year:
                issues.append(
                    Issue(
                        "error",
                        key,
                        f"key year {match.group(2)} does not match year field {year}",
                    )
                )
        for name in missing_required(entry):
            issues.append(Issue("error", key, f"missing {name}"))
        entry_type = entry_field(entry, "ENTRYTYPE").lower()
        if entry_type == "book" and not entry_field(entry, "publisher") and (
            entry_field(entry, "url") or entry_field(entry, "doi")
        ):
            issues.append(
                Issue(
                    "warning",
                    key,
                    "@book has no publisher; a url or doi stands in for a preprint or manuscript",
                )
            )
        if entry_type in {"misc", "online", "electronic"} and not year:
            issues.append(Issue("warning", key, f"@{entry_type} entry has no year"))

    doi_groups: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        doi = normalize_doi(entry_field(entry, "doi"))
        if doi:
            doi_groups[doi].append(entry_field(entry, "ID"))
    for doi, keys in sorted(doi_groups.items()):
        if len(keys) > 1:
            issues.append(Issue("error", keys[0], f"duplicate DOI {doi} shared with {', '.join(keys[1:])}"))

    titled: list[tuple[str, str, str]] = []
    by_title: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for entry in entries:
        key = entry_field(entry, "ID")
        title = normalize_text(entry_field(entry, "title"))
        year = entry_field(entry, "year")
        if title:
            titled.append((key, title, year))
            by_title[title].append((key, year))
    reported: set[tuple[str, str]] = set()
    for index, (left_key, left_title, left_year) in enumerate(titled):
        for right_key, right_title, right_year in titled[index + 1 :]:
            if not left_year or left_year != right_year:
                continue
            if not titles_near_identical(left_title, right_title):
                continue
            pair = tuple(sorted((left_key, right_key)))
            if pair in reported:
                continue
            reported.add(pair)
            issues.append(
                Issue(
                    "error",
                    left_key,
                    f"near-duplicate of {right_key} (normalized title and year {left_year})",
                )
            )
    for title, group in sorted(by_title.items()):
        if len(group) < 2:
            continue
        years = {year for _, year in group}
        if len(years) < 2 and "" not in years:
            continue
        detail = ", ".join(f"{key} ({year or 'year missing'})" for key, year in group)
        issues.append(Issue("warning", group[0][0], f"same normalized title in {detail}"))
    return issues


def record_from_message(doi: str, message: dict, fetched_at: str) -> CrossrefRecord:
    titles = message.get("title") or []
    subtitles = message.get("subtitle") or []
    issued = (message.get("issued") or {}).get("date-parts") or []
    year = ""
    if issued and issued[0] and issued[0][0]:
        year = str(issued[0][0])
    container = message.get("container-title") or []
    authors = []
    for author in message.get("author") or []:
        raw = {
            "family": str(author.get("family") or ""),
            "given": str(author.get("given") or ""),
            "name": str(author.get("name") or ""),
            "particle": str(author.get("particle") or ""),
        }
        if any(raw.values()):
            authors.append(raw)
    return CrossrefRecord(
        doi=doi,
        title=str(titles[0]).strip() if titles else "",
        subtitle=str(subtitles[0]).strip() if subtitles else "",
        year=year,
        authors=authors,
        publisher=str(message.get("publisher") or "").strip(),
        container_title=str(container[0]).strip() if container else "",
        volume=str(message.get("volume") or "").strip(),
        issue=str(message.get("issue") or "").strip(),
        page=str(message.get("page") or "").strip(),
        type=str(message.get("type") or "").strip(),
        fetched_at=fetched_at,
    )


def load_cache(path: Path) -> dict[str, CrossrefRecord]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records", payload)
    loaded: dict[str, CrossrefRecord] = {}
    for doi, raw in records.items():
        if doi.startswith("_"):
            continue
        loaded[normalize_doi(doi)] = CrossrefRecord(**raw)
    return loaded


def save_cache(path: Path, records: dict[str, CrossrefRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "records": {doi: asdict(record) for doi, record in sorted(records.items())},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def fetch_message(doi: str, timeout: float = 30.0) -> dict:
    url = "https://api.crossref.org/works/" + urllib.parse.quote(doi)
    agent = USER_AGENT
    mailto = os.environ.get("CROSSREF_MAILTO", "").strip()
    if mailto:
        agent = f"{USER_AGENT}; mailto:{mailto}"
    request = urllib.request.Request(url, headers={"User-Agent": agent})
    delay = 1.0
    last_error: Exception | None = None
    for _ in range(4):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            return payload["message"]
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code not in {429, 500, 502, 503, 504}:
                raise
        except urllib.error.URLError as error:
            last_error = error
        time.sleep(delay)
        delay *= 2
    assert last_error is not None
    raise last_error


def ensure_crossref(
    entries: list[dict],
    cache_path: Path,
    refresh: bool = False,
) -> tuple[dict[str, CrossrefRecord], list[Issue]]:
    records = {} if refresh else load_cache(cache_path)
    issues: list[Issue] = []
    fetched = False
    seen: set[str] = set()
    for entry in entries:
        doi = normalize_doi(entry_field(entry, "doi"))
        if not doi or doi in seen:
            continue
        seen.add(doi)
        if doi in records:
            continue
        try:
            message = fetch_message(doi)
        except Exception as error:  # noqa: BLE001 — report and continue with the rest.
            issues.append(Issue("error", entry_field(entry, "ID"), f"Crossref fetch failed for {doi}: {error}"))
            continue
        records[doi] = record_from_message(
            doi,
            message,
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        fetched = True
        time.sleep(0.2)
    if fetched or refresh:
        save_cache(cache_path, records)
    return records, issues


def compare_crossref(entries: list[dict], records: dict[str, CrossrefRecord]) -> list[Issue]:
    issues: list[Issue] = []
    for entry in entries:
        doi = normalize_doi(entry_field(entry, "doi"))
        if not doi:
            continue
        record = records.get(doi)
        if record is None:
            issues.append(Issue("error", entry_field(entry, "ID"), f"no Crossref cache entry for {doi}"))
            continue
        key = entry_field(entry, "ID")
        local_title = normalize_text(entry_field(entry, "title"))
        remote_title = normalize_text(record.full_title)
        if local_title and remote_title and not titles_compatible(local_title, remote_title):
            issues.append(
                Issue(
                    "error",
                    key,
                    f"title differs from Crossref {record.full_title!r}",
                )
            )
        if record.year and entry_field(entry, "year") and entry_field(entry, "year") != record.year:
            issues.append(
                Issue(
                    "error",
                    key,
                    f"year {entry_field(entry, 'year')} differs from Crossref issued year {record.year}",
                )
            )
        local_authors = {normalize_name(name) for name in bib_surnames(entry_field(entry, "author"))}
        remote_names = record.author_names()
        remote_authors = {normalize_name(name) for name in remote_names}
        local_authors.discard("")
        remote_authors.discard("")
        if local_authors and remote_authors and local_authors != remote_authors:
            issues.append(
                Issue(
                    "error",
                    key,
                    "authors differ from Crossref " + ", ".join(remote_names),
                )
            )
        entry_type = entry_field(entry, "ENTRYTYPE").lower()
        if entry_type == "article" and entry_field(entry, "journal") and record.container_title:
            local_journal = normalize_text(entry_field(entry, "journal"))
            remote_journal = normalize_text(record.container_title)
            if local_journal != remote_journal:
                similarity = max(
                    difflib.SequenceMatcher(None, local_journal, remote_journal).ratio(),
                    token_jaccard(local_journal, remote_journal),
                )
                if similarity < JOURNAL_SIMILARITY:
                    issues.append(
                        Issue(
                            "error",
                            key,
                            f"journal {entry_field(entry, 'journal')!r} differs from Crossref "
                            f"{record.container_title!r}",
                        )
                    )
                else:
                    issues.append(
                        Issue(
                            "warning",
                            key,
                            f"journal {entry_field(entry, 'journal')!r} is close to Crossref "
                            f"{record.container_title!r}",
                        )
                    )
        if entry_field(entry, "publisher") and record.publisher and not publishers_compatible(
            entry_field(entry, "publisher"), record.publisher
        ):
            issues.append(
                Issue(
                    "error",
                    key,
                    f"publisher {entry_field(entry, 'publisher')!r} differs from Crossref {record.publisher!r}",
                )
            )
        for local_name, remote_value, label in (
            ("volume", record.volume, "volume"),
            ("number", record.issue, "number"),
            ("pages", record.page, "pages"),
        ):
            local_value = entry_field(entry, local_name)
            if not local_value or not remote_value:
                continue
            if local_name == "pages":
                same = normalize_pages(local_value) == normalize_pages(remote_value)
            else:
                same = local_value == remote_value
            if not same:
                issues.append(
                    Issue(
                        "error",
                        key,
                        f"{label} {local_value} differs from Crossref {remote_value}",
                    )
                )
        fillable = FILL_FIELDS.get(entry_type, ())
        suggestions = {
            "publisher": record.publisher,
            "volume": record.volume,
            "number": record.issue,
            "pages": record.page,
        }
        for name in fillable:
            if not entry_field(entry, name) and suggestions[name]:
                issues.append(
                    Issue(
                        "info",
                        key,
                        f"Crossref can fill {name}: {suggestions[name]}",
                    )
                )
    return issues


def fills_for_entry(entry: dict, record: CrossrefRecord) -> dict[str, str]:
    entry_type = entry_field(entry, "ENTRYTYPE").lower()
    suggestions = {
        "publisher": record.publisher,
        "volume": record.volume,
        "number": record.issue,
        "pages": record.page,
    }
    return {
        name: suggestions[name]
        for name in FILL_FIELDS.get(entry_type, ())
        if not entry_field(entry, name) and suggestions[name]
    }


def entry_end(text: str, key: str) -> int | None:
    match = re.search(rf"(?m)^@\w+\s*\{{\s*{re.escape(key)}\s*,", text)
    if not match:
        return None
    start = text.find("{", match.start())
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def bibtex_value(value: str) -> str:
    value = value.replace("\\", "").replace("{", "(").replace("}", ")")
    for char in "&%$#_":
        value = value.replace(char, "\\" + char)
    return value


def apply_fills(text: str, entries: list[dict], records: dict[str, CrossrefRecord]) -> tuple[str, int]:
    insertions: list[tuple[int, str]] = []
    applied = 0
    for entry in entries:
        doi = normalize_doi(entry_field(entry, "doi"))
        record = records.get(doi)
        if record is None:
            continue
        fills = fills_for_entry(entry, record)
        if not fills:
            continue
        end = entry_end(text, entry_field(entry, "ID"))
        if end is None:
            continue
        if end > 0 and text[end - 1] != "\n":
            fills_block_prefix = "\n"
        else:
            fills_block_prefix = ""
        block = fills_block_prefix + "".join(
            f"  {name} = {{{bibtex_value(value)}}},\n" for name, value in fills.items()
        )
        insertions.append((end, block))
        applied += len(fills)
    for end, block in sorted(insertions, key=lambda item: item[0], reverse=True):
        text = text[:end] + block + text[end:]
    return text, applied


def check_text(
    text: str,
    allowlist: set[str],
    records: dict[str, CrossrefRecord] | None = None,
) -> tuple[list[dict], list[Issue]]:
    raw_keys = [match.group(2) for match in RAW_ENTRY_RE.finditer(text)]
    entries = parse_bibliography(text)
    issues = check_entries(entries, raw_keys, allowlist)
    if records is not None:
        issues.extend(compare_crossref(entries, records))
    return entries, issues


def print_report(path: Path, entry_count: int, issues: list[Issue]) -> int:
    rank = {"error": 0, "warning": 1, "info": 2}
    ordered = sorted(issues, key=lambda issue: (rank.get(issue.level, 9), issue.key, issue.message))
    for issue in ordered:
        print(issue.format())
    errors = sum(issue.level == "error" for issue in issues)
    warnings = sum(issue.level == "warning" for issue in issues)
    infos = sum(issue.level == "info" for issue in issues)
    print(f"{path}: {entry_count} entries, {errors} errors, {warnings} warnings, {infos} notes")
    return 1 if errors else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check bibliography.bib")
    parser.add_argument(
        "bib",
        nargs="?",
        type=Path,
        default=ROOT / "bibliography.bib",
        help="BibTeX file (default: bibliography.bib)",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=ROOT / "scripts" / "key_allowlist.txt",
        help="Legacy keys exempt from authorYEARkeyword",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="Fill the Crossref cache and compare DOI metadata",
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=ROOT / "cache" / "crossref.json",
        help="Crossref cache JSON (default: cache/crossref.json)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="With --fetch, ignore existing cache entries and download again",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="With --fetch, insert missing publisher, volume, number, and pages",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply and not args.fetch:
        print("ERROR bibliography: --apply requires --fetch", file=sys.stderr)
        return 2
    if not args.bib.is_file():
        print(f"ERROR bibliography: file not found: {args.bib}", file=sys.stderr)
        return 2
    if not args.allowlist.is_file():
        print(f"ERROR bibliography: allowlist not found: {args.allowlist}", file=sys.stderr)
        return 2
    text = args.bib.read_text(encoding="utf-8")
    allowlist = load_allowlist(args.allowlist)
    records: dict[str, CrossrefRecord] | None = None
    fetch_issues: list[Issue] = []
    if args.fetch:
        entries = parse_bibliography(text)
        records, fetch_issues = ensure_crossref(entries, args.cache, refresh=args.refresh)
        if args.apply:
            updated, applied = apply_fills(text, entries, records)
            if applied:
                args.bib.write_text(updated, encoding="utf-8")
                text = updated
                print(f"INFO bibliography: inserted {applied} missing fields into {args.bib}")
    entries, issues = check_text(text, allowlist, records)
    issues.extend(fetch_issues)
    return print_report(args.bib, len(entries), issues)


if __name__ == "__main__":
    sys.exit(main())
