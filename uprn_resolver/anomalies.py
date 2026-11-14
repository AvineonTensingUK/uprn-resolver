"""Upstream data-anomaly evidence pack: set-based DuckDB checks over the national Parquet inputs.

``run(parquet_dir, out_dir)`` evaluates each anomaly in ``SPECS``, writes one CSV per anomaly to ``out_dir``
(identifiers and numeric measures only: UPRN / TOID / USRN / INSPIRE ids, LAD codes, distances; never address text
or postcodes) and ``out_dir/summary.json``. ``render_markdown`` turns the summary into the report published to
``docs/upstream-anomalies.md``.

EPC address, postcode and posttown columns are used only inside DuckDB (to look up a Code-Point centroid) and are
never selected into an output table. Anomalies whose input files are absent are recorded as ``skipped``.

CLI: ``uv run python -m uprn_resolver.anomalies [--parquet-dir data/parquet] [--out-dir data/anomalies]``.
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from uprn_resolver.convert import _load_spatial
from uprn_resolver.convert import _q as _lit

LIDS_TOID_FAR_M = 100.0
USRN_FAR_M = 250.0
EPC_PC_FAR_M = 5000.0
CELL_M = 10_000
ISOLATION_CELLS = 2  # +-2 cells of 10 km: nothing else of the same LAD within >= 10 km
ISOLATION_MAX_UPRNS = 3
N_EXAMPLES = 5

PC_NORM = "regexp_replace(upper(replace(trim({c}), ' ', '')), '^(.+)(.{{3}})$', '\\1 \\2')"


def _pc(col: str) -> str:
    return PC_NORM.format(c=col)


@dataclass
class Spec:
    id: str
    publisher: str  # key into PUBLISHERS
    title: str
    base_desc: str
    severity: str
    method: str
    fix: str
    inputs: tuple[str, ...]  # glob patterns relative to parquet_dir that must exist
    id_col: str = "uprn"
    order_by: str | None = None  # SQL ORDER BY for picking examples (default: id_col)
    csv: bool = True
    extra_sql: dict[str, str] = field(default_factory=dict)  # name -> SQL returning one number from table ``a``
    note: str = ""


def _rel(parquet_dir: Path, pattern: str) -> list[Path]:
    return sorted(parquet_dir.glob(pattern))


def _view(con, name: str, parquet_dir: Path, pattern: str | list[str], union: bool = False) -> None:
    pats = [pattern] if isinstance(pattern, str) else pattern
    p = "[" + ", ".join(_lit(parquet_dir / x) for x in pats) + "]"
    opt = ", union_by_name=true" if union else ""
    con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet({p}, hive_partitioning=false{opt})")


# --------------------------------------------------------------------------------------------------------------
# Anomaly builders. Each creates TEMP TABLE ``a`` (the offending rows) and returns the base count (the denominator).
# --------------------------------------------------------------------------------------------------------------


def _b_onsud_not_open(con, pq) -> int:
    con.execute("CREATE TEMP TABLE a AS SELECT o.uprn, o.lad FROM onsud o ANTI JOIN open_uprn u ON u.uprn = o.uprn")
    return con.execute("SELECT count(*) FROM onsud").fetchone()[0]


def _b_open_not_onsud(con, pq) -> int:
    con.execute("CREATE TEMP TABLE a AS SELECT u.uprn FROM open_uprn u ANTI JOIN onsud o ON o.uprn = u.uprn")
    return con.execute("SELECT count(*) FROM open_uprn").fetchone()[0]


def _b_no_coords(con, pq) -> int:
    con.execute("""CREATE TEMP TABLE a AS
        SELECT u.uprn, o.lad FROM open_uprn u LEFT JOIN onsud o ON o.uprn = u.uprn
        WHERE u.x IS NULL OR u.y IS NULL OR u.lat IS NULL OR u.lon IS NULL OR (u.x = 0 AND u.y = 0)""")
    return con.execute("SELECT count(*) FROM open_uprn").fetchone()[0]


def _b_far_from_lad(con, pq) -> int:
    con.execute("""CREATE TEMP TABLE pts AS
        SELECT o.uprn, o.lad, u.x, u.y FROM onsud o JOIN open_uprn u ON u.uprn = o.uprn
        WHERE u.x IS NOT NULL AND u.y IS NOT NULL AND o.lad IS NOT NULL AND o.lad <> ''""")
    con.execute(f"""CREATE TEMP TABLE cells AS
        SELECT lad, CAST(floor(x / {CELL_M}) AS INTEGER) AS cx, CAST(floor(y / {CELL_M}) AS INTEGER) AS cy, count(*) AS n
        FROM pts GROUP BY ALL""")
    con.execute(f"""CREATE TEMP TABLE iso AS
        SELECT c.lad, c.cx, c.cy, sum(d.n) AS tot
        FROM cells c JOIN cells d ON d.lad = c.lad AND abs(d.cx - c.cx) <= {ISOLATION_CELLS} AND abs(d.cy - c.cy) <= {ISOLATION_CELLS}
        GROUP BY ALL HAVING sum(d.n) <= {ISOLATION_MAX_UPRNS}""")
    con.execute("CREATE TEMP TABLE med AS SELECT lad, median(x) AS mx, median(y) AS my FROM pts GROUP BY lad")
    con.execute(f"""CREATE TEMP TABLE a AS
        SELECT p.uprn, p.lad, CAST(i.tot AS INTEGER) AS lad_uprns_within_20km_window,
               round(sqrt((p.x - m.mx) ^ 2 + (p.y - m.my) ^ 2)) AS dist_to_lad_median_m
        FROM pts p
        JOIN iso i ON i.lad = p.lad AND i.cx = CAST(floor(p.x / {CELL_M}) AS INTEGER) AND i.cy = CAST(floor(p.y / {CELL_M}) AS INTEGER)
        JOIN med m ON m.lad = p.lad""")
    return con.execute("SELECT count(*) FROM pts").fetchone()[0]


def _b_other_territory(con, pq) -> int:
    """UPRNs that are the only one of their ONSUD LAD within +-2 km while 100+ UPRNs of other LADs surround them."""
    con.execute("""CREATE TEMP TABLE pts AS
        SELECT o.uprn, o.lad, CAST(floor(u.x / 1000) AS INTEGER) AS cx, CAST(floor(u.y / 1000) AS INTEGER) AS cy
        FROM onsud o JOIN open_uprn u ON u.uprn = o.uprn
        WHERE u.x IS NOT NULL AND u.y IS NOT NULL AND o.lad IS NOT NULL AND o.lad <> ''""")
    con.execute("CREATE TEMP TABLE cells AS SELECT lad, cx, cy, count(*) AS n FROM pts GROUP BY ALL")
    con.execute("CREATE TEMP TABLE allc AS SELECT cx, cy, sum(n) AS n FROM cells GROUP BY ALL")
    con.execute("""CREATE TEMP TABLE tot AS
        SELECT a.cx, a.cy, sum(b.n) AS tot FROM allc a JOIN allc b ON abs(a.cx - b.cx) <= 2 AND abs(a.cy - b.cy) <= 2
        GROUP BY ALL""")
    con.execute("""CREATE TEMP TABLE own AS
        SELECT a.lad, a.cx, a.cy, sum(b.n) AS own FROM cells a JOIN cells b
          ON b.lad = a.lad AND abs(a.cx - b.cx) <= 2 AND abs(a.cy - b.cy) <= 2 GROUP BY ALL""")
    con.execute("""CREATE TEMP TABLE a AS
        SELECT p.uprn, p.lad, CAST(t.tot AS INTEGER) AS uprns_within_2km_window, CAST(w.own AS INTEGER) AS own_lad_uprns_in_window
        FROM pts p JOIN own w ON w.lad = p.lad AND w.cx = p.cx AND w.cy = p.cy
        JOIN tot t ON t.cx = p.cx AND t.cy = p.cy
        WHERE w.own = 1 AND t.tot >= 100""")
    return con.execute("SELECT count(*) FROM pts").fetchone()[0]


def _b_lids_toid_far(con, pq) -> int:
    con.execute("CREATE TEMP TABLE lt AS SELECT DISTINCT toid, toid_x, toid_y FROM open_toid")
    con.execute(f"""CREATE TEMP TABLE a AS
        SELECT l.uprn, l.toid, round(sqrt((u.x - t.toid_x) ^ 2 + (u.y - t.toid_y) ^ 2), 1) AS dist_m
        FROM lids l JOIN open_uprn u ON u.uprn = l.uprn JOIN lt t ON t.toid = l.toid
        WHERE u.x IS NOT NULL AND t.toid_x IS NOT NULL
          AND (u.x - t.toid_x) ^ 2 + (u.y - t.toid_y) ^ 2 > {LIDS_TOID_FAR_M**2}""")
    return con.execute("""SELECT count(*) FROM lids l JOIN open_uprn u ON u.uprn = l.uprn JOIN lt t ON t.toid = l.toid
        WHERE u.x IS NOT NULL AND t.toid_x IS NOT NULL""").fetchone()[0]


def _b_uprn_far_street(con, pq) -> int:
    _load_spatial(con)
    con.execute("""CREATE TEMP TABLE ug AS
        SELECT usrn, ST_GeomFromWKB(geom_wkb) AS g, ST_XMin(ST_GeomFromWKB(geom_wkb)) AS x0,
               ST_YMin(ST_GeomFromWKB(geom_wkb)) AS y0, ST_XMax(ST_GeomFromWKB(geom_wkb)) AS x1, ST_YMax(ST_GeomFromWKB(geom_wkb)) AS y1
        FROM open_usrn WHERE geom_wkb IS NOT NULL""")
    con.execute("""CREATE TEMP TABLE links AS
        SELECT s.uprn, s.usrn, u.x, u.y FROM uprn_usrn s JOIN open_uprn u ON u.uprn = s.uprn
        WHERE u.x IS NOT NULL AND u.y IS NOT NULL AND s.usrn IS NOT NULL""")
    # bbox lower bound first (cheap); exact point-to-line distance only for links inside the expanded bbox,
    # then the exact distance is recomputed for the (few) survivors so the reported value is never a sentinel
    con.execute(f"""CREATE TEMP TABLE cand AS
        SELECT k.uprn, k.usrn, k.x, k.y
        FROM links k JOIN ug g ON g.usrn = k.usrn
        WHERE k.x < g.x0 - {USRN_FAR_M} OR k.x > g.x1 + {USRN_FAR_M} OR k.y < g.y0 - {USRN_FAR_M} OR k.y > g.y1 + {USRN_FAR_M}
           OR ST_Distance(ST_Point(k.x, k.y), g.g) > {USRN_FAR_M}""")
    con.execute("""CREATE TEMP TABLE a AS
        SELECT c.uprn, c.usrn, round(ST_Distance(ST_Point(c.x, c.y), g.g), 1) AS dist_m
        FROM cand c JOIN ug g ON g.usrn = c.usrn""")
    return con.execute("SELECT count(*) FROM links k SEMI JOIN ug g ON g.usrn = k.usrn").fetchone()[0]


def _b_usrn_missing(con, pq) -> int:
    con.execute("""CREATE TEMP TABLE a AS
        SELECT usrn, count(*) FILTER (WHERE src = 'uprn') AS uprn_links, count(*) FILTER (WHERE src = 'road') AS road_links
        FROM (SELECT usrn, 'uprn' AS src FROM uprn_usrn WHERE usrn IS NOT NULL
              UNION ALL SELECT usrn, 'road' FROM road_usrn WHERE usrn IS NOT NULL) l
        ANTI JOIN open_usrn g ON g.usrn = l.usrn GROUP BY usrn""")
    return con.execute("""SELECT count(DISTINCT usrn) FROM (SELECT usrn FROM uprn_usrn UNION ALL SELECT usrn FROM road_usrn)
        WHERE usrn IS NOT NULL""").fetchone()[0]


def _b_road_toid_no_name(con, pq) -> int:
    has_roads = "open_roads" in pq
    named = "r.toid IS NOT NULL" if has_roads else "false"
    join = (
        "LEFT JOIN (SELECT DISTINCT toid FROM open_roads WHERE name_1 IS NOT NULL AND name_1 <> '') r ON r.toid = d.toid"
        if has_roads
        else ""
    )
    con.execute(f"""CREATE TEMP TABLE a AS
        SELECT d.toid, d.usrn_links, {named} AS named_in_open_roads
        FROM (SELECT toid, count(*) AS usrn_links FROM road_usrn WHERE toid IS NOT NULL GROUP BY toid) d
        {join}
        WHERE d.toid NOT IN (SELECT toid FROM open_names WHERE toid IS NOT NULL)""")
    return con.execute("SELECT count(DISTINCT toid) FROM road_usrn WHERE toid IS NOT NULL").fetchone()[0]


def _b_lids_toid_missing(con, pq) -> int:
    con.execute("""CREATE TEMP TABLE miss AS
        SELECT l.toid, count(*) AS uprns, min(l.uprn) AS example_uprn
        FROM lids l ANTI JOIN open_toid t ON t.toid = l.toid WHERE l.toid IS NOT NULL GROUP BY l.toid""")
    con.execute("CREATE TEMP TABLE a AS SELECT toid, uprns, example_uprn FROM miss")
    return con.execute("SELECT count(DISTINCT toid) FROM lids WHERE toid IS NOT NULL").fetchone()[0]


def _b_hmlr_uprn(con, pq) -> int:
    con.execute("""CREATE TEMP TABLE a AS
        SELECT h.transaction_id, TRY_CAST(h.value AS BIGINT) AS uprn FROM hmlr_uprn h
        ANTI JOIN onsud o ON o.uprn = TRY_CAST(h.value AS BIGINT)
        WHERE TRY_CAST(h.value AS BIGINT) IS NOT NULL""")
    return con.execute("SELECT count(*) FROM hmlr_uprn WHERE TRY_CAST(value AS BIGINT) IS NOT NULL").fetchone()[0]


def _b_hmlr_inspire(con, pq) -> int:
    con.execute("""CREATE TEMP TABLE a AS
        SELECT h.transaction_id, h.value AS inspire_id FROM hmlr_inspire h
        ANTI JOIN (SELECT DISTINCT inspire_id FROM inspire) p ON p.inspire_id = h.value""")
    return con.execute("SELECT count(*) FROM hmlr_inspire").fetchone()[0]


def _epc_union(pq) -> str:
    parts = []
    if "epc_ew" in pq:
        parts.append("SELECT TRY_CAST(trim(uprn) AS BIGINT) AS uprn, 'E&W' AS src FROM epc_ew")
    if "epc_scot" in pq:
        parts.append("SELECT TRY_CAST(uprn AS BIGINT) AS uprn, 'Scotland' AS src FROM epc_scot")
    return " UNION ALL ".join(parts)


def _b_epc_not_onsud(con, pq) -> int:
    con.execute(f"""CREATE TEMP TABLE e AS
        SELECT uprn, string_agg(DISTINCT src, '+') AS src FROM ({_epc_union(pq)}) WHERE uprn IS NOT NULL AND uprn > 0 GROUP BY uprn""")
    con.execute("CREATE TEMP TABLE a AS SELECT e.uprn, e.src FROM e ANTI JOIN onsud o ON o.uprn = e.uprn")
    return con.execute("SELECT count(*) FROM e").fetchone()[0]


def _b_epc_pc_far(con, pq) -> int:
    # pairs (uprn, normalised postcode) are built and compared inside DuckDB; only uprn + distance are kept
    con.execute(f"""CREATE TEMP TABLE pairs AS
        SELECT DISTINCT uprn, pc FROM (
            SELECT TRY_CAST(trim(uprn) AS BIGINT) AS uprn, {_pc("postcode")} AS pc FROM epc_ew) WHERE uprn > 0 AND pc IS NOT NULL""")
    con.execute(f"""CREATE TEMP TABLE d AS
        SELECT p.uprn, sqrt((u.x - c.x) ^ 2 + (u.y - c.y) ^ 2) AS dist
        FROM pairs p JOIN open_uprn u ON u.uprn = p.uprn
        JOIN (SELECT {_pc("postcode")} AS pc, x, y FROM codepoint WHERE x IS NOT NULL) c ON c.pc = p.pc
        WHERE u.x IS NOT NULL""")
    con.execute(f"""CREATE TEMP TABLE a AS
        SELECT uprn, round(max(dist)) AS dist_m, count(*) AS far_postcodes FROM d WHERE dist > {EPC_PC_FAR_M} GROUP BY uprn
        HAVING min(dist) > {EPC_PC_FAR_M}""")
    con.execute("DROP TABLE pairs")
    return con.execute("SELECT count(DISTINCT uprn) FROM d").fetchone()[0]


def _b_pc_not_codepoint(con, pq) -> int:
    con.execute(f"""CREATE TEMP TABLE a AS
        SELECT o.pc AS pc_key, count(*) AS uprns FROM (SELECT uprn, {_pc("postcode")} AS pc FROM onsud WHERE postcode IS NOT NULL AND trim(postcode) <> '') o
        ANTI JOIN (SELECT DISTINCT {_pc("postcode")} AS pc FROM codepoint) c ON c.pc = o.pc GROUP BY o.pc""")
    return con.execute(
        "SELECT count(DISTINCT postcode) FROM onsud WHERE postcode IS NOT NULL AND trim(postcode) <> ''"
    ).fetchone()[0]


# (builder, views) ; views map name -> glob under parquet_dir
_V = {
    "onsud": "onsud/onsud.parquet",
    "open_uprn": "open_uprn/open_uprn.parquet",
    "lids": "lids/lids.parquet",
    "open_toid": "open_toid/*.parquet",
    "open_usrn": "open_usrn/open_usrn.parquet",
    "uprn_usrn": "streets/uprn_usrn.parquet",
    "road_usrn": "streets/road_usrn.parquet",
    "open_names": "streets/open_names.parquet",
    "open_roads": "open_roads/open_roads.parquet",
    "hmlr_uprn": "hmlr/pp-uprn-lookup-*.parquet",
    "hmlr_inspire": "hmlr/pp-inspire-id-lookup-*.parquet",
    "inspire": "inspire/*.parquet",
    "codepoint": "codepoint/codepoint.parquet",
    "epc_ew": ["epc_ew/domestic/certificates-*.parquet", "epc_ew/non_domestic/certificates-*.parquet"],
    "epc_scot": "epc/s_*.parquet",
}

SPECS: list[tuple[Spec, object]] = [
    (
        Spec(
            "onsud_uprn_not_in_open_uprn",
            "ons",
            "UPRNs in ONSUD that have no point in OS Open UPRN",
            "ONSUD UPRNs",
            "Medium",
            "Anti-join ONSUD.uprn against Open UPRN.uprn (same release window).",
            "Publish the ONSUD UPRN set and Open UPRN from the same AddressBase epoch, or document which UPRNs (e.g. non-geographic, historic) ONSUD carries that Open UPRN omits.",
            ("onsud/onsud.parquet", "open_uprn/open_uprn.parquet"),
            order_by="uprn",
        ),
        _b_onsud_not_open,
    ),
    (
        Spec(
            "open_uprn_not_in_onsud",
            "os",
            "UPRNs in OS Open UPRN that are absent from ONSUD",
            "Open UPRN UPRNs",
            "Medium",
            "Anti-join Open UPRN.uprn against ONSUD.uprn.",
            "Align the AddressBase extract date of Open UPRN and ONSUD, or flag in the ONSUD release notes the UPRNs that postdate its cut-off.",
            ("onsud/onsud.parquet", "open_uprn/open_uprn.parquet"),
        ),
        _b_open_not_onsud,
    ),
    (
        Spec(
            "open_uprn_no_coordinates",
            "os",
            "OS Open UPRN rows with no usable coordinates",
            "Open UPRN rows",
            "Medium",
            "Open UPRN rows where x, y, lat or lon is NULL, or (x, y) = (0, 0).",
            "Drop such rows from the CSV or state in the product guide that they exist; where the coordinate is known to GeoPlace, supply it.",
            ("open_uprn/open_uprn.parquet", "onsud/onsud.parquet"),
        ),
        _b_no_coords,
    ),
    (
        Spec(
            "uprn_far_from_onsud_lad",
            "ons",
            "UPRN point isolated from every other UPRN of its ONSUD local authority",
            "UPRNs with a point and an ONSUD LAD",
            "High",
            f"For each ONSUD LAD, bin Open UPRN points into {CELL_M // 1000} km cells; flag UPRNs whose cell and the surrounding "
            f"+-{ISOLATION_CELLS} cells hold at most {ISOLATION_MAX_UPRNS} UPRNs of that LAD, i.e. nothing else of the LAD within ~10 km. "
            "Catches a point near Cambridge for a Torridge UPRN. Remote islands and bothies can be genuine; check the distance to the LAD median.",
            "Either the ONSUD LAD or the Open UPRN coordinate is wrong for these UPRNs. Re-derive the ONSUD geography from the Open UPRN point (ONSUD is spatially joined from it) or correct the coordinate at source (GeoPlace/local authority).",
            ("onsud/onsud.parquet", "open_uprn/open_uprn.parquet"),
            order_by="dist_to_lad_median_m DESC",
            extra_sql={
                "over_100km_from_lad_median": "SELECT count(*) FROM a WHERE dist_to_lad_median_m > 100000",
                "over_300km_from_lad_median": "SELECT count(*) FROM a WHERE dist_to_lad_median_m > 300000",
            },
        ),
        _b_far_from_lad,
    ),
    (
        Spec(
            "uprn_inside_other_lad_territory",
            "ons",
            "UPRN that is the only one of its ONSUD LAD within 2 km but sits among 100+ UPRNs of other LADs",
            "UPRNs with a point and an ONSUD LAD",
            "High",
            "Bin Open UPRN points into 1 km cells; flag a UPRN when no other UPRN of its ONSUD LAD lies in the surrounding +-2 cell window while the window holds at least 100 UPRNs of other LADs. A point deep inside another authority is a wrong LAD or a wrong coordinate (genuine exclaves are rare; boundary UPRNs are not flagged).",
            "Re-derive ONSUD geography from the Open UPRN point, or correct the coordinate at source; publish a point-in-LAD consistency check with the directory.",
            ("onsud/onsud.parquet", "open_uprn/open_uprn.parquet"),
            order_by="uprns_within_2km_window DESC",
        ),
        _b_other_territory,
    ),
    (
        Spec(
            "lids_topo_toid_far_from_uprn",
            "os",
            f"LIDS UPRN to TopographicArea TOID link where the Open TOID point is > {LIDS_TOID_FAR_M:.0f} m from the UPRN",
            "LIDS links with both points",
            "Medium",
            f"LIDS BLPU_UPRN_TopographicArea_TOID_5 joined to Open UPRN (point) and Open TOID (point guaranteed inside the MasterMap feature); Euclidean distance > {LIDS_TOID_FAR_M:.0f} m. Large features (parks, estates) legitimately exceed 100 m; the >1 km and >10 km tiers are the real errors.",
            "Re-check the UPRN to TOID association for the >1 km tier (likely a wrong TOID or UPRN position); document the expected tolerance for large features in the LIDS guide.",
            ("lids/lids.parquet", "open_uprn/open_uprn.parquet", "open_toid/*.parquet"),
            id_col="uprn",
            order_by="dist_m DESC",
            extra_sql={
                "over_1km": "SELECT count(*) FROM a WHERE dist_m > 1000",
                "over_10km": "SELECT count(*) FROM a WHERE dist_m > 10000",
                "over_100km": "SELECT count(*) FROM a WHERE dist_m > 100000",
            },
        ),
        _b_lids_toid_far,
    ),
    (
        Spec(
            "uprn_far_from_usrn_street",
            "os",
            f"LIDS UPRN to USRN link where the UPRN is > {USRN_FAR_M:.0f} m from the Open USRN geometry",
            "UPRN-to-USRN links with a point and a street geometry",
            "Medium",
            f"LIDS BLPU_UPRN_Street_USRN_11 joined to Open UPRN points and Open USRN lines; distance from the point to the line > {USRN_FAR_M:.0f} m (bbox shortcut for the clearly far, exact ST_Distance otherwise).",
            "Review the UPRN to street association for the far links (many are the wrong USRN being the nearest named street rather than the access street); consider a published tolerance/quality flag on the link.",
            ("streets/uprn_usrn.parquet", "open_uprn/open_uprn.parquet", "open_usrn/open_usrn.parquet"),
            order_by="dist_m DESC",
            extra_sql={
                "over_1km": "SELECT count(*) FROM a WHERE dist_m > 1000",
                "over_10km": "SELECT count(*) FROM a WHERE dist_m > 10000",
            },
        ),
        _b_uprn_far_street,
    ),
    (
        Spec(
            "usrn_missing_from_open_usrn",
            "os",
            "USRNs referenced by LIDS that are missing from OS Open USRN",
            "distinct USRNs in LIDS street links",
            "Medium",
            "Anti-join the USRNs of BLPU_UPRN_Street_USRN_11 and Road_TOID_Street_USRN_10 against Open USRN.usrn; counts of affected links kept.",
            "Publish Open USRN and LIDS from the same epoch, or state which USRN types (e.g. private, unadopted, Scottish) Open USRN omits.",
            ("streets/uprn_usrn.parquet", "streets/road_usrn.parquet", "open_usrn/open_usrn.parquet"),
            id_col="usrn",
            order_by="uprn_links DESC",
            extra_sql={"uprn_links_affected": "SELECT coalesce(sum(uprn_links), 0) FROM a"},
        ),
        _b_usrn_missing,
    ),
    (
        Spec(
            "road_toid_missing_from_open_names",
            "os",
            "Road TOIDs in LIDS (Road_TOID_Street_USRN_10) with no OS Open Names entry",
            "distinct road TOIDs in LIDS",
            "Low",
            "Anti-join distinct Road_TOID_Street_USRN_10 road TOIDs against the Open Names Named Road TOID. `named_in_open_roads` marks those that carry a name in OS Open Roads, so the omission is not just an unnamed road.",
            "Include the named-in-Open-Roads cases in Open Names (or explain the omission rule); unnamed roads are expected to be absent.",
            ("streets/road_usrn.parquet", "streets/open_names.parquet"),
            id_col="toid",
            extra_sql={"named_in_open_roads": "SELECT count(*) FROM a WHERE named_in_open_roads"},
        ),
        _b_road_toid_no_name,
    ),
    (
        Spec(
            "lids_toid_missing_from_open_toid",
            "os",
            "TopographicArea TOIDs referenced by LIDS that are missing from OS Open TOID",
            "distinct TOIDs in LIDS BLPU_UPRN_TopographicArea_TOID_5",
            "High",
            "Anti-join LIDS.toid against Open TOID.toid (all 54 grid-square files).",
            "Add the missing TOIDs to Open TOID or note why they are excluded (e.g. TOIDs retired since the LIDS cut); a missing 100 km tile would show as a block.",
            ("lids/lids.parquet", "open_toid/*.parquet"),
            id_col="toid",
            order_by="uprns DESC",
            extra_sql={"uprns_affected": "SELECT coalesce(sum(uprns), 0) FROM a"},
        ),
        _b_lids_toid_missing,
    ),
    (
        Spec(
            "hmlr_pp_uprn_not_in_onsud",
            "hmlr",
            "Price Paid UPRN lookup UPRNs not present in ONSUD",
            "Price Paid UPRN lookup rows with a numeric UPRN",
            "Low",
            "Anti-join pp-uprn-lookup.value (cast to BIGINT) against ONSUD.uprn.",
            "Confirm the UPRN source epoch of the lookup and the ONSUD/AddressBase vintage; drop or flag UPRNs that are retired/non-geographic.",
            ("hmlr/pp-uprn-lookup-*.parquet", "onsud/onsud.parquet"),
            id_col="transaction_id",
        ),
        _b_hmlr_uprn,
    ),
    (
        Spec(
            "hmlr_pp_inspire_not_in_polygons",
            "hmlr",
            "Price Paid INSPIRE ID lookup values not found in the INSPIRE Index Polygons",
            "Price Paid INSPIRE lookup rows",
            "Medium",
            "Anti-join pp-inspire-id-lookup.value against the inspire_id of all INSPIRE Index Polygon council files.",
            "Release the lookup and the INSPIRE polygons from the same snapshot, or document that parcels are removed (title closures, merges) without back-filling the lookup.",
            ("hmlr/pp-inspire-id-lookup-*.parquet", "inspire/*.parquet"),
            id_col="transaction_id",
        ),
        _b_hmlr_inspire,
    ),
    (
        Spec(
            "epc_uprn_not_in_onsud",
            "epc",
            "EPC certificate UPRNs that do not exist in ONSUD",
            "distinct EPC UPRNs (E&W + Scotland)",
            "Medium",
            "Anti-join distinct (UPRN > 0) from EPC certificates (England & Wales domestic and non-domestic; Scotland) against ONSUD.uprn. No address field is read.",
            "Validate the lodged UPRN against AddressBase at lodgement (the register accepts free-text UPRNs); correct or null the invalid ones in the bulk data.",
            ("epc_ew/domestic/certificates-*.parquet",),
            order_by="uprn",
        ),
        _b_epc_not_onsud,
    ),
    (
        Spec(
            "epc_uprn_far_from_postcode_centroid",
            "epc",
            f"EPC records whose UPRN point is > {EPC_PC_FAR_M / 1000:.0f} km from the centroid of the EPC postcode",
            "distinct EPC (England & Wales) UPRNs with a point and a Code-Point postcode",
            "Medium",
            f"EPC E&W certificates give (UPRN, postcode); the postcode is normalised and looked up in Code-Point Open (centroid) and compared with the Open UPRN point entirely inside DuckDB. UPRNs whose every certificate postcode is > {EPC_PC_FAR_M / 1000:.0f} km away are kept (ids and distance only; no postcode leaves the database). ONSUD carries no coordinates, so Code-Point is the centroid source.",
            "Cross-check lodged UPRN against postcode at lodgement; these are almost certainly a mistyped UPRN or postcode.",
            ("epc_ew/domestic/certificates-*.parquet", "open_uprn/open_uprn.parquet", "codepoint/codepoint.parquet"),
            order_by="dist_m DESC",
            extra_sql={"over_50km": "SELECT count(*) FROM a WHERE dist_m > 50000"},
        ),
        _b_epc_pc_far,
    ),
    (
        Spec(
            "onsud_postcode_not_in_codepoint",
            "os",
            "ONSUD postcodes absent from Code-Point Open",
            "distinct ONSUD postcodes",
            "Low",
            "Normalise ONSUD.postcode and Code-Point Open postcode (upper, single space before the 3-character inward code) and anti-join. Count only: no CSV.",
            "Expected for postcodes created or terminated between the two releases; align release dates or document the lag.",
            ("onsud/onsud.parquet", "codepoint/codepoint.parquet"),
            id_col="pc_key",
            csv=False,
            extra_sql={"uprns_affected": "SELECT coalesce(sum(uprns), 0) FROM a"},
        ),
        _b_pc_not_codepoint,
    ),
]


def _note(to: str, items: str) -> str:
    return f"""To: {to}
