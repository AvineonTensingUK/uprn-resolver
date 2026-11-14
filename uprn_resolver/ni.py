"""Northern Ireland coverage from OpenStreetMap (ODbL-1.0). NO UPRNs.

Northern Ireland UPRNs exist only in OSNI Pointer / AddressBase Islands (paid). There is no open UPRN, LIDS,
OpenMap Local, INSPIRE or bulk EPC for NI. This module gives the best that open data allows: OSM building
polygons and OSM ``addr:*`` objects, joined to each other and to named streets. Nothing here carries a UPRN
(the OSM ``ref:GB:uprn`` tag is essentially absent in NI), so it can NOT be joined to GB UPRN products; it is a
building/address layer for NI, kept separate from every OGL output because ODbL share-alike applies.

National Parquet (``data/parquet/ni/``), GeoParquet 1.1, written by ``extract_ni``. Primary geometry column
``geometry`` is WKB in EPSG:29903 (TM75 / Irish Grid; plain PROJ/DuckDB transform = the 7-parameter Helmert
"TM75 to ETRS89 (2)"; measured at most 0.48 m, median 0.18 m, from the official OSi/LPS polynomial grid route in
``bng.wgs84_to_irish_grid`` over all 950k buildings / 92k addresses / 840k MS footprints, i.e. below the 0.5 m
materiality threshold, so the data was NOT re-transformed); ``geometry_wgs84`` is the same WKB in EPSG:4326 (lon/lat).
OSM ids are (osm_type, osm_id).
  ``buildings.parquet``  osm_type, osm_id, building, name, housenumber, housename, street, postcode, city,
                         area_m2, lon, lat, x, y (point on surface), xmin/ymin/xmax/ymax (EPSG:29903),
                         geometry, geometry_wgs84 (Polygon/MultiPolygon)
  ``addresses.parquet``  osm_type ('node'|'way'|'relation'), osm_id, housenumber, housename, unit, street,
                         postcode, city, is_building, lon, lat, x, y, geometry, geometry_wgs84 (Point; for
                         ways/relations the point on surface of the polygon)
  ``highways.parquet``   way_id, highway, name, xmin/ymin/xmax/ymax, geometry, geometry_wgs84 (named ways)

Boundary: the OSM admin_level=4 relation 156393 "Northern Ireland / Tuaisceart Eireann", taken from the same
PBF, rather than a bbox (which would drag in Donegal, Louth, Monaghan, Cavan and Scotland's Galloway coast) or
an ONS/OSNI shapefile (extra download, different vintage/coast definition, and the OSM boundary is by
construction consistent with the OSM data being cut). Buildings and addresses are kept when their point on
surface is inside the boundary; highways when they intersect it (border roads stay whole). The boundary follows
the coast/maritime limit as mapped in OSM, so a few foreshore objects may be included.

``ni_address_points`` writes ``data/out/ni/ni_address_buildings.parquet`` (+ LICENCE sidecar): one row per
OSM address object. See ``COLUMN_DOCS``. When Microsoft footprint tiles exist (``msbuildings/ni/``) it also adds
``ms_building_id`` / ``ms_area_m2`` (the MS footprint containing the address point).

``ni_ms_coverage`` compares the OSM building polygons with the Microsoft GlobalML footprints (CDLA-Permissive-2.0):
an OSM building has an MS counterpart when some MS footprint has IoU > 0.5 with it. MS footprints with no such OSM
counterpart go to ``ni_ms_buildings_unmatched.parquet`` (GeoParquet, EPSG:29903) for coverage QA. The OSM (ODbL) and MS
(CDLA) data are kept in separate files; only the MS id and area (facts) are copied onto the ODbL address file.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import duckdb

from uprn_resolver.convert import _append_manifest, _connect, _free_gb, _load_spatial, _q

MIN_FREE_GB = 15
NI = "ni"
NI_RELATION = 156393
LICENCE = "ODbL-1.0"
ATTRIBUTION = "© OpenStreetMap contributors"
CRS_GRID = "EPSG:29903"
STREET_RADIUS_M = 50.0
LICENCE_NAME = "ni_address_buildings.LICENCE.txt"
OUT_NAME = "ni_address_buildings.parquet"
MS_UNMATCHED_NAME = "ni_ms_buildings_unmatched.parquet"
MS_STATS_NAME = "ni_ms_coverage.json"
MS_LICENCE = "CDLA-Permissive-2.0"
MS_ATTRIBUTION = (
    "Contains Microsoft Building Footprints (GlobalMLBuildingFootprints), licensed under CDLA-Permissive-2.0"
)
IOU_MATCH = 0.5

LICENCE_TEXT = """\
ni_address_buildings.parquet -- OpenStreetMap-derived address objects and building polygons for Northern Ireland.

