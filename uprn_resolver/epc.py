"""Energy Performance Certificate (EPC) attributes keyed by UPRN -- non-address fields only.

Sources
  * England & Wales: https://get-energy-performance-data.communities.gov.uk/ (the former
    epc.opendatacommunities.org, which now 301-redirects there). Bulk CSV needs a GOV.UK One Login
    sign-in, so this module never downloads it: place the files you download yourself in
    ``<raw_dir>/epc/`` as ``*domestic*certificates*.zip`` (names containing ``non-domestic`` are treated
    as non-domestic) and they are converted too.
  * Scotland: Scottish Government open data on data.gov.scot (OGL, no login): "extended historic" domestic
    and non-domestic EPC extracts (7z archives). The UPRN field is ``OSG_REFERENCE_NUMBER``.

Every non-address column is kept (DENYLIST, not allowlist): names are normalised to snake_case, then any
column whose name matches ``ADDRESS_DENY`` (address, postcode, post town, locality, street, county,
building/house name or number, free-text recommendation/addendum fields, assessor/contact details) is never
read into the output. Conversion fails if an output column name matches the denylist (defence in depth).
Dropped names are recorded in the manifest entry (``dropped_columns``). Rows without a UPRN are dropped.
Types: all-numeric columns become INTEGER/DOUBLE, ``*_date`` DATE, ``*_datetime``/``created_at`` TIMESTAMP,
everything else (and id/code-like or leading-zero columns) VARCHAR. The text "Data removed" becomes NULL.
Certificate types: domestic, non-domestic, dec (Display Energy Certificates, E&W zips named *display*/*dec*).
Recommendations files are never read. Output: ``data/parquet/epc/<name>.parquet`` (zstd) plus a manifest
entry ``manifest["converted"]["epc/<name>.parquet"]``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import duckdb
import requests

from .convert import ROW_GROUP_SIZE, _append_manifest, _copy, _csv_header, _free_gb, _ident, _q

log = logging.getLogger(__name__)

MIN_FREE_GB = 15
TIMEOUT = 120
EW_HOME = "https://get-energy-performance-data.communities.gov.uk/"
SCOT_BASE = "https://data.gov.scot/dataset"
_D = "domestic_energy_performance_certificates___extended_historic_dataset_to_q1_2026"
_N = "non_domestic_energy_performance_certificates___extended_historic_dataset_to_q1_2026"
_DOM_URL = f"{SCOT_BASE}/{_D}/resource/b7bc304c-debb-4365-9a86-1c8cb82cc60d/download"
_ND_URL = f"{SCOT_BASE}/{_N}/resource/c7873ae4-d19a-43ee-8d9f-415e7d0f05d7/download"
SCOT_ARCHIVES = {
    "scot_domestic_historic.7z": (_DOM_URL, "domestic"),
    "scot_nondomestic_historic.7z": (_ND_URL, "non-domestic"),
}
CORE_COLS = (
    "uprn", "uprn_source", "lmk_key", "certificate_type", "country", "property_type", "built_form",
    "total_floor_area", "construction_age_band", "current_energy_rating", "lodgement_date",
)  # fmt: skip
MIN_LODGEMENT_YEAR = 2007
# Matched against snake_case output names, token-wise. Conservative: free text that could hold an address
# (improvements/addendum/special energy uses) and personal/contact fields are dropped too.
ADDRESS_DENY = re.compile(
    r"(?:^|_)(?:address\d*|addr\d*|postcode|postal|post|posttown|town|locality|street|thoroughfare|paon|saon|"
    r"county|premises|organi[sz]ation|building_name|building_number|house_name|house_number|flat_name|"
    r"flat_number|unit_name|unit_number|property_name|site_name|improvements?|recommendations?|addendum|"
    r"addendum_text|special_energy_uses|comments?|notes?|remarks|assessor|accreditation|company|contact|"
    r"email|phone|telephone)(?:$|_)"
)
_ID_LIKE = re.compile(r"reference_number|_key$|_id$|code|constituency$|data_zone|uprn|rrn|lmk|^historic$")
_ALIASES_RATING = {
    "domestic": ("current_energy_rating", "current_energy_band"),
    "non-domestic": ("asset_rating_band", "current_energy_performance_band", "current_energy_rating"),
    "dec": ("operational_rating_band", "current_energy_rating"),
}
EW_NEEDS_LOGIN_MSG = (
    "England & Wales EPC bulk data needs a free GOV.UK One Login. Sign in at "
    f"{EW_HOME} (Start now > Download files), download the domestic, non-domestic and display (DEC) certificate zips, "
    "and save them in {dir}/ (e.g. all-domestic-certificates.zip, all-non-domestic-certificates.zip), "
    "then re-run."
)


def _epc_parquet_dir(parquet_dir: Path) -> Path:
    return Path(parquet_dir) / "epc"


def snake(col: str) -> str:
    """Normalise a source header to snake_case ('3_YR_X' -> 'n3_yr_x', 'LMK_KEY' -> 'lmk_key')."""
    c = col.lstrip("\ufeff").strip()
    c = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", c)
    c = re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_")
    return f"n{c}" if c[:1].isdigit() else c


def is_denied(name: str) -> bool:
    return bool(ADDRESS_DENY.search(name))


def validate_columns(names) -> None:
    """Defence in depth: raise if any output column name looks like an address/free-text/contact field."""
    bad = [n for n in names if ADDRESS_DENY.search(n)]
    if bad:
        raise ValueError(f"EPC output would contain denylisted columns: {bad}")


def valid_date_sql(expr: str) -> str:
    """Dates before 2007 or after today are bad lodgements -> NULL."""
    return (
        f"(CASE WHEN {expr} BETWEEN DATE '{MIN_LODGEMENT_YEAR}-01-01' AND DATE '{dt.date.today().isoformat()}' "
        f"THEN {expr} END)"
    )


_NUM = r"[-+]?\d+(\.\d+)?"


def _v(c: str) -> str:
    return f"NULLIF(NULLIF(trim({_ident(c)}), ''), 'Data removed')"


def _num(c: str, typ: str = "DOUBLE") -> str:
    return f"TRY_CAST(CASE WHEN regexp_full_match({_v(c)}, '{_NUM}') THEN {_v(c)} END AS {typ})"


def _date(c: str) -> str:
    d = f"substr(trim({_ident(c)}), 1, 10)"
    return f"COALESCE(TRY_CAST({d} AS DATE), TRY_STRPTIME({d}, '%d/%m/%Y')::DATE)"


def _ts(c: str) -> str:
    t = f"trim({_ident(c)})"
    return f"COALESCE(TRY_CAST({t} AS TIMESTAMP), TRY_STRPTIME({t}, '%d/%m/%Y %H:%M:%S'))"


def _kind(name: str) -> str | None:
    if name.endswith("_date"):
        return "date"
    if name.endswith("_datetime") or name in ("created_at", "updated_at") or name.endswith("_timestamp"):
        return "ts"
    return None


def _rel(src: Path) -> str:
    return f"read_csv({_q(src)}, header=true, all_varchar=true, ignore_errors=true, strict_mode=false)"


def _type_extras(con, base: str, extra: list[tuple[str, str]], out: dict) -> None:
    """Add the remaining (all-VARCHAR source) columns to ``out`` with inferred types (one stats scan of ``base``)."""
    # one scan to find which remaining columns are cleanly numeric (and whether integers)
    cand = [(n, c) for n, c in extra if _kind(n) is None and not _ID_LIKE.search(n)]
    stats: dict[str, tuple[int, int, int, int]] = {}
    if cand:
        sel = []
        for i, (_, c) in enumerate(cand):
            v = _v(c)
            sel.append(
                f"count({v}) AS nn{i}, count(CASE WHEN regexp_full_match({v}, '{_NUM}') THEN 1 END) AS nu{i}, "
                f"count(CASE WHEN regexp_full_match({v}, '-?\\d{{1,9}}') THEN 1 END) AS ni{i}, "
                f"count(CASE WHEN regexp_matches({v}, '^[-+]?0\\d') THEN 1 END) AS lz{i}"
            )
        row = con.execute(f"SELECT {', '.join(sel)} FROM {base}").fetchone()
        for i, (n, _) in enumerate(cand):
            stats[n] = tuple(row[4 * i : 4 * i + 4])
    for n, c in extra:
        k = _kind(n)
        if k == "date":
            out[n] = (_date(c), "DATE")
        elif k == "ts":
            out[n] = (_ts(c), "TIMESTAMP")
        elif n in stats and stats[n][0] > 0 and stats[n][0] == stats[n][1] and stats[n][3] == 0:
            nn, _, ni, _ = stats[n]
            typ = "INTEGER" if ni == nn else "DOUBLE"
            out[n] = (_num(c, typ), typ)
        else:
            out[n] = (_v(c), "VARCHAR")


def _plan(con, src: Path, certificate_type: str, country: str) -> tuple[str, list[str], list[str]]:
    """Return (SELECT sql, kept column names, dropped column names) for one CSV."""
    raw = _csv_header(con, src)
    snk = {c: snake(c) for c in raw}
    by: dict[str, str] = {}
    for c in raw:
        by.setdefault(snk[c], c)
    uprn_col = by.get("osg_reference_number" if country == "S" else "uprn")
    if uprn_col is None:
        raise ValueError(f"no UPRN column in {src.name}: {raw}")
    lmk_names = ("lmk_key", "report_reference_number", "certificate_number", "rrn")
    lmk_col = next((by[k] for k in lmk_names if k in by), None)
    consumed = {uprn_col}
    if lmk_col:
        consumed.add(lmk_col)
    out: dict[str, tuple[str, str]] = {}  # name -> (expr, type)
    out["uprn"] = (f"TRY_CAST(trim({_ident(uprn_col)}) AS BIGINT)", "BIGINT")
    if country == "S":
        out["uprn_source"] = ("'OSG_REFERENCE_NUMBER'", "VARCHAR")
    else:
        out["uprn_source"] = (_v(by["uprn_source"]) if "uprn_source" in by else "CAST(NULL AS VARCHAR)", "VARCHAR")
        consumed.add(by.get("uprn_source", uprn_col))
    out["lmk_key"] = (_v(lmk_col) if lmk_col else "CAST(NULL AS VARCHAR)", "VARCHAR")
    out["certificate_type"] = (f"'{certificate_type}'", "VARCHAR")
    out["country"] = (f"'{country}'", "VARCHAR")
    for k in ("property_type", "built_form"):
        out[k] = (_v(by[k]) if k in by else "CAST(NULL AS VARCHAR)", "VARCHAR")
        consumed.add(by.get(k, uprn_col))
    # total floor area (ND files call it FLOOR_AREA; that column is also kept as-is)
    parts = [_num(by[k]) for k in ("total_floor_area", "floor_area") if k in by] or ["CAST(NULL AS DOUBLE)"]
    out["total_floor_area"] = (f"COALESCE({', '.join(parts)})", "DOUBLE")
    consumed.add(by.get("total_floor_area", uprn_col))
    out["construction_age_band"] = (
        _v(by["construction_age_band"]) if "construction_age_band" in by else "CAST(NULL AS VARCHAR)",
        "VARCHAR",
    )
    consumed.add(by.get("construction_age_band", uprn_col))
    rparts = [_v(by[k]) for k in _ALIASES_RATING.get(certificate_type, ()) if k in by] or ["CAST(NULL AS VARCHAR)"]
    out["current_energy_rating"] = (f"COALESCE({', '.join(rparts)})", "VARCHAR")
    consumed.add(by.get("current_energy_rating", uprn_col))
    dparts = []
    if "lodgement_date" in by:
        dparts.append(_date(by["lodgement_date"]))
    if "lodgement_datetime" in by:
        dparts.append(f"CAST({_ts(by['lodgement_datetime'])} AS DATE)")
    if "lodgement_date_time" in by:
        dparts.append(f"CAST({_ts(by['lodgement_date_time'])} AS DATE)")
    dexpr = valid_date_sql(f"COALESCE({', '.join(dparts)})") if dparts else "CAST(NULL AS DATE)"
    out["lodgement_date"] = (dexpr, "DATE")
    consumed.add(by.get("lodgement_date", uprn_col))

    dropped, extra = [], []
    for c in raw:
        n = snk[c]
        if c in consumed:
            continue
        if is_denied(n):
            dropped.append(n)
        elif n in out or n in dict(extra):
            dropped.append(f"{n} (duplicate name)")
        else:
            extra.append((n, c))

    base = f"(SELECT * FROM {_rel(src)} WHERE TRY_CAST(trim({_ident(uprn_col)}) AS BIGINT) > 0)"
    _type_extras(con, base, extra, out)
    validate_columns(out)
    cols = ",\n        ".join(f"{e} AS {_ident(n)}" for n, (e, _) in out.items())
    return f"SELECT {cols} FROM {base}", list(out), dropped


def convert_epc_csv(
    src: Path, parquet_dir: Path, name: str, certificate_type: str, country: str, *, total_rows: bool = True
) -> tuple[int, int]:
    """Convert one EPC CSV to ``parquet_dir/epc/<name>.parquet``; returns (rows_in_csv, rows_with_uprn).

    Keeps every non-denylisted column; records the conversion (kept/dropped names) in the manifest.
    The caller deletes the CSV.
    """
    src, parquet_dir = Path(src), Path(parquet_dir)
    out = _epc_parquet_dir(parquet_dir) / f"{name}.parquet"
    tmpdir = parquet_dir.parent / "tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET temp_directory='{tmpdir}'")
    con.execute("SET memory_limit='4GB'")
    try:
        sql, kept, dropped = _plan(con, src, certificate_type, country)
        n_out = _copy(con, sql, out)
        written = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({_q(out)})").fetchall()]
        try:
            validate_columns(written)
        except ValueError:
            out.unlink(missing_ok=True)
            raise
        n_in = n_out
        if total_rows:
            n_in = con.execute(f"SELECT count(*) FROM {_rel(src)}").fetchone()[0]
    finally:
        con.close()
    _append_manifest(
        parquet_dir,
        f"epc/{out.name}",
        {
            "dataset": "epc",
            "source": src.name,
            "source_size": src.stat().st_size,
            "rows_in": int(n_in),
            "rows": n_out,
            "certificate_type": certificate_type,
            "country": country,
            "columns": kept,
            "dropped_columns": dropped,
            "parquet_size": out.stat().st_size,
            "converted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )
    return int(n_in), n_out


def _check_disk(path: Path) -> None:
    free = _free_gb(path)
    if free < MIN_FREE_GB:
        raise RuntimeError(f"only {free:.1f} GB free; need at least {MIN_FREE_GB} GB. Aborting EPC extraction.")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _fetch(url: str, dest: Path) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=TIMEOUT) as r:
        r.raise_for_status()
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    part.replace(dest)


def _7z_members(archive: Path) -> list[str]:
    out = subprocess.run(["bsdtar", "-tf", str(archive)], check=True, capture_output=True, text=True).stdout
    return [ln for ln in out.splitlines() if ln.lower().endswith(".csv") and "/" not in ln and ".." not in ln]


def _extract_7z_member(archive: Path, member: str, dest: Path) -> Path:
    """Extract exactly one top-level member to ``dest`` (bsdtar -O streams it; names are not trusted)."""
    with open(dest, "wb") as f:
        subprocess.run(["bsdtar", "-xOf", str(archive), member], check=True, stdout=f)
    return dest


def _zip_members(archive: Path) -> list[str]:
    with zipfile.ZipFile(archive) as zf:
        return [
            i.filename
            for i in zf.infolist()
            if not i.is_dir()
            and Path(i.filename).name.lower() == "certificates.csv"
            and ".." not in Path(i.filename).parts
            and not Path(i.filename).is_absolute()
        ]


def _extract_zip_member(archive: Path, member: str, dest: Path) -> Path:
    with zipfile.ZipFile(archive) as zf, zf.open(member) as s, open(dest, "wb") as f:
        shutil.copyfileobj(s, f, 1 << 20)
    return dest


def _done(parquet_dir: Path, name: str) -> bool:
    try:
        m = json.loads((parquet_dir / "manifest.json").read_text())
    except (OSError, ValueError):
        return False
    return (
        f"epc/{name}.parquet" in m.get("converted", {}) and (_epc_parquet_dir(parquet_dir) / f"{name}.parquet").exists()
    )


def _process_archive(archive: Path, parquet_dir: Path, work: Path, certificate_type: str, country: str,
                     prefix: str, members, extract) -> list[tuple[int, int]]:  # fmt: skip
    results = []
    for member in members(archive):
        stem = Path(member).parent.name if country == "EW" else Path(member).stem
        name = _slug(f"{prefix}_{stem or Path(member).stem}")
        if _done(parquet_dir, name):
            log.info("epc: %s already converted", name)
            continue
        _check_disk(work)
        csv = work / "current.csv"
        extract(archive, member, csv)
        try:
            n_in, n_out = convert_epc_csv(csv, parquet_dir, name, certificate_type, country)
        finally:
            csv.unlink(missing_ok=True)
        log.info("epc: %s -> %s rows (%s with UPRN)", member, n_in, n_out)
        results.append((n_in, n_out))
    return results


EW_BULK_KINDS = {"domestic": "domestic", "non-domestic": "non_domestic", "dec": "display"}


def convert_ew_bulk(
    zip_path: Path, out_dir: Path, *, delete_zip: bool = True, min_free_gb: float = MIN_FREE_GB
) -> list[dict]:
    """Convert EVERY CSV in an E&W bulk zip (certificates/recommendations-YYYY) as-is (all columns, VARCHAR)
    to ``out_dir/<domestic|non_domestic|display>/<csv stem>.parquet`` (zstd).

    One entry at a time is extracted under ``out_dir/.work`` and deleted afterwards (never the whole zip).
    Disk is checked before each entry (abort below ``min_free_gb``). CSV and Parquet row counts must match.
    Records each file in the manifest under the lock; deletes the zip only once all entries succeeded.
    No filtering or denylist: the output holds address columns and is for local use only.
    """
    zip_path, out_dir = Path(zip_path), Path(out_dir)
    kind = EW_BULK_KINDS[_ew_type(zip_path.name)]
    dest = out_dir / kind
    dest.mkdir(parents=True, exist_ok=True)
    pq_root = out_dir.parent  # data/parquet (manifest lives here)
    work = out_dir / ".work"
    work.mkdir(exist_ok=True)
    tmpdir = work / "duck"
    tmpdir.mkdir(exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        members = sorted(
            (i for i in zf.infolist()
            if not i.is_dir() and i.filename.lower().endswith(".csv") and ".." not in Path(i.filename).parts
            and not Path(i.filename).is_absolute()),
            key=lambda i: i.filename,
        )  # fmt: skip
        results = []
        for info in members:
            stem = Path(info.filename).stem
            out = dest / f"{stem}.parquet"
            free = _free_gb(work)
            if free < min_free_gb:
                raise RuntimeError(f"only {free:.1f} GB free; need at least {min_free_gb} GB. Aborting.")
            csv = work / "current.csv"
            try:
                with zf.open(info) as s, open(csv, "wb") as f:
                    shutil.copyfileobj(s, f, 1 << 20)
                tmp = out.with_name(out.name + ".tmp")
                con = duckdb.connect()
                try:
                    con.execute(f"SET temp_directory={_q(tmpdir)}")
                    con.execute("SET memory_limit='6GB'")
                    con.execute("SET preserve_insertion_order=false")
                    rel = f"read_csv({_q(csv)}, header=true, all_varchar=true, strict_mode=true)"
                    con.execute(
                        f"COPY (SELECT * FROM {rel}) TO {_q(tmp)} "
                        f"(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {ROW_GROUP_SIZE})"
                    )
                    n_csv = con.execute(f"SELECT count(*) FROM {rel}").fetchone()[0]
                    n_pq = con.execute(f"SELECT count(*) FROM read_parquet({_q(tmp)})").fetchone()[0]
                    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({_q(tmp)})").fetchall()]
                finally:
                    con.close()
                if n_csv != n_pq:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(f"{info.filename}: row mismatch csv={n_csv} parquet={n_pq}")
                tmp.replace(out)
            finally:
                csv.unlink(missing_ok=True)
            entry = {
                "dataset": "epc_ew", "source": f"{zip_path.name}:{info.filename}", "source_size": info.file_size,
                "rows": int(n_pq), "columns": cols, "parquet_size": out.stat().st_size,
                "converted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }  # fmt: skip
            _append_manifest(pq_root, f"{out_dir.name}/{kind}/{out.name}", entry)
            log.info("epc_ew: %s -> %s rows", info.filename, n_pq)
            results.append({"file": out.name, "rows": int(n_pq), "parquet_size": out.stat().st_size, "columns": cols})
    shutil.rmtree(work, ignore_errors=True)
    if delete_zip:
        zip_path.unlink()
    return results


def convert_scot_bulk(raw_dir: Path, out_dir: Path, *, min_free_gb: float = MIN_FREE_GB) -> list[dict]:
    """Convert the Scottish "extended historic" domestic and non-domestic archives as-is (all columns, VARCHAR,
    zstd) to ``out_dir/<domestic|non_domestic>/<csv stem>.parquet``.

    Archives (``SCOT_ARCHIVES``) are downloaded into ``raw_dir`` and CSVs extracted one at a time with bsdtar
    under ``out_dir/.work``; the CSV is deleted after each conversion and the archive once all its members
    succeeded. Disk is checked before each extraction (abort below ``min_free_gb``).

    Scottish CSVs carry a column-description row directly after the header. Like ``convert_epc_csv`` (whose
    UPRN filter drops it implicitly) the description row is NOT written: it is detected as a first data row
    whose OSG_REFERENCE_NUMBER is non-numeric and removed by exact whole-row match. Verification:
    csv_rows == parquet_rows + dropped_description_rows. Output holds address columns; local use only.
    """
    raw_dir, out_dir = Path(raw_dir), Path(out_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    pq_root = out_dir.parent
    work = out_dir / ".work"
    work.mkdir(parents=True, exist_ok=True)
    tmpdir = work / "duck"
    tmpdir.mkdir(exist_ok=True)
    results = []
    for fname, (url, ctype) in SCOT_ARCHIVES.items():
        kind = ctype.replace("-", "_")
        dest = out_dir / kind
        dest.mkdir(parents=True, exist_ok=True)
        archive = raw_dir / fname
        if not archive.exists():
            _fetch(url, archive)
        for member in sorted(_7z_members(archive)):
            out = dest / f"{Path(member).stem}.parquet"
            free = _free_gb(work)
            if free < min_free_gb:
                raise RuntimeError(f"only {free:.1f} GB free; need at least {min_free_gb} GB. Aborting.")
            csv = work / "current.csv"
            try:
                _extract_7z_member(archive, member, csv)
                tmp = out.with_name(out.name + ".tmp")
                con = duckdb.connect()
                try:
                    con.execute(f"SET temp_directory={_q(tmpdir)}")
                    con.execute("SET memory_limit='6GB'")
                    rel = f"read_csv({_q(csv)}, header=true, all_varchar=true, strict_mode=false)"
                    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()]
                    uprn_col = next((c for c in cols if snake(c) == "osg_reference_number"), None)
                    first = con.execute(f"SELECT * FROM {rel} LIMIT 1").fetchone()
                    where, n_desc = "", 0
                    if first is not None and uprn_col is not None:
                        v = first[cols.index(uprn_col)]
                        if v is None or not re.fullmatch(r"\d+", v.strip()):
                            eq = " AND ".join(
                                f"{_ident(c)} IS NOT DISTINCT FROM {_q(x) if x is not None else 'NULL'}"
                                for c, x in zip(cols, first, strict=True)
                            )
                            where, n_desc = f" WHERE NOT ({eq})", 1
                    con.execute(
                        f"COPY (SELECT * FROM {rel}{where}) TO {_q(tmp)} "
                        f"(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {ROW_GROUP_SIZE})"
                    )
                    n_csv = con.execute(f"SELECT count(*) FROM {rel}").fetchone()[0]
                    n_pq = con.execute(f"SELECT count(*) FROM read_parquet({_q(tmp)})").fetchone()[0]
                finally:
                    con.close()
                if n_csv != n_pq + n_desc:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(f"{member}: row mismatch csv={n_csv} parquet={n_pq} desc_rows={n_desc}")
                tmp.replace(out)
            finally:
                csv.unlink(missing_ok=True)
            entry = {
                "dataset": "epc_scot", "source": f"{fname}:{member}", "rows": int(n_pq),
                "rows_in_csv": int(n_csv), "description_rows_dropped": n_desc, "columns": cols,
                "parquet_size": out.stat().st_size, "converted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }  # fmt: skip
            _append_manifest(pq_root, f"{out_dir.name}/{kind}/{out.name}", entry)
            log.info("epc_scot: %s -> %s rows", member, n_pq)
            results.append({"type": kind, "file": out.name, "rows": int(n_pq), "rows_in_csv": int(n_csv),
                            "parquet_size": out.stat().st_size, "columns": cols})  # fmt: skip
        archive.unlink()
    shutil.rmtree(work, ignore_errors=True)
    return results


def _ew_type(filename: str) -> str:
    if re.search(r"non[-_ ]?domestic", filename, re.I):
        return "non-domestic"
    if re.search(r"display|(?:^|[-_ ])dec(?:[-_ .]|$)", filename, re.I):
        return "dec"
    return "domestic"


def download_epc(raw_dir: Path, parquet_dir: Path | None = None) -> None:
    """Fetch Scotland EPC open data and convert it (and any user-supplied E&W zips) to Parquet.

    England & Wales needs a GOV.UK One Login, so only zips already placed in ``raw_dir/epc/`` are used;
    otherwise a warning says what to do. Each inner CSV is extracted, converted and deleted in turn;
    archives are deleted once fully converted.
    """
    raw = Path(raw_dir) / "epc"
    raw.mkdir(parents=True, exist_ok=True)
    pq = Path(parquet_dir) if parquet_dir else Path(raw_dir).parent / "parquet"
    work = raw / "work"
    work.mkdir(exist_ok=True)

    ew = sorted(raw.glob("*.zip"))
    if not ew:
        log.warning("epc: %s", EW_NEEDS_LOGIN_MSG.format(dir=raw))
    for z in ew:
        ctype = _ew_type(z.name)
        _process_archive(z, pq, work, ctype, "EW", f"ew_{ctype}", _zip_members, _extract_zip_member)
        z.unlink()

    for fname, (url, ctype) in SCOT_ARCHIVES.items():
        arc = raw / fname
        if not arc.exists():
            _check_disk(raw)
            log.info("epc: downloading %s", url)
            try:
                _fetch(url, arc)
            except requests.RequestException as e:
                log.warning("epc: could not fetch Scotland %s: %s", ctype, e)
                continue
        _process_archive(arc, pq, work, ctype, "S", f"s_{ctype}", _7z_members, _extract_7z_member)
        arc.unlink()
    shutil.rmtree(work, ignore_errors=True)


def _ew_dir(parquet_dir: Path) -> Path:
    return Path(parquet_dir) / "epc_ew"


def _ew_typed_select(con, tbl: str, certificate_type: str) -> tuple[str, list[str], list[str]]:
    """SELECT (typed, core columns first) over ``tbl`` (all-VARCHAR E&W certificates already filtered to the
    LAD). Returns (sql, kept names, dropped names)."""
    names = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {tbl}").fetchall()]
    have = set(names)
    out: dict[str, tuple[str, str]] = {}
    null = lambda t: f"CAST(NULL AS {t})"  # noqa: E731
    out["uprn"] = (f"TRY_CAST(trim({_ident('uprn')}) AS BIGINT)", "BIGINT")
    out["uprn_source"] = (_v("uprn_source") if "uprn_source" in have else null("VARCHAR"), "VARCHAR")
    out["lmk_key"] = (_v("certificate_number") if "certificate_number" in have else null("VARCHAR"), "VARCHAR")
    out["certificate_type"] = (f"'{certificate_type}'", "VARCHAR")
    out["country"] = ("'EW'", "VARCHAR")
    for k in ("property_type", "built_form"):
        out[k] = (_v(k) if k in have else null("VARCHAR"), "VARCHAR")
    parts = [_num(k) for k in ("total_floor_area", "floor_area") if k in have] or [null("DOUBLE")]
    out["total_floor_area"] = (f"COALESCE({', '.join(parts)})", "DOUBLE")
    out["construction_age_band"] = (
        _v("construction_age_band") if "construction_age_band" in have else null("VARCHAR"),
        "VARCHAR",
    )
    rparts = [_v(k) for k in _ALIASES_RATING[certificate_type] if k in have] or [null("VARCHAR")]
    out["current_energy_rating"] = (f"COALESCE({', '.join(rparts)})", "VARCHAR")
    dparts = []
    if "lodgement_date" in have:
        dparts.append(_date("lodgement_date"))
    if "lodgement_datetime" in have:
        dparts.append(f"CAST({_ts('lodgement_datetime')} AS DATE)")
    out["lodgement_date"] = (valid_date_sql(f"COALESCE({', '.join(dparts)})") if dparts else null("DATE"), "DATE")
    consumed = {"uprn", "uprn_source", "certificate_number", "property_type", "built_form", "total_floor_area",
                "construction_age_band", "current_energy_rating", "lodgement_date", "country"}  # fmt: skip
    dropped = [n for n in names if n not in consumed and is_denied(n)]
    extra = [(n, n) for n in names if n not in consumed and not is_denied(n) and n not in out]
    _type_extras(con, tbl, extra, out)
    validate_columns(out)
    cols = ",\n        ".join(f"{e} AS {_ident(n)}" for n, (e, _) in out.items())
    return f"SELECT {cols} FROM {tbl}", list(out), dropped


def stage_epc(lad: str, raw_dir: Path, staged_dir: Path) -> None:
    """Stage one row per ONSUD UPRN: the latest valid certificate (domestic wins ties) with all retained
    columns plus ``epc_count`` (all certificates for that UPRN). Bad lodgement dates (before 2007 or after
    today) are NULL and sort last.

    Sources: E&W as-is conversions ``parquet/epc_ew/{domestic,non_domestic,display}/certificates-*.parquet``
    (all VARCHAR, address columns present: they are filtered to the LAD's UPRNs first, then typed and the
    address/free-text columns dropped via the denylist) plus Scotland ``parquet/epc/s_*.parquet`` (already
    filtered). The old ``epc/ew_*`` conversions are ignored (superseded; would double count).
    """
    sdir = Path(staged_dir) / lad
    onsud = sdir / "onsud.parquet"
    if not onsud.exists():
        raise FileNotFoundError(f"{onsud} missing; stage ONSUD first")
    # drop any previous output first: when the source is absent (or this run fails) build must see absence,
    # not the data of an earlier run
    (sdir / "epc.parquet").unlink(missing_ok=True)
    (sdir / "epc.parquet.tmp").unlink(missing_ok=True)
    pq = Path(raw_dir).parent / "parquet"
    pdir, ewdir = _epc_parquet_dir(pq), _ew_dir(pq)
    scot = sorted(pdir.glob("s_*.parquet")) if pdir.exists() else []
    ew = {
        t: sorted((ewdir / d).glob("certificates-*.parquet"))
        for t, d in (("domestic", "domestic"), ("non-domestic", "non_domestic"), ("dec", "display"))
    } if ewdir.exists() else {}  # fmt: skip
    if not scot and not any(ew.values()):
        log.warning("epc: no EPC parquet under %s or %s; skipping epc.parquet", pdir, ewdir)
        return
    con = duckdb.connect()
    try:
        con.execute(f"SET temp_directory={_q(pq.parent / 'tmp')}")
        con.execute("SET memory_limit='6GB'")
        con.execute(f"CREATE TEMP TABLE lad_uprn AS SELECT DISTINCT uprn FROM read_parquet({_q(onsud)}) WHERE uprn > 0")
        con.execute("CREATE TEMP TABLE lad_uprn_s AS SELECT CAST(uprn AS VARCHAR) AS uprn FROM lad_uprn")
        parts: list[str] = []
        for t, files in ew.items():
            if not files:
                continue
            lst = ", ".join(_q(f) for f in files)
            src = f"read_parquet([{lst}], union_by_name=true)"
            have = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
            keep = [n for n in sorted(have) if not is_denied(n) or n == "uprn"]
            tbl = "raw_" + _slug(t)
            sel = ", ".join(_ident(n) for n in keep)
            # semi join on the VARCHAR uprn: only matching rows are materialised (dropped columns never are)
            con.execute(
                f"CREATE TEMP TABLE {tbl} AS SELECT {sel} FROM {src} WHERE uprn IN (SELECT uprn FROM lad_uprn_s)"
            )
            n = con.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
            log.info("epc: %s E&W %s certificates for %s", n, t, lad)
            if n:
                sql, _, _ = _ew_typed_select(con, tbl, t)
                parts.append(sql)
        if scot:
            lst = ", ".join(_q(f) for f in scot)
            con.execute(
                f"CREATE TEMP TABLE raw_scot AS SELECT * FROM read_parquet([{lst}], union_by_name=true) e "
                "SEMI JOIN lad_uprn l ON l.uprn = e.uprn"
            )
            if con.execute("SELECT count(*) FROM raw_scot").fetchone()[0]:
                parts.append("SELECT * FROM raw_scot")
        if not parts:
            log.warning("epc: no certificates for %s; skipping epc.parquet", lad)
            return
        union = " UNION ALL BY NAME ".join(f"({p})" for p in parts)
        sql = f"""
            WITH certs AS (
                SELECT * REPLACE ({valid_date_sql("lodgement_date")} AS lodgement_date) FROM ({union})
            ),
            certs2 AS (
                SELECT *, row_number() OVER (PARTITION BY uprn, lmk_key ORDER BY lodgement_date DESC NULLS LAST) AS dup
                FROM certs
            ),
            uniq AS (SELECT * EXCLUDE (dup) FROM certs2 WHERE dup = 1 OR lmk_key IS NULL),
            ranked AS (
                SELECT *, count(*) OVER (PARTITION BY uprn) AS epc_count,
                       row_number() OVER (
                           PARTITION BY uprn
                           ORDER BY lodgement_date DESC NULLS LAST,
                                    (certificate_type = 'domestic') DESC, lmk_key DESC NULLS LAST
                       ) AS rn
                FROM uniq
            )
            SELECT * EXCLUDE (rn) FROM ranked WHERE rn = 1 ORDER BY uprn"""
        out = sdir / "epc.parquet"
        tmp = out.with_name(out.name + ".tmp")
        con.execute(f"COPY ({sql}) TO {_q(tmp)} (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {ROW_GROUP_SIZE})")
        cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({_q(tmp)})").fetchall()]
        try:
            validate_columns(cols)
        except ValueError:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(out)
        n = con.execute(f"SELECT count(*) FROM read_parquet({_q(out)})").fetchone()[0]
    finally:
        con.close()
    log.info("epc: %s UPRNs with EPC for %s", n, lad)
