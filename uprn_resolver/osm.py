"""OpenStreetMap companion table (ODbL-1.0): OSM street names and addr:* house numbers/names for UPRNs.

OSM data is licensed ODbL (share-alike). Everything here is kept in a SEPARATE file, ``streets_odbl.parquet``,
and must never be merged into the OGL ``uprn.parquet``.

National Parquet (``data/parquet/osm/``), written by ``convert_osm`` (EPSG:27700 via OSTN15, WKB):
  ``highways.parquet``   way_id BIGINT, highway, name, name_cy, name_gd, xmin/ymin/xmax/ymax, geom_wkb
                         (named highway ways only)
  ``addresses.parquet``  osm_type ('node'|'way'|'relation'), osm_id, housenumber, housename, street, unit, flats,
                         postcode, city, ref_gb_uprn (raw tag), is_building BOOL, x, y (node, or point on
                         surface of the polygon), geom_wkb (polygon for ways/relations, NULL for nodes)

Extraction uses ``osmium`` (tags-filter then export to GeoJSON-seq) and DuckDB spatial. The PBF is untrusted:
it is only ever read by osmium / DuckDB, never executed, and is deleted after the Parquet is verified.

Per-LAD output ``out/lad=<lad>/streets_odbl.parquet`` has one row per UPRN (see ``osm_for_lad``).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.request
import warnings
from pathlib import Path

import duckdb

from uprn_resolver import bng
from uprn_resolver.convert import _append_manifest, _connect, _copy, _free_gb, _load_spatial, _q

MIN_FREE_GB = 15
OSM = "osm"
GEOFABRIK = "https://download.geofabrik.de/europe/"
LICENCE = "ODbL-1.0"
ATTRIBUTION = "© OpenStreetMap contributors"
LICENCE_NAME = "streets_odbl.LICENCE.txt"
LICENCE_TEXT = """\
streets_odbl.parquet -- OpenStreetMap-derived street names and address tags per UPRN.

Contains information from OpenStreetMap ({release} extract via Geofabrik), (c) OpenStreetMap contributors,
made available under the Open Database Licence (ODbL 1.0): https://opendatacommons.org/licenses/odbl/1-0/
Attribution: (c) OpenStreetMap contributors. https://www.openstreetmap.org/copyright

This file is a Derivative Database under the ODbL. Share-alike applies: if you publicly use it, or a database
built from it, you must offer it under the ODbL. Keep it SEPARATE from the OGL uprn.parquet; do not merge
the two into a single published database.

