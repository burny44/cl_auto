"""Reference lists of people, locations and organizations (reference/*.csv).

The CSVs are meant to be edited by hand in Excel / LibreOffice:

  people.csv         nume, functie, variante, verificat, observatii, prima_aparitie
  locations.csv      nume, zona, tip, variante, verificat, observatii, prima_aparitie
  organizations.csv  nume, tip, relatie, variante, verificat, observatii, prima_aparitie

Only rows with verificat = "da" are given to the model as ground truth. After
each meeting the model proposes NEW entries it saw; they are appended with
verificat = "nu" so they can be reviewed (corrected, then switched to "da", or
deleted) before the next meeting uses them.

"variante" holds alternative spellings / mis-hearings separated by ";".
"""

from __future__ import annotations

import csv
import io
import json
import os
import unicodedata
from dataclasses import dataclass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFERENCE_DIR = os.path.join(PROJECT_ROOT, "reference")
ZONES = ("Slănic", "Prăjani", "Groșani")


@dataclass(frozen=True)
class Table:
    key: str            # JSON key used by the model ("people", ...)
    filename: str
    fields: tuple[str, ...]
    label: str          # label in emails / logs (Romanian)
    extra: tuple[str, ...]  # descriptive columns copied from the model's answer

    @property
    def path(self) -> str:
        return os.path.join(REFERENCE_DIR, self.filename)


_COMMON_TAIL = ("variante", "verificat", "observatii", "prima_aparitie")

TABLES = (
    Table("people", "people.csv", ("nume", "functie") + _COMMON_TAIL,
          "Persoane", ("functie",)),
    Table("locations", "locations.csv", ("nume", "zona", "tip") + _COMMON_TAIL,
          "Locații", ("zona", "tip")),
    Table("organizations", "organizations.csv", ("nume", "tip", "relatie") + _COMMON_TAIL,
          "Organizații", ("tip", "relatie")),
)


# ---------------------------------------------------------------- CSV I/O

def _read_text(path: str) -> str:
    with open(path, "rb") as fh:
        raw = fh.read()
    # Excel's plain "CSV" export uses the Windows code page, not UTF-8.
    for enc in ("utf-8-sig", "cp1250"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _delimiter(text: str) -> str:
    # Excel with Romanian regional settings saves with ";".
    header = text.splitlines()[0] if text else ""
    return ";" if header.count(";") > header.count(",") else ","


def load(path: str) -> tuple[list[dict], str]:
    """Return (rows, delimiter). Missing file -> ([], ",")."""

    if not os.path.isfile(path):
        return [], ","
    text = _read_text(path)
    delim = _delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    rows = []
    for row in reader:
        clean = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if clean.get("nume"):
            rows.append(clean)
    return rows, delim


def save(path: str, rows: list[dict], fields, delimiter: str = ",") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fields), delimiter=delimiter,
                                extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({f: row.get(f, "") for f in fields})


def _verified(row: dict) -> bool:
    return row.get("verificat", "").strip().lower() in ("da", "yes", "y", "1", "x")


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.lower().replace("-", " ").split())


def _known_keys(rows: list[dict]) -> set[str]:
    keys = set()
    for row in rows:
        keys.add(_norm(row["nume"]))
        for variant in row.get("variante", "").split(";"):
            if variant.strip():
                keys.add(_norm(variant))
    return keys


# ---------------------------------------------------------------- prompt text

def _entry_line(row: dict, descriptive: tuple[str, ...]) -> str:
    desc = ", ".join(row[f] for f in descriptive if row.get(f))
    line = f"- {row['nume']}"
    if desc:
        line += f" — {desc}"
    if row.get("variante"):
        line += f" (variante auzite: {row['variante']})"
    if row.get("observatii"):
        line += f" [{row['observatii']}]"
    return line


def reference_text() -> str:
    """The verified entries of every table, formatted for inclusion in a prompt."""

    people, locations, orgs = (
        [r for r in load(t.path)[0] if _verified(r)] for t in TABLES
    )

    out = [
        "Reference lists for Slănic (Prahova). Use them ONLY to correct names of",
        "people, places and organizations that appear incomplete, misspelled or",
        "garbled in the speech-to-text transcript. When a name is a close phonetic",
        "or partial match to an entry (or to one of its known variants), replace it",
        "with the entry's correct name. Never invent names that are not in the",
        "transcript.",
        "",
        "PEOPLE (name — role):",
    ]
    out += [_entry_line(p, ("functie",)) for p in people]

    out += ["", "LOCATIONS, grouped by area (Slănic town, Prăjani village, Groșani village):"]
    groups: dict[str, list[dict]] = {}
    for loc in locations:
        groups.setdefault(loc.get("zona") or "zonă necunoscută", []).append(loc)
    order = [z for z in ZONES if z in groups] + [z for z in groups if z not in ZONES]
    for zone in order:
        out.append(f"{zone}:")
        out += [_entry_line(l, ("tip",)) for l in groups[zone]]

    out += ["", "ORGANIZATIONS (companies, public institutions, operators — name — type, "
                "relation to the town):"]
    out += [_entry_line(o, ("tip", "relatie")) for o in orgs]
    return "\n".join(out)


def known_names_json() -> str:
    """Every name already in the CSVs (verified or not), for dedup hints."""

    return json.dumps(
        {t.key: [r["nume"] for r in load(t.path)[0]] for t in TABLES},
        ensure_ascii=False,
    )


# ---------------------------------------------------------------- learning

def add_candidates(candidates: dict, meeting_date: str) -> dict[str, list[str]]:
    """Append new entries (verificat = "nu"). Returns {table key: added names}."""

    added: dict[str, list[str]] = {}
    for table in TABLES:
        rows, delim = load(table.path)
        keys = _known_keys(rows)
        new: list[str] = []
        for item in candidates.get(table.key) or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("nume", "")).strip()
            if not name or _norm(name) in keys:
                continue
            keys.add(_norm(name))
            heard = str(item.get("varianta_auzita", "")).strip()
            row = {f: str(item.get(f, "")).strip() for f in table.extra}
            if "zona" in row:
                row["zona"] = next((z for z in ZONES if _norm(z) == _norm(row["zona"])), "")
            row.update({
                "nume": name,
                "variante": heard if heard and _norm(heard) != _norm(name) else "",
                "verificat": "nu",
                "observatii": str(item.get("context", "")).strip(),
                "prima_aparitie": meeting_date,
            })
            rows.append(row)
            new.append(name)
        if new:
            save(table.path, rows, table.fields, delim)
        added[table.key] = new
    return added


def labels() -> dict[str, tuple[str, str]]:
    """{table key: (Romanian label, filename)} for emails / logs."""

    return {t.key: (t.label, t.filename) for t in TABLES}
