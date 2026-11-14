"""Registers of Scotland INSPIRE Cadastral Parcels (Land Register ownership polygons, Scotland).

Licence: Open Government Licence v3 (the RoS download page https://ros-inspire.themapcloud.com/ links
"Open Government Licence" to https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
Attribution required by the catalogue record (spatialdata.gov.scot): "(c) Crown copyright. Reproduced with the
permission of Registers of Scotland." and "Contains OS data (c) Crown copyright and database right (year)."
Source: one zip per registration county (33), ``/maps/download/ros-cp.cadastralparcel/<CODE>`` on the RoS
download host; each holds ``<CODE>_bng.shp`` (EPSG:27700) and ``<CODE>_etrs89.shp`` (same data, ETRS89).
Only the ``_bng`` shapefile is read; downloads are treated as untrusted data.

Pipeline: ``download_ros`` writes ``data/parquet/ros/<code>.parquet`` (inspire_id VARCHAR, geom_wkb BLOB,
EPSG:27700). ``stage_ros`` joins a Scottish LAD's UPRNs to those polygons and writes the same files as
``inspire.stage_inspire`` plus ``parcel_source='ros'``.
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

from .convert import _append_manifest, _connect, _copy, _free_gb, _load_spatial, _q

log = logging.getLogger(__name__)

DOWNLOAD_URL = "https://ros-inspire.themapcloud.com/maps/download/ros-cp.cadastralparcel"
LICENCE = "OGL-3.0"
LICENCE_URL = "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
ATTRIBUTION = (
    "© Crown copyright. Reproduced with the permission of Registers of Scotland. "
    "Contains OS data © Crown copyright and database right."
)
MIN_FREE_GB = 15
TIMEOUT = 120
COUNTIES = (
    "ABN ANG ARG AYR BNF BER BUT CTH CLK DMB DMF ELN FFE GLA INV KNC KNR KRK LAN MID MOR NRN OAZ PBL PTH "
    "REN ROS ROX SEL STG STH WLN WGN"
).split()
_CODE = re.compile(r"[A-Z]{3}")


def _extract_shp(zip_path: Path, code: str, out_dir: Path) -> Path:
    """Extract only the ``<code>_bng`` shapefile parts (flattened names, no path traversal)."""
    want = {".shp", ".shx", ".dbf", ".prj"}
    found = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            p = Path(info.filename)
            if info.is_dir() or p.stem.lower() != f"{code.lower()}_bng" or p.suffix.lower() not in want:
                continue
            with zf.open(info) as src, open(out_dir / f"parcels{p.suffix.lower()}", "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            found += 1
    shp = out_dir / "parcels.shp"
    if not shp.exists() or not (out_dir / "parcels.dbf").exists():
        raise ValueError(f"no {code}_bng shapefile in {zip_path.name}")
    return shp


def convert_ros(shp: Path, out: Path, parquet_dir: Path, source: str) -> int:
    """Convert one county shapefile to parquet (inspire_id VARCHAR, geom_wkb BLOB), verify count, manifest."""
    con = _connect(parquet_dir)
    try:
        _load_spatial(con)
        n_src = int(con.execute(f"SELECT count(*) FROM ST_Read({_q(shp)})").fetchone()[0])
        desc = con.execute(f"DESCRIBE SELECT * FROM ST_Read({_q(shp)})").fetchall()
        names = {r[0].lower(): r[0] for r in desc}
        gcols = [r[0] for r in desc if str(r[1]).upper().startswith("GEOMETRY")]
        if len(gcols) != 1 or "inspireid" not in names:
            raise ValueError(f"inspireid/geometry missing in {source}: {[r[0] for r in desc]}")
        crs = [str(r[1]) for r in desc if r[0] == gcols[0]][0]
        if "27700" not in crs:
            raise ValueError(f"{source}: expected EPSG:27700, got {crs}")
        sql = f"""SELECT CAST("{names["inspireid"]}" AS VARCHAR) AS inspire_id,
                         ST_AsWKB("{gcols[0]}") AS geom_wkb
                  FROM ST_Read({_q(shp)}) WHERE "{gcols[0]}" IS NOT NULL AND NOT ST_IsEmpty("{gcols[0]}")"""
        n = _copy(con, sql, out)
        if n_src - n:
            log.warning("ros: %s: %d features without geometry dropped", source, n_src - n)
        migrated = None
        if "date_migra" in names:
            migrated = str(con.execute(f'SELECT max("{names["date_migra"]}") FROM ST_Read({_q(shp)})').fetchone()[0])
    finally:
        con.close()
    _append_manifest(
        parquet_dir,
        str(out.relative_to(parquet_dir)),
        {
            "dataset": "ros",
            "source": source,
            "source_size": shp.stat().st_size,
            "rows": n,
            "source_features": n_src,
            "parquet_size": out.stat().st_size,
            "converted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "data_date": migrated,
            "crs": "EPSG:27700",
            "licence": LICENCE,
            "licence_url": LICENCE_URL,
            "attribution": ATTRIBUTION,
        },
    )
    return n


def _fetch(session: requests.Session, code: str, dest: Path, retries: int = 3) -> None:
    part = dest.with_suffix(".zip.part")
    for attempt in range(retries):
        try:
            with session.get(f"{DOWNLOAD_URL}/{code}", stream=True, timeout=TIMEOUT) as r:
                r.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            if not zipfile.is_zipfile(part):
                raise ValueError(f"{code}: download is not a zip")
            part.replace(dest)
            return
        except (requests.RequestException, ValueError):
            part.unlink(missing_ok=True)
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def download_ros(raw_dir: Path, parquet_dir: Path | None = None, only: list[str] | None = None) -> None:
    """Download each registration county and convert to ``parquet_dir/ros/<code>.parquet``.

    One county at a time (download -> extract -> convert -> delete). Skips counties already converted.
    ``only`` restricts to the given county codes. Aborts if free disk < 15 GB.
    """
    raw = Path(raw_dir) / "ros"
    raw.mkdir(parents=True, exist_ok=True)
    pq_dir = Path(parquet_dir) if parquet_dir else Path(raw_dir).parent / "parquet"
    (pq_dir / "ros").mkdir(parents=True, exist_ok=True)
    codes = [c for c in COUNTIES if not only or c in {o.upper() for o in only}]
    session = requests.Session()
    for code in codes:
        out = pq_dir / "ros" / f"{code.lower()}.parquet"
        if out.exists():
            log.info("ros: %s already converted", code)
            continue
        free = _free_gb(raw)
        if free < MIN_FREE_GB:
            raise RuntimeError(f"only {free:.1f} GB free; need at least {MIN_FREE_GB} GB. Aborting.")
        zpath = raw / f"{code}.zip"
        work = Path(tempfile.mkdtemp(prefix="shp_", dir=raw))
        try:
            _fetch(session, code, zpath)
            shp = _extract_shp(zpath, code, work)
            n = convert_ros(shp, out, pq_dir, f"{code}.zip")
            log.info("ros: %s -> %d polygons", code, n)
        finally:
            shutil.rmtree(work, ignore_errors=True)
            zpath.unlink(missing_ok=True)
            zpath.with_suffix(".zip.part").unlink(missing_ok=True)


def _has_rows(path: Path) -> bool:
    if not path.exists():
        return False
    con = _connect(path.parent.parent.parent / "parquet")
    try:
        return int(con.execute(f"SELECT count(*) FROM read_parquet({_q(path)})").fetchone()[0]) > 0
    finally:
        con.close()


def stage_ros(lad: str, raw_dir: Path, staged_dir: Path, files: list | None = None) -> None:
    """Point-in-polygon of a Scottish LAD's UPRNs against RoS parcels.

    Same outputs as ``inspire.stage_inspire`` (``inspire_parcel.parquet``: uprn, parcel_inspire_id,
    parcel_area_m2, uprns_in_parcel; ``inspire_polygons.parquet``) plus ``parcel_source`` = 'ros'. Only for
    LAD codes starting with 'S', and never overwrites an existing inspire_parcel.parquet that has rows.
    """
    if not lad.startswith("S"):
        log.info("ros: %s is not a Scottish LAD; skipping", lad)
        return
    sdir = Path(staged_dir) / lad
    target = sdir / "inspire_parcel.parquet"
    if _has_rows(target):
        log.info("ros: %s already has inspire_parcel.parquet; not overwriting", lad)
        return
    pq_root = Path(raw_dir).parent / "parquet"
    if files is None:
        files = sorted((pq_root / "ros").glob("*.parquet"))
    if not files:
        log.warning("ros: no RoS parquet in %s; skipping", pq_root / "ros")
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
            log.warning("ros: no UPRN points for %s", lad)
            return
        x0, y0, x1, y1, _ = bb
        lst = ", ".join(_q(f) for f in files)
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
                      (count(*) OVER (PARTITION BY inspire_id))::INTEGER AS uprns_in_parcel,
                      'ros'::VARCHAR AS parcel_source
               FROM best ORDER BY uprn""",
            target,
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
