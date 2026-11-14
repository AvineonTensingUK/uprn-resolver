"""Provenance / QA sidecar per UPRN, built from OGL inputs only.

New national inputs (all OGL v3; downloaded, md5-checked, converted to zstd Parquet, source deleted):

* ``data/parquet/open_usrn/open_usrn.parquet``  (OS Open USRN, GeoPackage layer ``openUSRN``, 1.77M lines)
    usrn BIGINT, street_type VARCHAR ('Designated Street Name' | 'Numbered Street' |
    'Officially Described Street' | 'Unofficial Street Name'), geom_wkb BLOB (2D LINESTRING /
    MULTILINESTRING, EPSG:27700). Sorted by usrn.
* ``data/parquet/codepoint/codepoint.parquet``  (Code-Point Open, CSV, ~1.7M postcode units)
    postcode VARCHAR ("N1 0AA", single space), x, y DOUBLE (EPSG:27700, NULL when quality = 90),
    positional_quality INTEGER, country_code, nhs_region_code, nhs_ha_code, admin_county_code,
    admin_district_code, admin_ward_code VARCHAR.
    Attribution: "Contains OS data (c) Crown copyright and database right; Contains Royal Mail data (c)
    Royal Mail copyright and database right; Contains National Statistics data (c) Crown copyright and
    database right" (the product's licence.txt, OS OpenData licence = OGL v3-compatible).
* ``data/parquet/open_roads/open_roads.parquet`` (OS Open Roads RoadLink, GeoPackage, named links only)
    toid, name_1, name_1_lang, name_2, name_2_lang, road_classification, road_classification_number,
    form_of_way, road_function, xmin, ymin, xmax, ymax, geom_wkb.
    Used for the street-name fallback: Open Names Named Road rows carry only a point/MBR (no line), and
    Open Roads links have line geometry plus names, so a USRN line can be matched to named road links.

``provenance_for_lad`` writes ``out_dir/lad=<lad>/provenance.parquet`` (one row per ONSUD UPRN).
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

import numpy as np
import pyarrow as pa

from uprn_resolver import download as dl
from uprn_resolver.convert import _append_manifest, _connect, _copy, _free_gb, _load_spatial, _q

MIN_FREE_GB = 15
USRN_FAR_M = 50.0
POSTCODE_FAR_M = 250.0
NAME_NEAR_M = 10.0  # a USRN point counts as covered by a road link within this distance
NAME_MIN_COVER = 0.8  # fraction of the USRN line that must be covered by the winning name
NAME_MAX_RUNNER_UP = 0.2  # ... and the runner-up name may cover at most this much
NEAREST_MARGIN_M = 5000.0

# dataset -> (OS product, format, file-name regex, parquet path under parquet_dir)
SOURCES = {
    "open_usrn": ("OpenUSRN", "GeoPackage", r"osopenusrn_\d+_gpkg", "open_usrn/open_usrn.parquet"),
    "codepoint": ("CodePointOpen", "CSV", r"^codepo_gb\.zip$", "codepoint/codepoint.parquet"),
    "open_roads": ("OpenRoads", "GeoPackage", r"oproad_gpkg_gb", "open_roads/open_roads.parquet"),
}
CODEPOINT_COLS = ["PC", "PQ", "EA", "NO", "CY", "RH", "LH", "CC", "DC", "WC"]


# ---------------------------------------------------------------- conversion
def _gpkg_layer(con, src: Path, want: str) -> str:
    meta = con.execute(f"SELECT layers FROM ST_Read_Meta({_q(src)})").fetchall()
    names = [lay["name"] for row in meta for lay in row[0]]
    for n in names:
        if re.sub(r"[^a-z]", "", n.lower()) == want:
            return n
    raise ValueError(f"no layer like {want!r} in {src.name}: {names}")


def _open_usrn(con, src: Path, out: Path) -> int:
    _load_spatial(con)
    layer = _gpkg_layer(con, src, "openusrn")
    sql = f"""
        SELECT CAST(usrn AS BIGINT) AS usrn, street_type, ST_AsWKB(ST_Force2D(geometry)) AS geom_wkb
        FROM ST_Read({_q(src)}, layer={_q(layer)})
        WHERE usrn IS NOT NULL AND geometry IS NOT NULL
        ORDER BY usrn"""
    return _copy(con, sql, out)


def _codepoint(con, src: Path, out: Path) -> int:
    """src: extracted Code-Point Open directory (``Data/CSV/*.csv`` headerless)."""
    files = sorted(f for f in src.rglob("*.csv") if "header" not in f.name.lower() and f.parent.name.lower() == "csv")
    if not files:
        raise FileNotFoundError(f"no Code-Point CSVs under {src}")
    spec = "{" + ", ".join(f"'{c}': 'VARCHAR'" for c in CODEPOINT_COLS) + "}"
    flist = "[" + ", ".join(_q(f) for f in files) + "]"
    rel = f"read_csv({flist}, header=false, columns={spec}, auto_detect=false, delim=',', quote='\"')"
    nul = lambda c: f"NULLIF(trim({c}), '')"  # noqa: E731
    sql = f"""
        SELECT regexp_replace(upper(replace(trim(PC), ' ', '')), '^(.+)(.{{3}})$', '\\1 \\2') AS postcode,
               CASE WHEN CAST(PQ AS INTEGER) = 90 THEN NULL ELSE CAST(EA AS DOUBLE) END AS x,
               CASE WHEN CAST(PQ AS INTEGER) = 90 THEN NULL ELSE CAST(NO AS DOUBLE) END AS y,
               CAST(PQ AS INTEGER) AS positional_quality,
               {nul("CY")} AS country_code, {nul("RH")} AS nhs_region_code, {nul("LH")} AS nhs_ha_code,
               {nul("CC")} AS admin_county_code, {nul("DC")} AS admin_district_code,
               {nul("WC")} AS admin_ward_code
        FROM {rel}
        ORDER BY postcode"""
    return _copy(con, sql, out)


def _open_roads(con, src: Path, out: Path) -> int:
    _load_spatial(con)
    layer = _gpkg_layer(con, src, "roadlink")
    rel = f"ST_Read({_q(src)}, layer={_q(layer)})"
    have = {r[0].lower(): r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()}
    for need in ("id", "name_1"):
        if need not in have:
            raise ValueError(f"Open Roads layer {layer} lacks {need!r}: {sorted(have)}")

    def col(name: str, alias: str | None = None) -> str:
        a = alias or name
        return f"{have[name]} AS {a}" if name in have else f"CAST(NULL AS VARCHAR) AS {a}"

    sql = f"""
        SELECT id_ AS toid, name_1, name_1_lang, name_2, name_2_lang, road_classification,
               road_classification_number, form_of_way, road_function,
               ST_XMin(g) AS xmin, ST_YMin(g) AS ymin, ST_XMax(g) AS xmax, ST_YMax(g) AS ymax,
               ST_AsWKB(g) AS geom_wkb
        FROM (
            SELECT {have["id"]} AS id_, {col("name_1")}, {col("name_1_lang")}, {col("name_2")}, {col("name_2_lang")},
                   {col("road_classification")}, {col("road_classification_number")}, {col("form_of_way")},
                   {col("road_function")}, ST_Force2D(geometry) AS g
            FROM {rel}
        )
        WHERE (name_1 IS NOT NULL OR name_2 IS NOT NULL) AND g IS NOT NULL
        ORDER BY xmin"""
    return _copy(con, sql, out)


def convert_provenance(dataset: str, src: Path, parquet_dir: Path) -> int:
    """Convert one extracted source (GPKG file for open_usrn/open_roads, CSV directory for codepoint)
    to its Parquet path under ``parquet_dir``; record in the manifest; return the row count."""
    if dataset not in SOURCES:
        raise ValueError(f"unknown dataset {dataset!r}; expected one of {sorted(SOURCES)}")
    src, parquet_dir = Path(src), Path(parquet_dir)
    if not src.exists():
        raise FileNotFoundError(src)
    parquet_dir.mkdir(parents=True, exist_ok=True)
    free = _free_gb(parquet_dir)
    if free < MIN_FREE_GB:
        raise RuntimeError(f"only {free:.1f} GB free; need at least {MIN_FREE_GB} GB. Aborting conversion.")
    out = parquet_dir / SOURCES[dataset][3]
    con = _connect(parquet_dir, "6GB")
    try:
        n = {"open_usrn": _open_usrn, "codepoint": _codepoint, "open_roads": _open_roads}[dataset](con, src, out)
    finally:
        con.close()
    if n <= 0:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"{dataset}: converted 0 rows")
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


# ---------------------------------------------------------------- download
def _select(dataset: str, listing: list[dict]) -> dict:
    _, fmt, pat, _ = SOURCES[dataset]
    hits = [d for d in listing if d.get("area") == "GB" and d.get("format") == fmt and re.search(pat, d["fileName"])]
    if len(hits) != 1:
        raise RuntimeError(f"{dataset}: expected exactly one OS download matching {pat!r}, got {len(hits)}")
    return hits[0]


def _process(dataset: str, zpath: Path, parquet_dir: Path, size: int | None, md5: str | None) -> int:
    """Extract -> convert -> verify -> delete extracted files and zip (one at a time)."""
    with dl._CONVERT_LOCK:
        free = _free_gb(zpath.parent)
        if free < MIN_FREE_GB:
            raise RuntimeError(f"only {free:.1f} GB free before extracting {zpath.name}; need {MIN_FREE_GB}")
        extra = zpath.parent / f".x_{zpath.stem}"
        if extra.exists():
            shutil.rmtree(extra)
        extra.mkdir(parents=True)
        try:
            files = dl._unzip(zpath, extra)
            if dataset == "codepoint":
                src = extra
            else:
                gpkgs = [f for f in files if f.suffix.lower() == ".gpkg"]
                if len(gpkgs) != 1:
                    raise RuntimeError(f"{zpath.name}: expected one GeoPackage, found {len(gpkgs)}")
                src = gpkgs[0]
            rows = convert_provenance(dataset, src, parquet_dir)
        finally:
            shutil.rmtree(extra, ignore_errors=True)
        zpath.unlink()
    dl._record(parquet_dir, dataset, zpath.name, size, md5, rows)
    print(f"  converted {zpath.name}: {rows} rows; source deleted")
    return rows


def download_provenance_sources(raw_dir: Path, datasets: list[str] | None = None) -> None:
    """Download + convert open_usrn, codepoint, open_roads into ``raw_dir.parent/parquet`` (skips converted)."""
    raw_dir = Path(raw_dir)
    parquet_dir = raw_dir.parent / "parquet"
    for ds in datasets or list(SOURCES):
        d = _select(ds, dl._os_listing(SOURCES[ds][0]))
        if dl._already_converted(parquet_dir, d["fileName"], d.get("md5"), SOURCES[ds][3]):
            print(f"  {d['fileName']}: in manifest, skipping")
            continue
        print(f"[download] {ds}")
        z = dl._stream(d["url"], raw_dir / ds / d["fileName"], d.get("size"), d.get("md5"))
        _process(ds, z, parquet_dir, d.get("size"), d.get("md5"))


# ---------------------------------------------------------------- provenance
def _name_fallback(con, roads: Path) -> None:
    """Fill temp table ``fallback(usrn, name, alt, cover, runner_up)`` for unnamed USRNs in ``usrn_geom``."""
    con.execute("""CREATE TEMP TABLE unnamed AS
        SELECT usrn, g, ST_Length(g) AS len, ST_XMin(g) - 15 AS x0, ST_YMin(g) - 15 AS y0,
               ST_XMax(g) + 15 AS x1, ST_YMax(g) + 15 AS y1
        FROM usrn_geom WHERE needs_name AND ST_Length(g) > 0""")
    ext = con.execute("SELECT min(x0), min(y0), max(x1), max(y1), count(*) FROM unnamed").fetchone()
    con.execute("CREATE TEMP TABLE fallback(usrn BIGINT, name VARCHAR, alt VARCHAR, cover DOUBLE, runner_up DOUBLE)")
    if not ext[4] or not roads.exists():
        return
    con.execute(f"""CREATE TEMP TABLE rl AS
        SELECT CASE WHEN name_1_lang IN ('cym','gla') AND name_2 IS NOT NULL THEN name_2 ELSE name_1 END AS name,
               CASE WHEN name_1_lang IN ('cym','gla') AND name_2 IS NOT NULL THEN name_1
                    WHEN name_2 IS NOT NULL AND name_2 <> name_1 THEN name_2 END AS alt,
               xmin, ymin, xmax, ymax, ST_GeomFromWKB(geom_wkb) AS g
        FROM read_parquet({_q(roads)})
        WHERE xmax >= {ext[0]} AND xmin <= {ext[2]} AND ymax >= {ext[1]} AND ymin <= {ext[3]}""")
    con.execute("DELETE FROM rl WHERE name IS NULL")
    con.execute(f"""CREATE TEMP TABLE pairs AS
        SELECT u.usrn, r.name, any_value(r.alt) AS alt,
               ST_Length(ST_Intersection(any_value(u.g), ST_Buffer(ST_Union_Agg(r.g), {NAME_NEAR_M})))
                   / any_value(u.len) AS cover
        FROM unnamed u JOIN rl r
          ON r.xmax >= u.x0 AND r.xmin <= u.x1 AND r.ymax >= u.y0 AND r.ymin <= u.y1
         AND ST_DWithin(u.g, r.g, {NAME_NEAR_M})
        GROUP BY u.usrn, r.name""")
    con.execute(f"""INSERT INTO fallback
        SELECT usrn, name, alt, cover, coalesce(runner_up, 0) FROM (
            SELECT usrn, name, alt, cover,
                   row_number() OVER (PARTITION BY usrn ORDER BY cover DESC, name) AS rn,
                   lead(cover) OVER (PARTITION BY usrn ORDER BY cover DESC, name) AS runner_up
            FROM pairs)
        WHERE rn = 1 AND cover >= {NAME_MIN_COVER} AND coalesce(runner_up, 0) <= {NAME_MAX_RUNNER_UP}""")


def _nearest_postcode(xy: np.ndarray, cx: np.ndarray, cy: np.ndarray, pcs: np.ndarray):
    """Nearest centroid per point: returns (postcode array, distance array)."""
    from shapely import STRtree, points

    tree = STRtree(points(cx, cy))
    idx = tree.query_nearest(points(xy[:, 0], xy[:, 1]), return_distance=True, all_matches=False)
    # query_nearest returns (2, n) input/tree indices (+ distances); points with no match are absent
    res_in, res_tree = idx[0][0], idx[0][1]
    pc = np.full(len(xy), None, dtype=object)
    dist = np.full(len(xy), np.nan)
    pc[res_in] = pcs[res_tree]
    dist[res_in] = idx[1]
    return pc, dist


def provenance_for_lad(lad: str, staged_dir: Path, out_dir: Path, parquet_dir: Path) -> dict:
    """Write ``out_dir/lad=<lad>/provenance.parquet`` (one row per ONSUD UPRN) and return summary stats.

    Columns: uprn, usrn, usrn_street_type, usrn_distance_m, usrn_link_flag ('ok' <= 50 m, 'far' > 50 m,
    'no_geometry' = no USRN link or USRN not in Open USRN), street_name_fallback,
    street_name_fallback_alt, street_name_fallback_method ('open_roads_overlap'),
    street_name_fallback_cover (fraction of the USRN line within 10 m of the named road links),
    postcode_onsud, postcode_centroid_distance_m, postcode_flag ('ok' <= 250 m, 'far', 'not_in_codepoint'
    -- includes UPRNs with no ONSUD postcode and centroids with no coordinates),
    codepoint_positional_quality, postcode_nearest_cpo, postcode_nearest_cpo_distance_m,
    nearest_agrees_onsud (NULL if either postcode is missing).

    The fallback name is only filled for UPRNs whose ``streets.parquet`` row has no street_name, and only
    when one road name covers >= 80% of the USRN line (within 10 m) and no other name covers > 20%.
    """
    staged, parquet_dir = Path(staged_dir) / lad, Path(parquet_dir)
    need = {k: staged / f"{k}.parquet" for k in ("onsud", "open_uprn", "streets")}
    need["open_usrn"] = parquet_dir / SOURCES["open_usrn"][3]
    need["codepoint"] = parquet_dir / SOURCES["codepoint"][3]
    missing = [str(p) for p in need.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"{lad}: missing inputs {missing}")
    roads = parquet_dir / SOURCES["open_roads"][3]
    t0 = time.time()
    con = _connect(parquet_dir, "6GB")
    con.execute("SET preserve_insertion_order=false")
    try:
        _load_spatial(con)
        con.execute(f"""CREATE TEMP TABLE base AS
            SELECT o.uprn, CAST(p.x AS DOUBLE) AS x, CAST(p.y AS DOUBLE) AS y,
                   regexp_replace(upper(replace(trim(o.postcode), ' ', '')), '^(.+)(.{{3}})$', '\\1 \\2') AS pc,
                   s.usrn, s.street_name IS NULL AS unnamed
            FROM read_parquet({_q(need["onsud"])}) o
            LEFT JOIN read_parquet({_q(need["open_uprn"])}) p USING (uprn)
            LEFT JOIN read_parquet({_q(need["streets"])}) s USING (uprn)""")
        con.execute(f"""CREATE TEMP TABLE usrn_geom AS
            SELECT g.usrn, g.street_type, ST_GeomFromWKB(g.geom_wkb) AS g,
                   coalesce(bool_or(b.unnamed), false) AS needs_name
            FROM read_parquet({_q(need["open_usrn"])}) g
            JOIN base b ON b.usrn = g.usrn
            GROUP BY g.usrn, g.street_type, g.geom_wkb""")
        _name_fallback(con, roads)

        # postcode centroid of the ONSUD postcode, then nearest centroid in Python (STRtree)
        ext = con.execute("SELECT min(x), min(y), max(x), max(y) FROM base").fetchone()
        m = NEAREST_MARGIN_M
        cpo = con.execute(f"""SELECT postcode, x, y FROM read_parquet({_q(need["codepoint"])})
            WHERE x IS NOT NULL AND x BETWEEN {ext[0] - m} AND {ext[2] + m}
              AND y BETWEEN {ext[1] - m} AND {ext[3] + m}""").fetchnumpy()
        ux = con.execute("SELECT uprn, x, y FROM base ORDER BY uprn").fetchnumpy()
        n = len(ux["uprn"])
        pc = np.full(n, None, dtype=object)
        dist = np.full(n, np.nan)
        # fetchnumpy() returns masked arrays for columns with NULLs; fill masks with NaN so NULL
        # coordinates are never treated as (0, 0)
        ux_x = np.ma.filled(np.ma.asarray(ux["x"]).astype(float), np.nan)
        ux_y = np.ma.filled(np.ma.asarray(ux["y"]).astype(float), np.nan)
        ok = (np.isfinite(ux_x) & np.isfinite(ux_y)) if len(cpo["x"]) else np.zeros(n, bool)
        if len(cpo["x"]) and ok.any():
            xy = np.column_stack([ux_x[ok], ux_y[ok]])
            cxy = (cpo["x"].astype(float), cpo["y"].astype(float))
            a, b = _nearest_postcode(xy, *cxy, np.asarray(cpo["postcode"], dtype=object))
            pc[ok], dist[ok] = a, b
        near = pa.table(
            {
                "uprn": pa.array(ux["uprn"]),
                "postcode_nearest_cpo": pa.array(list(pc), pa.string()),
                "postcode_nearest_cpo_distance_m": pa.array(dist, from_pandas=True),
            }
        )
        con.register("near", near)

        sql = f"""
            SELECT b.uprn, b.usrn, ug.street_type AS usrn_street_type,
                   round(d.dist, 2) AS usrn_distance_m,
                   CASE WHEN d.dist IS NULL THEN 'no_geometry' WHEN d.dist <= {USRN_FAR_M} THEN 'ok' ELSE 'far' END
                       AS usrn_link_flag,
                   f.name AS street_name_fallback, f.alt AS street_name_fallback_alt,
                   CASE WHEN f.name IS NOT NULL THEN 'open_roads_overlap' END AS street_name_fallback_method,
                   round(f.cover, 3) AS street_name_fallback_cover,
                   b.pc AS postcode_onsud,
                   round(pd.dist, 2) AS postcode_centroid_distance_m,
                   CASE WHEN pd.dist IS NULL THEN 'not_in_codepoint' WHEN pd.dist <= {POSTCODE_FAR_M} THEN 'ok'
                        ELSE 'far' END AS postcode_flag,
                   c.positional_quality AS codepoint_positional_quality,
                   n.postcode_nearest_cpo,
                   round(n.postcode_nearest_cpo_distance_m, 2) AS postcode_nearest_cpo_distance_m,
                   CASE WHEN b.pc IS NULL OR n.postcode_nearest_cpo IS NULL THEN NULL
                        ELSE b.pc = n.postcode_nearest_cpo END AS nearest_agrees_onsud
            FROM base b
            LEFT JOIN usrn_geom ug ON ug.usrn = b.usrn
            LEFT JOIN LATERAL (SELECT CASE WHEN b.x IS NULL OR ug.g IS NULL THEN NULL
                                           ELSE ST_Distance(ST_Point(b.x, b.y), ug.g) END AS dist) d ON true
            LEFT JOIN fallback f ON f.usrn = b.usrn AND b.unnamed
            LEFT JOIN cpo_lad c ON c.postcode = b.pc
            LEFT JOIN LATERAL (SELECT CASE WHEN b.x IS NULL OR c.x IS NULL THEN NULL
                                           ELSE sqrt((b.x - c.x) ^ 2 + (b.y - c.y) ^ 2) END AS dist) pd ON true
            LEFT JOIN near n ON n.uprn = b.uprn
            ORDER BY b.uprn"""
        con.execute(f"""CREATE TEMP TABLE cpo_lad AS
            SELECT postcode, x, y, positional_quality FROM read_parquet({_q(need["codepoint"])})
            WHERE postcode IN (SELECT DISTINCT pc FROM base WHERE pc IS NOT NULL)""")
        dest = Path(out_dir) / f"lad={lad}" / "provenance.parquet"
        _copy(con, sql, dest)
        cur = con.execute(f"""SELECT count(*) AS uprns,
                count(*) FILTER (usrn_link_flag = 'ok') AS usrn_ok,
                count(*) FILTER (usrn_link_flag = 'far') AS usrn_far,
                count(*) FILTER (usrn_link_flag = 'no_geometry') AS usrn_no_geometry,
                count(street_name_fallback) AS fallback_uprns,
                count(DISTINCT usrn) FILTER (street_name_fallback IS NOT NULL) AS fallback_usrns,
                count(*) FILTER (postcode_flag = 'ok') AS pc_ok, count(*) FILTER (postcode_flag = 'far') AS pc_far,
                count(*) FILTER (postcode_flag = 'not_in_codepoint') AS pc_missing,
                count(*) FILTER (nearest_agrees_onsud) AS nearest_agrees,
                count(nearest_agrees_onsud) AS nearest_comparable
            FROM read_parquet({_q(dest)})""")
        stats = dict(zip([d[0] for d in cur.description], cur.fetchone(), strict=True))
    finally:
        con.close()
    out = {k: int(v) for k, v in stats.items()}
    out["seconds"] = round(time.time() - t0, 1)
    return out


def provenance_all(
    staged_dir: Path, out_dir: Path, parquet_dir: Path, lads: list[str] | None = None, skip_existing: bool = True
) -> dict:
    """Run ``provenance_for_lad`` over LADs with staged onsud + open_uprn + streets (resumable).

    Returns {"built": [...], "skipped": [...], "failed": {lad: error}}.
    """
    staged_dir, out_dir = Path(staged_dir), Path(out_dir)
    if lads is None:
        lads = sorted(
            p.name
            for p in staged_dir.iterdir()
            if p.is_dir() and all((p / f"{k}.parquet").exists() for k in ("onsud", "open_uprn", "streets"))
        )
    res: dict = {"built": [], "skipped": [], "failed": {}}
    for lad in lads:
        if skip_existing and (Path(out_dir) / f"lad={lad}" / "provenance.parquet").exists():
            res["skipped"].append(lad)
            continue
        try:
            provenance_for_lad(lad, staged_dir, out_dir, parquet_dir)
            res["built"].append(lad)
            print(f"[provenance] {lad} done")
        except Exception as e:  # keep going
            res["failed"][lad] = f"{type(e).__name__}: {e}"
            print(f"[provenance] {lad} FAILED: {e}")
    return res