Subject: Possible inconsistencies found when joining the open datasets (evidence attached)

Hello,

While building an open UPRN-to-building lookup for Great Britain we cross-checked the open releases against
each other and found the inconsistencies below. Each is reproducible with set-based joins on identifiers only;
the attached CSVs contain UPRN / TOID / USRN / INSPIRE ids and numeric measures, with no address text.

{items}

We are happy to share the exact queries (uprn_resolver/anomalies.py) and to re-run them against a corrected release.
Please tell us if any of these are known and intended.

Kind regards,
[name]"""


PUBLISHERS: dict[str, dict] = {
    "os": {
        "name": "Ordnance Survey (Open UPRN, Open USRN, Open TOID, Open Names, Linked Identifiers, Code-Point Open)",
        "channels": [
            "OS Data Hub errors-and-omissions reporting tool (needs a free OS Data Hub account): https://osdatahub.os.uk/apps/errors-and-omissions/reporting",
            "OS report-a-data-error page (also phone 03453 757535, Mon-Fri 8-6): https://www.ordnancesurvey.co.uk/contact-us/business-government/report-error",
            "Check known issues first: https://docs.os.uk/os-downloads/identifiers/os-open-uprn/os-open-uprn-known-data-issues and https://docs.os.uk/os-downloads/identifiers/os-open-usrn/os-open-usrn-known-data-issues",
        ],
        "note": _note(
            "OS Data Hub support (via the errors-and-omissions tool)",
            "- Open UPRN / ONSUD set differences and UPRNs without coordinates.\n- LIDS UPRN to TOID links where the Open TOID point is far from the UPRN.\n- LIDS UPRN to USRN links far from the Open USRN line; LIDS USRNs missing from Open USRN.\n- Road TOIDs in LIDS with no Open Names entry; ONSUD postcodes missing from Code-Point Open.",
        ),
    },
    "geoplace": {
        "name": "GeoPlace (custodian of the UPRN/USRN address and street gazetteers; upstream of OS Open UPRN, Open USRN and LIDS)",
        "channels": [
            "support@geoplace.co.uk (UPRN integration/support contact in GeoPlace's published checklist: https://static.geoplace.co.uk/downloads/UPRNIntegrationChecklist.pdf)",
            "Corrections to a UPRN position or street link are made by the owning local authority via GeoPlace's data-improvement process; send the id lists for the UPRN/USRN anomalies and ask for routing.",
        ],
        "cc": [
            "uprn_far_from_onsud_lad",
            "uprn_inside_other_lad_territory",
            "lids_topo_toid_far_from_uprn",
            "uprn_far_from_usrn_street",
            "usrn_missing_from_open_usrn",
        ],
        "note": _note(
            "GeoPlace support",
            "- UPRNs whose point lies far from their local authority (e.g. a Torridge UPRN positioned near Cambridge).\n- UPRN to USRN and UPRN to TOID links where the UPRN is far from the street / feature.\n- USRNs referenced by the links but absent from Open USRN.",
        ),
    },
    "ons": {
        "name": "Office for National Statistics (ONS UPRN Directory, ONSUD)",
        "channels": [
            "ons.geography@ons.gov.uk (ONS Geography enquiries address listed on each ONSUD release at data.gov.uk, e.g. https://www.data.gov.uk/dataset/dc27178b-62b0-4fac-a2dc-11959a999914/ons-uprn-directory-march-2024-user-guide)",
        ],
        "note": _note(
            "ONS Geography (ons.geography@ons.gov.uk)",
            "- UPRNs that are the only one of their ONSUD LAD within 2 km but sit among 100+ UPRNs of other LADs.\n- UPRNs isolated by 10+ km from the rest of their ONSUD LAD (e.g. 10093645993: ONSUD Torridge, point near Cambridge).\n- ONSUD vs Open UPRN set differences.",
        ),
    },
    "hmlr": {
        "name": "HM Land Registry (Price Paid UPRN / INSPIRE ID lookups, INSPIRE Index Polygons)",
        "channels": [
            "HMLR Data Services Team: data.services@mail.landregistry.gov.uk, 0300 006 0478 (Mon-Fri 9-5), contact form linked from https://www.gov.uk/government/statistical-data-sets/transaction-unique-identifier-and-inspire-id-look-up-table-dataset",
            "INSPIRE Index Polygons guidance: https://www.gov.uk/inspire-index-polygons-spatial-data",
        ],
        "note": _note(
            "HM Land Registry Data Services Team",
            "- Price Paid INSPIRE ID lookup values that are not present in the INSPIRE Index Polygons.\n- Price Paid UPRN lookup UPRNs that are not in the ONS UPRN Directory.",
        ),
    },
    "epc": {
        "name": "MHCLG (Energy Performance of Buildings Register)",
        "channels": [
            "mhclg.digital-services@communities.gov.uk (enquiries about the data; use subject prefix 'EPB - '), service page https://get-energy-performance-data.communities.gov.uk/",
            "Service feedback form linked from the same page.",
        ],
        "note": _note(
            "MHCLG EPB digital services (subject: EPB - UPRN data quality)",
            "- EPC certificates whose UPRN does not exist in the ONS UPRN Directory.\n- Certificates whose UPRN point is more than 5 km from the centroid of the lodged postcode.",
        ),
    },
}


# --------------------------------------------------------------------------------------------------------------


def _open(parquet_dir: Path, memory_limit: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    tmp = parquet_dir.parent / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory={_lit(tmp)}")
    con.execute(f"SET memory_limit={_lit(memory_limit)}")
    con.execute("SET preserve_insertion_order=false")
    return con


def _check_dir(parquet_dir: Path, spec: Spec) -> list[str]:
    return [p for p in spec.inputs if not _rel(parquet_dir, p)]


def run(
    parquet_dir: Path,
    out_dir: Path,
    only: list[str] | None = None,
    memory_limit: str = "10GB",
) -> dict:
    """Evaluate every anomaly; write CSVs + ``summary.json`` into ``out_dir`` and return the summary dict."""
    parquet_dir, out_dir = Path(parquet_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    con = _open(parquet_dir, memory_limit)
    present: set[str] = set()
    for name, pat in _V.items():
        if all(_rel(parquet_dir, x) for x in ([pat] if isinstance(pat, str) else pat)):
            union = name in ("epc_ew", "epc_scot")
            _view(con, name, parquet_dir, pat, union=union)
            present.add(name)
    pq = present
    results = []
    for spec, builder in SPECS:
        if only and spec.id not in only:
            continue
        t0 = time.time()
        rec: dict = {
            "id": spec.id,
            "publisher": spec.publisher,
            "title": spec.title,
            "base_desc": spec.base_desc,
            "severity": spec.severity,
            "method": spec.method,
            "fix": spec.fix,
            "id_col": spec.id_col,
        }
        missing = _check_dir(parquet_dir, spec)
        if missing:
            rec.update(status="skipped", reason=f"missing inputs: {', '.join(missing)}")
            results.append(rec)
            continue
        con.execute("DROP TABLE IF EXISTS a")
        try:
            base = int(builder(con, pq))
            n = con.execute("SELECT count(*) FROM a").fetchone()[0]
            order = spec.order_by or spec.id_col
            ex = [
                r[0] for r in con.execute(f"SELECT {spec.id_col} FROM a ORDER BY {order} LIMIT {N_EXAMPLES}").fetchall()
            ]
            rec.update(
                status="ok",
                count=int(n),
                base=base,
                pct=(100.0 * n / base) if base else None,
                examples=[str(e) for e in ex] if spec.csv else [],
                columns=[r[0] for r in con.execute("DESCRIBE a").fetchall()],
            )
            extras = {}
            for k, sql in spec.extra_sql.items():
                v = con.execute(sql).fetchone()[0]
                extras[k] = int(v) if v is not None else None
            if extras:
                rec["extra"] = extras
            if spec.csv:
                path = out_dir / f"{spec.id}.csv"
                con.execute(f"COPY (SELECT * FROM a ORDER BY {spec.id_col}) TO {_lit(path)} (FORMAT csv, HEADER)")
                rec["csv"] = path.name
        except duckdb.Error as e:
            rec.update(status="error", reason=str(e)[:500])
        rec["elapsed_s"] = round(time.time() - t0, 1)
        results.append(rec)
        for t in (
            "a",
            "pts",
            "cells",
            "iso",
            "med",
            "allc",
            "tot",
            "own",
            "cand",
            "ug",
            "links",
            "lt",
            "miss",
            "e",
            "d",
        ):
            con.execute(f"DROP TABLE IF EXISTS {t}")
    con.close()
    summary = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "thresholds": {
            "lids_toid_far_m": LIDS_TOID_FAR_M,
            "usrn_far_m": USRN_FAR_M,
            "epc_postcode_far_m": EPC_PC_FAR_M,
            "lad_isolation_cell_m": CELL_M,
            "lad_isolation_cells": ISOLATION_CELLS,
        },
        "anomalies": results,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


def render_markdown(summary: dict, publishers: dict[str, dict]) -> str:
    """Report body (summary table per publisher + method/fix + covering note) from ``summary`` and publisher metadata."""
    lines = [
        "# Upstream data anomalies: evidence pack",
        "",
        f"Generated {summary['generated']} by `uv run python -m uprn_resolver.anomalies` over `data/parquet/*`. Per-anomaly CSVs",
        "(identifiers and numeric measures only) are in `data/anomalies/`. No address text or postcodes from restricted sources appear",
        "here or in the CSVs. Nothing in this pack has been sent to anyone.",
        "",
    ]
    lines += [
        "Caveats: counts are candidates for publisher review, not proven errors. `severity` is our judgement of downstream harm.",
        "The local Open TOID Parquet is filtered to TOIDs present in LIDS at conversion time, so `lids_toid_missing_from_open_toid`",
        "measures LIDS TOIDs absent from every Open TOID tile we hold (0 means none). Open Names covers named roads only. Code-Point",
        "is the only open source of postcode centroids (ONSUD has none). EPC and Price Paid ids are used as identifiers only.",
        "Scotland is excluded from the EPC postcode-distance check because the Scottish register carries no usable UPRN column in our data.",
        "",
        "## Headline",
        "",
        "| Anomaly | Count | % of base |",
        "|---|---:|---:|",
    ]
    for r in summary["anomalies"]:
        if r["status"] == "ok":
            lines.append(f"| {r['title']} | {r['count']:,} | {r['pct']:.4f}% |")
    lines.append("")
    by: dict[str, list[dict]] = {}
    for r in summary["anomalies"]:
        by.setdefault(r["publisher"], []).append(r)
    for key, pub in publishers.items():
        rows = by.get(key, []) or [r for r in summary["anomalies"] if r["id"] in pub.get("cc", [])]
        if not rows:
            continue
        lines += [f"## {pub['name']}", ""]
        if pub.get("cc"):
            lines += [
                "Also relevant (the anomalies below are listed under their publishing owner above; copy this body for routing).",
                "",
            ]
        lines += ["| Anomaly | Count | % of base | Severity | Example ids | CSV |", "|---|---:|---:|---|---|---|"]
        for r in rows:
            if r["status"] != "ok":
                lines.append(f"| {r['title']} | {r['status']} | | | {r.get('reason', '')} | |")
                continue
            pct = "" if r["pct"] is None else (f"{r['pct']:.4f}%" if r["pct"] < 0.01 else f"{r['pct']:.3f}%")
            ex = ", ".join(f"`{e}`" for e in r["examples"][:3])
            lines.append(
                f"| {r['title']} | {r['count']:,} | {pct} | {r['severity']} | {ex} | `{r.get('csv', 'count only')}` |"
            )
        lines.append("")
        for r in rows:
            if r["status"] != "ok":
                continue
            lines += [f"### {r['title']}", ""]
            lines.append(f"- Count: {r['count']:,} of {r['base']:,} {r['base_desc']}.")
            for k, v in (r.get("extra") or {}).items():
                lines.append(f"- {k.replace('_', ' ')}: {v:,}")
            lines += [
                f"- Examples: {', '.join('`' + e + '`' for e in r['examples'])}",
                f"- Method: {r['method']}",
                f"- Suggested fix: {r['fix']}",
                "",
            ]
        lines += ["### Where to report", ""]
        lines += [f"- {c}" for c in pub["channels"]] + [
            "",
            "### Covering note (draft, not sent)",
            "",
            "```",
            pub["note"].strip(),
            "```",
            "",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--parquet-dir", type=Path, default=Path("data/parquet"))
    ap.add_argument("--out-dir", type=Path, default=Path("data/anomalies"))
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--doc", type=Path, help="also write the markdown report here (from summary.json)")
    ap.add_argument(
        "--doc-only", action="store_true", help="skip the queries; render --doc from an existing summary.json"
    )
    ap.add_argument("--memory-limit", default="10GB")
    a = ap.parse_args(argv)
    if a.doc_only:
        s = json.loads((a.out_dir / "summary.json").read_text())
    else:
        s = run(a.parquet_dir, a.out_dir, a.only, a.memory_limit)
        for r in s["anomalies"]:
            print(r["id"], r["status"], r.get("count"), r.get("base"), r.get("elapsed_s"), r.get("reason", ""))
    if a.doc:
        a.doc.write_text(render_markdown(s, PUBLISHERS))


if __name__ == "__main__":
    main()
