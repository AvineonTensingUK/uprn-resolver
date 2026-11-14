"""Microsoft GlobalML Building Footprints for the UK: download, convert, and per-LAD outline match.

Source and licence (checked 2026-10-22 on Microsoft's own repository, not Overture's ODbL copy):

* https://github.com/microsoft/GlobalMLBuildingFootprints README, section "License": "This data is licensed by
  Microsoft under the CDLA Permissive 2.0" (https://cdla.dev/permissive-2-0/). Attribution is the
  CDLA-Permissive-2.0 notice: keep the licence text / attribution "Contains Microsoft Building Footprints
  (GlobalMLBuildingFootprints), licensed under CDLA-Permissive-2.0".
* Tile index: ``dataset-links.csv`` (README link, release 2026-08-13), columns
  ``Location,QuadKey,Url,Size,UploadDate``. Region ``UnitedKingdom`` has 194 level-9 quadkey tiles; Northern
  Ireland sits partly in ``UnitedKingdom`` tiles and the Republic in ``Ireland`` tiles (not used here).
* Tiles are line-delimited GeoJSON (WGS84) with ``properties.height`` and ``properties.confidence``; height is
  not a goal and is dropped. ``confidence`` is kept only when the tile carries real values (not all -1).

Layout (under ``parquet_dir``):

* ``msbuildings/gb/<quadkey>.parquet``: GeoParquet, ``geometry`` WKB in EPSG:27700, ``ms_id`` (first 16 hex chars of
  md5 of the stored WKB), ``area_m2``, a ``bbox`` struct (CRS units, GeoParquet covering) and WGS84
  ``lon_min/lat_min/lon_max/lat_max``.
* ``msbuildings/ni/<quadkey>.parquet``: same schema in EPSG:29903 (Irish Grid) for the NI agent.
  A polygon in a tile that spans both is routed to NI by centroid (lon < -5.3 and lat < 55.35, and for the
  Kintyre tile also lon < -5.9), otherwise GB.
* Each tile is recorded in ``parquet_dir/manifest.json`` under ``converted`` (written under an fcntl lock on
  ``parquet_dir/.manifest.lock``) with the licence and attribution.

GB reprojection uses the OSTN15 grid via ``bng.duckdb_st_transform_sql`` (plain DuckDB EPSG:27700 is Helmert-only,
~2 m out); non-finite results are dropped and counted. NI uses DuckDB's EPSG:29903 transform (no grid).

Outline matching mirrors ``build.py`` (see ``ms_outline_for_lad``).
"""

from __future__ import annotations

import csv
import fcntl
import gzip
import json
import math
import os
import shutil
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from . import bng
from .build import EPSG_27700_PROJJSON, begin_outputs, lad_out_dir, write_text_atomic

LICENCE = "CDLA-Permissive-2.0"
LICENCE_URL = "https://cdla.dev/permissive-2-0/"
ATTRIBUTION = "Contains Microsoft Building Footprints (GlobalMLBuildingFootprints), licensed under CDLA-Permissive-2.0"
REPO_URL = "https://github.com/microsoft/GlobalMLBuildingFootprints"
LINKS_URL = "https://bfppub.blob.core.windows.net/%24web/2026-08-13/dataset-links.csv"
REGION = "UnitedKingdom"
MIN_FREE_GB = 15
NEAREST_M = 5.0
ROW_GROUP_SIZE = 122880
NI_BBOX = (-8.2, 54.0, -5.4, 55.4)  # lon_min, lat_min, lon_max, lat_max (generous, used to find candidate tiles)
MS_DIR = "msbuildings"

LICENCE_TEXT = f"""Microsoft Building Footprints (GlobalMLBuildingFootprints)

Licence: {LICENCE} ({LICENCE_URL})
Source:  {REPO_URL}
Attribution: {ATTRIBUTION}

The polygons in outline_ms_buildings.parquet are derived from Microsoft's footprints (reprojected to EPSG:27700).
The per-UPRN columns in outline_ms.parquet are derived from them by spatial join with OS Open UPRN / Open TOID.
"""


