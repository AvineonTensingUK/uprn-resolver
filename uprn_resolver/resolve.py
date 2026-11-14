"""Consolidated best-answer-per-UPRN products, built after ``build`` / ``provenance`` / ``poi`` / ``osm`` / ``address``.

Two products per LAD in ``out/lad=<lad>/``:

* ``resolved_open.parquet`` (publishable, OGL + a clearly labelled Overture POI attachment): point, best outline id
  (OpenMap Local building id or INSPIRE parcel id; never parcel geometry), outline confidence, street (OS Open
  Names, else OS Open Roads fallback), EPC property type/built form, POI name/category (high/medium matches only),
  LAD/LSOA, and a ``quality`` score + notes.  Postcode only with ``include_postcode=True``.
* ``resolved_local.parquet`` (NEVER published): everything above, the postcode, and the best full address:
  EPC (latest cert) > HMLR PPD address (fallback ``ppd``, restricted)
  > OSM addr:* (ODbL; ref_gb_uprn / inside_building_single / nearest_node_10m)
  > open partial.

Best outline: the candidate with the fewest UPRNs (ties: OpenMap building > OSM building [local only] > Microsoft
building > parcel).  Open lane candidates are OpenMap building (OGL-3.0), Microsoft building (CDLA-Permissive-2.0,
``outline_ms.parquet``) and INSPIRE/RoS parcel id (OGL-3.0); ``best_outline_licence`` labels the chosen one.  The
local file adds OSM buildings (ODbL, ``outline_odbl.parquet``) as ``*_local`` columns; no OSM-derived value is ever in
resolved_open.  Guard: an MS/OSM outline matched by a ``nearest*`` fallback is ignored when the OpenMap building
already contains the UPRN, unless it has strictly fewer UPRNs AND area ratio vs OpenMap < 0.8.

Outline counts: ``uprns_in_building`` in ``uprn.parquet`` counts only the LAD's own UPRNs.  When
``<out_dir>/national/building_uprn_counts.parquet`` exists (written by ``national``) and is not older than the LAD's
``uprn.parquet``, the OpenMap building candidate uses its ``uprns_in_building_national`` (cross-LAD) instead, so a
building straddling a LAD boundary gets the same outline count and confidence in both LADs; otherwise it falls back
to the LAD-local count (``resolved.stats.json`` -> ``building_counts``).  Parcel / Microsoft / OSM counts stay
LAD-local.

Completion: ``resolved.stats.json`` is the marker; it is deleted before any output is written, outputs are written
via ``.tmp`` + atomic rename and the marker is written last.

Optional inputs (provenance, outline_ms, outline_odbl, epc, poi, address_local, streets_odbl, staged open_uprn) may be
absent: the related columns are NULL and ``quality`` says so.  Only ``uprn.parquet`` is required.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import duckdb

from uprn_resolver import bng
from uprn_resolver.build import _geo_meta, _tmp, begin_outputs, write_text_atomic

log = logging.getLogger(__name__)

# fallback confidence when streets_odbl.parquet predates osm_addr_confidence
OSM_METHOD_CONFIDENCE = {"ref_gb_uprn": "high", "inside_building_single": "medium", "nearest_node_10m": "low"}
POI_LICENCE = "Overture Maps Places; per provider: CDLA-Permissive-2.0 / Apache-2.0 / CC0-1.0"
OPEN_NAME = "resolved_open.parquet"
LOCAL_NAME = "resolved_local.parquet"
MARKER = "resolved.stats.json"
OWNED = (OPEN_NAME, LOCAL_NAME, "resolved_open.LICENCE.txt", "resolved_local.LICENCE.txt")
NATIONAL_COUNTS = "national/building_uprn_counts.parquet"

OPEN_LICENCE_TEXT = """resolved_open.parquet - consolidated best answer per UPRN (publishable subset)

Contains OS data (c) Crown copyright and database right; HM Land Registry data (c) Crown copyright and database
right (INSPIRE parcel IDs only, no parcel geometry); EPC non-address fields (MHCLG / Scottish Government), all
under the Open Government Licence v3.0.

The poi_* columns (name, category) come from Overture Maps Places and keep their own licences, listed in
poi_licence (CDLA-Permissive-2.0 / Apache-2.0 / CC0-1.0 per provider); attribute Overture when you reuse them.
The postcode column exists only when the file was built with include_postcode=True (ONS licence pending).
Outline ids, confidence and quality are heuristic derived values.  No address text is present.
"""
LOCAL_LICENCE_TEXT = """resolved_local.parquet - LOCAL USE ONLY, DO NOT PUBLISH OR REDISTRIBUTE

Contains everything in resolved_open.parquet plus address text and postcode.  Per-row licence is in
address_licence: 'EPC-restricted' (EPC address fields, published with OS / Royal Mail consent for restricted use
only), 'PPD-restricted' (HM Land Registry Price Paid Data address fields: Contains HM Land Registry data (c) Crown
copyright and database right 2021, OGL v3.0, but the address data is restricted by Royal Mail / OS to personal and
non-commercial use), 'ODbL-1.0' (OpenStreetMap contributors; share-alike applies to derived databases) or
'OGL' (open partial: OS Open Names street/locality + ONSUD postcode).  This file must never be added to a release.

