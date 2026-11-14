"""Attach Overture Maps Places (POIs) to UPRNs, mainly to give business names to non-residential UPRNs.

``download_places`` pulls the latest Overture ``places`` release for a GB bbox straight from the public
bucket (DuckDB httpfs, anonymous) into ``parquet_dir/overture_places/places_gb.parquet`` (EPSG:27700, OSTN15).
``poi_for_lad`` matches each POI to a UPRN (same building -> same INSPIRE parcel -> nearest within 30 m)
and writes ``out_dir/lad=<lad>/poi.parquet``; ``poi_all`` loops over LADs, resumable.

Overture Places are CDLA-Permissive-2.0 for most providers, Apache-2.0 (Foursquare) and CC0-1.0
(AllThePlaces); there is no OpenStreetMap content in the theme. Attribution is written to ``poi.LICENCE.txt``.
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import duckdb

from . import bng
from .convert import _append_manifest, _connect, _copy, _free_gb, _load_spatial, _q

MIN_FREE_GB = 15
GB_BBOX = (-8.7, 49.8, 1.9, 60.9)  # lon_min, lat_min, lon_max, lat_max
STAC_CATALOG = "https://stac.overturemaps.org/catalog.json"
S3_PLACES = "s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"
PLACES_REL = "overture_places/places_gb.parquet"
PLACES_LAD_REL = "overture_places/places_gb_lad.parquet"
ASSIGN_M = 100.0
BUFFER_M = 100.0
NEAREST_M = 30.0
LICENCE_NAME = "poi.LICENCE.txt"

LICENCE_TEXT = """Overture Maps Places (POIs) - attribution