# --------------------------------------------------------------------------- helpers
def _q(path: Path | str) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _connect(memory_limit: str = "4GB", temp_dir: Path | None = None, threads: int | None = None):
    con = duckdb.connect()
    try:
        con.execute("LOAD spatial")
    except duckdb.Error:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
    con.execute(f"SET memory_limit='{memory_limit}'")
    if temp_dir is not None:
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory={_q(temp_dir)}")
    if threads:
        con.execute(f"SET threads={int(threads)}")
    con.execute("SET preserve_insertion_order=false")
    return con


def _geom_expr(con, source: str, col: str = "geometry") -> str:
    """SQL geometry expression for a WKB column; DuckDB may expose GeoParquet columns as GEOMETRY already."""
    typ = str(con.execute(f"SELECT column_type FROM (DESCRIBE SELECT {col} FROM {source})").fetchone()[0])
    return col if typ.startswith("GEOMETRY") else f"ST_GeomFromWKB({col})"


def free_gb(path: Path) -> float:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1e9


def quadkey_bounds(qk: str) -> tuple[float, float, float, float]:
    """(lon_min, lat_min, lon_max, lat_max) of a Bing quadkey tile."""
    x = y = 0
    for c in qk:
        d = int(c)
        x = x * 2 + (d & 1)
        y = y * 2 + (d >> 1)
    n = 2 ** len(qk)

    def lat(t: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * t / n))))

    return x / n * 360 - 180, lat(y + 1), (x + 1) / n * 360 - 180, lat(y)


def _intersects(a, b) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _crs_json(epsg: int) -> dict:
    if epsg == 27700:
        return EPSG_27700_PROJJSON
    from pyproj import CRS  # dev/geopandas dependency, only needed for NI

    return CRS.from_epsg(epsg).to_json_dict()


def _geo_meta(epsg: int, bbox: list[float] | None) -> str:
    col: dict = {
        "encoding": "WKB",
        "geometry_types": ["Polygon", "MultiPolygon"],
        "crs": _crs_json(epsg),
        "covering": {"bbox": {k: ["bbox", k] for k in ("xmin", "ymin", "xmax", "ymax")}},
    }
    if bbox:
        col["bbox"] = bbox
    return json.dumps({"version": "1.1.0", "primary_column": "geometry", "columns": {"geometry": col}})


def _read_manifest(parquet_dir: Path) -> dict:
    """The conversion manifest ({} if absent). A corrupt file is moved to ``manifest.json.corrupt`` and a
    RuntimeError is raised: silently resetting it would erase every converted/downloads entry."""
    mpath = Path(parquet_dir) / "manifest.json"
    if not mpath.exists():
        return {}
    try:
        return json.loads(mpath.read_text())
    except json.JSONDecodeError as e:
        bad = mpath.with_name("manifest.json.corrupt")
        os.replace(mpath, bad)
        raise RuntimeError(
            f"{mpath} is corrupt ({e}); moved to {bad} - restore it from a backup before continuing"
        ) from e


