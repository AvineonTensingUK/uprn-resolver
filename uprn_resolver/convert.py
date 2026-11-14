"""Convert one extracted national source file to the zstd Parquet layout in CONTRACT.md.

``convert(dataset, src, parquet_dir) -> int`` returns the number of rows written. The caller deletes the
source afterwards. Every call appends an entry to ``parquet_dir/manifest.json`` (file-locked, so safe for
concurrent callers). Entries live under ``manifest["converted"][<parquet path relative to parquet_dir>]``.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path

import duckdb

DATASETS = ("lids", "open_uprn", "open_toid", "openmap_local", "onsud")
ROW_GROUP_SIZE = 122880
MIN_FREE_GB = 6


def _connect(parquet_dir: Path, memory_limit: str = "4GB") -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    tmp = parquet_dir.parent / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory='{tmp}'")
    con.execute(f"SET memory_limit='{memory_limit}'")
    return con


def _load_spatial(con: duckdb.DuckDBPyConnection) -> None:
    try:
        con.execute("LOAD spatial")
    except duckdb.Error:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")


def _q(path: Path | str) -> str:
    """Quote a path (or any string) as a SQL string literal."""
    return "'" + str(path).replace("'", "''") + "'"


_LAD_RE = re.compile(r"(?:[EWS]\d{8}|ni)")


def _lad(code: str) -> str:
    """Validate a LAD code (``[EWS]\\d{8}`` or ``ni``) before it is used in SQL or a path; returns it unchanged.

    Raises ValueError otherwise, so a hostile/garbled code can never be interpolated into a query.
    """
    if not isinstance(code, str) or not _LAD_RE.fullmatch(code):
        raise ValueError(f"invalid LAD code {code!r} (expected [EWS]dddddddd or 'ni')")
    return code


def _copy(con: duckdb.DuckDBPyConnection, select_sql: str, out: Path) -> int:
    """COPY a query to ``out`` atomically (tmp file + rename); return the row count written."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.unlink(missing_ok=True)
    con.execute(f"COPY ({select_sql}) TO {_q(tmp)} (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {ROW_GROUP_SIZE})")
    n = con.execute(f"SELECT count(*) FROM read_parquet({_q(tmp)})").fetchone()[0]
    os.replace(tmp, out)
    return int(n)


def _csv_header(con: duckdb.DuckDBPyConnection, path: Path) -> list[str]:
    rel = con.execute(f"SELECT * FROM read_csv({_q(path)}, header=true, all_varchar=true, sample_size=1)")
    return [d[0].lstrip("﻿") for d in rel.description]


def _pick(cols: list[str], *patterns: str, required: bool = True) -> str | None:
    """First column whose upper-cased name matches a regex pattern (patterns in priority order)."""
    for pat in patterns:
        for c in cols:
            if re.fullmatch(pat, c.strip().upper()):
                return c
    if required:
        raise ValueError(f"no column matching any of {patterns} in {cols}")
    return None


def _ident(c: str) -> str:
    return '"' + c.replace('"', '""') + '"'


def _csv_rel(path: Path | str, types: dict[str, str]) -> str:
    cols = ", ".join(f"{_ident(k)}: '{v}'" for k, v in types.items())
    return f"read_csv({_q(path)}, header=true, columns={{{cols}}}, ignore_errors=false)"


def _lids(con, src: Path, out: Path) -> int:
    cols = _csv_header(con, src)
    expected = {
        "IDENTIFIER_1", "VERSION_NUMBER_1", "VERSION_DATE_1",
        "IDENTIFIER_2", "VERSION_NUMBER_2", "VERSION_DATE_2", "CONFIDENCE",
    }  # fmt: skip
    if not expected <= {c.upper() for c in cols}:
        raise ValueError(f"unexpected LIDS header: {cols}")
    types = {c: "VARCHAR" for c in cols}
    types.update({c: "BIGINT" for c in cols if c.upper() == "IDENTIFIER_1"})
    # The BOM on the first header name is stripped by DuckDB's CSV sniffer when columns are named explicitly.
    sql = f"""
        SELECT IDENTIFIER_1 AS uprn, IDENTIFIER_2 AS toid, CONFIDENCE AS lids_confidence,
               VERSION_NUMBER_1 AS version_number_1, VERSION_DATE_1 AS version_date_1,
               VERSION_NUMBER_2 AS version_number_2, VERSION_DATE_2 AS version_date_2
        FROM {_csv_rel(src, {c: types[c] for c in cols})}"""
    return _copy(con, sql, out)