Contains information from OpenStreetMap ({release} extract via Geofabrik), (c) OpenStreetMap contributors,
made available under the Open Database Licence (ODbL 1.0): https://opendatacommons.org/licenses/odbl/1-0/
Attribution: (c) OpenStreetMap contributors. https://www.openstreetmap.org/copyright

This file is a Derivative Database under the ODbL. Share-alike applies: if you publicly use it, or a database
built from it, you must offer it under the ODbL. Keep it SEPARATE from the OGL outputs; do not merge the two into
a single published database.

NO UPRNs. Northern Ireland UPRNs are only in OSNI Pointer / AddressBase Islands (paid). Nothing in this file is a
UPRN or can be joined to one by key; it is a best-effort open address/building layer built from crowd-sourced OSM
tags. Address coverage is sparse and uneven, and OSM house numbers are hints, not authoritative addresses.
ms_building_id / ms_area_m2 are identifiers/areas of Microsoft Building Footprints (CDLA-Permissive-2.0,
https://cdla.dev/permissive-2-0/): Contains Microsoft Building Footprints (GlobalMLBuildingFootprints). MS polygons
themselves are not included here.
Coordinates: geometry / x / y are EPSG:29903 (Irish Grid, PROJ transform); lon / lat are WGS84.

Columns:
{docs}
"""

COLUMN_DOCS = {
    "osm_type": "'node' | 'way' | 'relation' of the OSM address object (NOT a UPRN)",
    "osm_id": "OSM id of the address object (with osm_type; the only identifier; NOT a UPRN)",
    "housenumber": "addr:housenumber",
    "housename": "addr:housename",
    "unit": "addr:unit or addr:flats",
    "street": "addr:street, else the building's own addr:street, else nearest named highway within 50 m",
    "street_source": "'osm_addr' | 'building_addr' | 'nearest_highway_50m' | NULL",
    "street_distance_m": "distance to the street, only when street_source = 'nearest_highway_50m'",
    "postcode": "addr:postcode, else the building's own addr:postcode (OSM tag only; no postcode lookup)",
    "postcode_source": "'osm_addr' | 'building_addr' | NULL",
    "city": "addr:city",
    "building_osm_type": "osm_type of the matched building polygon, NULL if none",
    "building_osm_id": "osm_id of the matched building polygon, NULL if none",
    "building": "value of the building=* tag of the matched polygon",
    "building_match": "'own_building' (the address object is itself the building) | 'inside_building' "
    "(address point inside the smallest containing building polygon) | 'none'",
    "n_buildings_containing": "number of building polygons containing the address point (>1 = nested/ambiguous)",
    "addresses_in_building": "number of address objects matched to the same building (>1 = flats/multi-address)",
    "building_area_m2": "area of the matched building polygon (EPSG:29903 plane area, m2)",
    "ms_building_id": "ms_id of the Microsoft GlobalML footprint (CDLA-Permissive-2.0) containing the address "
    "point (smallest if several), NULL if none; see data/parquet/msbuildings/ni",
    "ms_area_m2": "area of that Microsoft footprint (EPSG:29903 plane area, m2)",
    "has_housenumber": "housenumber present",
    "has_postcode": "postcode present (tag or building tag)",
    "has_street_tag": "street came from an OSM tag (not inferred from the nearest highway)",
    "confidence": "'high' = building matched, and housenumber/housename, tagged street and postcode all present, "
    "and building not ambiguous; 'medium' = building matched but street inferred / postcode missing / ambiguous "
    "nesting; 'low' = no building matched",
    "lon": "WGS84 longitude of the address point",
    "lat": "WGS84 latitude of the address point",
    "x": "Irish Grid (EPSG:29903) easting",
    "y": "Irish Grid (EPSG:29903) northing",
    "geometry": "Point WKB, EPSG:29903 (GeoParquet primary geometry)",
    "geometry_wgs84": "Point WKB, EPSG:4326",
    "licence": "ODbL-1.0",
}


_GTYPES = {"POINT": "Point", "LINESTRING": "LineString", "POLYGON": "Polygon", "MULTIPOLYGON": "MultiPolygon",
           "MULTILINESTRING": "MultiLineString"}  # fmt: skip


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)  # noqa: S603 - fixed argv, no shell


def _tag(key: str) -> str:
    return f"json_extract_string(properties, '$.\"{key}\"')"


def _crs_projjson() -> dict | None:
    try:
        from pyproj import CRS

        return CRS.from_epsg(29903).to_json_dict()
    except Exception:  # noqa: BLE001 - metadata is best-effort without pyproj
        return None


def _copy_geo(con: duckdb.DuckDBPyConnection, select_sql: str, out: Path, geoms: dict[str, str | None]) -> int:
    """COPY a query to ``out`` as GeoParquet 1.1 (atomic); ``geoms`` maps WKB column -> CRS ('grid'|'wgs84')."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.unlink(missing_ok=True)
    con.execute(f"CREATE OR REPLACE TEMP TABLE _geo_out AS {select_sql}")
    cols = {}
    for col, crs in geoms.items():
        types = [
            _GTYPES.get(r[0], r[0])
            for r in con.execute(
                f"SELECT DISTINCT ST_GeometryType(ST_GeomFromWKB({col})) FROM _geo_out WHERE {col} IS NOT NULL"
            ).fetchall()
        ]
        meta: dict = {"encoding": "WKB", "geometry_types": sorted(types)}
        if crs == "grid" and (pj := _crs_projjson()):
            meta["crs"] = pj
        bb = con.execute(
            f"SELECT min(ST_XMin(g)), min(ST_YMin(g)), max(ST_XMax(g)), max(ST_YMax(g)) "
            f"FROM (SELECT ST_GeomFromWKB({col}) g FROM _geo_out)"
        ).fetchone()
        if bb[0] is not None:
            meta["bbox"] = [float(v) for v in bb]
        cols[col] = meta
    geo = json.dumps({"version": "1.1.0", "primary_column": next(iter(geoms)), "columns": cols})
    con.execute(
        f"COPY _geo_out TO {_q(tmp)} (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 122880, "
        f"KV_METADATA {{geo: {_q(geo)}}})"
    )
    n = con.execute(f"SELECT count(*) FROM read_parquet({_q(tmp)})").fetchone()[0]
    tmp.replace(out)
    con.execute("DROP TABLE _geo_out")
    return int(n)


def _release_of_name(name: str) -> str:
    import re

    m = re.search(r"-(\d{6})\.osm\.pbf$", name)
    return f"20{m.group(1)[:2]}-{m.group(1)[2:4]}-{m.group(1)[4:]}" if m else "unknown"


def _boundary_geojson(pbf: Path, work: Path) -> Path:
    """Write the NI admin_level=4 boundary (WGS84 GeoJSON geometry) from the PBF itself."""
    rel = work / "ni_rel.osm.pbf"
    _run(["osmium", "getid", "-r", "--overwrite", "-o", str(rel), str(pbf), f"r{NI_RELATION}"])
    gj = work / "ni_rel.geojson"
    _run(["osmium", "export", "--overwrite", "-f", "geojson", "--geometry-types=polygon", "-o", str(gj), str(rel)])
    for f in json.loads(gj.read_text())["features"]:
        if f["properties"].get("admin_level") == "4" and f["properties"].get("boundary") == "administrative":
            out = work / "ni_boundary.geojson"
            out.write_text(json.dumps(f["geometry"]))
            return out
    raise RuntimeError(f"admin_level=4 polygon for relation {NI_RELATION} not found in {pbf.name}")


def _convert_features(con: duckdb.DuckDBPyConnection, seq: Path, boundary: Path, ni_dir: Path) -> dict:
    """Features GeoJSON-seq (osmium export, -u type_id) + boundary GeoJSON -> the three GeoParquet files."""
    con.execute(
        f"""CREATE TEMP VIEW feat AS SELECT id, properties, geometry,
            json_extract_string(geometry, '$.type') AS gtype
            FROM read_json({_q(seq)}, format='newline_delimited',
            columns={{id: 'VARCHAR', properties: 'JSON', geometry: 'JSON'}}, maximum_object_size=67108864)"""
    )
    con.execute(f"CREATE TEMP TABLE bnd AS SELECT ST_GeomFromGeoJSON({_q(boundary.read_text())}) AS g")
    tr = "ST_Transform({g}, 'EPSG:4326', '" + CRS_GRID + "', always_xy := true)"
    # one decoded, validated WGS84 geometry per feature; OSM type/id decoded as in osm.py ('a' = area id)
    con.execute(
        f"""CREATE TEMP TABLE f AS
        SELECT CASE WHEN id LIKE 'n%' THEN 'node' WHEN id LIKE 'w%' THEN 'way' WHEN id LIKE 'r%' THEN 'relation'
                    WHEN CAST(substr(id, 2) AS BIGINT) % 2 = 0 THEN 'way' ELSE 'relation' END AS osm_type,
               CASE WHEN id LIKE 'a%' THEN CAST(substr(id, 2) AS BIGINT) // 2
                    ELSE CAST(substr(id, 2) AS BIGINT) END AS osm_id,
               gtype, properties, CASE WHEN ST_IsValid(g) THEN g ELSE ST_MakeValid(g) END AS g
        FROM (SELECT id, properties, gtype, ST_GeomFromGeoJSON(geometry) AS g FROM feat
              WHERE gtype IN ('Point', 'LineString', 'Polygon', 'MultiPolygon')
                AND ({_tag("highway")} IS NOT NULL OR {_tag("building")} IS NOT NULL
                     OR {_tag("addr:housenumber")} IS NOT NULL OR {_tag("addr:housename")} IS NOT NULL))"""
    )
    con.execute(
        """CREATE TEMP TABLE fp AS
        SELECT *, CASE WHEN gtype = 'Point' THEN g
                       WHEN gtype = 'LineString' THEN ST_LineInterpolatePoint(g, 0.5)
                       ELSE ST_PointOnSurface(g) END AS pt
        FROM f"""
    )
    con.execute("CREATE TEMP TABLE fi AS SELECT fp.* FROM fp, bnd WHERE ST_Intersects(bnd.g, fp.pt)")
    con.execute(
        "CREATE TEMP TABLE fhw AS SELECT f.* FROM f, bnd WHERE gtype = 'LineString' AND ST_Intersects(bnd.g, f.g)"
    )
    addr_tags = (
        f"{_tag('addr:housenumber')} AS housenumber, {_tag('addr:housename')} AS housename, "
        f"{_tag('addr:street')} AS street, {_tag('addr:postcode')} AS postcode, {_tag('addr:city')} AS city"
    )
    g_grid = tr.format(g="g")
    pt_grid = tr.format(g="pt")
    out: dict = {}
    # ---- buildings
    out["buildings"] = _copy_geo(
        con,
        f"""SELECT osm_type, osm_id, building, name, housenumber, housename, street, postcode, city,
                ST_Area(ga) AS area_m2, ST_X(pt) AS lon, ST_Y(pt) AS lat, ST_X(ptg) AS x, ST_Y(ptg) AS y,
                ST_XMin(ga) AS xmin, ST_YMin(ga) AS ymin, ST_XMax(ga) AS xmax, ST_YMax(ga) AS ymax,
                ST_AsWKB(ga) AS geometry, ST_AsWKB(g) AS geometry_wgs84
            FROM (SELECT osm_type, osm_id, {_tag("building")} AS building, {_tag("name")} AS name, {addr_tags},
                    g, pt, {g_grid} AS ga, {pt_grid} AS ptg
                  FROM fi WHERE gtype IN ('Polygon', 'MultiPolygon') AND {_tag("building")} IS NOT NULL
                    AND {_tag("building")} <> 'no')
            ORDER BY ST_Hilbert(ptg)""",
        ni_dir / "buildings.parquet",
        {"geometry": "grid", "geometry_wgs84": "wgs84"},
    )
    # ---- addresses
    out["addresses"] = _copy_geo(
        con,
        f"""SELECT osm_type, osm_id, housenumber, housename, unit, street, postcode, city, is_building,
                ST_X(pt) AS lon, ST_Y(pt) AS lat, ST_X(ptg) AS x, ST_Y(ptg) AS y,
                ST_AsWKB(ptg) AS geometry, ST_AsWKB(pt) AS geometry_wgs84
            FROM (SELECT osm_type, osm_id, {addr_tags},
                    coalesce({_tag("addr:unit")}, {_tag("addr:flats")}) AS unit,
                    {_tag("building")} IS NOT NULL AS is_building, pt, {pt_grid} AS ptg
                  FROM fi WHERE gtype IN ('Point', 'Polygon', 'MultiPolygon') AND {_tag("highway")} IS NULL
                    AND ({_tag("addr:housenumber")} IS NOT NULL OR {_tag("addr:housename")} IS NOT NULL))
            ORDER BY ST_Hilbert(ptg)""",
        ni_dir / "addresses.parquet",
        {"geometry": "grid", "geometry_wgs84": "wgs84"},
    )
    # ---- highways
    out["highways"] = _copy_geo(
        con,
        f"""SELECT way_id, highway, name, ST_XMin(ga) AS xmin, ST_YMin(ga) AS ymin, ST_XMax(ga) AS xmax,
                ST_YMax(ga) AS ymax, ST_AsWKB(ga) AS geometry, ST_AsWKB(g) AS geometry_wgs84
            FROM (SELECT osm_id AS way_id, {_tag("highway")} AS highway, {_tag("name")} AS name, g, {g_grid} AS ga
                  FROM fhw WHERE osm_type = 'way' AND {_tag("highway")} IS NOT NULL AND {_tag("name")} IS NOT NULL)
            ORDER BY ST_Hilbert(ga)""",
        ni_dir / "highways.parquet",
        {"geometry": "grid", "geometry_wgs84": "wgs84"},
    )
    return out


def extract_ni(pbf: Path, parquet_dir: Path) -> dict:
    """Cut Northern Ireland out of a UK/GB+NI PBF -> ``parquet_dir/ni/{buildings,addresses,highways}.parquet``.

    The PBF is only read (never modified or deleted). Counts are recorded in ``manifest.json``.
    """
    pbf, parquet_dir = Path(pbf), Path(parquet_dir)
    if not pbf.exists():
        raise FileNotFoundError(pbf)
    if shutil.which("osmium") is None:
        raise RuntimeError("osmium not found on PATH (e.g. `brew install osmium-tool`)")
    free = _free_gb(parquet_dir if parquet_dir.exists() else parquet_dir.parent)
    if free < MIN_FREE_GB:
        raise RuntimeError(f"only {free:.1f} GB free; need at least {MIN_FREE_GB} GB. Aborting.")
    ni_dir = parquet_dir / NI
    ni_dir.mkdir(parents=True, exist_ok=True)
    release = _release_of_name(pbf.name)
    work = parquet_dir.parent / "tmp" / "ni_work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    t0 = time.time()
    try:
        boundary = _boundary_geojson(pbf, work)
        cut = work / "ni.osm.pbf"
        # smart strategy keeps multipolygon building relations whole
        _run(["osmium", "extract", "--overwrite", "-s", "smart", "-p", str(boundary), "-o", str(cut), str(pbf)])
        filt = work / "filtered.osm.pbf"
        _run([
            "osmium", "tags-filter", "--overwrite", "-o", str(filt), str(cut),
            "w/highway", "nwr/building", "nwr/addr:housenumber", "nwr/addr:housename",
        ])  # fmt: skip
        cut.unlink()
        seq = work / "features.geojsonseq"
        _run([
            "osmium", "export", "--overwrite", "-f", "geojsonseq", "-x", "print_record_separator=false",
            "-u", "type_id", "--geometry-types=point,linestring,polygon", "-o", str(seq), str(filt),
        ])  # fmt: skip
        filt.unlink()
        con = _connect(parquet_dir, "8GB")
        try:
            _load_spatial(con)
            counts = _convert_features(con, seq, boundary, ni_dir)
        finally:
            con.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if min(counts.values()) <= 0:
        raise RuntimeError(f"NI extraction produced empty output: {counts}")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for key, n in counts.items():
        p = ni_dir / f"{key}.parquet"
        _append_manifest(
            parquet_dir,
            f"{NI}/{key}.parquet",
            {
                "dataset": f"{NI}/{key}",
                "source": pbf.name,
                "source_size": pbf.stat().st_size,
                "rows": n,
                "parquet_size": p.stat().st_size,
                "release": release,
                "licence": LICENCE,
                "attribution": ATTRIBUTION,
                "crs": f"{CRS_GRID} (+ EPSG:4326 geometry_wgs84)",
                "boundary": f"OSM relation {NI_RELATION} (admin_level=4)",
                "converted_at": now,
            },
        )
    return {"release": release, **counts, "seconds": round(time.time() - t0, 1)}


def _release_of(parquet_dir: Path) -> str:
    try:
        return json.loads((parquet_dir / "manifest.json").read_text())["converted"][f"{NI}/buildings.parquet"][
            "release"
        ]
    except (OSError, KeyError, json.JSONDecodeError):
        return "unknown"


def _ms_glob(parquet_dir: Path) -> str:
    return str(Path(parquet_dir) / "msbuildings" / NI / "*.parquet")


def _has_ms(parquet_dir: Path) -> bool:
    return any((Path(parquet_dir) / "msbuildings" / NI).glob("*.parquet"))


def ni_address_points(parquet_dir: Path, out_dir: Path, memory_limit: str = "8GB") -> dict:
    """Write ``out_dir/ni/ni_address_buildings.parquet`` (+ LICENCE sidecar); return stats.

    One row per OSM address object (``addresses.parquet``). Building: the object itself if it is a building
    polygon (``own_building``), else the smallest building polygon containing its point (``inside_building``).
    Street: addr:street, else the building's addr:street, else the nearest named highway within 50 m.
    Postcode: addr:postcode, else the building's. No UPRNs (see ``COLUMN_DOCS``).
    """
    parquet_dir, out_dir = Path(parquet_dir), Path(out_dir)
    ni_dir = parquet_dir / NI
    bpq, apq, hpq = ni_dir / "buildings.parquet", ni_dir / "addresses.parquet", ni_dir / "highways.parquet"
    for p in (bpq, apq, hpq):
        if not p.exists():
            raise FileNotFoundError(p)
    con = _connect(parquet_dir, memory_limit)
    try:
        _load_spatial(con)
        con.execute(
            f"""CREATE TEMP TABLE b AS
            SELECT osm_type, osm_id, building, street, postcode, area_m2, geometry AS g
            FROM read_parquet({_q(bpq)})"""
        )
        con.execute(
            f"""CREATE TEMP TABLE a AS
            SELECT row_number() OVER () AS aid, * EXCLUDE (geometry, geometry_wgs84),
                   geometry AS g, ST_AsWKB(geometry) AS geometry_wkb, ST_AsWKB(geometry_wgs84) AS wgs_wkb
            FROM read_parquet({_q(apq)})"""
        )
        # own building (same OSM object)
        con.execute(
            """CREATE TEMP TABLE m_own AS
            SELECT a.aid, b.osm_type AS bt, b.osm_id AS bid, 1 AS n_contain
            FROM a JOIN b ON a.is_building AND a.osm_type = b.osm_type AND a.osm_id = b.osm_id"""
        )
        # containing buildings for the rest
        con.execute(
            """CREATE TEMP TABLE inside AS
            SELECT a.aid, b.osm_type AS bt, b.osm_id AS bid, b.area_m2
            FROM (SELECT aid, g FROM a WHERE aid NOT IN (SELECT aid FROM m_own)) a
            JOIN b ON ST_Within(a.g, b.g)"""
        )
        con.execute(
            """CREATE TEMP TABLE m_in AS
            SELECT aid, bt, bid, n_contain FROM (
                SELECT aid, bt, bid, count(*) OVER (PARTITION BY aid) AS n_contain,
                       row_number() OVER (PARTITION BY aid ORDER BY area_m2, bt, bid) AS rn FROM inside)
            WHERE rn = 1"""
        )
        # Microsoft footprint containing the address point (smallest wins; deterministic tie-break on ms_id)
        if _has_ms(parquet_dir):
            con.execute(
                f"""CREATE TEMP TABLE msm AS
                SELECT aid, ms_id, area_m2 FROM (
                    SELECT a.aid, m.ms_id, m.area_m2,
                           row_number() OVER (PARTITION BY a.aid ORDER BY m.area_m2, m.ms_id) AS rn
                    FROM a JOIN (SELECT ms_id, area_m2, geometry AS g FROM read_parquet({_q(_ms_glob(parquet_dir))})) m
                      ON ST_Within(a.g, m.g))
                WHERE rn = 1"""
            )
        else:
            con.execute("CREATE TEMP TABLE msm (aid BIGINT, ms_id VARCHAR, area_m2 DOUBLE)")
        # nearest named highway within 50 m, only for addresses that will lack a street tag
        r = STREET_RADIUS_M
        con.execute(
            f"""CREATE TEMP TABLE need AS
            SELECT a.aid, ST_MakeEnvelope(a.x - {r}, a.y - {r}, a.x + {r}, a.y + {r}) AS box, a.g
            FROM a LEFT JOIN m_own o USING (aid) LEFT JOIN m_in i USING (aid)
            LEFT JOIN b ON b.osm_type = coalesce(o.bt, i.bt) AND b.osm_id = coalesce(o.bid, i.bid)
            WHERE a.street IS NULL AND b.street IS NULL"""
        )
        con.execute(
            f"""CREATE TEMP TABLE hw AS
            SELECT way_id, name, geometry AS g FROM read_parquet({_q(hpq)})"""
        )
        con.execute(
            f"""CREATE TEMP TABLE near AS
            SELECT aid, name, d FROM (
                SELECT n.aid, h.name, ST_Distance(n.g, h.g) AS d,
                       row_number() OVER (PARTITION BY n.aid ORDER BY ST_Distance(n.g, h.g), h.way_id) AS rn
                FROM need n JOIN hw h ON ST_Intersects(n.box, h.g))
            WHERE rn = 1 AND d <= {r}"""
        )
        sql = f"""
            SELECT osm_type, osm_id, housenumber, housename, unit, street, street_source, street_distance_m,
                   postcode, postcode_source, city, building_osm_type, building_osm_id, building, building_match,
                   n_buildings_containing, addresses_in_building, building_area_m2,
                   ms_building_id, ms_area_m2,
                   housenumber IS NOT NULL OR housename IS NOT NULL AS has_housenumber,
                   postcode IS NOT NULL AS has_postcode,
                   coalesce(street_source IN ('osm_addr', 'building_addr'), false) AS has_street_tag,
                   CASE WHEN building_match = 'none' THEN 'low'
                        WHEN street_source IN ('osm_addr', 'building_addr') AND postcode IS NOT NULL
                             AND n_buildings_containing = 1 THEN 'high'
                        ELSE 'medium' END AS confidence,
                   lon, lat, x, y, geometry, geometry_wgs84, '{LICENCE}' AS licence
            FROM (
                SELECT a.osm_type, a.osm_id, a.housenumber, a.housename, a.unit,
                       coalesce(a.street, b.street, nr.name) AS street,
                       CASE WHEN a.street IS NOT NULL THEN 'osm_addr' WHEN b.street IS NOT NULL THEN 'building_addr'
                            WHEN nr.name IS NOT NULL THEN 'nearest_highway_50m' END AS street_source,
                       CASE WHEN a.street IS NULL AND b.street IS NULL THEN round(nr.d, 1) END AS street_distance_m,
                       coalesce(a.postcode, b.postcode) AS postcode,
                       CASE WHEN a.postcode IS NOT NULL THEN 'osm_addr' WHEN b.postcode IS NOT NULL
                            THEN 'building_addr' END AS postcode_source,
                       a.city, mm.bt AS building_osm_type, mm.bid AS building_osm_id, b.building,
                       CASE WHEN o.aid IS NOT NULL THEN 'own_building' WHEN mm.aid IS NOT NULL
                            THEN 'inside_building' ELSE 'none' END AS building_match,
                       coalesce(mm.n_contain, 0) AS n_buildings_containing,
                       CASE WHEN mm.aid IS NOT NULL THEN count(*) OVER (PARTITION BY mm.bt, mm.bid) END
                            AS addresses_in_building,
                       b.area_m2 AS building_area_m2, ms.ms_id AS ms_building_id, ms.area_m2 AS ms_area_m2,
                       a.lon, a.lat, a.x, a.y, a.geometry_wkb AS geometry, a.wgs_wkb AS geometry_wgs84
                FROM a LEFT JOIN m_own o USING (aid)
                  LEFT JOIN (SELECT * FROM m_own UNION ALL SELECT * FROM m_in) mm ON mm.aid = a.aid
                  LEFT JOIN b ON b.osm_type = mm.bt AND b.osm_id = mm.bid
                  LEFT JOIN near nr ON nr.aid = a.aid
                  LEFT JOIN msm ms ON ms.aid = a.aid)
            ORDER BY ST_Hilbert(ST_GeomFromWKB(geometry))"""
        out = out_dir / NI / OUT_NAME
        n = _copy_geo(con, sql, out, {"geometry": "grid", "geometry_wgs84": "wgs84"})
        q = f"read_parquet({_q(out)})"
        row = con.execute(
            f"""SELECT count(*) FILTER (WHERE building_match <> 'none'), count(*) FILTER (WHERE has_postcode),
                   count(*) FILTER (WHERE has_street_tag), count(*) FILTER (WHERE street IS NOT NULL),
                   count(*) FILTER (WHERE confidence = 'high'), count(*) FILTER (WHERE confidence = 'medium'),
                   count(*) FILTER (WHERE confidence = 'low'),
                   count(DISTINCT (building_osm_type, building_osm_id))
                       FILTER (WHERE building_osm_id IS NOT NULL),
                   count(*) FILTER (WHERE ms_building_id IS NOT NULL) FROM {q}"""
        ).fetchone()
        n_b = con.execute(f"SELECT count(*) FROM read_parquet({_q(bpq)})").fetchone()[0]
    finally:
        con.close()
    keys = ("with_building", "with_postcode", "with_street_tag", "with_street", "high", "medium", "low")
    stats = {
        "addresses": n,
        "buildings": n_b,
        "buildings_with_address": row[7],
        "with_ms_building": row[8],
        **dict(zip(keys, row[:7], strict=True)),
    }
    docs = "\n".join(f"  {k}: {v}" for k, v in COLUMN_DOCS.items())
    (out.parent / LICENCE_NAME).write_text(LICENCE_TEXT.format(release=_release_of(parquet_dir), docs=docs))
    return stats


def ni_ms_coverage(parquet_dir: Path, out_dir: Path, memory_limit: str = "8GB") -> dict:
    """Compare OSM building polygons with Microsoft footprints (both EPSG:29903); write the MS-only polygons.

    OSM building counterpart: some MS footprint with IoU > 0.5 (intersection area / union area). Writes
    ``out_dir/ni/ni_ms_buildings_unmatched.parquet`` (GeoParquet: MS footprints with no IoU > 0.5 OSM building,
    columns ms_id, area_m2, max_iou_osm, overlaps_osm, lon/lat bbox centre, x/y centroid, geometry, licence,
    attribution) and ``ni_ms_coverage.json`` (the returned stats). Raises if there are no MS tiles.
    """
    parquet_dir, out_dir = Path(parquet_dir), Path(out_dir)
    bpq = parquet_dir / NI / "buildings.parquet"
    if not bpq.exists():
        raise FileNotFoundError(bpq)
    if not _has_ms(parquet_dir):
        raise FileNotFoundError(f"no Microsoft NI tiles under {Path(parquet_dir) / 'msbuildings' / NI}")
    apq = out_dir / NI / OUT_NAME
    con = _connect(parquet_dir, memory_limit)
    try:
        _load_spatial(con)
        con.execute(
            f"""CREATE TEMP TABLE b AS SELECT osm_type, osm_id, ST_Area(geometry) AS a, geometry AS g
            FROM read_parquet({_q(bpq)})"""
        )
        con.execute(
            f"""CREATE TEMP TABLE m AS
            SELECT ms_id, ST_Area(geometry) AS a, geometry AS g, ST_Centroid(geometry) AS c,
                   (lon_min + lon_max) / 2 AS lon, (lat_min + lat_max) / 2 AS lat
            FROM read_parquet({_q(_ms_glob(parquet_dir))})"""
        )
        # every intersecting (OSM, MS) pair with its IoU
        con.execute(
            """CREATE TEMP TABLE pairs AS
            SELECT osm_type, osm_id, ms_id, i / (ba + ma - i) AS iou FROM (
                SELECT b.osm_type, b.osm_id, m.ms_id, b.a AS ba, m.a AS ma,
                       ST_Area(ST_Intersection(b.g, m.g)) AS i
                FROM b JOIN m ON ST_Intersects(b.g, m.g))"""
        )
        con.execute(
            """CREATE TEMP TABLE m_best AS
            SELECT ms_id, max(iou) AS max_iou FROM pairs GROUP BY ms_id"""
        )
        con.execute("CREATE TEMP TABLE b_best AS SELECT osm_type, osm_id, max(iou) AS max_iou FROM pairs GROUP BY ALL")
        n_b, n_m = con.execute("SELECT (SELECT count(*) FROM b), (SELECT count(*) FROM m)").fetchone()
        b_any = con.execute("SELECT count(*) FROM b_best").fetchone()[0]
        b_match = con.execute(f"SELECT count(*) FROM b_best WHERE max_iou > {IOU_MATCH}").fetchone()[0]
        m_any = con.execute("SELECT count(*) FROM m_best").fetchone()[0]
        m_match = con.execute(f"SELECT count(*) FROM m_best WHERE max_iou > {IOU_MATCH}").fetchone()[0]
        stats: dict = {
            "iou_threshold": IOU_MATCH,
            "osm_buildings": n_b,
            "ms_buildings": n_m,
            "osm_with_ms_counterpart": b_match,
            "osm_with_ms_counterpart_pct": round(100 * b_match / n_b, 2) if n_b else None,
            "osm_overlapping_any_ms": b_any,
            "osm_overlapping_any_ms_pct": round(100 * b_any / n_b, 2) if n_b else None,
            "ms_with_osm_counterpart": m_match,
            "ms_with_osm_counterpart_pct": round(100 * m_match / n_m, 2) if n_m else None,
            "ms_without_osm_counterpart": n_m - m_match,
            "ms_without_osm_counterpart_pct": round(100 * (n_m - m_match) / n_m, 2) if n_m else None,
            "ms_with_no_osm_overlap_at_all": n_m - m_any,
            "ms_with_no_osm_overlap_at_all_pct": round(100 * (n_m - m_any) / n_m, 2) if n_m else None,
        }
        if apq.exists():
            tot, hit = con.execute(
                f"""WITH ab AS (SELECT DISTINCT building_osm_type AS osm_type, building_osm_id AS osm_id
                               FROM read_parquet({_q(apq)}) WHERE building_osm_id IS NOT NULL)
                    SELECT count(*), count(*) FILTER (WHERE bb.max_iou > {IOU_MATCH})
                    FROM ab LEFT JOIN b_best bb USING (osm_type, osm_id)"""
            ).fetchone()
            stats["osm_addressed_buildings"] = tot
            stats["osm_addressed_buildings_with_ms_counterpart"] = hit
            stats["osm_addressed_buildings_with_ms_counterpart_pct"] = round(100 * hit / tot, 2) if tot else None
        sql = f"""SELECT m.ms_id, m.a AS area_m2, mb.max_iou AS max_iou_osm, mb.max_iou IS NOT NULL AS overlaps_osm,
                    m.lon, m.lat, ST_X(m.c) AS x, ST_Y(m.c) AS y, ST_AsWKB(m.g) AS geometry,
                    '{MS_LICENCE}' AS licence, '{MS_ATTRIBUTION}' AS attribution
                FROM m LEFT JOIN m_best mb USING (ms_id)
                WHERE coalesce(mb.max_iou, 0) <= {IOU_MATCH}
                ORDER BY ST_Hilbert(m.c)"""
        stats["unmatched_rows"] = _copy_geo(con, sql, out_dir / NI / MS_UNMATCHED_NAME, {"geometry": "grid"})
    finally:
        con.close()
    (out_dir / NI / MS_STATS_NAME).write_text(json.dumps(stats, indent=2))
    return stats