def _append_manifest(parquet_dir: Path, key: str, entry: dict) -> None:
    mpath = Path(parquet_dir) / "manifest.json"
    with open(Path(parquet_dir) / ".manifest.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read_manifest(parquet_dir)
        data.setdefault("converted", {})[key] = entry
        tmp = mpath.with_name(f"manifest.json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        os.replace(tmp, mpath)


# --------------------------------------------------------------------------- tile index
def fetch_links(dest: Path, url: str = LINKS_URL) -> Path:
    """Download dataset-links.csv to ``dest`` (small; parsed only with the csv module)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:  # noqa: S310
        shutil.copyfileobj(r, f)
    return dest


def parse_size(s: str) -> float:
    s = s.strip().upper()
    for suf, mult in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3), ("B", 1.0)):
        if s.endswith(suf):
            return float(s[: -len(suf)]) * mult
    return float(s)


def uk_tiles(links_csv: Path, region: str = REGION) -> list[dict]:
    """Tiles of ``region`` from dataset-links.csv, each with bounds and an ``spans_ni`` flag."""
    out = []
    with open(links_csv, newline="") as f:
        for r in csv.DictReader(f):
            if r["Location"] != region:
                continue
            b = quadkey_bounds(r["QuadKey"])
            out.append(
                {
                    "quadkey": r["QuadKey"],
                    "url": r["Url"],
                    "size_bytes": parse_size(r["Size"]),
                    "upload_date": r["UploadDate"],
                    "bounds": b,
                    "spans_ni": _intersects(b, NI_BBOX),
                }
            )
    return sorted(out, key=lambda t: t["quadkey"])


def _ni_predicate(qk: str) -> str:
    """SQL predicate on lon/lat centroid columns (``clon``, ``clat``): polygon belongs to NI."""
    if qk == "031310113":  # Antrim glens + Mull of Kintyre
        return "clon < -5.9 AND clat < 55.35"
    return "clon < -5.3 AND clat < 55.35"


# --------------------------------------------------------------------------- download
def download_tile(tile: dict, raw_dir: Path, retries: int = 3) -> Path:
    """Download one tile gzip to ``raw_dir/<quadkey>/tile.csv.gz`` (untrusted; only read by DuckDB).

    Aborts (RuntimeError) when free disk space is below ``MIN_FREE_GB``. Verifies the gzip stream.
    """
    d = Path(raw_dir) / tile["quadkey"]
    d.mkdir(parents=True, exist_ok=True)
    dest = d / "tile.csv.gz"
    if free_gb(d) < MIN_FREE_GB:
        raise RuntimeError(f"free disk {free_gb(d):.1f} GB < {MIN_FREE_GB} GB; aborting download")
    last: Exception | None = None
    for attempt in range(retries):
        try:
            part = dest.with_name("tile.csv.gz.part")
            with urllib.request.urlopen(tile["url"], timeout=300) as r, open(part, "wb") as f:  # noqa: S310
                shutil.copyfileobj(r, f, 1 << 20)
            with gzip.open(part, "rb") as g:  # integrity check
                while g.read(1 << 22):
                    pass
            os.replace(part, dest)
            return dest
        except Exception as e:  # retry network / truncated downloads
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"download failed for {tile['quadkey']}: {last}")


# --------------------------------------------------------------------------- convert
def convert_tile(
    raw_gz: Path,
    quadkey: str,
    parquet_dir: Path,
    *,
    spans_ni: bool = False,
    size_bytes: float = 0,
    url: str = "",
    upload_date: str = "",
    memory_limit: str = "6GB",
) -> dict:
    """Convert one raw tile to ``gb/<qk>.parquet`` and (if any polygons fall in NI) ``ni/<qk>.parquet``.

    Verifies the written Parquet row counts against the source before returning; the caller deletes the raw
    file afterwards. Returns ``{"gb": n, "ni": n, "dropped": n, "seconds": s}``.
    """
    written: list[Path] = []
    try:
        return _convert_tile(
            raw_gz,
            quadkey,
            parquet_dir,
            spans_ni=spans_ni,
            size_bytes=size_bytes,
            url=url,
            upload_date=upload_date,
            memory_limit=memory_limit,
            written=written,
        )
    except BaseException:
        for f in written:  # outputs of a tile that failed part-way are never left behind unmanifested
            f.unlink(missing_ok=True)
        raise


def _convert_tile(
    raw_gz: Path,
    quadkey: str,
    parquet_dir: Path,
    *,
    spans_ni: bool = False,
    size_bytes: float = 0,
    url: str = "",
    upload_date: str = "",
    memory_limit: str = "6GB",
    written: list[Path],
) -> dict:
    """Body of ``convert_tile``; appends each output it replaces to ``written``."""
    t0 = time.time()
    parquet_dir = Path(parquet_dir)
    con = _connect(memory_limit, parquet_dir.parent / "tmp")
    src = (
        f"read_json({_q(raw_gz)}, format='newline_delimited', columns={{properties: "
        f"'STRUCT(height DOUBLE, confidence DOUBLE)', geometry: 'JSON'}}, compression='gzip')"
    )
    n_src = con.execute(f"SELECT count(*) FROM {src}").fetchone()[0]
    has_conf = bool(
        con.execute(
            f"SELECT count(*) FROM {src} WHERE properties.confidence IS NOT NULL AND properties.confidence >= 0"
        ).fetchone()[0]
    )
    conf = ", confidence" if has_conf else ""
    conf_in = ", CASE WHEN properties.confidence >= 0 THEN properties.confidence END AS confidence" if has_conf else ""
    con.execute(f"""
        CREATE TEMP TABLE w AS
        SELECT g AS gw, ST_Centroid(g) AS c{conf_in}
        FROM (SELECT ST_MakeValid(ST_GeomFromGeoJSON(geometry::VARCHAR)) AS g, properties FROM {src})
        WHERE g IS NOT NULL AND NOT ST_IsEmpty(g)
    """)
    n_valid = con.execute("SELECT count(*) FROM w").fetchone()[0]
    con.execute("ALTER TABLE w ADD COLUMN clon DOUBLE; ALTER TABLE w ADD COLUMN clat DOUBLE")
    con.execute("UPDATE w SET clon = ST_X(c), clat = ST_Y(c)")
    ni_pred = _ni_predicate(quadkey) if spans_ni else "false"
    con.execute(f"ALTER TABLE w ADD COLUMN is_ni BOOLEAN; UPDATE w SET is_ni = ({ni_pred})")
    b = quadkey_bounds(quadkey)
    hil = f"{{min_x: {b[0]}, min_y: {b[1]}, max_x: {b[2]}, max_y: {b[3]}}}::BOX_2D"
    counts = {}
    pending: list[tuple[str, dict]] = []  # manifest entries, written only once every output is verified
    outside = 0  # polygons whose transform was not finite (outside the OSTN15 grid); dropped
    for name, epsg, flag in (("gb", 27700, "false"), ("ni", 29903, "true")):
        n = con.execute(f"SELECT count(*) FROM w WHERE is_ni = {flag}").fetchone()[0]
        out = parquet_dir / MS_DIR / name / f"{quadkey}.parquet"
        if n == 0:
            out.unlink(missing_ok=True)
            counts[name] = 0
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".tmp")
        tmp.unlink(missing_ok=True)
        # GB: OSTN15 grid via bng (DuckDB's plain EPSG:27700 transform is Helmert-only, ~2 m out); NI unchanged
        tf = (
            bng.duckdb_st_transform_sql("gw")
            if epsg == 27700
            else "ST_Transform(gw, 'EPSG:4326', 'EPSG:29903', always_xy := true)"
        )
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE o AS
            SELECT substr(md5(wkb), 1, 16) AS ms_id, wkb AS geometry, ST_Area(p) AS area_m2,
                   {{xmin: ST_XMin(p), ymin: ST_YMin(p), xmax: ST_XMax(p), ymax: ST_YMax(p)}} AS bbox,
                   lon_min, lat_min, lon_max, lat_max{conf}, clon, clat
            FROM (SELECT p, ST_AsWKB(p)::BLOB AS wkb, ST_XMin(gw) AS lon_min, ST_YMin(gw) AS lat_min,
                         ST_XMax(gw) AS lon_max, ST_YMax(gw) AS lat_max{conf}, clon, clat
                  FROM (SELECT *, {tf} AS p
                        FROM w WHERE is_ni = {flag}))
            WHERE isfinite(ST_XMin(p)) AND isfinite(ST_YMin(p)) AND isfinite(ST_XMax(p)) AND isfinite(ST_YMax(p))
        """)
        n_ok = con.execute("SELECT count(*) FROM o").fetchone()[0]
        outside += n - n_ok
        n = n_ok
        if n == 0:
            out.unlink(missing_ok=True)
            counts[name] = 0
            continue
        ext = con.execute("SELECT min(bbox.xmin), min(bbox.ymin), max(bbox.xmax), max(bbox.ymax) FROM o").fetchone()
        geo = _geo_meta(epsg, [float(v) for v in ext]).replace("'", "''")
        con.execute(
            f"COPY (SELECT ms_id, geometry, area_m2, bbox, lon_min, lat_min, lon_max, lat_max{conf} FROM o "
            f"ORDER BY ST_Hilbert(clon, clat, {hil}), ms_id) TO {_q(tmp)} "
            f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE {ROW_GROUP_SIZE}, KV_METADATA {{geo: '{geo}'}})"
        )
        got = con.execute(f"SELECT count(*) FROM read_parquet({_q(tmp)})").fetchone()[0]
        if got != n:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"{quadkey}/{name}: wrote {got} rows, expected {n}")
        os.replace(tmp, out)
        written.append(out)
        counts[name] = n
        pending.append(
            (
                f"{MS_DIR}/{name}/{quadkey}.parquet",
                {
                    "converted_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "dataset": f"msbuildings_{name}",
                    "source": url or str(raw_gz.name),
                    "source_size": size_bytes,
                    "source_upload_date": upload_date,
                    "parquet_size": out.stat().st_size,
                    "rows": n,
                    "crs": f"EPSG:{epsg}",
                    "licence": LICENCE,
                    "licence_url": LICENCE_URL,
                    "attribution": ATTRIBUTION,
                    "region": REGION,
                    "quadkey": quadkey,
                },
            )
        )
    if counts["gb"] + counts["ni"] + outside != n_valid:
        raise RuntimeError(f"{quadkey}: {counts} rows written, {n_valid} valid source rows")
    con.close()
    # every expected output exists and is verified: now record them, and the tile itself, as done
    for key, entry in pending:
        _append_manifest(parquet_dir, key, entry)
    done = parquet_dir / MS_DIR / f"{quadkey}.done"
    done.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(done, json.dumps({"outputs": [n for n in ("gb", "ni") if counts[n]], "counts": counts}))
    return {
        **counts,
        "dropped": n_src - n_valid,
        "outside_grid": outside,
        "source_rows": n_src,
        "seconds": round(time.time() - t0, 1),
    }