def _open_uprn(con, src: Path, out: Path) -> int:
    cols = _csv_header(con, src)
    m = {
        "uprn": _pick(cols, r"UPRN"),
        "x": _pick(cols, r"X_COORDINATE", r"X", r"EASTING"),
        "y": _pick(cols, r"Y_COORDINATE", r"Y", r"NORTHING"),
        "lat": _pick(cols, r"LATITUDE", r"LAT"),
        "lon": _pick(cols, r"LONGITUDE", r"LON", r"LNG"),
    }
    t = {c: "VARCHAR" for c in cols}
    t[m["uprn"]] = "BIGINT"
    for k in ("x", "y", "lat", "lon"):
        t[m[k]] = "DOUBLE"
    sel = ", ".join(f"{_ident(v)} AS {k}" for k, v in m.items())
    return _copy(con, f"SELECT {sel} FROM {_csv_rel(src, t)}", out)


def _open_toid(con, src: Path, out: Path, lids_path: Path) -> int:
    if not lids_path.exists():
        raise FileNotFoundError(f"Open TOID conversion needs {lids_path} (convert lids first)")
    cols = _csv_header(con, src)
    m = {
        "toid": _pick(cols, r"TOID"),
        "toid_x": _pick(cols, r"EASTING", r"X_COORDINATE", r"X"),
        "toid_y": _pick(cols, r"NORTHING", r"Y_COORDINATE", r"Y"),
    }
    t = {c: "VARCHAR" for c in cols}
    t[m["toid_x"]] = "DOUBLE"
    t[m["toid_y"]] = "DOUBLE"
    sql = f"""
        SELECT {_ident(m["toid"])} AS toid, {_ident(m["toid_x"])} AS toid_x, {_ident(m["toid_y"])} AS toid_y
        FROM {_csv_rel(src, t)} o
        WHERE {_ident(m["toid"])} IN (SELECT DISTINCT toid FROM read_parquet({_q(lids_path)}))"""
    return _copy(con, sql, out)


def _openmap_local(con, src: Path, out: Path) -> int:
    _load_spatial(con)
    if src.suffix.lower() == ".gpkg":
        rel = f"ST_Read({_q(src)}, layer='building')"
        try:
            meta = con.execute(f"SELECT layers FROM ST_Read_Meta({_q(src)})").fetchall()
            layers = {lay["name"].lower(): lay["name"] for row in meta for lay in row[0]}
        except duckdb.Error:
            layers = {}
        if "building" in layers:
            rel = f"ST_Read({_q(src)}, layer={_q(layers['building'])})"
        elif layers:
            raise ValueError(f"no Building layer in {src}: {sorted(layers)}")
    else:
        rel = f"ST_Read({_q(src)})"
    sql = f"""
        SELECT sha1(wkb) AS building_id, wkb AS geom_wkb
        FROM (SELECT ST_AsWKB(geom) AS wkb FROM {rel} WHERE geom IS NOT NULL)"""
    return _copy(con, sql, out)


def _onsud(con, src: Path, out: Path) -> int:
    files = sorted(src.rglob("*.csv")) if src.is_dir() else [src]
    if not files:
        raise FileNotFoundError(f"no CSV files under {src}")
    selects = []
    for f in files:
        cols = _csv_header(con, f)
        m = {
            "uprn": _pick(cols, r"UPRN"),
            "lad": _pick(cols, r"LAD\d\dCD", r"LAD\d{2,4}CD", r"LAD"),
            "oa": _pick(cols, r"OA\d\dCD", r"OA\d{2,4}CD", r"OA", required=False),
            "lsoa": _pick(cols, r"LSOA\d\dCD", r"LSOA\d{2,4}CD", r"LSOA", required=False),
            "msoa": _pick(cols, r"MSOA\d\dCD", r"MSOA\d{2,4}CD", r"MSOA", required=False),
            "postcode": _pick(cols, r"PCDS", r"PCD7", r"PCD8", r"PCD", r"POSTCODE", required=False),
        }
        t = {c: "VARCHAR" for c in cols}
        t[m["uprn"]] = "BIGINT"
        parts = []
        for k, v in m.items():
            if v is None:
                parts.append(f"CAST(NULL AS {'BIGINT' if k == 'uprn' else 'VARCHAR'}) AS {k}")
            elif k == "uprn":
                parts.append(f"{_ident(v)} AS uprn")
            else:
                parts.append(f"NULLIF(trim({_ident(v)}), '') AS {k}")
        selects.append(f"SELECT {', '.join(parts)} FROM {_csv_rel(f, t)}")
    return _copy(con, " UNION ALL ".join(selects), out)