Match methods (osm_addr_match_method), with osm_addr_confidence: ref_gb_uprn (exact ref:GB:uprn tag) [confidence
'high'] > inside_building_single (UPRN point inside an OSM building polygon with addr:* that contains exactly one
UPRN) [confidence 'medium'] > nearest_node_10m (mutual nearest addr:* node and UPRN within 10 m) [confidence
'low']. Lower-confidence fallbacks, never overriding the above:
inside_building_flats (UPRN inside an addr:* building holding several UPRNs, with flat evidence; the number
identifies the BUILDING, building_level_address=true), parcel_single_node (exactly one addr node in a
single-UPRN INSPIRE parcel) and nearest_node_25m_mutual (mutual nearest within 25 m with agreeing street
name); all 'low' because EPC agreement is below 85% (see CONFIDENCE).
There is one row per UPRN; UPRNs without coordinates have NULL fields.
See osm_addr_confidence. Rows are NULL where no match exists. OSM house numbers are
incomplete and crowd-sourced; treat as hints, not authoritative addresses.
"""

NAME_RADIUS_M = 50.0
NODE_RADIUS_M = 10.0
NODE_RADIUS_LOW_M = 25.0
# tier -> confidence. Rule: a fallback tier whose agreement with EPC house numbers is < 85% is 'low'. Measured on
# E09000019 (validate_against_epc): flats 79.8%, parcel_single_node 76.9% (n=39), nearest_node_25m_mutual 74.1%
# (n=58) -> all 'low'. Re-run the validation and promote a tier to 'medium' only if it reaches >= 85%.
# Islington house-number agreement with EPC (EPC itself tops out ~95%): ref 94.6%,
# building_single 75.8%, node_10m 65.3%, flats 79.8%, parcel 76.9%, 25m 74.1%.
CONFIDENCE = {
    "ref_gb_uprn": "high",
    "inside_building_single": "medium",
    "nearest_node_10m": "low",
    "inside_building_flats": "low",
    "parcel_single_node": "low",
    "nearest_node_25m_mutual": "low",
}

_ABBREV = {
    "st": "street", "rd": "road", "ave": "avenue", "av": "avenue", "ln": "lane", "dr": "drive", "ct": "court",
    "cl": "close", "pl": "place", "sq": "square", "gdns": "gardens", "gdn": "garden", "cres": "crescent",
    "ter": "terrace", "terr": "terrace", "pk": "park", "gr": "grove", "hse": "house", "mt": "mount",
    "sth": "south", "nth": "north", "wy": "way", "cir": "circus", "pde": "parade", "rdg": "ridge",
    "ct.": "court", "bldgs": "buildings", "est": "estate", "pas": "passage", "wk": "walk",
}  # fmt: skip


def normalise_name(name: str | None) -> str | None:
    """Normalise a street name for comparison: case/punctuation/abbreviation-insensitive, leading 'the' dropped."""
    if name is None:
        return None
    s = name.lower().replace("&", " and ").replace("'", "").replace("’", "")
    toks = [_ABBREV.get(t, t) for t in re.sub(r"[^a-z0-9À-ɏ]+", " ", s).split()]
    if len(toks) > 1 and toks[0] == "the":
        toks = toks[1:]
    out = " ".join(toks)
    return out or None


# ---------------------------------------------------------------- download / conversion
def _latest_url(timeout: float = 30) -> tuple[str, str]:
    """Resolve great-britain-latest.osm.pbf to the dated file; return (url, 'YYMMDD')."""
    req = urllib.request.Request(GEOFABRIK + "great-britain-latest.osm.pbf", method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # follows the 307 to the dated file
        url = r.geturl()
    m = re.search(r"great-britain-(\d{6})\.osm\.pbf$", url)
    if not m:
        raise RuntimeError(f"cannot resolve dated Geofabrik URL from {url}")
    return url, m.group(1)


def _md5(path: Path) -> str:
    import hashlib

    h = hashlib.md5()  # noqa: S324 - integrity check against the published .md5, not security
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def download_osm(raw_dir: Path) -> Path:
    """Download the dated GB extract to ``raw_dir/osm/`` and verify its md5. Returns the .pbf path."""
    out = Path(raw_dir) / OSM
    out.mkdir(parents=True, exist_ok=True)
    free = _free_gb(out)
    if free < MIN_FREE_GB:
        raise RuntimeError(f"only {free:.1f} GB free; need at least {MIN_FREE_GB} GB. Aborting download.")
    url, stamp = _latest_url()
    dest = out / f"great-britain-{stamp}.osm.pbf"
    with urllib.request.urlopen(url + ".md5", timeout=60) as r:
        want = r.read().decode().split()[0]
    if not dest.exists() or _md5(dest) != want:
        part = dest.with_name(dest.name + ".part")
        with urllib.request.urlopen(url, timeout=120) as r, open(part, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 22)
        if _md5(part) != want:
            part.unlink()
            raise RuntimeError(f"md5 mismatch for {url}")
        part.replace(dest)
    return dest


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)  # noqa: S603 - fixed argv, no shell


def _tag(key: str) -> str:
    return f"json_extract_string(properties, '$.\"{key}\"')"


def convert_osm(pbf: Path, parquet_dir: Path, delete_source: bool = False) -> dict:
    """Convert a GB OSM PBF to ``parquet_dir/osm/{highways,addresses}.parquet``; record in the manifest.

    Returns ``{"highways": n, "addresses": n, ...counts}``. With ``delete_source`` the PBF is removed once
    the Parquet row counts are verified (> 0).
    """
    pbf, parquet_dir = Path(pbf), Path(parquet_dir)
    if not pbf.exists():
        raise FileNotFoundError(pbf)
    if shutil.which("osmium") is None:
        raise RuntimeError("osmium not found on PATH (e.g. `brew install osmium-tool`)")
    free = _free_gb(parquet_dir)
    if free < MIN_FREE_GB:
        raise RuntimeError(f"only {free:.1f} GB free; need at least {MIN_FREE_GB} GB. Aborting conversion.")
    m = re.search(r"-(\d{6})\.osm\.pbf$", pbf.name)
    release = f"20{m.group(1)[:2]}-{m.group(1)[2:4]}-{m.group(1)[4:]}" if m else "unknown"
    work = parquet_dir.parent / "tmp" / "osm_work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    t0 = time.time()
    try:
        filt = work / "filtered.osm.pbf"
        _run([
            "osmium", "tags-filter", "--overwrite", "-o", str(filt), str(pbf),
            "w/highway", "nwr/addr:housenumber", "nwr/addr:housename",
        ])  # fmt: skip
        seq = work / "features.geojsonseq"
        _run([
            "osmium", "export", "--overwrite", "-f", "geojsonseq", "-x", "print_record_separator=false",
            "-u", "type_id", "--geometry-types=point,linestring,polygon", "-o", str(seq), str(filt),
        ])  # fmt: skip
        filt.unlink()
        con = _connect(parquet_dir, "6GB")
        try:
            _load_spatial(con)
            con.execute(
                f"""CREATE TEMP VIEW feat AS SELECT id, properties, geometry,
                    json_extract_string(geometry, '$.type') AS gtype
                    FROM read_json({_q(seq)}, format='newline_delimited',
                    columns={{id: 'VARCHAR', properties: 'JSON', geometry: 'JSON'}}, maximum_object_size=67108864)"""
            )
            hw = parquet_dir / OSM / "highways.parquet"
            ad = parquet_dir / OSM / "addresses.parquet"
            g_hw = bng.duckdb_st_transform_sql("ST_GeomFromGeoJSON(geometry)")  # OSTN15, not DuckDB's Helmert
            n_hw = _copy(
                con,
                f"""SELECT way_id, highway, name, name_cy, name_gd,
                       ST_XMin(g) AS xmin, ST_YMin(g) AS ymin, ST_XMax(g) AS xmax, ST_YMax(g) AS ymax,
                       ST_AsWKB(g) AS geom_wkb
                    FROM (SELECT CAST(substr(id, 2) AS BIGINT) AS way_id, {_tag("highway")} AS highway,
                       {_tag("name")} AS name, {_tag("name:cy")} AS name_cy, {_tag("name:gd")} AS name_gd,
                       CASE WHEN gtype IN ('Polygon', 'MultiPolygon') THEN ST_Boundary({g_hw}) ELSE {g_hw} END AS g
                       FROM feat WHERE id LIKE 'w%' AND gtype IN ('LineString', 'Polygon', 'MultiPolygon')
                         AND {_tag("highway")} IS NOT NULL AND {_tag("name")} IS NOT NULL)
                    ORDER BY ST_Hilbert(g)""",
                hw,
            )
            g_ad = bng.duckdb_st_transform_sql("ST_GeomFromGeoJSON(geometry)")
            n_ad = _copy(
                con,
                f"""SELECT osm_type, osm_id, housenumber, housename, street, unit, flats, postcode, city,
                       ref_gb_uprn, is_building, ST_X(pt) AS x, ST_Y(pt) AS y,
                       CASE WHEN osm_type <> 'node' THEN ST_AsWKB(g) END AS geom_wkb
                    FROM (SELECT
                       CASE WHEN id LIKE 'n%' THEN 'node' WHEN id LIKE 'w%' THEN 'way'
                            WHEN id LIKE 'r%' THEN 'relation'
                            WHEN CAST(substr(id, 2) AS BIGINT) % 2 = 0 THEN 'way' ELSE 'relation' END AS osm_type,
                       CASE WHEN id LIKE 'a%' THEN CAST(substr(id, 2) AS BIGINT) // 2
                            ELSE CAST(substr(id, 2) AS BIGINT) END AS osm_id,
                       {_tag("addr:housenumber")} AS housenumber, {_tag("addr:housename")} AS housename,
                       {_tag("addr:street")} AS street, {_tag("addr:unit")} AS unit, {_tag("addr:flats")} AS flats,
                       {_tag("addr:postcode")} AS postcode, {_tag("addr:city")} AS city,
                       {_tag("ref:GB:uprn")} AS ref_gb_uprn, {_tag("building")} IS NOT NULL AS is_building,
                       {g_ad} AS g,
                       CASE WHEN gtype = 'Point' THEN {g_ad} ELSE ST_PointOnSurface({g_ad}) END AS pt
                       FROM feat WHERE gtype IN ('Point', 'Polygon', 'MultiPolygon')
                         AND ({_tag("addr:housenumber")} IS NOT NULL OR {_tag("addr:housename")} IS NOT NULL)
                         AND {_tag("highway")} IS NULL)
                    ORDER BY ST_Hilbert(pt)""",
                ad,
            )
            bng.assert_parquet_finite(con, hw, "xmin", "ymin", "xmax", "ymax")
            bng.assert_parquet_finite(con, ad, "x", "y")
        finally:
            con.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if n_hw <= 0 or n_ad <= 0:
        raise RuntimeError(f"OSM conversion produced no rows (highways={n_hw}, addresses={n_ad})")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for key, n, p in (("highways", n_hw, hw), ("addresses", n_ad, ad)):
        _append_manifest(
            parquet_dir,
            str(p.relative_to(parquet_dir)),
            {
                "dataset": f"{OSM}/{key}",
                "source": pbf.name,
                "source_size": pbf.stat().st_size,
                "rows": n,
                "parquet_size": p.stat().st_size,
                "release": release,
                "licence": LICENCE,
                "attribution": ATTRIBUTION,
                "converted_at": now,
            },
        )
    if delete_source:
        pbf.unlink()
    return {"release": release, "highways": n_hw, "addresses": n_ad, "seconds": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- per-LAD matching
def _release_of(parquet_dir: Path) -> str:
    try:
        m = json.loads((parquet_dir / "manifest.json").read_text())
        return m["converted"][f"{OSM}/highways.parquet"]["release"]
    except (OSError, KeyError, json.JSONDecodeError):
        return "unknown"


def _register_norm(con: duckdb.DuckDBPyConnection, table: str, src_sql: str) -> None:
    """Create ``table(name, norm)`` for the distinct names produced by ``src_sql`` (column ``name``)."""
    names = [r[0] for r in con.execute(f"SELECT DISTINCT name FROM ({src_sql}) WHERE name IS NOT NULL").fetchall()]
    con.execute(f"CREATE OR REPLACE TEMP TABLE {table} (name VARCHAR, norm VARCHAR)")
    if names:
        con.executemany(f"INSERT INTO {table} VALUES (?, ?)", [(n, normalise_name(n)) for n in names])


def osm_for_lad(lad: str, staged_dir: Path, out_dir: Path, parquet_dir: Path, memory_limit: str = "8GB") -> dict:
    """Write ``out_dir/lad=<lad>/streets_odbl.parquet`` (one row per UPRN) and the LICENCE sidecar; return stats.

    Columns: uprn, osm_street_name, osm_way_id, osm_street_distance_m, osm_agrees_os, osm_housenumber,
    osm_housename, osm_unit, osm_addr_street, osm_addr_postcode, osm_addr_match_method, osm_addr_osm_type,
    osm_addr_osm_id, osm_addr_confidence, building_level_address, licence.

    Street: the nearest named highway within 50 m, preferring (even if farther) a way within 50 m whose name,
    name:cy or name:gd matches the OS ``street_name``/``street_name_alt`` after normalisation.
    ``osm_agrees_os`` compares the *nearest* named way (unbiased by that preference) with the OS name; NULL
    unless both exist.

    Address (each OSM object is assigned to at most one UPRN, except through exact ``ref:GB:uprn`` tags, and
    objects carrying such a tag are used only for that method): ref_gb_uprn > inside_building_single >
    nearest_node_10m (mutual nearest node/UPRN pair within 10 m) -- confidence 'high', 'medium' and 'low'
    respectively (see CONFIDENCE) -- then the fallbacks inside_building_flats (building-level), parcel_single_node,
    nearest_node_25m_mutual (all 'low'). "Exactly one UPRN" in a building is counted against all national Open UPRN
    points, not just this LAD's. UPRNs without coordinates get a row with NULL fields.
    Fallbacks never override a stricter match; tiers b/c use each OSM object at most once.
    """
    staged = Path(staged_dir) / lad
    parquet_dir = Path(parquet_dir)
    out_lad = Path(out_dir) / f"lad={lad}"
    hw_pq, ad_pq = parquet_dir / OSM / "highways.parquet", parquet_dir / OSM / "addresses.parquet"
    for p in (staged / "onsud.parquet", staged / "open_uprn.parquet", hw_pq, ad_pq):
        if not p.exists():
            raise FileNotFoundError(p)
    streets_pq = staged / "streets.parquet"
    out_lad.mkdir(parents=True, exist_ok=True)
    con = _connect(parquet_dir, memory_limit)
    try:
        _load_spatial(con)
        st = f"LEFT JOIN read_parquet({_q(streets_pq)}) s USING (uprn)" if streets_pq.exists() else ""
        osn = "s.street_name" if streets_pq.exists() else "CAST(NULL AS VARCHAR)"
        osa = "s.street_name_alt" if streets_pq.exists() else "CAST(NULL AS VARCHAR)"
        con.execute(f"""
            CREATE TEMP TABLE u AS
            SELECT o.uprn, p.x, p.y, {osn} AS os_name, {osa} AS os_alt
            FROM (SELECT DISTINCT uprn FROM read_parquet({_q(staged / "onsud.parquet")})) o
            LEFT JOIN read_parquet({_q(staged / "open_uprn.parquet")}) p USING (uprn)
            {st}""")
        n_uprn, *bbox = con.execute("SELECT count(x), min(x), min(y), max(x), max(y) FROM u").fetchone()
        if not n_uprn:
            raise RuntimeError(f"{lad}: no UPRNs with coordinates")
        bx0, by0, bx1, by1 = (float(v) for v in bbox)
        pad = 120.0
        # Occupied 1 km grid cells (dilated by one cell, > the 500 m national pad): the LAD bbox can be huge when
        # a UPRN's coordinate is an outlier, so all spatial prefilters use these cells, not the bbox alone.
        con.execute("""
            CREATE TEMP TABLE cells AS
            SELECT DISTINCT cx + dx AS cx, cy + dy AS cy FROM (
                SELECT DISTINCT CAST(floor(x / 1000) AS BIGINT) AS cx, CAST(floor(y / 1000) AS BIGINT) AS cy
                FROM u WHERE x IS NOT NULL), (SELECT unnest([-1, 0, 1]) AS dx), (SELECT unnest([-1, 0, 1]) AS dy)""")
        # ---------------- streets
        r = NAME_RADIUS_M
        con.execute(f"""
            CREATE TEMP TABLE h AS
            SELECT way_id, name, name_cy, name_gd, xmin - {r} AS xmin, ymin - {r} AS ymin,
                   xmax + {r} AS xmax, ymax + {r} AS ymax, ST_GeomFromWKB(geom_wkb) AS g
            FROM read_parquet({_q(hw_pq)})
            WHERE xmax >= {bx0 - pad} AND xmin <= {bx1 + pad} AND ymax >= {by0 - pad} AND ymin <= {by1 + pad}
              AND way_id IN (
                SELECT way_id FROM (
                    SELECT way_id, cx, unnest(range(cy0, cy1 + 1)) AS cy FROM (
                        SELECT way_id, unnest(range(cx0, cx1 + 1)) AS cx, cy0, cy1 FROM (
                            SELECT way_id, CAST(floor((xmin - {r + pad}) / 1000) AS BIGINT) AS cx0,
                                   CAST(floor((xmax + {r + pad}) / 1000) AS BIGINT) AS cx1,
                                   CAST(floor((ymin - {r + pad}) / 1000) AS BIGINT) AS cy0,
                                   CAST(floor((ymax + {r + pad}) / 1000) AS BIGINT) AS cy1
                            FROM read_parquet({_q(hw_pq)})
                            WHERE xmax >= {bx0 - pad} AND xmin <= {bx1 + pad}
                              AND ymax >= {by0 - pad} AND ymin <= {by1 + pad})))
                    JOIN cells USING (cx, cy))""")
        _register_norm(
            con,
            "hn",
            "SELECT name FROM h UNION SELECT name_cy FROM h UNION SELECT name_gd FROM h "
            "UNION SELECT os_name FROM u UNION SELECT os_alt FROM u",
        )
        con.execute("""
            CREATE TEMP TABLE hnorm AS
            SELECT h.way_id, h.name, h.xmin, h.ymin, h.xmax, h.ymax, h.g,
                   a.norm AS n0, b.norm AS n1, c.norm AS n2
            FROM h LEFT JOIN hn a ON a.name = h.name LEFT JOIN hn b ON b.name = h.name_cy
                   LEFT JOIN hn c ON c.name = h.name_gd""")
        con.execute("""
            CREATE TEMP TABLE unorm AS
            SELECT u.uprn, u.x, u.y, u.os_name, a.norm AS o0, b.norm AS o1
            FROM u LEFT JOIN hn a ON a.name = u.os_name LEFT JOIN hn b ON b.name = u.os_alt""")
        con.execute(f"""
            CREATE TEMP TABLE cand AS
            SELECT *, coalesce(o0 IN (n0, n1, n2), false) OR coalesce(o1 IN (n0, n1, n2), false) AS os_match FROM (
                SELECT u.uprn, h.way_id, h.name, ST_Distance(ST_Point(u.x, u.y), h.g) AS d,
                       u.o0, u.o1, h.n0, h.n1, h.n2
                FROM unorm u JOIN hnorm h ON u.x BETWEEN h.xmin AND h.xmax AND u.y BETWEEN h.ymin AND h.ymax)
            WHERE d <= {r}""")
        con.execute("""
            CREATE TEMP TABLE street AS
            SELECT c.uprn, c.name AS osm_street_name, c.way_id AS osm_way_id,
                   round(c.d, 1) AS osm_street_distance_m,
                   CASE WHEN u.o0 IS NULL AND u.o1 IS NULL THEN NULL
                        ELSE coalesce(n.n0 = u.o0, false) OR coalesce(n.n0 = u.o1, false) END AS osm_agrees_os
            FROM (SELECT *, row_number() OVER (PARTITION BY uprn ORDER BY coalesce(NOT os_match, true), d, way_id) AS rn
                  FROM cand) c
            JOIN unorm u USING (uprn)
            JOIN (SELECT uprn, n0 FROM (
                      SELECT uprn, n0, row_number() OVER (PARTITION BY uprn ORDER BY d, way_id) AS rn FROM cand)
                  WHERE rn = 1) n USING (uprn)
            WHERE c.rn = 1""")
        # ---------------- addresses
        con.execute(f"""
            CREATE TEMP TABLE a AS
            SELECT row_number() OVER () AS aid, osm_type, osm_id, housenumber, housename, street,
                   coalesce(unit, flats) AS unit, postcode, ref_gb_uprn, is_building, x, y, geom_wkb
            FROM read_parquet({_q(ad_pq)})
            WHERE x BETWEEN {bx0 - pad} AND {bx1 + pad} AND y BETWEEN {by0 - pad} AND {by1 + pad}
              AND (CAST(floor(x / 1000) AS BIGINT), CAST(floor(y / 1000) AS BIGINT)) IN (SELECT (cx, cy) FROM cells)""")
        con.execute("""
            CREATE TEMP TABLE m (uprn BIGINT, aid BIGINT, method VARCHAR)""")
        # 1. exact ref:GB:uprn (a tag may hold several ';'-separated UPRNs)
        con.execute("""
            INSERT INTO m
            SELECT uprn, aid, 'ref_gb_uprn' FROM (
                SELECT r.uprn, r.aid,
                       row_number() OVER (PARTITION BY r.uprn
                                  ORDER BY (a.housenumber IS NULL), a.osm_type, a.osm_id) AS rn
                FROM (SELECT aid, TRY_CAST(trim(v) AS BIGINT) AS uprn
                      FROM (SELECT aid, unnest(string_split(ref_gb_uprn, ';')) AS v FROM a
                            WHERE ref_gb_uprn IS NOT NULL)) r
                JOIN a USING (aid) JOIN u USING (uprn))
            WHERE rn = 1""")
        # 2. UPRN inside an addr-bearing building polygon holding exactly one UPRN
        con.execute("""
            CREATE TEMP TABLE poly AS
            SELECT aid, ST_GeomFromWKB(geom_wkb) AS g, ST_XMin(ST_GeomFromWKB(geom_wkb)) AS x0,
                   ST_YMin(ST_GeomFromWKB(geom_wkb)) AS y0, ST_XMax(ST_GeomFromWKB(geom_wkb)) AS x1,
                   ST_YMax(ST_GeomFromWKB(geom_wkb)) AS y1, ST_Area(ST_GeomFromWKB(geom_wkb)) AS area
            FROM a WHERE geom_wkb IS NOT NULL AND is_building AND ref_gb_uprn IS NULL""")
        # bucket polygons into 64 m grid cells (one row per covered cell) so point-in-polygon is an equi-join
        con.execute("""
            CREATE TEMP TABLE pcell AS
            SELECT aid, cx, unnest(range(CAST(floor(y0 / 64) AS BIGINT), CAST(floor(y1 / 64) AS BIGINT) + 1)) AS cy
            FROM (SELECT aid, y0, y1, unnest(range(CAST(floor(x0 / 64) AS BIGINT),
                                                   CAST(floor(x1 / 64) AS BIGINT) + 1)) AS cx FROM poly)""")
        con.execute("""
            CREATE TEMP TABLE inside AS
            SELECT p.aid, u.uprn, p.area FROM u
            JOIN pcell c ON c.cx = CAST(floor(u.x / 64) AS BIGINT) AND c.cy = CAST(floor(u.y / 64) AS BIGINT)
            JOIN poly p ON p.aid = c.aid
            WHERE ST_Within(ST_Point(u.x, u.y), p.g)""")
        # occupants are counted against ALL national Open UPRN points (not just this LAD's), so a building on a LAD
        # boundary with a UPRN in the neighbouring LAD is not mistaken for a single-UPRN building
        nat_pq = parquet_dir / "open_uprn" / "open_uprn.parquet"
        if not nat_pq.exists():
            warnings.warn(f"{nat_pq} missing: building occupancy counted from this LAD's UPRNs only", stacklevel=2)
            nat_pq = staged / "open_uprn.parquet"
        npad = 500.0
        con.execute(f"""
            CREATE TEMP TABLE nat AS
            SELECT uprn, x, y FROM read_parquet({_q(nat_pq)})
            WHERE x BETWEEN {bx0 - npad} AND {bx1 + npad} AND y BETWEEN {by0 - npad} AND {by1 + npad}
              AND (CAST(floor(x / 1000) AS BIGINT), CAST(floor(y / 1000) AS BIGINT)) IN (SELECT (cx, cy) FROM cells)""")
        con.execute("""
            CREATE TEMP TABLE occupants AS
            SELECT p.aid, count(DISTINCT n.uprn) AS n_uprn FROM nat n
            JOIN pcell c ON c.cx = CAST(floor(n.x / 64) AS BIGINT) AND c.cy = CAST(floor(n.y / 64) AS BIGINT)
            JOIN poly p ON p.aid = c.aid
            WHERE ST_Within(ST_Point(n.x, n.y), p.g) GROUP BY p.aid""")
        con.execute("""
            INSERT INTO m
            SELECT uprn, aid, 'inside_building_single' FROM (
                SELECT uprn, aid, row_number() OVER (PARTITION BY uprn ORDER BY area, aid) AS rn
                FROM inside WHERE aid IN (SELECT aid FROM occupants WHERE n_uprn = 1)
                  AND uprn NOT IN (SELECT uprn FROM m))
            WHERE rn = 1""")
        # 3. mutual nearest addr node <-> UPRN within 10 m (nearest computed over all, then used ones dropped)
        nr = NODE_RADIUS_M
        con.execute(f"""
            CREATE TEMP TABLE pairs AS
            SELECT a.aid, u.uprn, sqrt((u.x - a.x)^2 + (u.y - a.y)^2) AS d
            FROM (SELECT a0.aid, a0.x, a0.y, CAST(floor(a0.x / {nr}) AS BIGINT) + dx AS cx,
                         CAST(floor(a0.y / {nr}) AS BIGINT) + dy AS cy
                  FROM (SELECT * FROM a WHERE osm_type = 'node' AND ref_gb_uprn IS NULL) a0,
                       (SELECT unnest([-1, 0, 1]) AS dx), (SELECT unnest([-1, 0, 1]) AS dy)) a
            JOIN (SELECT *, CAST(floor(x / {nr}) AS BIGINT) AS cx, CAST(floor(y / {nr}) AS BIGINT) AS cy FROM u
                  WHERE x IS NOT NULL) u
              ON u.cx = a.cx AND u.cy = a.cy
            WHERE sqrt((u.x - a.x)^2 + (u.y - a.y)^2) <= {nr}""")
        con.execute("""
            CREATE TEMP TABLE best_u AS
            SELECT aid, uprn, d, count(*) OVER (PARTITION BY aid, d) AS ties FROM (
                SELECT *, row_number() OVER (PARTITION BY aid ORDER BY d, uprn) AS rn FROM pairs) WHERE rn = 1""")
        con.execute("""
            CREATE TEMP TABLE best_a AS
            SELECT aid, uprn, d, count(*) OVER (PARTITION BY uprn, d) AS ties FROM (
                SELECT *, row_number() OVER (PARTITION BY uprn ORDER BY d, aid) AS rn FROM pairs) WHERE rn = 1""")
        con.execute("""
            INSERT INTO m
            SELECT bu.uprn, bu.aid, 'nearest_node_10m'
            FROM best_u bu JOIN best_a ba ON ba.aid = bu.aid AND ba.uprn = bu.uprn
            WHERE bu.ties = 1 AND ba.ties = 1
              AND bu.uprn NOT IN (SELECT uprn FROM m)
              AND bu.aid NOT IN (SELECT aid FROM m)""")
        # ---- lower-confidence fallbacks (never override a strict match; see CONFIDENCE)
        has_epc = (staged / "epc.parquet").exists()
        flat_epc = (
            f"SELECT DISTINCT uprn FROM read_parquet({_q(staged / 'epc.parquet')}) "
            "WHERE lower(property_type) IN ('flat', 'maisonette')"
            if has_epc
            else "SELECT NULL::BIGINT AS uprn WHERE false"
        )
        con.execute(f"CREATE TEMP TABLE flat_epc AS {flat_epc}")
        # a. UPRN inside an addr-bearing building polygon holding several UPRNs, with flat evidence
        con.execute("""
            INSERT INTO m
            SELECT uprn, aid, 'inside_building_flats' FROM (
                SELECT i.uprn, i.aid, row_number() OVER (PARTITION BY i.uprn ORDER BY i.area, i.aid) AS rn
                FROM inside i JOIN a USING (aid)
                WHERE i.aid IN (
                        SELECT aid FROM occupants WHERE n_uprn >= 2)
                  AND (a.housenumber IS NOT NULL OR a.housename IS NOT NULL)
                  AND (a.unit IS NOT NULL
                       OR i.aid IN (SELECT aid FROM inside WHERE uprn IN (SELECT uprn FROM flat_epc)))
                  AND i.uprn NOT IN (SELECT uprn FROM m))
            WHERE rn = 1""")
        # b. exactly one addr node inside the UPRN's single-UPRN INSPIRE parcel
        par_pq, pol_pq = staged / "inspire_parcel.parquet", staged / "inspire_polygons.parquet"
        if par_pq.exists() and pol_pq.exists():
            con.execute(f"""
                CREATE TEMP TABLE pg AS
                SELECT p.uprn, ST_GeomFromWKB(q.geom_wkb) AS g
                FROM read_parquet({_q(par_pq)}) p
                JOIN read_parquet({_q(pol_pq)}) q USING (parcel_inspire_id)
                WHERE p.uprns_in_parcel = 1 AND p.uprn IN (SELECT uprn FROM u)
                  AND p.uprn NOT IN (SELECT uprn FROM m)""")
            con.execute("""
                CREATE TEMP TABLE pg2 AS
                SELECT uprn, g, ST_XMin(g) AS x0, ST_YMin(g) AS y0, ST_XMax(g) AS x1, ST_YMax(g) AS y1 FROM pg""")
            con.execute("""
                CREATE TEMP TABLE pin AS
                SELECT p.uprn, a.aid FROM pg2 p
                JOIN (SELECT aid, x, y FROM a WHERE osm_type = 'node') a
                  ON a.x BETWEEN p.x0 AND p.x1 AND a.y BETWEEN p.y0 AND p.y1
                WHERE ST_Within(ST_Point(a.x, a.y), p.g)""")
            con.execute("""
                INSERT INTO m
                SELECT p.uprn, p.aid, 'parcel_single_node'
                FROM pin p JOIN a USING (aid)
                WHERE p.uprn IN (SELECT uprn FROM pin GROUP BY uprn HAVING count(*) = 1)
                  AND p.aid IN (SELECT aid FROM pin GROUP BY aid HAVING count(*) = 1)
                  AND a.ref_gb_uprn IS NULL AND (a.housenumber IS NOT NULL OR a.housename IS NOT NULL)
                  AND p.uprn NOT IN (SELECT uprn FROM m) AND p.aid NOT IN (SELECT aid FROM m)""")
        # c. mutual nearest node/UPRN within 25 m (nearest over all), street name must agree
        nl = NODE_RADIUS_LOW_M
        _register_norm(con, "an", "SELECT street AS name FROM a")
        con.execute(f"""
            CREATE TEMP TABLE pairs2 AS
            SELECT a.aid, u.uprn, sqrt((u.x - a.x)^2 + (u.y - a.y)^2) AS d
            FROM (SELECT a0.aid, a0.x, a0.y, CAST(floor(a0.x / {nl}) AS BIGINT) + dx AS cx,
                         CAST(floor(a0.y / {nl}) AS BIGINT) + dy AS cy
                  FROM (SELECT * FROM a WHERE osm_type = 'node' AND ref_gb_uprn IS NULL) a0,
                       (SELECT unnest([-1, 0, 1]) AS dx), (SELECT unnest([-1, 0, 1]) AS dy)) a
            JOIN (SELECT *, CAST(floor(x / {nl}) AS BIGINT) AS cx, CAST(floor(y / {nl}) AS BIGINT) AS cy FROM u
                  WHERE x IS NOT NULL) u
              ON u.cx = a.cx AND u.cy = a.cy
            WHERE sqrt((u.x - a.x)^2 + (u.y - a.y)^2) <= {nl}""")
        con.execute("""
            CREATE TEMP TABLE best_u2 AS
            SELECT aid, uprn, d, count(*) OVER (PARTITION BY aid, d) AS ties FROM (
                SELECT *, row_number() OVER (PARTITION BY aid ORDER BY d, uprn) AS rn FROM pairs2) WHERE rn = 1""")
        con.execute("""
            CREATE TEMP TABLE best_a2 AS
            SELECT aid, uprn, d, count(*) OVER (PARTITION BY uprn, d) AS ties FROM (
                SELECT *, row_number() OVER (PARTITION BY uprn ORDER BY d, aid) AS rn FROM pairs2) WHERE rn = 1""")
        con.execute("""
            INSERT INTO m
            SELECT bu.uprn, bu.aid, 'nearest_node_25m_mutual'
            FROM best_u2 bu JOIN best_a2 ba ON ba.aid = bu.aid AND ba.uprn = bu.uprn
            JOIN a ON a.aid = bu.aid
            JOIN unorm un ON un.uprn = bu.uprn
            LEFT JOIN an ON an.name = a.street
            LEFT JOIN street st ON st.uprn = bu.uprn
            LEFT JOIN hn sn ON sn.name = st.osm_street_name
            WHERE bu.ties = 1 AND ba.ties = 1 AND (a.housenumber IS NOT NULL OR a.housename IS NOT NULL)
              AND an.norm IS NOT NULL AND an.norm IN (un.o0, un.o1, sn.norm)
              AND bu.uprn NOT IN (SELECT uprn FROM m)
              AND bu.aid NOT IN (SELECT aid FROM m)""")
        # safety net: a non-ref object may never serve two UPRNs
        con.execute(
            "DELETE FROM m WHERE method NOT IN ('ref_gb_uprn', 'inside_building_flats') "
            "AND aid IN (SELECT aid FROM m GROUP BY aid HAVING count(*) > 1)"
        )
        con.execute("CREATE TEMP TABLE conf (method VARCHAR, conf VARCHAR)")
        con.executemany("INSERT INTO conf VALUES (?, ?)", list(CONFIDENCE.items()))
        sql = f"""
            SELECT u.uprn, s.osm_street_name, s.osm_way_id, s.osm_street_distance_m, s.osm_agrees_os,
                   a.housenumber AS osm_housenumber, a.housename AS osm_housename, a.unit AS osm_unit,
                   a.street AS osm_addr_street, a.postcode AS osm_addr_postcode,
                   m.method AS osm_addr_match_method, a.osm_type AS osm_addr_osm_type, a.osm_id AS osm_addr_osm_id,
                   CASE WHEN m.method IS NULL THEN NULL ELSE cf.conf END AS osm_addr_confidence,
                   coalesce(m.method = 'inside_building_flats', false) AS building_level_address,
                   '{LICENCE}' AS licence
            FROM u LEFT JOIN street s USING (uprn) LEFT JOIN m USING (uprn) LEFT JOIN a ON a.aid = m.aid
            LEFT JOIN conf cf ON cf.method = m.method
            ORDER BY u.uprn"""
        out = out_lad / "streets_odbl.parquet"
        n = _copy(con, sql, out)
        stats: dict = {"lad": lad, "uprns": n}
        stats["with_osm_street"] = con.execute(
            f"SELECT count(*) FROM read_parquet({_q(out)}) WHERE osm_street_name IS NOT NULL"
        ).fetchone()[0]
        stats["os_missing_osm_has"] = con.execute(
            f"SELECT count(*) FROM read_parquet({_q(out)}) o JOIN u USING (uprn) "
            "WHERE u.os_name IS NULL AND o.osm_street_name IS NOT NULL"
        ).fetchone()[0]
        stats["os_missing"] = con.execute("SELECT count(*) FROM u WHERE os_name IS NULL").fetchone()[0]
        ag = con.execute(
            f"SELECT count(*) FILTER (WHERE osm_agrees_os), count(*) FILTER (WHERE osm_agrees_os IS NOT NULL) "
            f"FROM read_parquet({_q(out)})"
        ).fetchone()
        stats["agrees"], stats["agree_comparable"] = ag
        for meth, cnt in con.execute(
            f"SELECT osm_addr_match_method, count(*) FROM read_parquet({_q(out)}) "
            "WHERE osm_addr_match_method IS NOT NULL GROUP BY 1"
        ).fetchall():
            stats[meth] = cnt
    finally:
        con.close()
    (out_lad / LICENCE_NAME).write_text(LICENCE_TEXT.format(release=_release_of(parquet_dir)))
    return stats


def osm_all(
    staged_dir: Path,
    out_dir: Path,
    parquet_dir: Path,
    lads: list[str] | None = None,
    skip_existing: bool = True,
    **kw,
) -> dict:
    """Run ``osm_for_lad`` over LADs with staged onsud+open_uprn; resumable (skips existing output)."""
    staged_dir, out_dir = Path(staged_dir), Path(out_dir)
    avail = sorted(
        p.parent.name for p in staged_dir.glob("*/onsud.parquet") if (p.parent / "open_uprn.parquet").exists()
    )
    res: dict = {"done": [], "skipped": [], "failed": {}}
    for lad in lads or avail:
        if lad not in avail:
            res["failed"][lad] = "not staged"
            continue
        if skip_existing and (out_dir / f"lad={lad}" / "streets_odbl.parquet").exists():
            res["skipped"].append(lad)
            continue
        try:
            osm_for_lad(lad, staged_dir, out_dir, parquet_dir, **kw)
            res["done"].append(lad)
        except Exception as exc:  # noqa: BLE001 - keep going over LADs
            res["failed"][lad] = str(exc)
    return res


# ---------------------------------------------------------------- validation against EPC (aggregates only)
_FLAT_WORD = re.compile(r"\b(?:flat|apartment|apt|unit|room|suite|studio)\s+[0-9]+[a-z]?\b", re.I)
_NUM = re.compile(r"\d+")


def _house_numbers(text: str | None, strip_flat: bool = False) -> set[str]:
    """Integer tokens (leading zeros dropped) of ``text``; with ``strip_flat`` 'Flat 3'-style tokens are removed."""
    if not text:
        return set()
    if strip_flat:
        text = _FLAT_WORD.sub(" ", text)
    return {str(int(t)) for t in _NUM.findall(text)}


_RANGE = re.compile(r"(\d+)\s*(?:-|to|–)\s*(\d+)")


def _osm_number_set(hn: str | None) -> set[str]:
    """Numbers an OSM addr:housenumber covers: '12a' -> {12}; '10-20' -> 10..20 (max 60 wide); '1;3' -> {1, 3}."""
    if not hn:
        return set()
    out = _house_numbers(hn)
    for a, b in _RANGE.findall(hn):
        a, b = int(a), int(b)
        if a < b <= a + 60:
            out |= {str(i) for i in range(a, b + 1)}
    return out


def validate_against_epc(streets_pq: Path, address_pq: Path) -> dict[str, dict[str, int]]:
    """Per match method: how often the OSM house number agrees with the EPC address's house number.

    Only house-number *tokens* are compared (in memory); no EPC address text is returned or stored. A pair agrees
    when a number covered by the OSM value ('12a' -> 12; '10-20' -> 10..20; '1;3' -> 1, 3) is among the EPC
    building number tokens (flat/apartment/unit numbers removed). Returns ``{method: {n, compared, agree}}``.
    """
    con = duckdb.connect()
    try:
        rows = con.execute(
            f"""SELECT o.osm_addr_match_method, o.osm_housenumber,
                   concat_ws(' ', e.address_line1, e.address_line2, e.address_line3)
                FROM read_parquet({_q(streets_pq)}) o JOIN read_parquet({_q(address_pq)}) e USING (uprn)
                WHERE o.osm_addr_match_method IS NOT NULL AND o.osm_housenumber IS NOT NULL"""
        ).fetchall()
    finally:
        con.close()
    res: dict[str, dict[str, int]] = {}
    for meth, hn, epc in rows:
        r = res.setdefault(meth, {"n": 0, "compared": 0, "agree": 0})
        r["n"] += 1
        o, e = _osm_number_set(hn), _house_numbers(epc, strip_flat=True)
        if o and e:
            r["compared"] += 1
            r["agree"] += bool(o & e)
    return res