def tile_done(quadkey: str, parquet_dir: Path) -> bool:
    """A tile is done when every output it was converted to exists AND has a manifest entry.

    ``convert_tile`` writes ``msbuildings/<qk>.done`` (JSON: the outputs, ``[]`` for an empty tile) only after both
    GB and NI outputs are verified and manifested. Tiles converted by older versions have no such record: they
    count as done when a manifest entry exists for a gb or ni file that is on disk (or an old ``empty tile``
    marker exists)."""
    parquet_dir = Path(parquet_dir)
    conv = _read_manifest(parquet_dir).get("converted", {})

    def ok(n: str) -> bool:
        key = f"{MS_DIR}/{n}/{quadkey}.parquet"
        return key in conv and (parquet_dir / key).exists()

    marker = parquet_dir / MS_DIR / f"{quadkey}.done"
    if marker.exists():
        try:
            rec = json.loads(marker.read_text())
        except ValueError:
            rec = None
        if not isinstance(rec, dict):
            return True  # legacy "empty tile" marker
        return all(ok(n) for n in rec.get("outputs", []))
    return any(ok(n) for n in ("gb", "ni"))


def convert_all_tiles(
    links_csv: Path, parquet_dir: Path, raw_dir: Path, only: list[str] | None = None, force: bool = False
) -> dict:
    """Download + convert every UK tile one at a time, deleting each raw tile after verified conversion.

    Resumable: tiles already in the manifest are skipped (empty tiles leave an ``<qk>.done`` marker).
    """
    parquet_dir, raw_dir = Path(parquet_dir), Path(raw_dir)
    res: dict = {"converted": {}, "skipped": [], "failed": {}}
    for t in uk_tiles(links_csv):
        qk = t["quadkey"]
        if only and qk not in only:
            continue
        if not force and tile_done(qk, parquet_dir):
            res["skipped"].append(qk)
            continue
        try:
            raw = download_tile(t, raw_dir)
            r = convert_tile(
                raw,
                qk,
                parquet_dir,
                spans_ni=t["spans_ni"],
                size_bytes=t["size_bytes"],
                url=t["url"],
                upload_date=t["upload_date"],
            )
            shutil.rmtree(raw.parent)  # verified above; raw tile no longer needed
            res["converted"][qk] = r
        except Exception as e:
            res["failed"][qk] = f"{type(e).__name__}: {e}"
            if "free disk" in str(e):
                break
    return res


