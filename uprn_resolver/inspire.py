"""HM Land Registry INSPIRE Index Polygons (registered freehold parcels, England & Wales).

Licence: OGL v3 with Crown copyright + Ordnance Survey attribution (since July 2020).
Source: https://use-land-property-data.service.gov.uk/datasets/inspire/download  (one zip per local
authority, each containing ``Land_Registry_Cadastral_Parcels.gml`` in EPSG:27700; no login is needed - the
service issues an anonymous session and redirects to a short-lived signed S3 URL).

Pipeline: ``download_inspire`` (download -> convert -> delete, one council at a time) writes
``data/parquet/inspire/<council_slug>.parquet`` (inspire_id VARCHAR, geom_wkb BLOB). ``stage_inspire`` joins a
LAD's UPRN points to those polygons. Downloads are treated as untrusted data (only the .gml member is read).
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

import requests

from .convert import (
    _append_manifest,
    _connect,
    _copy,
    _free_gb,
    _load_spatial,
    _q,
)

log = logging.getLogger(__name__)

BASE_URL = "https://use-land-property-data.service.gov.uk"
LIST_URL = f"{BASE_URL}/datasets/inspire/download"
MIN_FREE_GB = 15
TIMEOUT = 120
_SCHEMA_LOC = re.compile(rb'\s+xsi:schemaLocation="[^"]*"')
_LINK = re.compile(r'href="/datasets/inspire/download/([A-Za-z0-9_.\-]+\.zip)"')


def _slug(zip_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", Path(zip_name).stem.lower()).strip("_")


def list_councils(session: requests.Session | None = None) -> list[str]:
    """Return the zip file names offered on the download page (sorted, de-duplicated)."""
    s = session or requests.Session()
    r = s.get(LIST_URL, timeout=TIMEOUT)
    r.raise_for_status()
    return sorted(set(_LINK.findall(r.text)))


def _fetch(session: requests.Session, name: str, dest: Path, retries: int = 3) -> None:
    part = dest.with_suffix(".zip.part")
    for attempt in range(retries):
        try:
            with session.get(f"{LIST_URL}/{name}", stream=True, timeout=TIMEOUT) as r:
                r.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            if not zipfile.is_zipfile(part):
                raise ValueError(f"{name}: download is not a zip")
            part.replace(dest)
            return
        except (requests.RequestException, ValueError):
            part.unlink(missing_ok=True)
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def _extract_gml(zip_path: Path, out_dir: Path) -> Path:
    """Extract only the .gml member (flattened name, no path traversal) into out_dir."""
    with zipfile.ZipFile(zip_path) as zf:
        members = [i for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith(".gml")]
        if not members:
            raise ValueError(f"no .gml in {zip_path.name}")
        info = max(members, key=lambda i: i.file_size)
        dest = out_dir / "parcels.gml"
        with zf.open(info) as src, open(dest, "wb") as dst:
            head = src.read(1 << 16)
            # The header names an unresolvable internal host in xsi:schemaLocation; GDAL tries to fetch it and
            # can hang for minutes. Drop the attribute (data is unaffected).
            head = _SCHEMA_LOC.sub(b"", head, count=1)
            dst.write(head)
            shutil.copyfileobj(src, dst, 1 << 20)
    return dest


def _gml_count(con, gml: Path) -> int:
    return int(con.execute(f"SELECT count(*) FROM ST_Read({_q(gml)})").fetchone()[0])


def convert_inspire(gml: Path, out: Path, parquet_dir: Path, source: str) -> int:
    """Convert one council GML to parquet (inspire_id VARCHAR, geom_wkb BLOB), verify count, record manifest."""
    con = _connect(parquet_dir)
    try:
        _load_spatial(con)
        n_src = _gml_count(con, gml)
        desc = con.execute(f"DESCRIBE SELECT * FROM ST_Read({_q(gml)})").fetchall()
        cols = [r[0].upper() for r in desc]
        gcols = [r[0] for r in desc if str(r[1]).upper().startswith("GEOMETRY")]
        if len(gcols) != 1 or "INSPIREID" not in cols:
            raise ValueError(f"INSPIREID column missing in {source}: {cols}")
        sql = f"""SELECT CAST(INSPIREID AS BIGINT)::VARCHAR AS inspire_id, ST_AsWKB("{gcols[0]}") AS geom_wkb
                  FROM ST_Read({_q(gml)}) WHERE "{gcols[0]}" IS NOT NULL"""
        n = _copy(con, sql, out)
        n_null = n_src - n
        if n_null:
            log.warning("inspire: %s: %d features without geometry dropped", source, n_null)
    finally:
        con.close()
    _append_manifest(
        parquet_dir,
        str(out.relative_to(parquet_dir)),
        {
            "dataset": "inspire",
            "source": source,
            "source_size": gml.stat().st_size,
            "rows": n,
            "source_features": n_src,
            "parquet_size": out.stat().st_size,
            "converted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )
    return n


def download_inspire(raw_dir: Path, parquet_dir: Path | None = None, only: list[str] | None = None) -> None:
    """Download every council's INSPIRE zip and convert to ``parquet_dir/inspire/<slug>.parquet``.

    One council at a time: download -> extract GML -> convert -> verify -> delete zip and GML. Skips
    councils already converted (parquet present). ``only`` restricts to names containing any of the given
    substrings (case-insensitive), e.g. ``["islington"]``. Aborts if free disk < 15 GB.
    """
    raw = Path(raw_dir) / "inspire"
    raw.mkdir(parents=True, exist_ok=True)
    pq_dir = Path(parquet_dir) if parquet_dir else Path(raw_dir).parent / "parquet"
    (pq_dir / "inspire").mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    names = list_councils(session)
    if only:
        names = [n for n in names if any(o.lower() in n.lower() for o in only)]
    log.info("inspire: %d councils", len(names))
    # Islington first (reference LAD), then the rest.
    names.sort(key=lambda n: (0 if "islington" in n.lower() else 1, n))
    for name in names:
        out = pq_dir / "inspire" / f"{_slug(name)}.parquet"
        if out.exists():
            log.info("inspire: %s already converted", name)
            continue
        free = _free_gb(raw)
        if free < MIN_FREE_GB:
            raise RuntimeError(f"only {free:.1f} GB free; need at least {MIN_FREE_GB} GB. Aborting.")
        zpath = raw / name
        work = Path(tempfile.mkdtemp(prefix="gml_", dir=raw))
        try:
            _fetch(session, name, zpath)
            gml = _extract_gml(zpath, work)
            n = convert_inspire(gml, out, pq_dir, name)
            log.info("inspire: %s -> %d polygons", name, n)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            zpath.unlink(missing_ok=True)
            zpath.with_suffix(".zip.part").unlink(missing_ok=True)


def _clear_stale_parcels(sdir: Path) -> None:
    """Remove a previous run's parcel outputs so an absent source leaves absence, not stale data.
    A Registers of Scotland parcel file (``parcel_source`` column, written by ``ros.stage_ros``) is kept."""
    target = sdir / "inspire_parcel.parquet"
    if target.exists():
        try:
            import pyarrow.parquet as pq

            if "parcel_source" in pq.read_schema(target).names:
                return
        except Exception:  # noqa: BLE001 - unreadable file is stale by definition
            pass
    for name in ("inspire_parcel.parquet", "inspire_polygons.parquet", "inspire_parcel.parquet.tmp",
                 "inspire_polygons.parquet.tmp"):  # fmt: skip
        (sdir / name).unlink(missing_ok=True)


def stage_inspire(lad: str, raw_dir: Path, staged_dir: Path, files: list | None = None) -> None:
    """Point-in-polygon of a LAD's UPRNs against INSPIRE parcels.

    Writes ``staged_dir/<lad>/inspire_parcel.parquet`` (uprn, parcel_inspire_id, parcel_area_m2,
    uprns_in_parcel) - the smallest-area polygon wins when a point is in several - and
    ``inspire_polygons.parquet`` (parcel_inspire_id, geom_wkb) for matched parcels only, which ``parcels.py``
    publishes as ``parcels.parquet`` (OGL, with the HM Land Registry and Ordnance Survey statements).
    Stale outputs of an earlier run are removed first; then a
    no-op (with a warning) if no INSPIRE parquet has been converted.
    """
    sdir = Path(staged_dir) / lad
    pq_root = Path(raw_dir).parent / "parquet"
    _clear_stale_parcels(sdir)
    if files is None:
        files = sorted((pq_root / "inspire").glob("*.parquet"))
    if not files:
        log.warning("inspire: no INSPIRE parquet in %s; skipping", pq_root / "inspire")
        return
    pts = sdir / "open_uprn.parquet"
    onsud = sdir / "onsud.parquet"
    if pts.exists():
        pts_sql = f"SELECT uprn, x, y FROM read_parquet({_q(pts)})"
    else:
        nat = pq_root / "open_uprn" / "open_uprn.parquet"
        if not nat.exists() or not onsud.exists():
            raise FileNotFoundError(f"need {pts} or ({nat} and {onsud})")
        pts_sql = (
            f"SELECT u.uprn, u.x, u.y FROM read_parquet({_q(nat)}) u "
            f"WHERE u.uprn IN (SELECT uprn FROM read_parquet({_q(onsud)}))"
        )
    con = _connect(pq_root)
    try:
        _load_spatial(con)
        con.execute(f"CREATE TEMP TABLE pts AS {pts_sql}")
        bb = con.execute("SELECT min(x)-200, min(y)-200, max(x)+200, max(y)+200, count(*) FROM pts").fetchone()
        if not bb[4]:
            log.warning("inspire: no UPRN points for %s", lad)
            return
        x0, y0, x1, y1, _ = bb
        lst = ", ".join(_q(f) for f in files)
        # Parquet files carry no bbox column; the cheap filter is done on the geometry envelope.
        con.execute(
            f"""CREATE TEMP TABLE polys AS
            SELECT inspire_id, geom, ST_Area(geom) AS area FROM (
                SELECT inspire_id, ST_GeomFromWKB(geom_wkb) AS geom FROM read_parquet([{lst}])
            ) WHERE ST_XMax(geom) >= {x0} AND ST_XMin(geom) <= {x1}
                AND ST_YMax(geom) >= {y0} AND ST_YMin(geom) <= {y1}"""
        )
        con.execute(
            """CREATE TEMP TABLE hits AS
            SELECT p.uprn, g.inspire_id, g.area
            FROM pts p JOIN polys g ON ST_Intersects(g.geom, ST_Point(p.x, p.y))"""
        )
        con.execute(
            """CREATE TEMP TABLE best AS
            SELECT uprn, inspire_id, area FROM (
                SELECT *, row_number() OVER (PARTITION BY uprn ORDER BY area, inspire_id) AS rn FROM hits
            ) WHERE rn = 1"""
        )
        sdir.mkdir(parents=True, exist_ok=True)
        _copy(
            con,
            """SELECT uprn::BIGINT AS uprn, inspire_id AS parcel_inspire_id, area::DOUBLE AS parcel_area_m2,
                      (count(*) OVER (PARTITION BY inspire_id))::INTEGER AS uprns_in_parcel
               FROM best ORDER BY uprn""",
            sdir / "inspire_parcel.parquet",
        )
        _copy(
            con,
            """SELECT inspire_id AS parcel_inspire_id, any_value(ST_AsWKB(geom)) AS geom_wkb
               FROM polys WHERE inspire_id IN (SELECT inspire_id FROM best) GROUP BY inspire_id
               ORDER BY inspire_id""",
            sdir / "inspire_polygons.parquet",
        )
    finally:
        con.close()
