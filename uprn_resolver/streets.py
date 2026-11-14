"""Open street names for UPRNs: UPRN -> USRN -> street name (all OGL).

Chain (verified against the 2026-09 OS releases, see ``stage_streets``):

1. OS Open Linked Identifiers ``BLPU_UPRN_Street_USRN_11`` links UPRN -> USRN (42.4M rows, GB).
   https://docs.os.uk/os-downloads/identifiers/os-open-linked-identifiers
2. OS Open Linked Identifiers ``Road_TOID_Street_USRN_10`` links Road TOID -> USRN.
3. OS Open Names ``Named Road`` entries use the Road TOID as their ``ID`` (``osgb4000000...``), so they
   join directly to (2) and carry NAME1/NAME2 (+ languages: cym/gla), POPULATED_PLACE, DISTRICT_BOROUGH.
   https://docs.os.uk/os-downloads/gazetteers/os-open-names
   (``Section Of Named Road`` IDs are not TOIDs and are ignored.)

Open USRN carries no names and Open Roads names are keyed by RoadLink TOID (an extra hop with lower
coverage), so Open Names is the chosen source.

National Parquet (``data/parquet/streets/``), written by ``convert_streets``:
  ``uprn_usrn.parquet``  uprn BIGINT, usrn BIGINT
  ``road_usrn.parquet``  toid VARCHAR, usrn BIGINT
  ``open_names.parquet`` toid, name1, name1_lang, name2, name2_lang, populated_place, district_borough,
                          county_unitary, postcode_district (VARCHAR) -- Named Road rows only
Staged: ``staged_dir/<lad>/streets.parquet`` (see ``stage_streets``).

Downloads are untrusted: streamed, md5-checked, unzipped with a path-traversal guard, converted, row
counts verified, then the extracted files and zip are deleted. Free disk must stay >= 15 GB.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb

from uprn_resolver import download as dl
from uprn_resolver.convert import _append_manifest, _connect, _copy, _csv_rel, _free_gb, _q

MIN_FREE_GB = 15
STREETS = "streets"
OS_API = dl.OS_API

# dataset -> (OS product, file-name regex, parquet file name)
SOURCES = {
    "uprn_usrn": ("LIDS", r"BLPU-UPRN-Street-USRN", "uprn_usrn.parquet"),
    "road_usrn": ("LIDS", r"Road-TOID-Street-USRN", "road_usrn.parquet"),
    "open_names": ("OpenNames", r"opname_csv_gb", "open_names.parquet"),
}
NAMES_COLS = [
    "ID", "NAMES_URI", "NAME1", "NAME1_LANG", "NAME2", "NAME2_LANG", "TYPE", "LOCAL_TYPE", "GEOMETRY_X",
    "GEOMETRY_Y", "MOST_DETAIL_VIEW_RES", "LEAST_DETAIL_VIEW_RES", "MBR_XMIN", "MBR_YMIN", "MBR_XMAX",
    "MBR_YMAX", "POSTCODE_DISTRICT", "POSTCODE_DISTRICT_URI", "POPULATED_PLACE", "POPULATED_PLACE_URI",
    "POPULATED_PLACE_TYPE", "DISTRICT_BOROUGH", "DISTRICT_BOROUGH_URI", "DISTRICT_BOROUGH_TYPE",
    "COUNTY_UNITARY", "COUNTY_UNITARY_URI", "COUNTY_UNITARY_TYPE", "REGION", "REGION_URI", "COUNTRY",
    "COUNTRY_URI", "RELATED_SPATIAL_OBJECT", "SAME_AS_DBPEDIA", "SAME_AS_GEONAMES",
]  # fmt: skip


# ---------------------------------------------------------------- conversion
def _lids_link(con, src: Path, out: Path, id1_type: str, id1_name: str, id2_name: str) -> int:
    header = con.execute(f"SELECT * FROM read_csv({_q(src)}, header=true, all_varchar=true, sample_size=1)")
    cols = [d[0].lstrip("﻿") for d in header.description]
    if not {"IDENTIFIER_1", "IDENTIFIER_2"} <= {c.upper() for c in cols}:
        raise ValueError(f"unexpected LIDS header: {cols}")
    types = {c: "VARCHAR" for c in cols}
    types["IDENTIFIER_1"] = id1_type
    types["IDENTIFIER_2"] = "BIGINT"
    sql = f"""
        SELECT IDENTIFIER_1 AS {id1_name}, IDENTIFIER_2 AS {id2_name}
        FROM {_csv_rel(src, types)}
        WHERE IDENTIFIER_1 IS NOT NULL AND IDENTIFIER_2 IS NOT NULL
        ORDER BY 1, 2"""
    return _copy(con, sql, out)


def _open_names(con, src: Path, out: Path) -> int:
    """src: extracted Open Names directory (``Data/*.csv`` headerless; header in ``Doc/``)."""
    files = sorted(src.rglob("*.csv"))
    files = [f for f in files if "header" not in f.name.lower()]
    if not files:
        raise FileNotFoundError(f"no Open Names CSVs under {src}")
    spec = "{" + ", ".join(f"'{c}': 'VARCHAR'" for c in NAMES_COLS + ["X1", "X2"]) + "}"
    flist = "[" + ", ".join(_q(f) for f in files) + "]"
    # Some rows carry an unquoted comma in SAME_AS_DBPEDIA (35 fields): pad/extend, ignore the extras.
    rel = (
        f"read_csv({flist}, header=false, columns={spec}, auto_detect=false, delim=',', "
        "quote='\"', escape='\"', null_padding=true, strict_mode=false)"
    )
    cols = ["NAME1", "NAME1_LANG", "NAME2", "NAME2_LANG", "POPULATED_PLACE", "DISTRICT_BOROUGH",
            "COUNTY_UNITARY", "POSTCODE_DISTRICT"]  # fmt: skip
    sel = ", ".join(f"NULLIF(trim({c}), '') AS {c.lower()}" for c in cols)
    sql = f"""
        SELECT trim(ID) AS toid, {sel} FROM {rel}
        WHERE LOCAL_TYPE = 'Named Road' AND ID LIKE 'osgb%' AND NULLIF(trim(NAME1), '') IS NOT NULL
        ORDER BY toid"""
    return _copy(con, sql, out)


def convert_streets(dataset: str, src: Path, parquet_dir: Path) -> int:
    """Convert one extracted source (``uprn_usrn``/``road_usrn`` CSV, ``open_names`` dir) to
    ``parquet_dir/streets/<name>.parquet``; record it in the manifest; return the row count."""
    if dataset not in SOURCES:
        raise ValueError(f"unknown streets dataset {dataset!r}; expected one of {sorted(SOURCES)}")
    src, parquet_dir = Path(src), Path(parquet_dir)
    if not src.exists():
        raise FileNotFoundError(src)
    parquet_dir.mkdir(parents=True, exist_ok=True)
    free = _free_gb(parquet_dir)
    if free < MIN_FREE_GB:
        raise RuntimeError(f"only {free:.1f} GB free; need at least {MIN_FREE_GB} GB. Aborting conversion.")
    out = parquet_dir / STREETS / SOURCES[dataset][2]
    con = _connect(parquet_dir)
    try:
        if dataset == "uprn_usrn":
            n = _lids_link(con, src, out, "BIGINT", "uprn", "usrn")
        elif dataset == "road_usrn":
            n = _lids_link(con, src, out, "VARCHAR", "toid", "usrn")
        else:
            n = _open_names(con, src, out)
    finally:
        con.close()
    if n <= 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"{dataset}: converted 0 rows")
    size = sum(p.stat().st_size for p in ([src] if src.is_file() else src.rglob("*")) if p.is_file())
    _append_manifest(
        parquet_dir,
        str(out.relative_to(parquet_dir)),
        {
            "dataset": f"streets/{dataset}",
            "source": src.name,
            "source_size": size,
            "rows": n,
            "parquet_size": out.stat().st_size,
            "converted_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )
    return n


# ---------------------------------------------------------------- download
def _select(dataset: str, listing: list[dict]) -> dict:
    import re

    _, pat, _ = SOURCES[dataset]
    hits = [d for d in listing if d.get("area") == "GB" and d.get("format") == "CSV" and re.search(pat, d["fileName"])]
    if len(hits) != 1:
        raise RuntimeError(f"{dataset}: expected exactly one OS download matching {pat!r}, got {len(hits)}")
    return hits[0]


def _process(dataset: str, zpath: Path, parquet_dir: Path, size: int | None, md5: str | None) -> int:
    """Extract -> convert -> verify -> delete extracted files and zip (one at a time)."""
    with dl._CONVERT_LOCK:
        free = _free_gb(zpath.parent)
        if free < MIN_FREE_GB:
            raise RuntimeError(f"only {free:.1f} GB free before extracting {zpath.name}; need {MIN_FREE_GB}")
        extra = zpath.parent / f".x_{zpath.stem}"
        if extra.exists():
            shutil.rmtree(extra)
        extra.mkdir(parents=True)
        try:
            files = dl._unzip(zpath, extra)
            if dataset == "open_names":
                src = extra
            else:
                csvs = [f for f in files if f.suffix.lower() == ".csv"]
                if len(csvs) != 1:
                    raise RuntimeError(f"{zpath.name}: expected one CSV, found {len(csvs)}")
                src = csvs[0]
            rows = convert_streets(dataset, src, parquet_dir)
        finally:
            shutil.rmtree(extra, ignore_errors=True)
        zpath.unlink()
    dl._record(parquet_dir, f"streets/{dataset}", zpath.name, size, md5, rows)
    print(f"  converted {zpath.name}: {rows} rows; source deleted")
    return rows


def download_streets(raw_dir: Path, datasets: list[str] | None = None) -> None:
    """Download + convert the street inputs into ``raw_dir.parent/parquet/streets/`` (skips converted)."""
    raw_dir = Path(raw_dir)
    parquet_dir = raw_dir.parent / "parquet"
    out = raw_dir / STREETS
    for ds in datasets or list(SOURCES):
        product = SOURCES[ds][0]
        d = _select(ds, dl._os_listing(product))
        key = f"{STREETS}/{SOURCES[ds][2]}"
        if dl._already_converted(parquet_dir, d["fileName"], d.get("md5"), key):
            print(f"  {d['fileName']}: in manifest, skipping")
            continue
        print(f"[download] streets/{ds}")
        z = dl._stream(d["url"], out / d["fileName"], d.get("size"), d.get("md5"))
        _process(ds, z, parquet_dir, d.get("size"), d.get("md5"))


# ---------------------------------------------------------------- staging
def stage_streets(lad: str, raw_dir: Path, staged_dir: Path) -> None:
    """Write ``staged_dir/<lad>/streets.parquet`` for the LAD's UPRNs (from staged ``onsud.parquet``).

    Columns: uprn BIGINT, usrn BIGINT, street_name VARCHAR, street_name_alt VARCHAR (Welsh/Gaelic),
    locality VARCHAR, street_method VARCHAR, usrn_count INT. Only UPRNs with at least one USRN link
    appear. Rules (all deterministic):

    * **Multi-USRN UPRN:** prefer a USRN that has an Open Names street name, then the smallest USRN.
      ``usrn_count`` = number of distinct USRNs linked to the UPRN.
    * **USRN -> name:** a USRN may map to several Road TOIDs; choose the name1 shared by most of them,
      ties broken by lexicographic order of (name1, toid). ``street_name`` is the English name: when
      NAME1_LANG is cym/gla and NAME2 exists, NAME2 is the street_name and NAME1 the alt; otherwise
      NAME1 is the street_name and NAME2 (if any) the alt.
    * ``locality`` = POPULATED_PLACE, else DISTRICT_BOROUGH, else COUNTY_UNITARY.
    * ``street_method`` = ``lids_usrn_open_names`` if a name was found, else ``lids_usrn``.

    Removes any previous ``streets.parquet`` first. Then a no-op (prints a note) if the staged onsud or
    national street Parquet is missing.
    """
    pq = Path(raw_dir).parent / "parquet" / STREETS
    out_dir = Path(staged_dir) / lad
    onsud = out_dir / "onsud.parquet"
    paths = {k: pq / v[2] for k, v in SOURCES.items()}
    missing = [str(p) for p in [onsud, *paths.values()] if not p.exists()]
    # a previous output must not survive a run that finds no source (or fails): build would use stale data
    (out_dir / "streets.parquet").unlink(missing_ok=True)
    (out_dir / "streets.parquet.tmp").unlink(missing_ok=True)
    if missing:
        print(f"[streets] skipping {lad}: missing {', '.join(missing)}")
        return
    tmp = Path(staged_dir).resolve().parent / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET temp_directory='{tmp}'")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"""
        CREATE TEMP TABLE links AS
        SELECT u.uprn, u.usrn FROM read_parquet({_q(paths["uprn_usrn"])}) u
        SEMI JOIN read_parquet({_q(onsud)}) o ON o.uprn = u.uprn""")
    con.execute(f"""
        CREATE TEMP TABLE cand AS
        SELECT r.usrn, n.name1, n.name1_lang, n.name2, n.name2_lang,
               n.populated_place, n.district_borough, n.county_unitary, n.toid
        FROM read_parquet({_q(paths["road_usrn"])}) r
        JOIN read_parquet({_q(paths["open_names"])}) n USING (toid)
        WHERE r.usrn IN (SELECT DISTINCT usrn FROM links)""")
    con.execute("""
        CREATE TEMP TABLE usrn_name AS
        SELECT usrn,
               CASE WHEN name1_lang IN ('cym','gla') AND name2 IS NOT NULL THEN name2 ELSE name1 END AS street_name,
               CASE WHEN name1_lang IN ('cym','gla') AND name2 IS NOT NULL THEN name1
                    WHEN name2 IS NOT NULL AND name2 <> name1 THEN name2 END AS street_name_alt,
               coalesce(populated_place, district_borough, county_unitary) AS locality
        FROM (
            SELECT *, row_number() OVER (PARTITION BY usrn ORDER BY cnt DESC, name1, toid) AS rn
            FROM (SELECT *, count(*) OVER (PARTITION BY usrn, name1) AS cnt FROM cand)
        ) WHERE rn = 1""")
    query = """
        SELECT uprn, usrn, street_name, street_name_alt, locality,
               CASE WHEN street_name IS NOT NULL THEN 'lids_usrn_open_names' ELSE 'lids_usrn' END AS street_method,
               CAST(usrn_count AS INTEGER) AS usrn_count
        FROM (
            SELECT l.uprn, l.usrn, n.street_name, n.street_name_alt, n.locality,
                   count(*) OVER (PARTITION BY l.uprn) AS usrn_count,
                   row_number() OVER (PARTITION BY l.uprn
                                      ORDER BY (n.street_name IS NULL), l.usrn) AS rn
            FROM (SELECT DISTINCT uprn, usrn FROM links) l LEFT JOIN usrn_name n USING (usrn)
        ) WHERE rn = 1
        ORDER BY uprn"""
    # usrn_count must count distinct USRNs: links are de-duplicated above, so the window count is exact.
    dest = out_dir / "streets.parquet"
    part = out_dir / "streets.parquet.tmp"
    part.unlink(missing_ok=True)
    con.execute(f"COPY ({query}) TO {_q(part)} (FORMAT PARQUET, COMPRESSION ZSTD)")
    part.replace(dest)
    con.close()
