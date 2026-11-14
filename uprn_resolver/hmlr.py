"""HM Land Registry Price Paid transaction lookup tables (identifiers only).

From 28 August 2026 HMLR publishes monthly lookup tables next to Price Paid Data:
  * pp-uprn-lookup-<mon>-<year>.csv        transaction unique identifier -> UPRN
  * pp-inspire-id-lookup-<mon>-<year>.csv  transaction unique identifier -> INSPIRE ID
Both are headerless, fully double-quoted CSV (UTF-8), 2 columns. Transaction IDs are
brace-wrapped upper-case GUIDs, e.g. "{326EFAEB-0447-475E-8A3A-02EE17C340BE}".
Not back-dated. No address fields are downloaded or kept.
"""

from __future__ import annotations

import datetime as dt
import logging
import zipfile
from pathlib import Path

import duckdb
import requests

log = logging.getLogger(__name__)

BASE_URL = "https://price-paid-data.publicdata.landregistry.gov.uk"
KINDS = {"uprn": "pp-uprn-lookup", "inspire": "pp-inspire-id-lookup"}
FIRST_MONTH = (2026, 8)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
TIMEOUT = 120


def _months(today: dt.date | None = None):
    """Yield (year, month) from Aug 2026 to the current month inclusive."""
    today = today or dt.date.today()
    y, m = FIRST_MONTH
    while (y, m) <= (today.year, today.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _fetch(url: str, dest: Path) -> bool:
    """Download url to dest (via .part). Returns False if the file does not exist."""
    r = requests.get(url, stream=True, timeout=TIMEOUT)
    if r.status_code in (403, 404):
        return False
    r.raise_for_status()
    part = dest.with_suffix(dest.suffix + ".part")
    with open(part, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    part.replace(dest)
    return True


def download_hmlr(raw_dir: Path) -> list[str]:
    """Download every available monthly lookup file (Aug 2026 onwards) to raw_dir/hmlr/, convert to Parquet.
    Returns the names of the files newly downloaded in this call."""
    new: list[str] = []
    out = Path(raw_dir) / "hmlr"
    out.mkdir(parents=True, exist_ok=True)
    for y, m in _months():
        for _kind, prefix in KINDS.items():
            name = f"{prefix}-{MONTHS[m - 1]}-{y}.csv"
            dest = out / name
            if dest.exists() or _pq_path(raw_dir, name).exists():
                log.info("hmlr: %s exists, skipping", name)
                continue
            try:
                ok = _fetch(f"{BASE_URL}/{name}", dest)
            except requests.RequestException as e:
                log.warning("hmlr: could not fetch %s: %s", name, e)
                continue
            if ok:
                new.append(name)
                log.info("hmlr: downloaded %s", name)
            else:
                log.info("hmlr: %s not published (yet)", name)
    # Defensive: if any zip was supplied/dropped here, unpack data only (never execute).
    for z in out.glob("*.zip"):
        with zipfile.ZipFile(z) as zf:
            for info in zf.infolist():
                base = Path(info.filename).name
                if info.is_dir() or not base.lower().endswith((".csv", ".txt")):
                    continue
                if not (out / base).exists():
                    (out / base).write_bytes(zf.read(info))
    convert_pending(raw_dir)
    return new


def archive_hmlr(raw_dir: Path, today: dt.date | None = None) -> dict:
    """Keep every monthly lookup. HMLR publishes these as 'current month' files that may be replaced or removed,
    so run this at least monthly. Downloads any month not yet archived (as Parquet in <data>/parquet/hmlr/),
    never re-fetches or deletes an archived month. Returns {"new": [...], "archived": [...], "missing": [...]}
    where missing lists expected files (Aug 2026 to the current month) that are neither archived nor
    downloadable; an old gap cannot be recovered, so it is logged as a warning."""
    new = download_hmlr(raw_dir)
    d = Path(raw_dir).parent / "parquet" / "hmlr"
    archived = sorted(p.name for p in d.glob("*.parquet"))
    have = {Path(a).stem for a in archived}
    missing = [
        f"{prefix}-{MONTHS[m - 1]}-{y}"
        for y, m in _months(today)
        for prefix in KINDS.values()
        if f"{prefix}-{MONTHS[m - 1]}-{y}" not in have
    ]
    cur = (today or dt.date.today()).replace(day=1)
    for stem in missing:
        # the current month is often simply not published yet; anything earlier is a permanent gap
        if not stem.endswith(f"-{MONTHS[cur.month - 1]}-{cur.year}"):
            log.warning("hmlr: %s is not archived and could not be downloaded (gap)", stem)
    return {"new": [Path(n).stem + ".parquet" for n in new], "archived": archived, "missing": missing}


_NORM = "'{' || upper(replace(replace(trim(%s), '{', ''), '}', '')) || '}'"


def _pq_path(raw_dir: Path, csv_name: str) -> Path:
    """Parquet copy of a monthly lookup CSV: <raw_dir>/../parquet/hmlr/<name>.parquet."""
    return Path(raw_dir).parent / "parquet" / "hmlr" / (Path(csv_name).stem + ".parquet")


def convert_pending(raw_dir: Path) -> list[Path]:
    """Convert every lookup CSV in raw_dir/hmlr/ to Parquet (transaction_id normalised, value as text), verify the
    row count, then delete the CSV (and any .txt twin). Returns the Parquet files written."""
    import duckdb

    written = []
    for csv in sorted((Path(raw_dir) / "hmlr").glob("*.csv")):
        pq = _pq_path(raw_dir, csv.name)
        pq.parent.mkdir(parents=True, exist_ok=True)
        tmp = pq.with_name(pq.name + ".tmp")
        con = duckdb.connect()
        src = f"""SELECT {_NORM % "c0"} AS transaction_id, trim(c1) AS value
            FROM read_csv('{_q(csv)}', header=false, columns={{'c0':'VARCHAR','c1':'VARCHAR'}},
                          quote='"', delim=',', all_varchar=true, ignore_errors=true)
            WHERE c0 IS NOT NULL"""
        n_src = con.execute(f"SELECT count(*) FROM ({src})").fetchone()[0]
        con.execute(f"COPY ({src}) TO '{_q(tmp)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        n_pq = con.execute(f"SELECT count(*) FROM read_parquet('{_q(tmp)}')").fetchone()[0]
        con.close()
        if n_pq != n_src:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"hmlr: {csv.name}: {n_pq} parquet rows != {n_src} csv rows")
        tmp.replace(pq)
        csv.unlink()
        csv.with_suffix(".txt").unlink(missing_ok=True)
        log.info("hmlr: converted %s -> %s (%d rows)", csv.name, pq, n_pq)
        written.append(pq)
    return written


def _q(p) -> str:
    return str(p).replace("'", "''")


def _files(raw_dir: Path, prefix: str) -> list[str]:
    convert_pending(raw_dir)
    d = Path(raw_dir).parent / "parquet" / "hmlr"
    return sorted(str(p) for p in d.glob(f"{prefix}-*.parquet"))


def _read(con, files: list[str], col2: str, typ: str) -> None:
    """Create temp view `t` (transaction_id VARCHAR, <col2>) from the converted lookup Parquet files."""
    lst = ", ".join("'" + _q(f) + "'" for f in files)
    con.execute(
        f"""CREATE OR REPLACE TEMP VIEW t AS
        SELECT transaction_id, TRY_CAST(value AS {typ}) AS {col2}
        FROM read_parquet([{lst}])"""
    )


def stage_hmlr(lad: str, raw_dir: Path, staged_dir: Path) -> None:
    """Stage identifier lookups for one LAD, filtered to its ONSUD UPRNs."""
    sdir = Path(staged_dir) / lad
    onsud = sdir / "onsud.parquet"
    if not onsud.exists():
        raise FileNotFoundError(f"{onsud} missing; stage ONSUD first")
    # remove outputs of an earlier run first so that an absent source (or a failure) leaves absence, not stale data
    for stale in (
        "hmlr_ppd_uprn.parquet",
        "hmlr_inspire.parquet",
        "hmlr_ppd_uprn.parquet.tmp",
        "hmlr_inspire.parquet.tmp",
    ):
        (sdir / stale).unlink(missing_ok=True)
    uprn_files = _files(raw_dir, KINDS["uprn"])
    if not uprn_files:
        log.warning("hmlr: no UPRN lookup files in %s/hmlr; skipping", raw_dir)
        return
    con = duckdb.connect()
    con.execute(f"CREATE TEMP TABLE lad_uprn AS SELECT DISTINCT uprn FROM read_parquet('{onsud}')")
    _read(con, uprn_files, "uprn", "BIGINT")
    con.execute(
        """CREATE TEMP TABLE ppd AS
        SELECT DISTINCT t.transaction_id, t.uprn::BIGINT AS uprn FROM t
        JOIN lad_uprn l ON l.uprn = t.uprn"""
    )
    n = con.execute("SELECT count(*) FROM ppd").fetchone()[0]
    ppd_tmp = sdir / "hmlr_ppd_uprn.parquet.tmp"
    con.execute(
        f"COPY (SELECT * FROM ppd ORDER BY transaction_id, uprn) TO '{ppd_tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    ppd_tmp.replace(sdir / "hmlr_ppd_uprn.parquet")
    log.info("hmlr: %s ppd-uprn rows for %s", n, lad)

    insp_files = _files(raw_dir, KINDS["inspire"])
    if not insp_files:
        log.warning("hmlr: no INSPIRE lookup files found; skipping hmlr_inspire.parquet")
        return
    _read(con, insp_files, "inspire_id", "VARCHAR")
    insp_tmp = sdir / "hmlr_inspire.parquet.tmp"
    con.execute(
        f"""COPY (SELECT DISTINCT t.transaction_id, t.inspire_id::VARCHAR AS inspire_id FROM t
                  WHERE t.inspire_id IS NOT NULL
                    AND t.transaction_id IN (SELECT transaction_id FROM ppd)
                  ORDER BY 1, 2)
            TO '{insp_tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)"""
    )
    insp_tmp.replace(sdir / "hmlr_inspire.parquet")
    con.close()