Contains Places data from Overture Maps Foundation (https://overturemaps.org), release {release}.
Source: https://docs.overturemaps.org/guides/places/ ; per-provider licences:
https://docs.overturemaps.org/attribution/

Licences by provider (Places theme contains no OpenStreetMap data):
  CDLA-Permissive-2.0: Meta, Microsoft, PinMeTo, Krick, RenderSEO, DAC, BrightQuery, Birdeye, Uberall,
    OpenTravel Alliance
  Apache-2.0: Foursquare (Copyright Foursquare Labs, Inc. 2024; data transformed to the Overture schema)
  CC0-1.0: AllThePlaces
Match columns (uprn, match_method, match_distance_m, match_confidence, candidates) are derived by
uprn-resolver and are heuristic: a POI is matched to a building/parcel/nearby UPRN by geometry only.
"""


def latest_release(timeout: float = 30) -> str:
    """Latest Overture release id from the STAC catalog."""
    with urllib.request.urlopen(STAC_CATALOG, timeout=timeout) as r:  # noqa: S310
        return json.load(r)["latest"]


def _s3_connect(parquet_dir: Path) -> duckdb.DuckDBPyConnection:
    con = _connect(parquet_dir, "6GB")
    _load_spatial(con)
    try:
        con.execute("LOAD httpfs")
    except duckdb.Error:
        con.execute("INSTALL httpfs")
        con.execute("LOAD httpfs")
    con.execute("SET s3_region='us-west-2'")
    return con


def download_places(parquet_dir: Path, release: str | None = None, bbox: tuple = GB_BBOX) -> int:
    """Download GB Overture Places to ``parquet_dir/overture_places/places_gb.parquet``; return the row count."""
    parquet_dir = Path(parquet_dir)
    parquet_dir.mkdir(parents=True, exist_ok=True)
    free = _free_gb(parquet_dir)
    if free < MIN_FREE_GB:
        raise RuntimeError(f"only {free:.1f} GB free; need at least {MIN_FREE_GB} GB. Aborting places download.")
    release = release or latest_release()
    out = parquet_dir / PLACES_REL
    x0, y0, x1, y1 = bbox
    src = S3_PLACES.format(release=release)
    con = _s3_connect(parquet_dir)
    try:
        sql = f"""
            SELECT id AS poi_id,
                   names.primary AS name,
                   basic_category,
                   taxonomy.primary AS taxonomy_primary,
                   array_to_string(taxonomy.hierarchy, '/') AS taxonomy_hierarchy,
                   confidence,
                   operating_status,
                   brand.names.primary AS brand_name,
                   array_to_string(list_distinct(list_transform(sources, lambda s: s.dataset)), ',') AS source_datasets,
                   array_to_string(list_distinct(list_transform(sources, lambda s: s.license)), ',') AS source_licences,
                   ST_X(g27700) AS x, ST_Y(g27700) AS y,
                   ST_AsWKB(g27700) AS geometry
            FROM (
                SELECT *, {bng.duckdb_st_transform_sql("geometry")} AS g27700
                FROM read_parquet({_q(src)}, hive_partitioning=1)
                WHERE bbox.xmin BETWEEN {x0} AND {x1} AND bbox.ymin BETWEEN {y0} AND {y1}
            )"""
        n = _copy(con, sql, out)
        bng.assert_parquet_finite(con, out, "x", "y")
    finally:
        con.close()
    _append_manifest(
        parquet_dir,
        PLACES_REL,
        {
            "dataset": "overture_places",
            "release": release,
            "source": src,
            "licence": "CDLA-Permissive-2.0 (Meta, Microsoft, PinMeTo, Krick, RenderSEO, DAC, BrightQuery, Birdeye, "
            "Uberall, OpenTravel Alliance); Apache-2.0 (Foursquare); CC0-1.0 (AllThePlaces)",
            "licence_url": "https://docs.overturemaps.org/attribution/",
            "bbox_lonlat": list(bbox),
            "rows": n,
            "parquet_size": out.stat().st_size,
            "converted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )
    return n


def assign_places_lad(parquet_dir: Path, max_dist_m: float = ASSIGN_M, memory_limit: str = "8GB") -> dict:
    """Assign every POI to exactly one LAD: the LAD of its nearest ONSUD UPRN within ``max_dist_m``.

    Set-based in DuckDB (100 m grid buckets, POI joined to the 3x3 neighbouring UPRN cells). Writes
    ``overture_places/places_gb_lad.parquet`` (poi_id, lad, nearest_uprn_dist_m); POIs with no UPRN in range
    get lad NULL and are ignored by ``poi_for_lad``.
    """
    parquet_dir = Path(parquet_dir)
    out = parquet_dir / PLACES_LAD_REL
    con = _connect(parquet_dir, memory_limit)
    try:
        con.execute("SET max_temp_directory_size='20GiB'")
        con.execute("SET preserve_insertion_order=false")
        d = max_dist_m
        con.execute(
            f"""CREATE TEMP TABLE u AS
                SELECT o.x AS ux, o.y AS uy, n.lad,
                       CAST(floor(o.x / {d}) AS BIGINT) * 100000 + CAST(floor(o.y / {d}) AS BIGINT) AS k,
                       CAST(floor(o.x / {d}) AS BIGINT) AS cx
                FROM read_parquet({_q(parquet_dir / "open_uprn" / "open_uprn.parquet")}) o
                JOIN read_parquet({_q(parquet_dir / "onsud" / "onsud.parquet")}) n USING (uprn)"""
        )
        con.execute(
            f"""CREATE TEMP TABLE p AS
                SELECT poi_id, x, y, CAST(floor(x / {d}) AS BIGINT) AS cx, CAST(floor(y / {d}) AS BIGINT) AS cy
                FROM read_parquet({_q(parquet_dir / PLACES_REL)})"""
        )
        con.execute("CREATE TEMP TABLE best (poi_id VARCHAR, lad VARCHAR, d DOUBLE)")
        # Process in vertical bands of grid columns so the intermediate pair set stays small.
        lo, hi = con.execute("SELECT min(cx), max(cx) FROM p").fetchone()
        step = 200  # grid columns per band (20 km at 100 m cells)
        for c0 in range(lo, hi + 1, step):
            c1 = c0 + step - 1
            con.execute(
                f"""INSERT INTO best
                    SELECT poi_id, min_by(lad, dist), min(dist) FROM (
                        SELECT pe.poi_id, u.lad, sqrt((u.ux - pe.px)^2 + (u.uy - pe.py)^2) AS dist
                        FROM (SELECT poi_id, x AS px, y AS py, (cx + dx) * 100000 + (cy + dy) AS k
                              FROM p, (VALUES (-1),(0),(1)) a(dx), (VALUES (-1),(0),(1)) b(dy)
                              WHERE cx BETWEEN {c0} AND {c1}) pe
                        JOIN (SELECT * FROM u WHERE cx BETWEEN {c0 - 1} AND {c1 + 1}) u ON u.k = pe.k
                    ) WHERE dist <= {d} GROUP BY poi_id"""
            )
        sql = """SELECT p.poi_id, b.lad, round(b.d, 2) AS nearest_uprn_dist_m
                 FROM p LEFT JOIN best b USING (poi_id)"""
        n = _copy(con, sql, out)
        assigned = con.execute(f"SELECT count(lad) FROM read_parquet({_q(out)})").fetchone()[0]
    finally:
        con.close()
    return {"pois": n, "assigned": int(assigned), "unassigned": n - int(assigned)}


def _release_of(parquet_dir: Path) -> str:
    try:
        m = json.loads((parquet_dir / "manifest.json").read_text())
        return m["converted"][PLACES_REL]["release"]
    except (OSError, KeyError, json.JSONDecodeError):
        return "unknown"


def _geom_expr(path: Path) -> str:
    """SQL geometry expression for a parquet file holding either ``geom_wkb`` or a native ``geometry`` column."""
    cols = {r[0] for r in duckdb.connect().execute(f"DESCRIBE SELECT * FROM read_parquet({_q(path)})").fetchall()}
    return "ST_GeomFromWKB(geom_wkb)" if "geom_wkb" in cols else "geometry"


# Candidate UPRNs per POI per method (building, parcel, 30 m), with counts for confidence.
_BLD_SQL = """
CREATE TEMP TABLE cand AS
WITH j AS (
    SELECT p.poi_id, 'same_building' AS method, u.uprn, u.nondom,
           sqrt((u.x - poi.x)^2 + (u.y - poi.y)^2) AS dist
    FROM poi_bld p JOIN poi USING (poi_id) JOIN u ON u.building_id = p.building_id
    UNION ALL
    SELECT p.poi_id, 'same_parcel', u.uprn, u.nondom, sqrt((u.x - poi.x)^2 + (u.y - poi.y)^2)
    FROM poi_par p JOIN poi USING (poi_id) JOIN u ON u.parcel_inspire_id = p.parcel_inspire_id
    UNION ALL
    SELECT poi.poi_id, 'nearest_30m', u.uprn, u.nondom, sqrt((u.x - poi.x)^2 + (u.y - poi.y)^2)
    FROM poi JOIN u ON abs(u.x - poi.x) <= {nm} AND abs(u.y - poi.y) <= {nm}
    WHERE sqrt((u.x - poi.x)^2 + (u.y - poi.y)^2) <= {nm}
)
SELECT *, count(*) OVER (PARTITION BY poi_id, method) AS ncand,
       sum(nondom::INT) OVER (PARTITION BY poi_id, method) AS nd_count,
       CASE method WHEN 'same_building' THEN 1 WHEN 'same_parcel' THEN 2 ELSE 3 END AS prio
FROM j
"""


def poi_for_lad(lad: str, staged_dir: Path, out_dir: Path, parquet_dir: Path) -> dict:
    """Match Overture POIs to UPRNs for one LAD; write ``out_dir/lad=<lad>/poi.parquet``; return stats."""
    staged = Path(staged_dir) / lad
    out_lad = Path(out_dir) / f"lad={lad}"
    parquet_dir = Path(parquet_dir)
    places = parquet_dir / PLACES_REL
    uprn_pq = out_lad / "uprn.parquet"
    assign = parquet_dir / PLACES_LAD_REL
    if not assign.exists():
        raise FileNotFoundError(f"{assign} missing; run assign_places_lad first")
    if not places.exists():
        raise FileNotFoundError(f"{places} missing; run download_places first")
    if not uprn_pq.exists():
        raise FileNotFoundError(f"{uprn_pq} missing; build the LAD first")

    con = _connect(parquet_dir, "4GB")
    try:
        _load_spatial(con)
        epc = staged / "epc.parquet"
        par = staged / "inspire_parcel.parquet"
        poly = staged / "inspire_polygons.parquet"
        bld = out_lad / "buildings.parquet"
        nondom = (
            f"""LEFT JOIN (SELECT uprn, bool_or(lower(certificate_type) <> 'domestic') AS nd
                        FROM read_parquet({_q(epc)}) GROUP BY uprn) e USING (uprn)"""
            if epc.exists()
            else ""
        )
        con.execute(
            f"""CREATE TEMP TABLE u AS
                SELECT uprn, x, y, building_id,
                       {"pc.parcel_inspire_id" if par.exists() else "NULL::VARCHAR"} AS parcel_inspire_id,
                       {"coalesce(e.nd, false)" if epc.exists() else "false"} AS nondom
                FROM read_parquet({_q(uprn_pq)})
                {f"LEFT JOIN read_parquet({_q(par)}) pc USING (uprn)" if par.exists() else ""}
                {nondom}"""
        )
        ext = con.execute("SELECT min(x), min(y), max(x), max(y) FROM u").fetchone()
        if ext[0] is None:
            raise RuntimeError(f"no UPRNs in {uprn_pq}")
        x0, y0, x1, y1 = (
            float(ext[0]) - BUFFER_M,
            float(ext[1]) - BUFFER_M,
            float(ext[2]) + BUFFER_M,
            float(ext[3]) + BUFFER_M,
        )
        con.execute(
            f"""CREATE TEMP TABLE poi AS
                SELECT poi_id, name, coalesce(basic_category, taxonomy_primary) AS category,
                       brand_name AS brand, confidence AS overture_confidence, operating_status, x, y,
                       ST_Point(x, y) AS pt
                FROM read_parquet({_q(places)})
                JOIN (SELECT poi_id FROM read_parquet({_q(assign)}) WHERE lad = '{lad}') USING (poi_id)
                WHERE x BETWEEN {x0} AND {x1} AND y BETWEEN {y0} AND {y1}"""
        )
        for name, path, key, ok in (
            ("poi_bld", bld, "building_id", bld.exists()),
            ("poi_par", poly, "parcel_inspire_id", poly.exists()),
        ):
            if ok:
                con.execute(
                    f"""CREATE TEMP TABLE {name} AS
                        SELECT poi_id, {key} FROM (
                            SELECT poi.poi_id, g.{key},
                                   row_number() OVER (PARTITION BY poi.poi_id ORDER BY ST_Area(g.geom)) AS rn
                            FROM poi JOIN (SELECT {key}, {_geom_expr(path)} AS geom
                                           FROM read_parquet({_q(path)})) g
                                 ON ST_Contains(g.geom, poi.pt)) WHERE rn = 1"""
                )
            else:
                con.execute(f"CREATE TEMP TABLE {name} (poi_id VARCHAR, {key} VARCHAR)")
        con.execute(_BLD_SQL.format(nm=NEAREST_M))
        sql = """
            SELECT poi.poi_id, poi.name, poi.category, poi.brand, poi.overture_confidence, poi.operating_status,
                   c.uprn, coalesce(c.method, 'none') AS match_method,
                   round(c.dist, 2) AS match_distance_m,
                   c.ncand::INTEGER AS candidates,
                   CASE WHEN c.uprn IS NULL THEN NULL
                        WHEN c.ncand = 1 AND (c.method <> 'nearest_30m' OR c.dist <= 15) THEN 'high'
                        WHEN c.method <> 'nearest_30m' AND c.nd_count = 1 THEN 'high'
                        WHEN c.nd_count > 1 OR c.ncand <= 5 AND c.method <> 'nearest_30m'
                             OR c.ncand = 1 THEN 'medium'
                        ELSE 'low' END AS match_confidence
            FROM poi LEFT JOIN (
                SELECT * FROM (
                    SELECT *, row_number() OVER (
                        PARTITION BY poi_id ORDER BY prio, nondom DESC, dist, uprn) AS rn
                    FROM cand
                    QUALIFY min(prio) OVER (PARTITION BY poi_id) = prio
                ) WHERE rn = 1) c USING (poi_id)
            ORDER BY poi.poi_id"""
        out = out_lad / "poi.parquet"
        n = _copy(con, sql, out)
        stats = {"lad": lad, "pois": n}
        for m, cnt in con.execute(f"SELECT match_method, count(*) FROM read_parquet({_q(out)}) GROUP BY 1").fetchall():
            stats[m] = cnt
        stats["nondomestic_uprns_named"] = con.execute(
            f"SELECT count(DISTINCT p.uprn) FROM read_parquet({_q(out)}) p JOIN u USING (uprn) "
            "WHERE u.nondom AND p.name IS NOT NULL"
        ).fetchone()[0]
    finally:
        con.close()
    (out_lad / LICENCE_NAME).write_text(LICENCE_TEXT.format(release=_release_of(parquet_dir)))
    return stats


def poi_all(
    staged_dir: Path, out_dir: Path, parquet_dir: Path, lads: list[str] | None = None, skip_existing: bool = True
) -> dict:
    """Run ``poi_for_lad`` over LADs with ``out/lad=*/uprn.parquet``; resumable (skips existing ``poi.parquet``)."""
    out_dir = Path(out_dir)
    avail = sorted(p.parent.name.removeprefix("lad=") for p in out_dir.glob("lad=*/uprn.parquet"))
    res: dict = {"done": [], "skipped": [], "failed": {}}
    for lad in lads or avail:
        if lad not in avail:
            res["failed"][lad] = "no uprn.parquet"
            continue
        if skip_existing and (out_dir / f"lad={lad}" / "poi.parquet").exists():
            res["skipped"].append(lad)
            continue
        try:
            poi_for_lad(lad, staged_dir, out_dir, parquet_dir)
            res["done"].append(lad)
        except Exception as exc:  # noqa: BLE001 - keep going over LADs
            res["failed"][lad] = str(exc)
    return res