# --------------------------------------------------------------------------- per-LAD outline match
def _gb_glob(parquet_dir: Path) -> str:
    return str(Path(parquet_dir) / MS_DIR / "gb" / "*.parquet")


MARKER = "outline_ms.stats.json"
OWNED = ("outline_ms.parquet", "outline_ms_buildings.parquet", "outline_ms.LICENCE.txt")


def ms_outline_for_lad(
    lad: str,
    staged_dir: Path,
    out_dir: Path,
    parquet_dir: Path,
    *,
    threads: int | None = None,
    memory_limit: str = "6GB",
) -> dict:
    """Match each UPRN of ``lad`` to a Microsoft footprint; write ``outline_ms*.parquet`` + LICENCE sidecar.

    Reads ``out_dir/lad=<lad>/uprn.parquet`` (uprn, x, y, toid_x, toid_y and the OGL ``building_id`` /
    ``uprns_in_building`` from ``build.py``) and ``buildings.parquet`` (OGL polygons) there, and the converted GB
    tiles in ``parquet_dir/msbuildings/gb``. ``staged_dir`` only locates the DuckDB temp directory.

    Match order per UPRN (like ``build.py``): Open TOID point inside a footprint, else the UPRN point inside
    one, else the nearest footprint within 5 m. Overlapping candidates: smallest area, then ms_id.
    """
    out = lad_out_dir(out_dir, lad)
    up, ogl_b = out / "uprn.parquet", out / "buildings.parquet"
    if not up.exists():
        raise FileNotFoundError(f"{up} missing: run build first")
    if not list((Path(parquet_dir) / MS_DIR / "gb").glob("*.parquet")):
        raise FileNotFoundError(f"no converted tiles in {Path(parquet_dir) / MS_DIR / 'gb'}")
    # completion marker removed first, written last; outputs go through .tmp + atomic rename
    begin_outputs(out, MARKER, OWNED)
    con = _connect(memory_limit, Path(staged_dir).resolve().parent / "tmp", threads)
    con.execute(f"""
        CREATE TEMP TABLE u AS
        SELECT uprn, x, y, toid_x, toid_y, building_id AS ogl_building_id, uprns_in_building AS ogl_uprns_in_building
        FROM read_parquet({_q(up)})""")
    ext = con.execute("SELECT min(x), min(y), max(x), max(y) FROM u WHERE x IS NOT NULL").fetchone()
    if ext[0] is None:
        raise ValueError(f"{lad}: no UPRN coordinates")
    pad = 50.0
    ext = [float(v) for v in ext]
    src = f"read_parquet({_q(_gb_glob(parquet_dir))})"
    con.execute(f"""
        CREATE TEMP TABLE ms AS
        SELECT DISTINCT ON (ms_id) ms_id, area_m2, {_geom_expr(con, src)} AS g
        FROM {src}
        WHERE bbox.xmax >= {ext[0] - pad} AND bbox.xmin <= {ext[2] + pad}
          AND bbox.ymax >= {ext[1] - pad} AND bbox.ymin <= {ext[3] + pad}
        ORDER BY ms_id""")
    n_ms_loaded = con.execute("SELECT count(*) FROM ms").fetchone()[0]

    def within(table: str, xc: str, yc: str, name: str) -> None:
        con.execute(f"""
            CREATE TEMP TABLE {name} AS
            SELECT p.uprn, arg_min(m.ms_id, (m.area_m2, m.ms_id)) AS ms_id
            FROM (SELECT uprn, ST_Point({xc}, {yc}) AS pt FROM {table}
                  WHERE {xc} IS NOT NULL AND {yc} IS NOT NULL) p
            JOIN ms m ON ST_Within(p.pt, m.g) GROUP BY p.uprn""")

    within("u", "toid_x", "toid_y", "m_toid")
    within("u", "x", "y", "m_uprn")
    con.execute("""
        CREATE TEMP TABLE rest AS
        SELECT uprn, ST_Point(x, y) AS pt FROM u
        WHERE x IS NOT NULL AND uprn NOT IN (SELECT uprn FROM m_toid) AND uprn NOT IN (SELECT uprn FROM m_uprn)""")
    con.execute(f"""
        CREATE TEMP TABLE m_near AS
        SELECT uprn, arg_min(ms_id, (d, ms_id)) AS ms_id, min(d) AS d
        FROM (SELECT r.uprn, m.ms_id, ST_Distance(r.pt, m.g) AS d
              FROM rest r JOIN ms m ON ST_DWithin(r.pt, m.g, {NEAREST_M})) GROUP BY uprn""")
    has_ogl = ogl_b.exists()
    ogl_src = f"read_parquet({_q(ogl_b)})"
    ogl_area = (
        (f"(SELECT building_id, ST_Area({_geom_expr(con, ogl_src)}) AS ogl_area_m2 FROM {ogl_src})")
        if has_ogl
        else "(SELECT NULL::VARCHAR AS building_id, NULL::DOUBLE AS ogl_area_m2 WHERE false)"
    )
    con.execute(f"""
        CREATE TEMP TABLE fin AS
        SELECT uprn, ms_building_id,
               CASE WHEN ms_building_id IS NOT NULL THEN count(*) OVER (PARTITION BY ms_building_id) END
                    AS uprns_in_ms_building,
               ms_area_m2, match_method, ms_match_distance_m,
               ogl_building_id, ogl_uprns_in_building AS uprns_in_building, ogl_area_m2,
               ms_area_m2 / NULLIF(ogl_area_m2, 0) AS area_ratio_ms_to_ogl
        FROM (
            SELECT u.uprn, coalesce(t.ms_id, p.ms_id, n.ms_id) AS ms_building_id, m.area_m2 AS ms_area_m2,
                   CASE WHEN t.ms_id IS NOT NULL THEN 'toid_point_in_polygon'
                        WHEN p.ms_id IS NOT NULL THEN 'uprn_point_in_polygon'
                        WHEN n.ms_id IS NOT NULL THEN 'nearest_within_5m' ELSE 'none' END AS match_method,
                   CASE WHEN t.ms_id IS NOT NULL OR p.ms_id IS NOT NULL THEN 0.0 ELSE n.d END
                        AS ms_match_distance_m,
                   u.ogl_building_id, u.ogl_uprns_in_building, a.ogl_area_m2
            FROM u LEFT JOIN m_toid t USING (uprn) LEFT JOIN m_uprn p USING (uprn) LEFT JOIN m_near n USING (uprn)
            LEFT JOIN ms m ON m.ms_id = coalesce(t.ms_id, p.ms_id, n.ms_id)
            LEFT JOIN {ogl_area} a ON a.building_id = u.ogl_building_id)""")
    cols = (
        "uprn, ms_building_id, uprns_in_ms_building, ms_area_m2, match_method, ms_match_distance_m, "
        "ogl_building_id, uprns_in_building, ogl_area_m2, area_ratio_ms_to_ogl"
    )
    tmp = out / "outline_ms.parquet.tmp"
    con.execute(f"COPY (SELECT {cols} FROM fin ORDER BY uprn) TO {_q(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD)")
    os.replace(tmp, out / "outline_ms.parquet")

    bb = con.execute("""
        SELECT min(ST_XMin(g)), min(ST_YMin(g)), max(ST_XMax(g)), max(ST_YMax(g)) FROM ms
        WHERE ms_id IN (SELECT ms_building_id FROM fin)""").fetchone()
    geo = _geo_meta(27700, [float(v) for v in bb] if bb[0] is not None else None).replace("'", "''")
    tmp = out / "outline_ms_buildings.parquet.tmp"
    con.execute(f"""
        COPY (SELECT ms_id AS ms_building_id, area_m2, ST_AsWKB(g)::BLOB AS geometry,
                     {{xmin: ST_XMin(g), ymin: ST_YMin(g), xmax: ST_XMax(g), ymax: ST_YMax(g)}} AS bbox
              FROM ms WHERE ms_id IN (SELECT ms_building_id FROM fin)
              ORDER BY ST_XMin(g) // 500, ST_YMin(g) // 500, ms_id)
        TO {_q(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD, KV_METADATA {{geo: '{geo}'}})""")
    os.replace(tmp, out / "outline_ms_buildings.parquet")
    write_text_atomic(out / "outline_ms.LICENCE.txt", LICENCE_TEXT)

    total = con.execute("SELECT count(*) FROM fin").fetchone()[0]

    def pct(n: int) -> float:
        return round(100.0 * n / total, 4) if total else 0.0

    methods = dict(con.execute("SELECT match_method, count(*) FROM fin GROUP BY 1").fetchall())
    dist = con.execute("""
        SELECT count(*) FILTER (WHERE uprns_in_ms_building = 1),
               count(*) FILTER (WHERE uprns_in_ms_building BETWEEN 2 AND 5),
               count(*) FILTER (WHERE uprns_in_ms_building BETWEEN 6 AND 20),
               count(*) FILTER (WHERE uprns_in_ms_building > 20) FROM fin""").fetchone()
    odist = con.execute("""
        SELECT count(*) FILTER (WHERE uprns_in_building = 1),
               count(*) FILTER (WHERE uprns_in_building BETWEEN 2 AND 5),
               count(*) FILTER (WHERE uprns_in_building BETWEEN 6 AND 20),
               count(*) FILTER (WHERE uprns_in_building > 20) FROM fin""").fetchone()
    moved = con.execute("""
        SELECT count(*) FILTER (WHERE uprns_in_building > 5 AND uprns_in_ms_building = 1),
               count(*) FILTER (WHERE uprns_in_building > 5 AND uprns_in_ms_building BETWEEN 2 AND 5)
        FROM fin""").fetchone()
    stats = {
        "lad": lad,
        "built_at": datetime.now(UTC).isoformat(),
        "licence": LICENCE,
        "total_uprns": total,
        "ms_polygons_loaded": n_ms_loaded,
        "ms_polygons_matched": con.execute("SELECT count(DISTINCT ms_building_id) FROM fin").fetchone()[0],
        "match_method": {
            m: {"count": methods.get(m, 0), "pct": pct(methods.get(m, 0))}
            for m in ("toid_point_in_polygon", "uprn_point_in_polygon", "nearest_within_5m", "none")
        },
        "uprns_in_ms_building": dict(zip(("1", "2-5", "6-20", ">20"), dist, strict=True)),
        "uprns_in_building_ogl": dict(zip(("1", "2-5", "6-20", ">20"), odist, strict=True)),
        "moved_from_block_gt5": {"to_1": moved[0], "to_2_5": moved[1]},
    }
    con.close()
    write_text_atomic(out / MARKER, json.dumps(stats, indent=2))  # completion marker: always last
    return stats


