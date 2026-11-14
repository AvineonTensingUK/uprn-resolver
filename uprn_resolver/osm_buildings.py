"""OpenStreetMap building outlines (ODbL-1.0): a finer-grained companion to the OGL OS OpenMap Local outline.

OSM building polygons are much finer than OpenMap Local in dense areas (terraces are mostly split per house;
Islington median 78 m2 vs 376 m2). The data is ODbL (share-alike), so it lives in SEPARATE files and is never
merged into the OGL ``uprn.parquet``.

National Parquet ``data/parquet/osm/buildings.parquet`` (written by ``convert_osm_buildings``; EPSG:27700, WKB):
  osm_type ('way'|'relation'), osm_id BIGINT, building (tag value), has_addr BOOL (any addr:housenumber or
  addr:housename), area_m2, xmin/ymin/xmax/ymax (bbox columns for row-group pruning), geom_wkb.
Hilbert-sorted. Great Britain only: Northern Ireland features are dropped (separate pipeline), and
``building=no`` is dropped. The PBF is untrusted and is only ever read by osmium; it is never modified.

Per-LAD output (``osm_outline_for_lad``):
  ``out/lad=<lad>/outline_odbl.parquet``            one row per UPRN (plain Parquet)
  ``out/lad=<lad>/outline_odbl_buildings.parquet``  the matched OSM polygons (GeoParquet 1.1, bbox covering)
  ``out/lad=<lad>/outline_odbl.LICENCE.txt``
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import duckdb

from uprn_resolver import bng
from uprn_resolver.build import EPSG_27700_PROJJSON
from uprn_resolver.convert import _append_manifest, _connect, _copy, _free_gb, _load_spatial, _q

MIN_FREE_GB = 15
OSM = "osm"
LICENCE = "ODbL-1.0"
ATTRIBUTION = "© OpenStreetMap contributors"
LICENCE_NAME = "outline_odbl.LICENCE.txt"
LICENCE_TEXT = """\
outline_odbl.parquet / outline_odbl_buildings.parquet -- OpenStreetMap building outlines matched to UPRNs.

Contains information from OpenStreetMap ({release} extract via Geofabrik), (c) OpenStreetMap contributors,
made available under the Open Database Licence (ODbL 1.0): https://opendatacommons.org/licenses/odbl/1-0/
Attribution: (c) OpenStreetMap contributors. https://www.openstreetmap.org/copyright

These files are a Derivative Database under the ODbL. Share-alike applies: if you publicly use them, or a
database built from them, you must offer it under the ODbL. Keep them SEPARATE from the OGL uprn.parquet and
buildings.parquet; do not merge them into a single published database.

