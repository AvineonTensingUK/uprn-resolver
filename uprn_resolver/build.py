"""Build per-LAD UPRN -> building outputs from staged Parquet (see CONTRACT.md).

Design notes
------------
* UPRN set = ONSUD. Everything else is LEFT JOINed.
* Multi-TOID rule: when LIDS maps a UPRN to several TOIDs, prefer a TOID that has
  coordinates in open_toid, then the lexicographically smallest TOID. ``toid_count``
  records the number of distinct TOIDs so consumers can filter ambiguous rows.
* Spatial join: DuckDB spatial ``ST_Within(point, polygon)`` join (DuckDB's SPATIAL_JOIN
  operator builds an R-tree on the polygon side), fully set-based. If a point falls in
  several overlapping polygons the smallest-area one wins, then the smallest building_id (deterministic).
* building_id = TOID-point match, else UPRN-point match, else NULL.
* Intermediates are slim: x/y are carried as scalars, geometry is only materialised in two
  narrow point tables for the spatial joins and as WKB at write time. ``uprns_in_building``
  is a window count.
* Optional staged tables (``streets``, ``inspire_parcel``, ``epc``) are LEFT JOINed on uprn when
  present; their columns are NULL when absent. EPC is written as a separate ``epc.parquet``
  (uprn + every EPC column); ``uprn.parquet`` only carries epc_built_form, epc_property_type,
  epc_count. INSPIRE polygons are never published, only parcel ids/areas/counts.
* ``outline_granularity``: 'single' if uprns_in_building = 1; else 'parcel_finer' when the UPRN's
  INSPIRE parcel holds fewer UPRNs than its building (the parcel splits the building, so a
  parcel is a finer outline than the building); else 'shared' (all building UPRNs share one
  parcel, or no parcel info). NULL when there is no building match.
* Outputs go to ``<out_dir>/lad=<code>/`` (hive style) so ``read_parquet('out/*/uprn.parquet',
  hive_partitioning=true)`` works. ``build_all`` loops over staged LADs (resumable).
* Completion: ``stats.json`` is the completion marker. ``build`` deletes it before writing anything, writes every
  output to ``<name>.tmp`` and renames atomically, and writes the marker last, so a crash leaves no marker.
  ``build_all`` only builds LADs whose ``.stage_done`` marker exists (see ``stage.py``) and rebuilds a LAD whose
  stage marker is newer than its ``stats.json``.
* DuckDB resources: ``threads``, ``memory_limit``, ``temp_directory`` arguments, falling back to
  env vars ``UPRN_RESOLVER_THREADS`` / ``UPRN_RESOLVER_MEMORY_LIMIT`` / ``UPRN_RESOLVER_TEMP_DIR``;
  memory_limit defaults to 70% of RAM (DuckDB's default is 80%), temp_directory to
  ``<staged_dir>/../tmp``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from uprn_resolver.stage import DONE_MARKER, read_marker

EPSG_27700_PROJJSON = {
    "$schema": "https://proj.org/schemas/v0.7/projjson.schema.json",
    "type": "ProjectedCRS",
    "name": "OSGB36 / British National Grid",
    "base_crs": {
        "type": "GeographicCRS",
        "name": "OSGB36",
        "datum": {
            "type": "GeodeticReferenceFrame",
            "name": "Ordnance Survey of Great Britain 1936",
            "ellipsoid": {
                "name": "Airy 1830",
                "semi_major_axis": 6377563.396,
                "inverse_flattening": 299.3249646,
            },
        },
        "coordinate_system": {
            "subtype": "ellipsoidal",
            "axis": [
                {"name": "Geodetic latitude", "abbreviation": "Lat", "direction": "north", "unit": "degree"},
                {"name": "Geodetic longitude", "abbreviation": "Lon", "direction": "east", "unit": "degree"},
            ],
        },
    },
    "conversion": {
        "name": "British National Grid",
        "method": {"name": "Transverse Mercator", "id": {"authority": "EPSG", "code": 9807}},
        "parameters": [
            {"name": "Latitude of natural origin", "value": 49, "unit": "degree"},
            {"name": "Longitude of natural origin", "value": -2, "unit": "degree"},
            {"name": "Scale factor at natural origin", "value": 0.9996012717, "unit": "unity"},
            {"name": "False easting", "value": 400000, "unit": "metre"},
            {"name": "False northing", "value": -100000, "unit": "metre"},
        ],
    },
    "coordinate_system": {
        "subtype": "Cartesian",
        "axis": [
            {"name": "Easting", "abbreviation": "E", "direction": "east", "unit": "metre"},
            {"name": "Northing", "abbreviation": "N", "direction": "north", "unit": "metre"},
        ],
    },
    "id": {"authority": "EPSG", "code": 27700},
}


def _geo_meta(geometry_types: list[str], bbox: list[float] | None) -> str:
    col: dict = {
        "encoding": "WKB",
        "geometry_types": geometry_types,
        "crs": EPSG_27700_PROJJSON,
    }
    if bbox is not None:
        col["bbox"] = bbox
    return json.dumps({"version": "1.1.0", "primary_column": "geometry", "columns": {"geometry": col}})


def _tmp(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def begin_outputs(out: Path, marker: str, owned: tuple[str, ...]) -> None:
    """Start of a (re)build: delete the completion ``marker`` first, then stray ``.tmp`` files of this product."""
    (out / marker).unlink(missing_ok=True)
    for name in (*owned, marker):
        _tmp(out / name).unlink(missing_ok=True)


def write_text_atomic(path: Path, text: str) -> None:
    tmp = _tmp(path)
    tmp.write_text(text)
    os.replace(tmp, path)


def _count(con, sql: str) -> int:
    return int(con.execute(sql).fetchone()[0])


REQUIRED_STAGED = ("onsud", "open_uprn", "lids_uprn_toid", "open_toid", "buildings")
OWNED_OUTPUTS = ("uprn.parquet", "epc.parquet", "buildings.parquet")


def lad_out_dir(out_dir: Path, lad: str) -> Path:
    """Output directory for one LAD (hive partition style)."""
    return Path(out_dir) / f"lad={lad}"


def _connect(staged_dir: Path, threads, memory_limit, temp_directory):
    threads = threads or os.environ.get("UPRN_RESOLVER_THREADS")
    memory_limit = memory_limit or os.environ.get("UPRN_RESOLVER_MEMORY_LIMIT") or "70%"
    temp_directory = (
        temp_directory or os.environ.get("UPRN_RESOLVER_TEMP_DIR") or Path(staged_dir).resolve().parent / "tmp"
    )
    Path(temp_directory).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    if threads:
        con.execute(f"SET threads={int(threads)}")
    con.execute(f"SET memory_limit='{memory_limit}'")
    con.execute(f"SET temp_directory='{temp_directory}'")
    con.execute("SET preserve_insertion_order=false")
    return con


def _write(con, query: str, path: Path, geo: str) -> None:
    geo_sql = geo.replace("'", "''")
    tmp = _tmp(path)
    con.execute(f"COPY ({query}) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD, KV_METADATA {{geo: '{geo_sql}'}})")
    os.replace(tmp, path)


def _stage_cols(path: Path) -> set[str]:
    import pyarrow.parquet as pq

    return set(pq.read_schema(path).names)


def _dedupe_order(path: Path, *keys: str) -> str:
    """ORDER BY list for ``DISTINCT ON (uprn)``: uprn, then the given tiebreak columns that exist in the file."""
    have = _stage_cols(path)
    return ", ".join(["uprn", *(k for k in keys if k.split()[0] in have)])


def build(
    lad: str,
    staged_dir: Path,
    out_dir: Path,
    include_postcode: bool = False,
    *,
    threads: int | None = None,
    memory_limit: str | None = None,
    temp_directory: Path | str | None = None,
) -> dict:
    staged = Path(staged_dir) / lad
    out = lad_out_dir(out_dir, lad)
    out.mkdir(parents=True, exist_ok=True)
    # a rebuild invalidates the completion marker first; it is written last (a crash leaves no marker)
    begin_outputs(out, "stats.json", OWNED_OUTPUTS)

    def p(name: str) -> Path:
        return staged / f"{name}.parquet"

    has_ppd = p("hmlr_ppd_uprn").exists()
    has_inspire = has_ppd and p("hmlr_inspire").exists()

    has_streets = p("streets").exists()
    has_parcel = p("inspire_parcel").exists()
    has_epc = p("epc").exists()

    con = _connect(staged_dir, threads, memory_limit, temp_directory)
    for name in REQUIRED_STAGED:
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{p(name)}')")

    counts = {n: _count(con, f"SELECT count(*) FROM {n}") for n in REQUIRED_STAGED}

    # --- UPRN -> TOID (deterministic single choice) -------------------------------
    con.execute("""
        CREATE TEMP TABLE uprn_toid AS
        SELECT l.uprn,
               arg_min(l.toid, (t.toid IS NULL, l.toid)) AS toid,
               arg_min(l.lids_confidence, (t.toid IS NULL, l.toid)) AS lids_confidence,
               count(DISTINCT l.toid) AS toid_count,
               arg_min(t.toid_x, (t.toid IS NULL, l.toid)) AS toid_x,
               arg_min(t.toid_y, (t.toid IS NULL, l.toid)) AS toid_y
        FROM lids_uprn_toid l
        LEFT JOIN open_toid t USING (toid)
        WHERE l.toid IS NOT NULL
        GROUP BY l.uprn
    """)

    con.execute("""
        CREATE TEMP TABLE base AS
        SELECT o.uprn, o.oa, o.lsoa, o.msoa, o.postcode,
               u.x, u.y,
               k.toid, coalesce(k.toid_count, 0) AS toid_count, k.lids_confidence,
               k.toid_x, k.toid_y
        FROM (SELECT DISTINCT ON (uprn) * FROM onsud ORDER BY uprn, postcode, lad, oa, lsoa, msoa) o
        LEFT JOIN (SELECT DISTINCT ON (uprn) * FROM open_uprn ORDER BY uprn, x, y) u USING (uprn)
        LEFT JOIN uprn_toid k USING (uprn)
    """)

    # --- point-in-polygon (set-based; DuckDB builds an R-tree for the join) -------
    con.execute("""
        CREATE TEMP TABLE bld AS
        SELECT building_id, g, ST_Area(g) AS area
        FROM (SELECT building_id, ST_GeomFromWKB(geom_wkb) AS g
              FROM (SELECT DISTINCT ON (building_id) * FROM buildings WHERE geom_wkb IS NOT NULL
                    ORDER BY building_id, geom_wkb))
    """)
    # narrow point tables: the only place point geometry lives
    con.execute("""
        CREATE TEMP TABLE pt_uprn AS
        SELECT uprn, ST_Point(x, y) AS pt FROM base WHERE x IS NOT NULL AND y IS NOT NULL
    """)
    con.execute("""
        CREATE TEMP TABLE m_uprn AS
        SELECT b.uprn, arg_min(g.building_id, (g.area, g.building_id)) AS building_id
        FROM pt_uprn b JOIN bld g ON ST_Within(b.pt, g.g) GROUP BY b.uprn
    """)
    con.execute("DROP TABLE pt_uprn")
    con.execute("""
        CREATE TEMP TABLE pt_toid AS
        SELECT uprn, ST_Point(toid_x, toid_y) AS pt FROM base
        WHERE toid_x IS NOT NULL AND toid_y IS NOT NULL
    """)
    con.execute("""
        CREATE TEMP TABLE m_toid AS
        SELECT b.uprn, arg_min(g.building_id, (g.area, g.building_id)) AS building_id
        FROM pt_toid b JOIN bld g ON ST_Within(b.pt, g.g) GROUP BY b.uprn
    """)
    con.execute("DROP TABLE pt_toid")

    ppd_sql = (
        (
            "(SELECT uprn, list(DISTINCT transaction_id ORDER BY transaction_id) AS ids "
            "FROM read_parquet('{}') GROUP BY uprn)"
        ).format(p("hmlr_ppd_uprn"))
        if has_ppd
        else None
    )
    insp_sql = (
        (
            "(SELECT pp.uprn, list(DISTINCT i.inspire_id ORDER BY i.inspire_id) AS ids "
            "FROM read_parquet('{}') pp JOIN read_parquet('{}') i USING (transaction_id) "
            "GROUP BY pp.uprn)"
        ).format(p("hmlr_ppd_uprn"), p("hmlr_inspire"))
        if has_inspire
        else None
    )

    st_cols = ("usrn", "street_name", "street_name_alt", "locality", "street_method", "usrn_count")
    st_types = ("BIGINT", "VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR", "INTEGER")
    pa_cols = ("parcel_inspire_id", "parcel_area_m2", "uprns_in_parcel")
    pa_types = ("VARCHAR", "DOUBLE", "INTEGER")
    epc_sel = (
        "o_epc.built_form AS epc_built_form, o_epc.property_type AS epc_property_type, o_epc.epc_count AS epc_count"
        if has_epc
        else "NULL::VARCHAR AS epc_built_form, NULL::VARCHAR AS epc_property_type, NULL::INTEGER AS epc_count"
    )
    joins = ""
    tiebreak = {
        "streets": ("usrn",),
        "inspire_parcel": ("parcel_inspire_id",),
        "epc": ("lodgement_date DESC NULLS LAST", "lmk_key DESC NULLS LAST"),
    }
    for flag, name, alias in (
        (has_streets, "streets", "o_streets"),
        (has_parcel, "inspire_parcel", "o_inspire_parcel"),
        (has_epc, "epc", "o_epc"),
    ):
        if flag:
            joins += (
                f"LEFT JOIN (SELECT DISTINCT ON (uprn) * FROM read_parquet('{p(name)}') "
                f"ORDER BY {_dedupe_order(p(name), *tiebreak[name])}) {alias} USING (uprn)\n            "
            )
    st_sel = ", ".join(
        (f"o_streets.{c}" if has_streets else f"NULL::{t}") + f" AS {c}" for c, t in zip(st_cols, st_types, strict=True)
    )
    pa_sel = ", ".join(
        (f"o_inspire_parcel.{c}" if has_parcel else f"NULL::{t}") + f" AS {c}"
        for c, t in zip(pa_cols, pa_types, strict=True)
    )

    con.execute(f"""
        CREATE TEMP TABLE final AS
        SELECT *,
               CASE WHEN uprns_in_building = 1 THEN 'single'
                    WHEN uprns_in_building IS NULL THEN NULL
                    WHEN uprns_in_parcel < uprns_in_building THEN 'parcel_finer'
                    ELSE 'shared' END AS outline_granularity
        FROM (
        SELECT *,
               CASE WHEN building_id IS NOT NULL
                    THEN count(*) OVER (PARTITION BY building_id) END AS uprns_in_building
        FROM (
            SELECT b.*,
                   coalesce(mt.building_id, mu.building_id) AS building_id,
                   CASE WHEN mt.building_id IS NOT NULL THEN 'toid_point_in_polygon'
                        WHEN mu.building_id IS NOT NULL THEN 'uprn_point_in_polygon'
                        ELSE 'none' END AS match_method,
                   CASE WHEN mt.building_id IS NOT NULL AND mu.building_id IS NOT NULL
                        THEN mt.building_id <> mu.building_id END AS methods_disagree,
                   {"pp.ids" if has_ppd else "NULL::VARCHAR[]"} AS ppd_transaction_ids,
                   {"ins.ids" if has_inspire else "NULL::VARCHAR[]"} AS inspire_ids,
                   {st_sel}, {pa_sel}, {epc_sel}
            FROM base b
            LEFT JOIN m_toid mt USING (uprn)
            LEFT JOIN m_uprn mu USING (uprn)
            {f"LEFT JOIN {ppd_sql} pp USING (uprn)" if has_ppd else ""}
            {f"LEFT JOIN {insp_sql} ins USING (uprn)" if has_inspire else ""}
            {joins}
        )
        )
    """)
    con.execute("DROP TABLE base; DROP TABLE m_uprn; DROP TABLE m_toid")

    # --- bbox + Hilbert ordering ---------------------------------------------------
    ext = con.execute(
        "SELECT min(x), min(y), max(x), max(y) FROM final WHERE x IS NOT NULL AND y IS NOT NULL"
    ).fetchone()
    bbox = [float(v) for v in ext] if ext[0] is not None else None
    if bbox:
        order = (
            f"ST_Hilbert(x, y, {{min_x: {bbox[0]}, min_y: {bbox[1]}, "
            f"max_x: {bbox[2]}, max_y: {bbox[3]}}}::BOX_2D) NULLS LAST, uprn"
        )
    else:
        order = "uprn"
    pc = ", postcode" if include_postcode else ""
    uprn_q = f"""
        SELECT uprn, x, y, CASE WHEN x IS NOT NULL AND y IS NOT NULL
                    THEN ST_AsWKB(ST_Point(x, y)) END::BLOB AS geometry, toid, toid_count, lids_confidence,
               toid_x, toid_y, building_id, match_method, methods_disagree, uprns_in_building,
               outline_granularity, oa, lsoa, msoa,
               usrn, street_name, street_name_alt, locality, street_method, usrn_count,
               parcel_inspire_id, parcel_area_m2, uprns_in_parcel,
               epc_built_form, epc_property_type, epc_count,
               ppd_transaction_ids, inspire_ids{pc}
        FROM final ORDER BY {order}
    """
    _write(con, uprn_q, out / "uprn.parquet", _geo_meta(["Point"], bbox))

    # --- EPC: separate file, uprn + all staged EPC columns ------------------------
    epc_path = out / "epc.parquet"
    if has_epc:
        epc_tmp = _tmp(epc_path)
        epc_order = _dedupe_order(p("epc"), *tiebreak["epc"])
        con.execute(
            f"COPY (SELECT * FROM (SELECT DISTINCT ON (uprn) * FROM read_parquet('{p('epc')}') "
            f"ORDER BY {epc_order}) WHERE uprn IN (SELECT uprn FROM final) ORDER BY uprn) "
            f"TO '{epc_tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        os.replace(epc_tmp, epc_path)
    elif epc_path.exists():
        epc_path.unlink()  # stale output from an earlier build

    # --- buildings matched by at least one UPRN -----------------------------------
    bb = con.execute("""
        SELECT min(ST_XMin(g)), min(ST_YMin(g)), max(ST_XMax(g)), max(ST_YMax(g)),
               list(DISTINCT ST_GeometryType(g)::VARCHAR)
        FROM bld WHERE building_id IN (SELECT building_id FROM final WHERE building_id IS NOT NULL)
    """).fetchone()
    bbbox = [float(v) for v in bb[:4]] if bb[0] is not None else None
    gtypes = sorted({"MultiPolygon" if t == "MULTIPOLYGON" else "Polygon" for t in (bb[4] or [])}) or ["Polygon"]
    hb = bbbox or [0.0, 0.0, 700000.0, 1300000.0]
    _write(
        con,
        f"""
        SELECT building_id, ST_AsWKB(g)::BLOB AS geometry FROM bld
        WHERE building_id IN (SELECT building_id FROM final WHERE building_id IS NOT NULL)
        ORDER BY ST_Hilbert(ST_X(ST_Centroid(g)), ST_Y(ST_Centroid(g)),
          {{min_x: {hb[0]}, min_y: {hb[1]}, max_x: {hb[2]}, max_y: {hb[3]}}}::BOX_2D), building_id
    """,
        out / "buildings.parquet",
        _geo_meta(gtypes, bbbox),
    )

    # --- stats ---------------------------------------------------------------------
    total = _count(con, "SELECT count(*) FROM final")

    def pct(n: int, d: int) -> float:
        return round(100.0 * n / d, 4) if d else 0.0

    methods = {
        m: _count(con, f"SELECT count(*) FROM final WHERE match_method='{m}'")
        for m in ("toid_point_in_polygon", "uprn_point_in_polygon", "none")
    }
    both = _count(con, "SELECT count(*) FROM final WHERE methods_disagree IS NOT NULL")
    dis = _count(con, "SELECT count(*) FROM final WHERE methods_disagree")
    dist = con.execute("""
        SELECT count(*) FILTER (WHERE uprns_in_building = 1),
               count(*) FILTER (WHERE uprns_in_building BETWEEN 2 AND 5),
               count(*) FILTER (WHERE uprns_in_building BETWEEN 6 AND 20),
               count(*) FILTER (WHERE uprns_in_building > 20)
        FROM final""").fetchone()
    pdist = con.execute("""
        SELECT count(*) FILTER (WHERE uprns_in_parcel = 1),
               count(*) FILTER (WHERE uprns_in_parcel BETWEEN 2 AND 5),
               count(*) FILTER (WHERE uprns_in_parcel BETWEEN 6 AND 20),
               count(*) FILTER (WHERE uprns_in_parcel > 20)
        FROM final""").fetchone()
    gran = dict(con.execute("SELECT coalesce(outline_granularity, 'none'), count(*) FROM final GROUP BY 1").fetchall())
    gran = {k: gran.get(k, 0) for k in ("single", "parcel_finer", "shared", "none")}
    forms = dict(
        con.execute(
            "SELECT epc_built_form, count(*) FROM final WHERE epc_built_form IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1"
        ).fetchall()
    )
    n_street = _count(con, "SELECT count(*) FROM final WHERE street_name IS NOT NULL")
    n_parcel = _count(con, "SELECT count(*) FROM final WHERE parcel_inspire_id IS NOT NULL")
    n_epc = _count(con, "SELECT count(*) FROM final WHERE epc_count IS NOT NULL")
    n_ppd = _count(con, "SELECT count(*) FROM final WHERE ppd_transaction_ids IS NOT NULL")
    n_ins = _count(con, "SELECT count(*) FROM final WHERE inspire_ids IS NOT NULL")
    stats = {
        "lad": lad,
        "built_at": datetime.now(UTC).isoformat(),
        "input_row_counts": counts,
        "hmlr_ppd_present": has_ppd,
        "hmlr_inspire_present": has_inspire,
        "total_uprns": total,
        "with_point": _count(con, "SELECT count(*) FROM final WHERE x IS NOT NULL AND y IS NOT NULL"),
        "with_toid": _count(con, "SELECT count(*) FROM final WHERE toid IS NOT NULL"),
        "with_toid_point": _count(con, "SELECT count(*) FROM final WHERE toid_x IS NOT NULL AND toid_y IS NOT NULL"),
        "with_multiple_toids": _count(con, "SELECT count(*) FROM final WHERE toid_count > 1"),
        "match_method": {m: {"count": n, "pct": pct(n, total)} for m, n in methods.items()},
        "methods_disagree": {"count": dis, "of_both_matched": both, "pct": pct(dis, both)},
        "uprns_in_building": {"1": dist[0], "2-5": dist[1], "6-20": dist[2], ">20": dist[3]},
        "streets_present": has_streets,
        "inspire_parcel_present": has_parcel,
        "epc_present": has_epc,
        "with_street_name": {"count": n_street, "pct": pct(n_street, total)},
        "with_parcel": {"count": n_parcel, "pct": pct(n_parcel, total)},
        "uprns_in_parcel": {"1": pdist[0], "2-5": pdist[1], "6-20": pdist[2], ">20": pdist[3]},
        "outline_granularity": gran,
        "with_epc": {"count": n_epc, "pct": pct(n_epc, total)},
        "epc_built_form": forms,
        "with_ppd": {"count": n_ppd, "pct": pct(n_ppd, total)},
        "with_inspire": {"count": n_ins, "pct": pct(n_ins, total)},
        "buildings_written": _count(con, "SELECT count(DISTINCT building_id) FROM final WHERE building_id IS NOT NULL"),
        "include_postcode": include_postcode,
    }
    con.close()
    write_text_atomic(out / "stats.json", json.dumps(stats, indent=2))  # completion marker: always last
    return stats


def _core_present(d: Path) -> bool:
    return all((d / f"{n}.parquet").exists() for n in REQUIRED_STAGED)


def staged_lads(staged_dir: Path) -> list[str]:
    """LAD codes with complete staged data: every required staged file AND the ``.stage_done`` marker.

    The marker is written by ``stage_all`` once the required step succeeded (and records which optional steps
    ran/failed); a LAD with core files but no marker is half-staged (or staged by an old/interrupted run) and is
    listed by ``unstaged_lads`` instead. Old empty/``ok`` markers count as complete."""
    d = Path(staged_dir)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and _core_present(p) and (p / DONE_MARKER).exists())


def unstaged_lads(staged_dir: Path) -> list[str]:
    """LAD dirs holding the core staged files but no ``.stage_done`` marker (staging unfinished or crashed)."""
    d = Path(staged_dir)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and _core_present(p) and not (p / DONE_MARKER).exists())


def _no_marker_msg(lad: str, staged_dir: Path) -> str:
    return (
        f"{Path(staged_dir) / lad}: no {DONE_MARKER} marker - staging unfinished or interrupted; "
        "re-run `stage` for this LAD (LADs staged by older versions need the marker created by re-running stage)"
    )


def build_all(staged_dir: Path, out_dir: Path, lads: list[str] | None = None, skip_existing: bool = True, **kw) -> dict:
    """Build every LAD that has staged data (or just ``lads``), one at a time.

    Only LADs with a ``.stage_done`` marker are built; the others are reported (not built) under
    ``"not_staged"`` (explicit ``lads`` lacking the marker go to ``"failed"``). LADs whose ``stats.json`` already
    exists are skipped when ``skip_existing`` (resumable) unless the stage marker is newer than ``stats.json``
    (re-staged since). A failing LAD is recorded and does not stop the run. Returns
    ``{"built": {lad: stats}, "skipped": [lad], "failed": {lad: message}, "not_staged": {lad: message},
    "warnings": {lad: [failed optional staging steps]}}``.

    Caveat: LADs are built independently. A building straddling a LAD boundary can appear in
    both LADs' ``buildings.parquet`` (same sha1 ``building_id``): de-duplicate on ``building_id``
    when merging. ``uprns_in_building`` counts only the UPRNs inside that LAD, so such a
    building has partial counts; ``national`` recomputes them over (uprn, building_id) after merging.
    """
    todo = list(lads) if lads else staged_lads(staged_dir)
    res: dict = {"built": {}, "skipped": [], "failed": {}, "not_staged": {}, "warnings": {}}
    if not lads:
        res["not_staged"] = {lad: _no_marker_msg(lad, staged_dir) for lad in unstaged_lads(staged_dir)}
    for lad in todo:
        marker_f = Path(staged_dir) / lad / DONE_MARKER
        marker = read_marker(Path(staged_dir) / lad)
        if marker is None:
            res["failed"][lad] = _no_marker_msg(lad, staged_dir)
            continue
        if marker.get("failed_steps"):
            res["warnings"][lad] = [f"optional staging step failed: {k}" for k in marker["failed_steps"]]
        stats_f = lad_out_dir(out_dir, lad) / "stats.json"
        if skip_existing and stats_f.exists() and stats_f.stat().st_mtime_ns >= marker_f.stat().st_mtime_ns:
            res["skipped"].append(lad)
            continue
        try:
            res["built"][lad] = build(lad, staged_dir, out_dir, **kw)
        except Exception as e:  # keep going; rerun resumes
            res["failed"][lad] = f"{type(e).__name__}: {e}"
    return res