def outline_current(d: Path) -> bool:
    """Outputs and marker exist and ``uprn.parquet`` / ``buildings.parquet`` are not newer than the marker."""
    d = Path(d)
    if not all((d / n).exists() for n in (MARKER, *OWNED[:2])):
        return False
    done = (d / MARKER).stat().st_mtime_ns
    return all((d / n).stat().st_mtime_ns <= done for n in ("uprn.parquet", "buildings.parquet") if (d / n).exists())


def ms_outline_all(
    staged_dir: Path, out_dir: Path, parquet_dir: Path, lads: list[str] | None = None, skip_existing: bool = True, **kw
) -> dict:
    """Run ``ms_outline_for_lad`` for every LAD with a built ``uprn.parquet`` (resumable).

    Returns ``{"built": {lad: stats}, "skipped": [lad], "failed": {lad: message}}``. As in ``build_all``, a
    footprint straddling a LAD boundary is counted per LAD only.
    """
    todo = (
        list(lads)
        if lads
        else sorted(p.parent.name.removeprefix("lad=") for p in Path(out_dir).glob("lad=*/uprn.parquet"))
    )
    res: dict = {"built": {}, "skipped": [], "failed": {}}
    for lad in todo:
        if skip_existing and outline_current(lad_out_dir(out_dir, lad)):
            res["skipped"].append(lad)
            continue
        try:
            res["built"][lad] = ms_outline_for_lad(lad, staged_dir, out_dir, parquet_dir, **kw)
        except Exception as e:
            res["failed"][lad] = f"{type(e).__name__}: {e}"
    return res


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m uprn_resolver.msbuildings")
    ap.add_argument("cmd", choices=("tiles", "lad", "all"))
    ap.add_argument("--lad", action="append")
    ap.add_argument("--force", action="store_true", help="tiles: re-download and re-convert done tiles")
    ap.add_argument("--data", type=Path, default=Path("data"))
    a = ap.parse_args(argv)
    pq = a.data / "parquet"
    if a.cmd == "tiles":
        links = fetch_links(a.data / "raw" / "msbuildings_links" / "dataset-links.csv")
        print(json.dumps(convert_all_tiles(links, pq, a.data / "raw" / "msbuildings", force=a.force), indent=1))
    elif a.cmd == "lad":
        for lad in a.lad or []:
            print(json.dumps(ms_outline_for_lad(lad, a.data / "staged", a.data / "out", pq), indent=1))
    else:
        print(json.dumps(ms_outline_all(a.data / "staged", a.data / "out", pq, a.lad), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