def _free_gb(path: Path) -> float:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1e9


def _append_manifest(parquet_dir: Path, key: str, entry: dict) -> None:
    mpath = parquet_dir / "manifest.json"
    with open(parquet_dir / ".manifest.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(mpath.read_text()) if mpath.exists() else {}
        except json.JSONDecodeError as e:
            # never silently reset: that would drop every other dataset's provenance record
            raise RuntimeError(
                f"corrupt manifest {mpath} ({e}); fix or restore it (a .tmp sibling or backup may help) "
                "before converting more data"
            ) from e
        if not isinstance(data, dict):
            raise RuntimeError(f"corrupt manifest {mpath}: top level is {type(data).__name__}, expected an object")
        data.setdefault("converted", {})[key] = entry
        tmp = mpath.with_name(f"manifest.json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, mpath)


def convert(dataset: str, src: Path, parquet_dir: Path) -> int:
    """Convert one extracted source file (CSV / shapefile / GPKG) and return the rows written.

    - ``lids``/``open_uprn``: src is the CSV -> ``<dataset>/<dataset>.parquet``.
    - ``open_toid``: src is a tile CSV (``osopentoid_<release>_<tile>.csv``) -> ``open_toid/<tile>.parquet``,
      keeping only TOIDs in ``lids/lids.parquet`` (which must exist).
    - ``openmap_local``: src is a ``*_Building.shp`` (or GPKG) -> ``openmap_local/<tile>.parquet``
      (tile = parent folder name if the shapefile stem is generic, else the stem minus ``_Building``).
    - ``onsud``: src is a CSV, or a directory of CSVs (preferred: all go into one ``onsud/onsud.parquet``).
    """
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}; expected one of {DATASETS}")
    src = Path(src)
    parquet_dir = Path(parquet_dir)
    if not src.exists():
        raise FileNotFoundError(src)
    parquet_dir.mkdir(parents=True, exist_ok=True)
    free = _free_gb(parquet_dir)
    if free < MIN_FREE_GB:
        raise RuntimeError(f"only {free:.1f} GB free; need at least {MIN_FREE_GB} GB. Aborting conversion.")

    if dataset in ("lids", "open_uprn", "onsud"):
        out = parquet_dir / dataset / f"{dataset}.parquet"
    elif dataset == "open_toid":
        tile = src.stem.split("_")[-1].lower()
        out = parquet_dir / "open_toid" / f"{tile}.parquet"
    else:
        stem = re.sub(r"_?building$", "", src.stem, flags=re.I)
        tile = (stem or src.parent.name).lower()
        out = parquet_dir / "openmap_local" / f"{tile}.parquet"

    con = _connect(parquet_dir)
    try:
        if dataset == "lids":
            n = _lids(con, src, out)
        elif dataset == "open_uprn":
            n = _open_uprn(con, src, out)
        elif dataset == "open_toid":
            n = _open_toid(con, src, out, parquet_dir / "lids" / "lids.parquet")
        elif dataset == "openmap_local":
            n = _openmap_local(con, src, out)
        else:
            n = _onsud(con, src, out)
    finally:
        con.close()

    size = sum(p.stat().st_size for p in ([src] if src.is_file() else src.rglob("*")) if p.is_file())
    _append_manifest(
        parquet_dir,
        str(out.relative_to(parquet_dir)),
        {
            "dataset": dataset,
            "source": src.name,
            "source_size": size,
            "rows": n,
            "parquet_size": out.stat().st_size,
            "converted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )
    return n