ppd_sales_count / ppd_last_price / ppd_last_sale_date / ppd_first_sale_date are per-UPRN Price Paid Data sale
summaries (HM Land Registry, OGL v3.0, ppd_local_summary.parquet); they are local-only until HMLR confirms the
open lane.
"""

OPEN_COLUMNS = (
    "uprn",
    "x",
    "y",
    "lat",
    "lon",
    "geometry",
    "best_outline_id",
    "best_outline_source",
    "best_outline_licence",
    "outline_uprn_count",
    "outline_confidence",
    "street_name",
    "street_source",
    "locality",
    "usrn",
    "usrn_link_flag",
    "postcode_flag",
    "property_type_epc",
    "built_form_epc",
    "epc_lmk_key",
    "epc_certificate_type",
    "epc_lodgement_date",
    "epc_count",
    "poi_name",
    "poi_category",
    "poi_match_confidence",
    "poi_licence",
    "lad",
    "lsoa",
    "quality_score",
    "quality",
)


def _q(p) -> str:
    return "'" + str(p).replace("'", "''") + "'"


def _cols(con, path: Path) -> set[str]:
    return {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({_q(path)})").fetchall()}


def _connect(memory_limit: str | None = None) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    if memory_limit:
        con.execute(f"SET memory_limit='{memory_limit}'")
    return con


def _sel(have: set[str], alias: str, col: str, typ: str, out: str | None = None) -> str:
    out = out or col
    if col in have:
        return f"{alias}.{col} AS {out}"
    return f"NULL::{typ} AS {out}"


def _single_line(parts: list[str]) -> str:
    return ", ".join(f"nullif(trim({p}), '')" for p in parts)


def resolve_lad(
    lad: str,
    out_dir: Path,
    include_postcode: bool = False,
    *,
    staged_dir: Path | None = None,
    memory_limit: str | None = None,
) -> dict:
    """Write ``resolved_open.parquet`` and ``resolved_local.parquet`` (+ LICENCE sidecars) for one LAD."""
    t0 = time.time()
    out_dir = Path(out_dir)
    d = out_dir / f"lad={lad}"
    uprn_p = d / "uprn.parquet"
    if not uprn_p.exists():
        raise FileNotFoundError(f"{uprn_p} missing")
    staged = Path(staged_dir) if staged_dir else out_dir.parent / "staged"
    begin_outputs(d, MARKER, OWNED)  # completion marker removed first, written last
    con = _connect(memory_limit)

    missing: list[str] = []
    u = _cols(con, uprn_p)

    def opt(name: str, p: Path):
        if p.exists():
            return p
        missing.append(name)
        return None

    prov_p = opt("provenance", d / "provenance.parquet")
    epc_p = opt("epc", d / "epc.parquet")
    poi_p = opt("poi", d / "poi.parquet")
    adr_p = opt("address_local", d / "address_local.parquet")
    ppd_p = opt("ppd_local_summary", d / "ppd_local_summary.parquet")
    osm_p = opt("streets_odbl", d / "streets_odbl.parquet")
    ms_p = opt("outline_ms", d / "outline_ms.parquet")
    od_p = opt("outline_odbl", d / "outline_odbl.parquet")
    ou_p = staged / lad / "open_uprn.parquet"
    ou_p = ou_p if ou_p.exists() else None

    # --- base ---------------------------------------------------------------------------------------------
    nat_p = out_dir / NATIONAL_COUNTS
    use_nat = (
        nat_p.exists()
        and "building_id" in u
        and "uprns_in_building" in u
        and nat_p.stat().st_mtime >= uprn_p.stat().st_mtime
    )
    if use_nat:
        nat_join = f"LEFT JOIN read_parquet({_q(nat_p)}) nat ON nat.building_id = t.building_id"
        n_in_b = "coalesce(nat.uprns_in_building_national::BIGINT, t.uprns_in_building::BIGINT) AS uprns_in_building"
    else:
        nat_join = ""
        n_in_b = _sel(u, "t", "uprns_in_building", "BIGINT")
        if nat_p.exists():
            missing.append("national building counts older than uprn.parquet (LAD-local counts used)")
    con.execute(f"""CREATE TEMP TABLE b AS SELECT
        t.uprn, t.x, t.y,
        {_sel(u, "t", "building_id", "VARCHAR")}, {n_in_b},
        {_sel(u, "t", "parcel_inspire_id", "VARCHAR")}, {_sel(u, "t", "uprns_in_parcel", "BIGINT")},
        {_sel(u, "t", "methods_disagree", "BOOLEAN")}, {_sel(u, "t", "usrn", "BIGINT", "usrn_u")},
        {_sel(u, "t", "street_name", "VARCHAR", "street_name_u")}, {_sel(u, "t", "locality", "VARCHAR")},
        {_sel(u, "t", "lsoa", "VARCHAR")}, {_sel(u, "t", "epc_built_form", "VARCHAR")},
        {_sel(u, "t", "epc_property_type", "VARCHAR")},
        {_sel(u, "t", "epc_count", "BIGINT", "epc_count_u")}, {_sel(u, "t", "postcode", "VARCHAR", "postcode_u")}
        FROM read_parquet({_q(uprn_p)}) t {nat_join}""")
    if ou_p:
        con.execute(f"""CREATE TEMP TABLE ll AS SELECT uprn, lat, lon FROM read_parquet({_q(ou_p)})""")
    else:
        missing.append("open_uprn(lat/lon computed by transform)")
        inv = bng.duckdb_st_transform_inverse_sql("ST_Point(x, y)")  # OSTN15 inverse
        con.execute(f"""CREATE TEMP TABLE ll AS SELECT uprn, ST_Y({inv}) AS lat, ST_X({inv}) AS lon
            FROM b WHERE x IS NOT NULL AND y IS NOT NULL""")

    # --- provenance ---------------------------------------------------------------------------------------
    if prov_p:
        pc = _cols(con, prov_p)
        con.execute(f"""CREATE TEMP TABLE pv AS SELECT uprn,
            {_sel(pc, "p", "usrn", "BIGINT", "usrn_p")}, {_sel(pc, "p", "usrn_link_flag", "VARCHAR")},
            {_sel(pc, "p", "street_name_fallback", "VARCHAR")}, {_sel(pc, "p", "postcode_flag", "VARCHAR")},
            {_sel(pc, "p", "postcode_onsud", "VARCHAR")}
            FROM read_parquet({_q(prov_p)}) p""")
    else:
        con.execute("""CREATE TEMP TABLE pv AS SELECT NULL::BIGINT uprn, NULL::BIGINT usrn_p,
            NULL::VARCHAR usrn_link_flag, NULL::VARCHAR street_name_fallback, NULL::VARCHAR postcode_flag,
            NULL::VARCHAR postcode_onsud WHERE false""")

    # --- epc ----------------------------------------------------------------------------------------------
    if epc_p:
        ec = _cols(con, epc_p)
        epc_order = ", ".join(
            [
                *(f"{c} DESC NULLS LAST" for c in ("lodgement_date", "lmk_key") if c in ec),
                *(f"{c} NULLS LAST" for c in ("property_type", "built_form") if c in ec),
                "uprn",
            ]
        )
        con.execute(f"""CREATE TEMP TABLE ep AS SELECT uprn,
            {_sel(ec, "e", "property_type", "VARCHAR", "pt")}, {_sel(ec, "e", "built_form", "VARCHAR", "bf")},
            {_sel(ec, "e", "lmk_key", "VARCHAR", "lmk")}, {_sel(ec, "e", "certificate_type", "VARCHAR", "ctype")},
            {_sel(ec, "e", "lodgement_date", "DATE", "ldate")}, {_sel(ec, "e", "epc_count", "BIGINT", "ecnt")}
            FROM (SELECT * FROM read_parquet({_q(epc_p)})) e
            QUALIFY row_number() OVER (PARTITION BY uprn ORDER BY {epc_order}) = 1""")
    else:
        con.execute("""CREATE TEMP TABLE ep AS SELECT NULL::BIGINT uprn, NULL::VARCHAR pt, NULL::VARCHAR bf,
            NULL::VARCHAR lmk, NULL::VARCHAR ctype, NULL::DATE ldate, NULL::BIGINT ecnt WHERE false""")

    # --- poi (one best per UPRN, high/medium only) ----------------------------------------------------------
    if poi_p:
        con.execute(f"""CREATE TEMP TABLE po AS SELECT uprn, name AS poi_name, category AS poi_category,
            match_confidence AS poi_match_confidence FROM read_parquet({_q(poi_p)})
            WHERE uprn IS NOT NULL AND match_confidence IN ('high', 'medium') AND name IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY uprn ORDER BY (match_confidence = 'high') DESC,
                overture_confidence DESC NULLS LAST, match_distance_m NULLS LAST, poi_id) = 1""")
    else:
        con.execute(
            "CREATE TEMP TABLE po AS SELECT NULL::BIGINT uprn, NULL::VARCHAR poi_name, "
            "NULL::VARCHAR poi_category, NULL::VARCHAR poi_match_confidence WHERE false"
        )

    # --- finer outline sources (MS = CDLA-Permissive-2.0, open lane; OSM = ODbL, local lane only) -----------
    def _outline_tbl(name: str, path, id_c: str, n_c: str, meth_c: tuple[str, ...], ratio_c: str, pre: str):
        if path is None:
            con.execute(
                f"CREATE TEMP TABLE {name} AS SELECT NULL::BIGINT uprn, NULL::VARCHAR {pre}_id, "
                f"NULL::BIGINT {pre}_n, NULL::BOOLEAN {pre}_nearest, NULL::DOUBLE {pre}_ratio WHERE false"
            )
            return
        oc = _cols(con, path)
        m = next((c for c in meth_c if c in oc), None)
        nearest = f"coalesce(t.{m} LIKE 'nearest%', false)" if m else "false"
        con.execute(f"""CREATE TEMP TABLE {name} AS SELECT t.uprn, t.{id_c}::VARCHAR AS {pre}_id,
            t.{n_c}::BIGINT AS {pre}_n, {nearest} AS {pre}_nearest, {_sel(oc, "t", ratio_c, "DOUBLE", pre + "_ratio")}
            FROM read_parquet({_q(path)}) t WHERE t.{id_c} IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY t.uprn ORDER BY t.{id_c}) = 1""")

    _outline_tbl("msb", ms_p, "ms_building_id", "uprns_in_ms_building", ("match_method",), "area_ratio_ms_to_ogl", "ms")
    _outline_tbl(
        "osb",
        od_p,
        "osm_building_id",
        "uprns_in_osm_building",
        ("osm_match_method", "match_method"),
        "osm_ogl_area_ratio",
        "osm",
    )
    parcel_src = "ros_parcel" if lad.startswith("S") else "inspire_parcel"

    def _guard(pre: str) -> str:
        # a 'nearest' fallback match is ignored when the OGL building already contains the point, unless it has
        # strictly fewer UPRNs AND is clearly smaller than the OGL building (area ratio < 0.8)
        return (
            f"(NOT {pre}_nearest OR b.building_id IS NULL OR "
            f"({pre}_n < coalesce(b.uprns_in_building, 99) AND coalesce({pre}_ratio, 1) < 0.8))"
        )

    def _cand(prio: int, src: str, id_e: str, n_e: str, lic: str, cond: str) -> str:
        return (
            f"CASE WHEN {cond} THEN struct_pack(n := coalesce({n_e}, 99)::BIGINT, p := {prio}, "
            f"s := '{src}', i := {id_e}::VARCHAR, l := '{lic}') END"
        )

    c_ogl = _cand(0, "openmap_building", "b.building_id", "b.uprns_in_building", "OGL-3.0", "b.building_id IS NOT NULL")
    c_par = _cand(
        3, parcel_src, "b.parcel_inspire_id", "b.uprns_in_parcel", "OGL-3.0", "b.parcel_inspire_id IS NOT NULL"
    )
    c_ms = _cand(
        2,
        "ms_building",
        "msb.ms_id",
        "msb.ms_n",
        "CDLA-Permissive-2.0",
        "msb.ms_id IS NOT NULL AND " + _guard("msb.ms"),
    )
    c_osm = _cand(
        1, "osm_building", "osb.osm_id", "osb.osm_n", "ODbL-1.0", "osb.osm_id IS NOT NULL AND " + _guard("osb.osm")
    )
    # ties: OpenMap > (OSM, local only) > MS > parcel.  list_sort on structs orders by count, then priority
    best_open = f"list_sort(list_filter([{c_ogl}, {c_ms}, {c_par}], lambda v: v IS NOT NULL))[1]"
    best_local = f"list_sort(list_filter([{c_ogl}, {c_osm}, {c_ms}, {c_par}], lambda v: v IS NOT NULL))[1]"

    # --- combine (open) -------------------------------------------------------------------------------------
    # outline: the parcel is chosen only when strictly finer than the building (or no building exists)
    pc_expr = "coalesce(postcode_onsud, postcode_u)"
    notes_missing = "".join(
        f" || '; no {m.split('(')[0]}'" if m in ("provenance", "epc", "poi") else "" for m in missing
    )
    con.execute(f"""CREATE TEMP TABLE r AS
        WITH c AS (
          SELECT b.*, ll.lat, ll.lon, pv.usrn_link_flag, pv.postcode_flag, pv.street_name_fallback,
                 pv.postcode_onsud, coalesce(b.usrn_u, pv.usrn_p) AS usrn,
                 {best_open} AS _bo, {best_local} AS _bl,
                 ep.pt, ep.bf, ep.lmk, ep.ctype, ep.ldate, ep.ecnt,
                 po.poi_name, po.poi_category, po.poi_match_confidence
          FROM b LEFT JOIN ll USING (uprn) LEFT JOIN pv USING (uprn) LEFT JOIN ep USING (uprn)
               LEFT JOIN po USING (uprn) LEFT JOIN msb USING (uprn) LEFT JOIN osb USING (uprn)
        ), c2 AS (
          SELECT * EXCLUDE (_bo, _bl),
                 _bo.i AS best_outline_id, coalesce(_bo.s, 'none') AS best_outline_source,
                 _bo.l AS best_outline_licence,
                 CASE WHEN _bo.i IS NULL THEN NULL ELSE _bo.n END AS outline_uprn_count,
                 _bl.i AS best_outline_id_local, coalesce(_bl.s, 'none') AS best_outline_source_local,
                 _bl.l AS best_outline_licence_local,
                 CASE WHEN _bl.i IS NULL THEN NULL ELSE _bl.n END AS outline_uprn_count_local
          FROM c
        ), c3 AS (
          SELECT *, CASE WHEN best_outline_id IS NULL THEN 'none'
                         WHEN coalesce(outline_uprn_count, 99) <= 1 THEN 'property'
                         WHEN outline_uprn_count <= 5 THEN 'small_group' ELSE 'block' END AS outline_confidence,
                 CASE WHEN best_outline_id_local IS NULL THEN 'none'
                         WHEN coalesce(outline_uprn_count_local, 99) <= 1 THEN 'property'
                         WHEN outline_uprn_count_local <= 5 THEN 'small_group' ELSE 'block' END
                      AS outline_confidence_local,
                 coalesce(street_name_u, street_name_fallback) AS street_name,
                 CASE WHEN street_name_u IS NOT NULL THEN 'os_open_names'
                      WHEN street_name_fallback IS NOT NULL THEN 'os_open_roads' END AS street_source
          FROM c2
        ), c4 AS (
          SELECT *,
            list_filter([
              CASE outline_confidence WHEN 'none' THEN 'no outline' WHEN 'block' THEN 'block outline'
                   WHEN 'small_group' THEN 'small-group outline' END,
              CASE WHEN best_outline_source = 'ms_building' THEN 'outline from Microsoft footprints' END,
              CASE WHEN usrn_link_flag = 'far' THEN 'street far from USRN'
                   WHEN usrn_link_flag = 'no_geometry' THEN 'USRN has no geometry' END,
              CASE WHEN street_name IS NULL THEN 'no street name'
                   WHEN street_source = 'os_open_roads' THEN 'street name from Open Roads fallback' END,
              CASE WHEN usrn IS NULL THEN 'no USRN' END,
              CASE WHEN postcode_flag = 'far' THEN 'postcode far from unit centroid'
                   WHEN postcode_flag = 'not_in_codepoint' THEN 'postcode not in Code-Point' END,
              CASE WHEN methods_disagree THEN 'building match methods disagree' END
            ], lambda v: v IS NOT NULL) AS nl,
            100 - CASE outline_confidence WHEN 'none' THEN 40 WHEN 'block' THEN 30 WHEN 'small_group' THEN 10
                                           ELSE 0 END
                - CASE WHEN usrn_link_flag = 'far' THEN 15 WHEN usrn_link_flag = 'no_geometry' THEN 5 ELSE 0 END
                - CASE WHEN street_name IS NULL THEN 20 WHEN street_source = 'os_open_roads' THEN 5 ELSE 0 END
                - CASE WHEN postcode_flag IN ('far', 'not_in_codepoint') THEN 5 ELSE 0 END
                - CASE WHEN methods_disagree THEN 10 ELSE 0 END AS quality_score
          FROM c3
        )
        SELECT uprn, x, y, lat, lon, best_outline_id, best_outline_source, best_outline_licence,
               outline_uprn_count::BIGINT AS outline_uprn_count, outline_confidence,
               best_outline_id_local, best_outline_source_local, best_outline_licence_local,
               outline_uprn_count_local::BIGINT AS outline_uprn_count_local, outline_confidence_local,
               street_name, street_source, locality, usrn,
               usrn_link_flag, {pc_expr} AS postcode, postcode_flag, pt AS property_type_epc,
               bf AS built_form_epc, lmk AS epc_lmk_key, ctype AS epc_certificate_type,
               ldate AS epc_lodgement_date, coalesce(ecnt, epc_count_u)::BIGINT AS epc_count,
               poi_name, poi_category, poi_match_confidence,
               CASE WHEN poi_name IS NOT NULL THEN {_q(POI_LICENCE)} END AS poi_licence,
               {lad!r}::VARCHAR AS lad, lsoa,
               greatest(quality_score, 0)::INTEGER AS quality_score,
               coalesce(nullif(array_to_string(nl, '; '), ''), 'ok'){notes_missing} AS quality,
               coalesce(pt, epc_property_type) AS _pt, coalesce(bf, epc_built_form) AS _bf
        FROM c4""")
    # fall back to uprn.parquet epc columns when epc.parquet lacks the UPRN
    con.execute("UPDATE r SET property_type_epc = _pt, built_form_epc = _bf")
    con.execute("ALTER TABLE r DROP COLUMN _pt; ALTER TABLE r DROP COLUMN _bf")

    ext = con.execute("SELECT min(x), min(y), max(x), max(y) FROM r WHERE x IS NOT NULL AND y IS NOT NULL").fetchall()[
        0
    ]
    bbox = [float(v) for v in ext] if ext[0] is not None else None
    order = (
        "uprn"
        if not bbox
        else f"ST_Hilbert(x, y, {{min_x: {bbox[0]}, min_y: {bbox[1]}, max_x: {bbox[2]}, max_y: {bbox[3]}}}"
        "::BOX_2D) NULLS LAST, uprn"
    )
    geo = _geo_meta(["Point"], bbox).replace("'", "''")

    def write(sql: str, path: Path) -> None:
        tmp = _tmp(path)
        con.execute(f"COPY ({sql}) TO {_q(tmp)} (FORMAT PARQUET, COMPRESSION ZSTD, KV_METADATA {{geo: '{geo}'}})")
        os.replace(tmp, path)

    geom = "CASE WHEN x IS NOT NULL AND y IS NOT NULL THEN ST_AsWKB(ST_Point(x, y)) END::BLOB AS geometry"
    rest = [c for c in OPEN_COLUMNS[6:] if c != "postcode_flag"]
    sel_open = ", ".join(
        ["uprn", "x", "y", "lat", "lon", geom, *rest] + (["postcode"] if include_postcode else []) + ["postcode_flag"]
    )
    out_open = d / OPEN_NAME
    write(f"SELECT {sel_open} FROM r ORDER BY {order}", out_open)
    write_text_atomic(d / "resolved_open.LICENCE.txt", OPEN_LICENCE_TEXT)

    # --- local: address precedence --------------------------------------------------------------------------
    if adr_p:
        ac = _cols(con, adr_p)
        con.execute(f"""CREATE TEMP TABLE ad AS SELECT uprn,
            CASE WHEN licence = 'EPC-restricted' THEN {"single_line_address" if "single_line_address" in ac else "NULL"}
            END AS epc_line,
            CASE WHEN licence = 'PPD-restricted' THEN {"single_line_address" if "single_line_address" in ac else "NULL"}
            END AS ppd_line, CASE WHEN licence = 'EPC-restricted' THEN address_source END AS epc_src,
            {_sel(ac, "a", "street_name_open", "VARCHAR")}, {_sel(ac, "a", "locality_open", "VARCHAR")},
            {_sel(ac, "a", "postcode_onsud", "VARCHAR", "pc_ad")}
            FROM read_parquet({_q(adr_p)}) a""")
    else:
        con.execute("""CREATE TEMP TABLE ad AS SELECT NULL::BIGINT uprn, NULL::VARCHAR epc_line,
            NULL::VARCHAR ppd_line, NULL::VARCHAR epc_src, NULL::VARCHAR street_name_open, NULL::VARCHAR locality_open,
            NULL::VARCHAR pc_ad WHERE false""")
    if osm_p:
        oc = _cols(con, osm_p)
        if "osm_addr_confidence" in oc:
            conf_expr = "o.osm_addr_confidence"
        else:
            whens = " ".join(f"WHEN '{m}' THEN '{c}'" for m, c in OSM_METHOD_CONFIDENCE.items())
            conf_expr = f"CASE o.osm_addr_match_method {whens} END"
        bl_expr = "coalesce(o.building_level_address, false)" if "building_level_address" in oc else "false"
        con.execute(f"""CREATE TEMP TABLE os AS SELECT uprn,
            {_sel(oc, "o", "osm_housenumber", "VARCHAR")}, {_sel(oc, "o", "osm_housename", "VARCHAR")},
            {_sel(oc, "o", "osm_unit", "VARCHAR")}, {_sel(oc, "o", "osm_addr_street", "VARCHAR")},
            {_sel(oc, "o", "osm_street_name", "VARCHAR")}, {_sel(oc, "o", "osm_addr_postcode", "VARCHAR")},
            {_sel(oc, "o", "osm_addr_match_method", "VARCHAR", "osm_m")},
            {conf_expr} AS osm_conf, {bl_expr} AS osm_bl
            FROM read_parquet({_q(osm_p)}) o
            WHERE (osm_housenumber IS NOT NULL OR osm_housename IS NOT NULL)""")
    else:
        con.execute("""CREATE TEMP TABLE os AS SELECT NULL::BIGINT uprn, NULL::VARCHAR osm_housenumber,
            NULL::VARCHAR osm_housename, NULL::VARCHAR osm_unit, NULL::VARCHAR osm_addr_street,
            NULL::VARCHAR osm_street_name, NULL::VARCHAR osm_addr_postcode, NULL::VARCHAR osm_m,
            NULL::VARCHAR osm_conf, NULL::BOOLEAN osm_bl WHERE false""")
    if ppd_p:
        pc_ = _cols(con, ppd_p)
        con.execute(f"""CREATE TEMP TABLE ps AS SELECT uprn,
            {_sel(pc_, "s", "sales_count", "BIGINT")}, {_sel(pc_, "s", "last_price", "BIGINT")},
            {_sel(pc_, "s", "last_sale_date", "DATE")}, {_sel(pc_, "s", "first_sale_date", "DATE")}
            FROM read_parquet({_q(ppd_p)}) s""")
    else:
        con.execute("""CREATE TEMP TABLE ps AS SELECT NULL::BIGINT uprn, NULL::BIGINT sales_count,
            NULL::BIGINT last_price, NULL::DATE last_sale_date, NULL::DATE first_sale_date WHERE false""")
    osm_line = (
        "array_to_string(list_filter([CASE WHEN os.osm_bl THEN NULL ELSE nullif(trim(os.osm_unit), '') END, "
        "nullif(trim(os.osm_housename), ''), "
        "nullif(trim(os.osm_housenumber), '') || ' ' || coalesce(nullif(trim(coalesce(os.osm_addr_street, "
        "r.street_name)), ''), '')], lambda v: v IS NOT NULL), ', ')"
    )
    # house number + street read naturally as one chunk; strip any trailing blank from a missing street
    osm_full = (
        f"trim(array_to_string(list_filter([{osm_line}, nullif(trim(r.locality), ''), "
        "nullif(trim(coalesce(os.osm_addr_postcode, r.postcode, ad.pc_ad)), '')], "
        "lambda v: v IS NOT NULL AND v <> ''), ', '))"
    )
    open_line = (
        "array_to_string(list_filter([nullif(trim(r.street_name), ''), nullif(trim(r.locality), ''), "
        "nullif(trim(coalesce(r.postcode, ad.pc_ad)), '')], lambda v: v IS NOT NULL), ', ')"
    )
    good = "os.osm_conf IN ('high', 'medium')"
    con.execute(f"""CREATE TEMP TABLE rl AS SELECT r.* EXCLUDE (postcode),
        coalesce(r.postcode, ad.pc_ad) AS postcode,
        CASE WHEN ad.epc_line IS NOT NULL THEN 'epc'
             WHEN ad.ppd_line IS NOT NULL THEN 'ppd'
             WHEN {good} THEN 'osm_' || os.osm_conf
             WHEN {open_line} <> '' THEN 'open_partial' END AS address_source,
        CASE WHEN ad.epc_line IS NOT NULL THEN 'EPC-restricted'
             WHEN ad.ppd_line IS NOT NULL THEN 'PPD-restricted' WHEN {good} THEN 'ODbL-1.0'
             WHEN {open_line} <> '' THEN 'OGL' END AS address_licence,
        CASE WHEN ad.epc_line IS NOT NULL THEN ad.epc_line
             WHEN ad.ppd_line IS NOT NULL THEN ad.ppd_line
             WHEN {good} THEN {osm_full}
             WHEN {open_line} <> '' THEN {open_line} END AS single_line_address,
        CASE WHEN ad.epc_line IS NULL AND ad.ppd_line IS NULL AND {good} THEN os.osm_m END AS address_osm_method,
        CASE WHEN ad.epc_line IS NULL AND ad.ppd_line IS NULL AND {good}
             THEN coalesce(os.osm_bl, false) END AS address_building_level,
        CASE WHEN ad.epc_line IS NULL AND ad.ppd_line IS NULL AND NOT coalesce({good}, false) AND os.osm_conf = 'low'
             THEN {osm_full} END AS osm_address_candidate,
        CASE WHEN ad.epc_line IS NULL AND ad.ppd_line IS NULL AND NOT coalesce({good}, false) AND os.osm_conf = 'low'
             THEN os.osm_conf END AS osm_address_candidate_confidence,
        CASE WHEN ad.epc_line IS NULL AND ad.ppd_line IS NULL AND NOT coalesce({good}, false) AND os.osm_conf = 'low'
             THEN os.osm_m END AS osm_address_candidate_method,
        ps.sales_count AS ppd_sales_count, ps.last_price AS ppd_last_price,
        ps.last_sale_date AS ppd_last_sale_date, ps.first_sale_date AS ppd_first_sale_date
        FROM r LEFT JOIN ad USING (uprn) LEFT JOIN os USING (uprn) LEFT JOIN ps USING (uprn)""")
    con.execute(
        "UPDATE rl SET quality = quality || '; outline from OSM (local only)' "
        "WHERE best_outline_source_local = 'osm_building'"
    )
    con.execute(
        "UPDATE rl SET quality = quality || '; address from HMLR Price Paid Data (restricted)' "
        "WHERE address_source = 'ppd'"
    )
    con.execute(
        "UPDATE rl SET quality = quality || '; address from OSM (medium confidence)' "
        "WHERE address_source = 'osm_medium'"
    )
    con.execute(
        "UPDATE rl SET quality = quality || '; address from OSM (high confidence)' WHERE address_source = 'osm_high'"
    )
    con.execute(
        "UPDATE rl SET quality = quality || '; address is building-level, not unit-level' WHERE address_building_level"
    )
    con.execute(
        "UPDATE rl SET quality = quality || '; OSM address candidate (low confidence) only' "
        "WHERE osm_address_candidate IS NOT NULL"
    )
    con.execute("UPDATE rl SET quality = quality || '; no full address' WHERE address_source = 'open_partial'")
    con.execute("UPDATE rl SET quality = quality || '; no address' WHERE address_source IS NULL")
    out_local = d / LOCAL_NAME
    local_sel = ", ".join(
        [
            "uprn",
            "x",
            "y",
            "lat",
            "lon",
            geom,
            *rest,
            "best_outline_id_local",
            "best_outline_source_local",
            "best_outline_licence_local",
            "outline_uprn_count_local",
            "outline_confidence_local",
            "postcode",
            "postcode_flag",
            "address_source",
            "address_licence",
            "single_line_address",
            "address_osm_method",
            "address_building_level",
            "osm_address_candidate",
            "osm_address_candidate_confidence",
            "osm_address_candidate_method",
            "ppd_sales_count",
            "ppd_last_price",
            "ppd_last_sale_date",
            "ppd_first_sale_date",
        ]
    )
    write(f"SELECT {local_sel} FROM rl ORDER BY {order}", out_local)
    write_text_atomic(d / "resolved_local.LICENCE.txt", LOCAL_LICENCE_TEXT)

    def dist(col: str) -> dict:
        return dict(
            con.execute(f"SELECT coalesce({col}, 'none'), count(*) FROM rl GROUP BY 1 ORDER BY 2 DESC").fetchall()
        )

    stats = {
        "lad": lad,
        "uprns": con.execute("SELECT count(*) FROM rl").fetchall()[0][0],
        "with_epc_lmk_key": con.execute("SELECT count(*) FROM rl WHERE epc_lmk_key IS NOT NULL").fetchall()[0][0],
        "outline_confidence": dist("outline_confidence"),
        "best_outline_source": dist("best_outline_source"),
        "outline_confidence_local": dist("outline_confidence_local"),
        "best_outline_source_local": dist("best_outline_source_local"),
        "address_source": dist("address_source"),
        "missing_inputs": missing,
        "building_counts": "national" if use_nat else "lad_local",
        "include_postcode": include_postcode,
        "open_bytes": out_open.stat().st_size,
        "local_bytes": out_local.stat().st_size,
        "seconds": round(time.time() - t0, 2),
    }
    con.close()
    write_text_atomic(d / MARKER, json.dumps(stats, indent=2))  # completion marker: always last
    return stats


INPUTS = (
    "uprn.parquet",
    "provenance.parquet",
    "epc.parquet",
    "poi.parquet",
    "address_local.parquet",
    "ppd_local_summary.parquet",
    "streets_odbl.parquet",
    "outline_ms.parquet",
    "outline_odbl.parquet",
)


def is_current(d: Path) -> bool:
    """Both products and the marker exist and none of the inputs (``uprn.parquet`` and the optional sources) is newer
    than the marker, which is written last (so it is the newest output)."""
    d = Path(d)
    if not all((d / n).exists() for n in (OPEN_NAME, LOCAL_NAME, MARKER)):
        return False
    done = (d / MARKER).stat().st_mtime_ns
    return all((d / n).stat().st_mtime_ns <= done for n in INPUTS if (d / n).exists())


def resolve_all(
    out_dir: Path, lads: list[str] | None = None, include_postcode: bool = False, skip_existing: bool = True, **kw
) -> dict:
    """Resolve every LAD having ``uprn.parquet`` (resumable: skips LADs whose products and resolved.stats.json are
    newer than ``uprn.parquet`` and the other inputs, see ``is_current``)."""
    out_dir = Path(out_dir)
    res: dict = {"done": [], "skipped": [], "failed": {}}
    avail = sorted(p.parent.name.split("=", 1)[1] for p in out_dir.glob("lad=*/uprn.parquet"))
    for lad in lads or avail:
        d = out_dir / f"lad={lad}"
        if lad not in avail:
            res["failed"][lad] = "no uprn.parquet"
            continue
        if skip_existing and is_current(d):
            res["skipped"].append(lad)
            continue
        try:
            resolve_lad(lad, out_dir, include_postcode, **kw)
            res["done"].append(lad)
        except Exception as e:  # keep going over a national run
            log.warning("resolve %s failed: %s", lad, e)
            res["failed"][lad] = str(e)
    return res


SQL_PATH = Path(__file__).parent / "sql" / "resolver.sql"


def create_views(con: duckdb.DuckDBPyConnection, out_dir: Path, local: bool = True) -> None:
    """Create ``resolved`` (open) and, if ``local``, ``resolved_local`` views over the hive-partitioned outputs."""
    out = str(Path(out_dir)).replace("'", "''")
    sql = SQL_PATH.read_text().replace("{OUT_DIR}", out)
    con.execute("INSTALL spatial; LOAD spatial;")
    for stmt in sql.split(";\n"):
        s = "\n".join(ln for ln in stmt.splitlines() if not ln.strip().startswith("--")).strip()
        if not s:
            continue
        if not local and "resolved_local" in s:
            continue
        con.execute(s)


def lookup(uprn: int, out_dir: Path = Path("data/out"), local: bool = False) -> dict | None:
    """Return the resolved row for one UPRN as a dict (open by default; ``local=True`` reads resolved_local)."""
    name = LOCAL_NAME if local else OPEN_NAME
    con = duckdb.connect()
    try:
        files = sorted(Path(out_dir).glob(f"lad=*/{name}"))
        if not files:
            return None
        cur = con.execute(
            f"SELECT * EXCLUDE (geometry) FROM read_parquet([{', '.join(_q(f) for f in files)}], "
            "union_by_name=true) WHERE uprn = ? LIMIT 1",
            [int(uprn)],
        )
        row = cur.fetchall()
        if not row:
            return None
        return dict(zip([c[0] for c in cur.description], row[0], strict=True))
    finally:
        con.close()