Match methods (osm_match_method): toid_point_in (Open TOID representative point inside the OSM polygon) >
uprn_point_in (OS Open UPRN point inside it) > nearest_5m (nearest polygon within 5 m of the UPRN point) > none.
Where several polygons contain the point the smallest is used. uprns_in_osm_building counts UPRNs of this LAD
only. OSM geometry is crowd-sourced; coordinates were transformed from WGS84 to EPSG:27700 with the OSTN15
grid (uprn_resolver.bng); residual offsets are OSM digitising error.
"""

NEAREST_M = 5.0
PAD_M = 50.0
CELL_M = 64.0  # grid cell size for the point-in-building equi-join index
BNG_EXTENT = (0.0, 0.0, 700_000.0, 1_300_000.0)
ROW_GROUP_SIZE = 100_000
GEO_TYPES = {"POLYGON": "Polygon", "MULTIPOLYGON": "MultiPolygon"}
COVERING = {"bbox": {k: ["bbox", k] for k in ("xmin", "ymin", "xmax", "ymax")}}
# osmium export: only the tags we keep, no object metadata (keeps the GeoJSON-seq small)
_EXPORT_CFG = {
    "attributes": {k: False for k in ("type", "id", "version", "changeset", "timestamp", "uid", "user", "way_nodes")},
    "linear_tags": False,
    "area_tags": True,
    "exclude_tags": [],
    "include_tags": ["building", "addr:housenumber", "addr:housename"],
}
# Northern Ireland lies west of the Irish Sea: lon < -5.4 and south of 55.25, plus Rathlin (lon < -5.9, lat < 55.4).
# Kintyre, Islay and Galloway are all outside both rules.
# lat > 54.0 keeps far-west Cornwall and the Isles of Scilly (lon < -5.4 but far south) in GB.
NI_RULE = "(lat > 54.0 AND ((lon < -5.4 AND lat < 55.25) OR (lon < -5.9 AND lat < 55.4)))"


def _tag(key: str) -> str:
    return f"json_extract_string(properties, '$.\"{key}\"')"


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)  # noqa: S603 - fixed argv, no shell


def _check_free(path: Path, what: str) -> None:
    free = _free_gb(path)
    if free < MIN_FREE_GB:
        raise RuntimeError(f"only {free:.1f} GB free; need at least {MIN_FREE_GB} GB. Aborting {what}.")


def _geo_json(types: list[str], bbox: list[float] | None) -> str:
    col: dict = {"encoding": "WKB", "geometry_types": types, "crs": EPSG_27700_PROJJSON, "covering": COVERING}
    if bbox is not None:
        col["bbox"] = bbox
    return json.dumps({"version": "1.1.0", "primary_column": "geometry", "columns": {"geometry": col}})


# ---------------------------------------------------------------- national extraction
def convert_osm_buildings(pbf: Path, parquet_dir: Path, work_dir: Path | None = None) -> dict:
    """Extract GB ``building=*`` polygons from an OSM PBF to ``parquet_dir/osm/buildings.parquet``.

    osmium tags-filter (ways + relations) -> osmium export (area geometries, GeoJSON-seq, zstd-compressed) ->
    DuckDB spatial (drop NI and ``building=no``, transform to EPSG:27700, repair invalid, Hilbert sort).
    Intermediates go under ``<parquet_dir>/../tmp/osm_bld`` and are deleted. The PBF is never modified.
    """
    pbf, parquet_dir = Path(pbf), Path(parquet_dir)
    if not pbf.exists():
        raise FileNotFoundError(pbf)
    for tool in ("osmium", "zstd"):
        if shutil.which(tool) is None:
            raise RuntimeError(f"{tool} not found on PATH")
    work = Path(work_dir) if work_dir else parquet_dir.parent / "tmp" / "osm_bld"
    work.mkdir(parents=True, exist_ok=True)
    _check_free(work, "extraction")
    m = re.search(r"-(\d{6})\.osm\.pbf$", pbf.name)
    release = f"20{m.group(1)[:2]}-{m.group(1)[2:4]}-{m.group(1)[4:]}" if m else "unknown"
    out = parquet_dir / OSM / "buildings.parquet"
    t0 = time.time()
    filt, seq, cfg = work / "bld_filtered.osm.pbf", work / "bld_features.geojsonseq.zst", work / "bld_export.json"
    try:
        _run(["osmium", "tags-filter", "--overwrite", "-o", str(filt), str(pbf), "wr/building"])
        cfg.write_text(json.dumps(_EXPORT_CFG))
        _check_free(work, "export")
        exp = subprocess.Popen(  # noqa: S603
            ["osmium", "export", "-c", str(cfg), "-f", "geojsonseq", "-x", "print_record_separator=false",
             "-u", "type_id", "--geometry-types=polygon", "-o", "-", str(filt)],
            stdout=subprocess.PIPE,
        )  # fmt: skip
        try:
            _run_zstd(exp.stdout, seq)
        finally:
            exp.stdout.close()
            rc = exp.wait()
        if rc != 0:
            raise RuntimeError(f"osmium export failed (exit {rc})")
        filt.unlink(missing_ok=True)
        _check_free(work, "conversion")
        con = _connect(parquet_dir, "8GB")
        try:
            _load_spatial(con)
            _, n = _build_national(con, seq, out)
        finally:
            con.close()
    finally:
        for p in (filt, seq, cfg):
            p.unlink(missing_ok=True)
    if n <= 0:
        raise RuntimeError("OSM building conversion produced no rows")
    _append_manifest(
        parquet_dir,
        str(out.relative_to(parquet_dir)),
        {
            "dataset": f"{OSM}/buildings",
            "source": pbf.name,
            "source_size": pbf.stat().st_size,
            "rows": n,
            "parquet_size": out.stat().st_size,
            "release": release,
            "licence": LICENCE,
            "attribution": ATTRIBUTION,
            "converted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )
    return {
        "release": release, "buildings": n, "parquet_size": out.stat().st_size,
        "seconds": round(time.time() - t0, 1),
    }  # fmt: skip


def _run_zstd(stdin, dest: Path) -> None:
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["zstd", "-q", "-3", "-T4", "-f", "-o", str(dest)], stdin=stdin, check=True
    )


def _build_national(con: duckdb.DuckDBPyConnection, seq: Path, out: Path) -> tuple[int, int]:
    """Read the GeoJSON-seq, write ``out``; return (rows, rows) (kept as a pair for symmetry/logging)."""
    con.execute(
        f"""CREATE TEMP VIEW feat AS SELECT id, properties, geometry
            FROM read_json({_q(seq)}, format='newline_delimited',
            columns={{id: 'VARCHAR', properties: 'JSON', geometry: 'JSON'}}, maximum_object_size=67108864)"""
    )
    inner = f"""
        SELECT osm_type, osm_id, building, has_addr, ST_Area(g) AS area_m2,
               ST_XMin(g) AS xmin, ST_YMin(g) AS ymin, ST_XMax(g) AS xmax, ST_YMax(g) AS ymax, ST_AsWKB(g) AS geom_wkb
        FROM (
          SELECT osm_type, osm_id, building, has_addr,
                 CASE WHEN ST_IsValid(g0) THEN g0 ELSE ST_MakeValid(g0) END AS g
          FROM (
            SELECT osm_type, osm_id, building, has_addr,
                   {bng.duckdb_st_transform_sql("g_ll")} AS g0
            FROM (
              SELECT CASE WHEN CAST(substr(id, 2) AS BIGINT) % 2 = 0 THEN 'way' ELSE 'relation' END AS osm_type,
                     CAST(substr(id, 2) AS BIGINT) // 2 AS osm_id,
                     {_tag("building")} AS building,
                     ({_tag("addr:housenumber")} IS NOT NULL OR {_tag("addr:housename")} IS NOT NULL) AS has_addr,
                     g_ll, ST_X(c) AS lon, ST_Y(c) AS lat
              FROM (SELECT id, properties, ST_GeomFromGeoJSON(geometry) AS g_ll,
                           ST_Centroid(ST_GeomFromGeoJSON(geometry)) AS c FROM feat
                    WHERE id LIKE 'a%' AND {_tag("building")} IS NOT NULL AND {_tag("building")} <> 'no')
            ) WHERE NOT {NI_RULE}
          )
        ) WHERE g IS NOT NULL AND ST_GeometryType(g) IN ('POLYGON', 'MULTIPOLYGON')
        """
    e = BNG_EXTENT
    sql = (
        f"SELECT * FROM ({inner}) ORDER BY ST_Hilbert((xmin + xmax) / 2, (ymin + ymax) / 2, "
        f"{{min_x: {e[0]}, min_y: {e[1]}, max_x: {e[2]}, max_y: {e[3]}}}::BOX_2D), osm_type, osm_id"
    )
    n = _copy(con, sql, out)
    bng.assert_parquet_finite(con, out, "xmin", "ymin", "xmax", "ymax")
    return n, n


# ---------------------------------------------------------------- per-LAD matching
def _release_of(parquet_dir: Path) -> str:
    try:
        m = json.loads((parquet_dir / "manifest.json").read_text())
        return m["converted"][f"{OSM}/buildings.parquet"]["release"]
    except (OSError, KeyError, json.JSONDecodeError):
        return "unknown"


def osm_outline_for_lad(
    lad: str, staged_dir: Path, out_dir: Path, parquet_dir: Path, memory_limit: str = "8GB"
) -> dict:
    """Write ``out_dir/lad=<lad>/outline_odbl.parquet`` (one row per UPRN), ``outline_odbl_buildings.parquet`` and
    the LICENCE sidecar; return stats.

    ``outline_odbl.parquet`` columns: uprn, osm_building_id ('way/123' | 'relation/45'), osm_building_tag,
    osm_building_has_addr, uprns_in_osm_building (UPRNs of this LAD in that polygon), osm_building_area_m2,
    osm_match_method ('toid_point_in' | 'uprn_point_in' | 'nearest_5m' | 'none'), osm_match_distance_m (nearest_5m
    only), osm_methods_disagree, ogl_building_id, ogl_uprns_in_building (uprn.parquet ``uprns_in_building``),
    ogl_building_area_m2, osm_ogl_area_ratio, osm_finer_than_ogl (fewer UPRNs per outline than the OGL one),
    licence.

    Matching mirrors build.py: the Open TOID representative point first, then the UPRN point; the smallest
    containing polygon wins; UPRNs with neither inside a polygon take the nearest polygon within 5 m.
    ``staged_dir`` is only used for EPC ``property_type`` (stats) when uprn.parquet lacks ``epc_property_type``.
    """
    parquet_dir = Path(parquet_dir)
    out_lad = Path(out_dir) / f"lad={lad}"
    uprn_pq, bld_pq = out_lad / "uprn.parquet", parquet_dir / OSM / "buildings.parquet"
    ogl_pq = out_lad / "buildings.parquet"
    for p in (uprn_pq, bld_pq):
        if not p.exists():
            raise FileNotFoundError(p)
    _check_free(out_lad, f"{lad} outline")
    con = _connect(parquet_dir, memory_limit)
    try:
        _load_spatial(con)
        has_ogl = ogl_pq.exists()
        con.execute(f"""
            CREATE TEMP TABLE u AS
            SELECT uprn, x, y, toid_x, toid_y, building_id AS ogl_building_id, uprns_in_building AS ogl_n
            FROM read_parquet({_q(uprn_pq)})""")
        n_uprn, *bb = con.execute(
            "SELECT count(*), min(least(x, coalesce(toid_x, x))), min(least(y, coalesce(toid_y, y))), "
            "max(greatest(x, coalesce(toid_x, x))), max(greatest(y, coalesce(toid_y, y))) FROM u"
        ).fetchone()
        if not n_uprn:
            raise RuntimeError(f"{lad}: no UPRNs with coordinates")
        x0, y0, x1, y1 = (float(v) for v in bb)
        con.execute(f"""
            CREATE TEMP TABLE b AS
            SELECT row_number() OVER () AS bid, osm_type, osm_id, osm_type || '/' || osm_id AS osm_building_id,
                   building, has_addr, area_m2, xmin, ymin, xmax, ymax, ST_GeomFromWKB(geom_wkb) AS g
            FROM read_parquet({_q(bld_pq)})
            WHERE xmax >= {x0 - PAD_M} AND xmin <= {x1 + PAD_M} AND ymax >= {y0 - PAD_M} AND ymin <= {y1 + PAD_M}""")
        # Grid index: each building's bbox (padded by NEAREST_M) expanded to the CELL_M cells it covers, so the
        # point joins below are hash equi-joins on (cx, cy) instead of slow BETWEEN range joins.
        con.execute(f"""
            CREATE TEMP TABLE bc AS
            SELECT b.bid, cx, cy FROM b,
                range(floor((b.xmin - {NEAREST_M}) / {CELL_M})::BIGINT,
                      floor((b.xmax + {NEAREST_M}) / {CELL_M})::BIGINT + 1) AS tx(cx),
                range(floor((b.ymin - {NEAREST_M}) / {CELL_M})::BIGINT,
                      floor((b.ymax + {NEAREST_M}) / {CELL_M})::BIGINT + 1) AS ty(cy)""")
        for name, xc, yc in (("m_toid", "toid_x", "toid_y"), ("m_uprn", "x", "y")):
            con.execute(f"""
                CREATE TEMP TABLE {name} AS
                SELECT uprn, arg_min(bid, (area_m2, bid)) AS bid FROM (
                    SELECT u.uprn, b.bid, b.area_m2 FROM u
                    JOIN bc ON bc.cx = floor(u.{xc} / {CELL_M})::BIGINT AND bc.cy = floor(u.{yc} / {CELL_M})::BIGINT
                    JOIN b ON b.bid = bc.bid
                    WHERE u.{xc} BETWEEN b.xmin AND b.xmax AND u.{yc} BETWEEN b.ymin AND b.ymax
                      AND ST_Within(ST_Point(u.{xc}, u.{yc}), b.g)) GROUP BY uprn""")
        con.execute(f"""
            CREATE TEMP TABLE m_near AS
            SELECT uprn, bid, d FROM (
                SELECT *, row_number() OVER (PARTITION BY uprn ORDER BY d, bid) AS rn FROM (
                    SELECT u.uprn, b.bid, ST_Distance(ST_Point(u.x, u.y), b.g) AS d
                    FROM (SELECT * FROM u WHERE uprn NOT IN (SELECT uprn FROM m_toid UNION SELECT uprn FROM m_uprn)) u
                    JOIN bc ON bc.cx = floor(u.x / {CELL_M})::BIGINT AND bc.cy = floor(u.y / {CELL_M})::BIGINT
                    JOIN b ON b.bid = bc.bid
                    WHERE u.x BETWEEN b.xmin - {NEAREST_M} AND b.xmax + {NEAREST_M}
                      AND u.y BETWEEN b.ymin - {NEAREST_M} AND b.ymax + {NEAREST_M})
                WHERE d <= {NEAREST_M}) WHERE rn = 1""")
        ogl_join = ""
        ogl_area = "CAST(NULL AS DOUBLE)"
        if has_ogl:
            con.execute(f"""
                CREATE TEMP TABLE ogl AS
                SELECT building_id, ST_Area({_ogl_geom(con, ogl_pq)}) AS a
                FROM read_parquet({_q(ogl_pq)})""")
            ogl_join = (
                "LEFT JOIN (SELECT building_id, any_value(a) AS a FROM ogl GROUP BY 1) o "
                "ON o.building_id = u.ogl_building_id"
            )
            ogl_area = "o.a"
        con.execute(f"""
            CREATE TEMP TABLE r AS
            SELECT u.uprn, coalesce(mt.bid, mu.bid, mn.bid) AS bid,
                   CASE WHEN mt.bid IS NOT NULL THEN 'toid_point_in' WHEN mu.bid IS NOT NULL THEN 'uprn_point_in'
                        WHEN mn.bid IS NOT NULL THEN 'nearest_5m' ELSE 'none' END AS osm_match_method,
                   round(mn.d, 2) AS osm_match_distance_m,
                   CASE WHEN mt.bid IS NOT NULL AND mu.bid IS NOT NULL THEN mt.bid <> mu.bid END
                        AS osm_methods_disagree,
                   u.ogl_building_id, u.ogl_n AS ogl_uprns_in_building, {ogl_area} AS ogl_building_area_m2
            FROM u LEFT JOIN m_toid mt USING (uprn) LEFT JOIN m_uprn mu USING (uprn)
            LEFT JOIN m_near mn ON mn.uprn = u.uprn AND mt.bid IS NULL AND mu.bid IS NULL
            {ogl_join}""")
        con.execute("""
            CREATE TEMP TABLE final AS
            SELECT r.uprn, b.osm_building_id, b.building AS osm_building_tag, b.has_addr AS osm_building_has_addr,
                   CASE WHEN r.bid IS NOT NULL THEN count(*) OVER (PARTITION BY r.bid) END AS uprns_in_osm_building,
                   b.area_m2 AS osm_building_area_m2, r.osm_match_method, r.osm_match_distance_m,
                   r.osm_methods_disagree, r.ogl_building_id, r.ogl_uprns_in_building, r.ogl_building_area_m2,
                   CASE WHEN r.ogl_building_area_m2 > 0 THEN round(b.area_m2 / r.ogl_building_area_m2, 4) END
                        AS osm_ogl_area_ratio,
                   CASE WHEN r.bid IS NOT NULL AND r.ogl_uprns_in_building IS NOT NULL
                        THEN count(*) OVER (PARTITION BY r.bid) < r.ogl_uprns_in_building END AS osm_finer_than_ogl,
                   r.bid
            FROM r LEFT JOIN b ON b.bid = r.bid""")
        out = out_lad / "outline_odbl.parquet"
        n = _copy(
            con,
            f"SELECT * EXCLUDE (bid), '{LICENCE}' AS licence FROM final ORDER BY uprn",
            out,
        )
        _write_matched(con, out_lad / "outline_odbl_buildings.parquet")
        stats: dict = {"lad": lad, "uprns": n}
        stats["methods"] = dict(
            con.execute("SELECT osm_match_method, count(*) FROM final GROUP BY 1 ORDER BY 1").fetchall()
        )
        stats["osm_uprns_in_building"] = _dist(con, "uprns_in_osm_building")
        stats["ogl_uprns_in_building"] = _dist(con, "ogl_uprns_in_building")
        stats["ogl_block_to_osm"] = dict(
            zip(
                ("1", "2-5", "6-20", ">20", "unmatched"),
                con.execute("""
                    SELECT count(*) FILTER (WHERE uprns_in_osm_building = 1),
                           count(*) FILTER (WHERE uprns_in_osm_building BETWEEN 2 AND 5),
                           count(*) FILTER (WHERE uprns_in_osm_building BETWEEN 6 AND 20),
                           count(*) FILTER (WHERE uprns_in_osm_building > 20),
                           count(*) FILTER (WHERE uprns_in_osm_building IS NULL)
                    FROM final WHERE ogl_uprns_in_building > 5""").fetchone(),
                strict=True,
            )
        )
        stats["ogl_block_by_property_type"] = _block_by_type(con, lad, staged_dir, uprn_pq)
    finally:
        con.close()
    (out_lad / LICENCE_NAME).write_text(LICENCE_TEXT.format(release=_release_of(parquet_dir)))
    return stats


def _ogl_geom(con: duckdb.DuckDBPyConnection, path: Path) -> str:
    """SQL giving a GEOMETRY for the OGL ``buildings.parquet`` geometry column (BLOB on older DuckDB)."""
    t = con.execute(f"SELECT typeof(geometry) FROM read_parquet({_q(path)}) LIMIT 1").fetchone()
    return "geometry" if t and str(t[0]).upper().startswith("GEOMETRY") else "ST_GeomFromWKB(geometry)"


def _dist(con: duckdb.DuckDBPyConnection, col: str) -> dict:
    r = con.execute(f"""
        SELECT count(*) FILTER (WHERE {col} = 1), count(*) FILTER (WHERE {col} BETWEEN 2 AND 5),
               count(*) FILTER (WHERE {col} BETWEEN 6 AND 20), count(*) FILTER (WHERE {col} > 20),
               count(*) FILTER (WHERE {col} IS NULL) FROM final""").fetchone()
    return dict(zip(("1", "2-5", "6-20", ">20", "none"), r, strict=True))


def _block_by_type(con, lad: str, staged_dir: Path, uprn_pq: Path) -> dict | None:
    """For OGL 'block' UPRNs (>5 per outline): OSM outline size class by EPC property_type (None if no EPC)."""
    epc = Path(staged_dir) / lad / "epc.parquet"
    if epc.exists():
        src = f"(SELECT DISTINCT ON (uprn) uprn, property_type FROM read_parquet({_q(epc)}) ORDER BY uprn)"
    else:
        src = (
            f"(SELECT uprn, epc_property_type AS property_type FROM read_parquet({_q(uprn_pq)}) "
            "WHERE epc_property_type IS NOT NULL)"
        )
    try:
        rows = con.execute(f"""
            SELECT CASE WHEN e.property_type IS NULL THEN '(no EPC)'
                        WHEN e.property_type IN ('House', 'Flat', 'Maisonette', 'Bungalow', 'Park home')
                        THEN e.property_type
                        ELSE 'Non-domestic' END AS pt,
                   count(*) AS block_uprns,
                   count(*) FILTER (WHERE f.uprns_in_osm_building = 1) AS osm_1,
                   count(*) FILTER (WHERE f.uprns_in_osm_building BETWEEN 2 AND 5) AS osm_2_5,
                   count(*) FILTER (WHERE f.uprns_in_osm_building > 5) AS osm_gt5,
                   count(*) FILTER (WHERE f.uprns_in_osm_building IS NULL) AS unmatched
            FROM final f LEFT JOIN {src} e USING (uprn)
            WHERE f.ogl_uprns_in_building > 5 GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    except duckdb.Error:
        return None
    keys = ("block_uprns", "osm_1", "osm_2_5", "osm_gt5", "unmatched")
    return {r[0]: dict(zip(keys, r[1:], strict=True)) for r in rows}


def _write_matched(con: duckdb.DuckDBPyConnection, dst: Path) -> None:
    """Matched OSM polygons as GeoParquet 1.1 (bbox covering, Hilbert sorted)."""
    con.execute("""
        CREATE OR REPLACE TEMP TABLE mb AS
        SELECT b.osm_building_id, b.osm_type, b.osm_id, b.building AS osm_building_tag,
               b.has_addr AS osm_building_has_addr, b.area_m2 AS osm_building_area_m2, c.n AS uprns_in_osm_building, b.g
        FROM b JOIN (SELECT bid, count(*) AS n FROM final WHERE bid IS NOT NULL GROUP BY bid) c ON c.bid = b.bid""")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE t AS
        SELECT * EXCLUDE (g), ST_AsWKB(g)::BLOB AS geometry,
               {'xmin': ST_XMin(g), 'ymin': ST_YMin(g), 'xmax': ST_XMax(g), 'ymax': ST_YMax(g)}::
                   STRUCT(xmin DOUBLE, ymin DOUBLE, xmax DOUBLE, ymax DOUBLE) AS bbox,
               ST_GeometryType(g)::VARCHAR AS _gt
        FROM mb""")
    x0, y0, x1, y1, types = con.execute(
        "SELECT min(bbox.xmin), min(bbox.ymin), max(bbox.xmax), max(bbox.ymax), list(DISTINCT _gt) FROM t"
    ).fetchone()
    gt = sorted(GEO_TYPES.get(t, t) for t in (types or []))
    geo = _geo_json(gt, [x0, y0, x1, y1] if x0 is not None else None)
    e = BNG_EXTENT
    order = (
        f"ST_Hilbert((bbox.xmin + bbox.xmax) / 2, (bbox.ymin + bbox.ymax) / 2, {{min_x: {e[0]}, min_y: {e[1]}, "
        f"max_x: {e[2]}, max_y: {e[3]}}}::BOX_2D), osm_building_id"
    )
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.unlink(missing_ok=True)
    kv = geo.replace("'", "''")
    con.execute(
        f"COPY (SELECT * EXCLUDE (_gt) FROM t ORDER BY {order}) TO {_q(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD, "
        f"COMPRESSION_LEVEL 9, ROW_GROUP_SIZE {ROW_GROUP_SIZE}, KV_METADATA {{geo: '{kv}'}})"
    )
    tmp.replace(dst)
    con.execute("DROP TABLE t; DROP TABLE mb")


def osm_outline_all(
    staged_dir: Path,
    out_dir: Path,
    parquet_dir: Path,
    lads: list[str] | None = None,
    skip_existing: bool = True,
    **kw,
) -> dict:
    """Run ``osm_outline_for_lad`` over LADs having ``uprn.parquet``; resumable (skips existing output)."""
    staged_dir, out_dir = Path(staged_dir), Path(out_dir)
    avail = sorted(p.parent.name.removeprefix("lad=") for p in out_dir.glob("lad=*/uprn.parquet"))
    res: dict = {"done": [], "skipped": [], "failed": {}}
    for lad in lads or avail:
        if lad not in avail:
            res["failed"][lad] = "no uprn.parquet"
            continue
        if skip_existing and (out_dir / f"lad={lad}" / "outline_odbl.parquet").exists():
            res["skipped"].append(lad)
            continue
        try:
            osm_outline_for_lad(lad, staged_dir, out_dir, parquet_dir, **kw)
            res["done"].append(lad)
        except Exception as exc:  # noqa: BLE001 - keep going over LADs
            res["failed"][lad] = str(exc)
    return res
