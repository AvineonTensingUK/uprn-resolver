"""Validation suite over the hive-partitioned build outputs (``out_dir/lad=<code>/...``).

``validate(out_dir, staged_dir, parquet_dir, report_dir, lads=None)`` runs set-based DuckDB checks, writes
``report_dir/report.html`` (single self-contained file) and ``report_dir/results.json`` and returns the results dict.

Every check ends as PASS / WARN / FAIL (or SKIP when its inputs are absent). Checks only ever report counts, UPRNs,
building ids and LAD codes: never address text (EPC address / postcode values are compared inside DuckDB only).
A LAD is validated when ``stats.json``, ``uprn.parquet`` and ``buildings.parquet`` all exist (the build writes the
stats last), so a build in progress is safe to validate.
"""

# ruff: noqa: E501
from __future__ import annotations

import html
import json
import random
import shutil
import statistics
import time
from pathlib import Path

import duckdb

from uprn_resolver.convert import _lad
from uprn_resolver.convert import _q as _lit
from uprn_resolver.publish import ALLOWED_NAMES, FORBIDDEN

METHODS = ("toid_point_in_polygon", "uprn_point_in_polygon", "none")
CONFIDENCES = ("high", "medium", "low")
GB_EXTENT = (0.0, 0.0, 700_000.0, 1_300_000.0)
MAX_EX = 10  # examples kept per check
LAD_EX = 5  # examples kept per LAD
REQUIRED_UPRN_COLS = (
    "uprn",
    "x",
    "y",
    "geometry",
    "building_id",
    "match_method",
    "methods_disagree",
    "uprns_in_building",
    "outline_granularity",
    "uprns_in_parcel",
    "street_name",
    "parcel_inspire_id",
    "toid_x",
    "toid_y",
)
OSM_ADDRESS_COLS = ("osm_housenumber", "osm_housename", "osm_addr_street", "osm_addr_postcode")

# id -> (group, name, rule); rule is "fail", "warn" or (warn_rate, fail_rate) applied to bad/total.
CHECKS: dict[str, tuple[str, str, object]] = {
    "schema_columns": ("Integrity", "uprn.parquet has the columns the checks need", "fail"),
    "uprn_unique_lad": ("Integrity", "uprn unique within each LAD", "fail"),
    "lad_column": ("Integrity", "lad column (if present) equals the partition", "fail"),
    "onsud_present": ("Integrity", "every ONSUD UPRN of the LAD is in uprn.parquet", "fail"),
    "onsud_extra": ("Integrity", "no uprn.parquet UPRN outside the ONSUD set", "fail"),
    "row_count": ("Integrity", "uprn.parquet rows == ONSUD LAD count", "fail"),
    "uprn_unique_national": ("Integrity", "uprn unique nationally", "fail"),
    "uprn_two_lads": ("Integrity", "no UPRN in two LADs", "fail"),
    "provenance_subset": ("Integrity", "provenance/epc/poi UPRNs all exist in uprn.parquet", "fail"),
    "geo_extent": ("Geometry", "points within the GB extent (x,y and geometry agree)", "fail"),
    "geo_outliers": ("Geometry", "no point > 300 km from its LAD median", "fail"),
    "geo_open_uprn": ("Geometry", "x,y equal staged Open UPRN and lie in its bbox", "fail"),
    "geo_null_rate": ("Geometry", "UPRNs without a point", (0.02, 0.2)),
    "bld_valid": ("Geometry", "building polygons valid (ST_IsValid)", (0.0001, 0.02)),
    "bld_nonempty": ("Geometry", "building polygons non-null, non-empty, area > 0, polygonal", "fail"),
    "bld_unique": ("Geometry", "building_id unique in buildings.parquet", "fail"),
    "bld_referenced": ("Geometry", "every referenced building_id exists in buildings.parquet", "fail"),
    "bld_point_in": (
        "Geometry",
        "matched point lies within its building polygon (1% sample; full for one LAD)",
        "fail",
    ),
    "bld_uprn_pt_out": ("Geometry", "UPRN point outside its TOID-matched polygon (sample)", (0.02, 0.2)),
    "geoparquet_meta": ("Geometry", "GeoParquet 'geo' metadata present and sane (uprn, buildings)", "fail"),
    "method_vs_building": ("Logic", "match_method consistent with building_id nullness", "fail"),
    "uprns_in_building": ("Logic", "uprns_in_building equals recomputed per-building count", "fail"),
    "outline_granularity": ("Logic", "outline_granularity follows its rule", "fail"),
    "methods_disagree": ("Logic", "methods_disagree null unless both methods matched", "fail"),
    "methods_disagree_rate": ("Logic", "methods_disagree = true rate", (0.01, 0.1)),
    "lad_outlier_street": ("Logic", "street-name coverage per LAD within 3 SD of national mean", "warn"),
    "lad_outlier_parcel": ("Logic", "parcel coverage per LAD within 3 SD of national mean", "warn"),
    "lad_outlier_match": ("Logic", "building match rate per LAD within 3 SD of national mean", "warn"),
    "coverage_range": ("Logic", "street coverage >= 50% and building match >= 80% per LAD", "warn"),
    "epc_postcode": ("Cross-source", "EPC postcode agrees with ONSUD postcode (normalised)", (0.05, 0.2)),
    "osm_street": (
        "Cross-source",
        "OS street name (or alt) equals the matched OSM street name (normalised)",
        (0.2, 0.5),
    ),
    "poi_values": ("Cross-source", "POI match_confidence valid and POI uprns exist", "fail"),
    "poi_confidence": ("Cross-source", "POI match confidence distribution (share 'low' of matched)", (0.5, 0.9)),
    "usrn_far_lad": ("Cross-source", "provenance usrn_link_flag 'far' rate per LAD within 3 SD", "warn"),
    "usrn_far_rate": ("Cross-source", "national usrn_link_flag 'far' rate", (0.1, 0.3)),
    "ppd_in_onsud": ("Cross-source", "HMLR PPD UPRNs present in ONSUD", (0.001, 0.05)),
    "lic_cols_uprn": ("Licence", "no address-like columns in uprn.parquet", "fail"),
    "lic_cols_buildings": ("Licence", "no address-like columns in buildings.parquet", "fail"),
    "lic_cols_epc": ("Licence", "no address-like columns in epc.parquet", "fail"),
    "lic_cols_poi": ("Licence", "no address-like columns in poi.parquet", "fail"),
    "lic_cols_provenance": ("Licence", "no address-like columns in provenance.parquet (not released)", "fail"),
    "lic_prov_postcode": ("Licence", "provenance.parquet postcode columns (local-only; stripped on release)", "warn"),
    "lic_address_local": ("Licence", "address_local only under out/lad=*/, never in release dirs or national/", "fail"),
    "lic_address_flag": (
        "Licence",
        "address_local.licence valid on every row (address rows must be 'EPC-restricted', or 'PPD-restricted' for PPD)",
        "fail",
    ),
    "lic_osm_cols": ("Licence", "no OSM-derived (ODbL) columns in OGL uprn.parquet", "fail"),
    "lic_odbl": ("Licence", "streets_odbl / outline_odbl .licence is 'ODbL-1.0' on every row", "fail"),
    "lic_resolved_open": (
        "Licence",
        "resolved_open has no address/postcode text columns (beyond OGL street attributes), no osm_* columns "
        "and no ODbL source/licence values",
        "fail",
    ),
    "lic_sidecars": (
        "Licence",
        "outline_ms / outline_odbl / streets_odbl / resolved_open / parcels parquet files have a .LICENCE.txt sidecar "
        "(ODbL sidecars must name the ODbL)",
        "fail",
    ),
    "lic_resolved_local": (
        "Licence",
        "resolved_local only under out/lad=*/, never in release dirs, national/, staged or parquet",
        "fail",
    ),
}

# OGL street attributes allowed in resolved_open despite matching the address-like column filter
OPEN_ALLOWED = frozenset({"street_name", "street_source", "locality", "postcode_flag"}) | frozenset(ALLOWED_NAMES)
# sidecar -> (parquet it covers, must mention ODbL)
SIDECARS = {
    "outline_ms": False,
    "outline_odbl": True,
    "streets_odbl": True,
    "resolved_open": False,
    "parcels": False,
}


class _Check:
    def __init__(self, cid: str):
        self.id = cid
        self.group, self.name, self.rule = CHECKS[cid]
        self.bad = 0
        self.total = 0
        self.examples: list[str] = []
        self.per_lad: dict[str, dict] = {}
        self.note = ""
        self.ran = False
        self.errors: list[str] = []

    def add(self, lad: str, bad: int, total: int, examples=()):
        self.ran = True
        bad, total = int(bad or 0), int(total or 0)
        self.bad += bad
        self.total += total
        ex = [str(e) for e in list(examples)[:LAD_EX]]
        if bad:
            self.per_lad[lad] = {"bad": bad, "total": total, "examples": ex}
            room = MAX_EX - len(self.examples)
            self.examples += [f"{lad}:{e}" if self.group != "Licence" else e for e in ex[: max(room, 0)]]

    @property
    def status(self) -> str:
        if self.errors:
            return "FAIL"
        if not self.ran:
            return "SKIP"
        if self.bad == 0:
            return "PASS"
        if self.rule == "fail":
            return "FAIL"
        if self.rule == "warn":
            return "WARN"
        warn, fail = self.rule
        rate = self.bad / max(self.total, 1)
        return "FAIL" if rate >= fail else "WARN" if rate >= warn else "PASS"

    def to_dict(self) -> dict:
        worst = sorted(self.per_lad.items(), key=lambda kv: -kv[1]["bad"])[:100]
        return {
            "id": self.id,
            "group": self.group,
            "name": self.name,
            "status": self.status,
            "bad": self.bad,
            "total": self.total,
            "rate": round(self.bad / self.total, 6) if self.total else None,
            "note": self.note,
            "examples": self.examples,
            "errors": self.errors,
            "lads_affected": len(self.per_lad),
            "per_lad": dict(worst),
        }


def _q(con, sql: str, params=None) -> list[tuple]:
    return con.execute(sql, params or []).fetchall()


def _norm_pc(expr: str) -> str:
    return f"upper(regexp_replace({expr}, '\\s+', '', 'g'))"


def _gx(con, path: Path, col: str = "geometry") -> str:
    """SQL expression yielding a GEOMETRY for ``col`` whether stored natively or as WKB."""
    typ = _q(
        con,
        f"SELECT column_type FROM (DESCRIBE SELECT * FROM read_parquet({_lit(path)}, hive_partitioning=false)) WHERE column_name = ?",
        [col],
    )
    if typ and str(typ[0][0]).upper().startswith("GEOMETRY"):
        return col
    return f"ST_GeomFromWKB({col})"


def _cols(con, path: Path) -> list[str]:
    return [r[0] for r in _q(con, f"DESCRIBE SELECT * FROM read_parquet({_lit(path)}, hive_partitioning=false)")]


def _zflags(rates: dict[str, float], min_n: int = 5, k: float = 3.0):
    """LADs further than k SD from the mean, grouped by country letter (E/W/S) when each group is big enough."""
    groups: dict[str, dict[str, float]] = {}
    for lad, r in rates.items():
        if r is not None:
            groups.setdefault(lad[:1], {})[lad] = r
    if any(len(g) < min_n for g in groups.values()):
        groups = {"all": {lad: r for lad, r in rates.items() if r is not None}}
    flagged, evaluated = [], 0
    for g in groups.values():
        if len(g) < min_n:
            continue
        evaluated += len(g)
        mu, sd = statistics.fmean(g.values()), statistics.pstdev(g.values())
        if sd == 0:
            continue
        flagged += [(lad, r, (r - mu) / sd) for lad, r in g.items() if abs(r - mu) > k * sd]
    return flagged, evaluated


def _check_geo_meta(path: Path) -> str | None:
    import pyarrow.parquet as pq

    try:
        md = pq.read_schema(path).metadata or {}
        geo = json.loads(md[b"geo"])
        col = geo["primary_column"]
        if col not in geo["columns"] or "encoding" not in geo["columns"][col]:
            return "geo metadata lacks primary column/encoding"
        if col not in pq.read_schema(path).names:
            return "geo primary column not in schema"
    except KeyError:
        return "no 'geo' key metadata"
    except Exception as e:  # noqa: BLE001
        return f"unreadable geo metadata ({type(e).__name__})"
    return None


class _Run:
    def __init__(
        self, out_dir: Path, staged_dir: Path, parquet_dir: Path, report_dir: Path, lads, release_dirs, full_lad, seed
    ):
        self.out, self.staged, self.pq, self.report_dir = out_dir, staged_dir, parquet_dir, report_dir
        self.checks = {cid: _Check(cid) for cid in CHECKS}
        self.release_dirs = release_dirs
        self.seed = seed
        self.lad_rows: dict[str, dict] = {}
        self.poi_dist = {c: 0 for c in CONFIDENCES}
        self.far: dict[str, float | None] = {}
        self.osm_flag = [0, 0]
        self.tmp = report_dir / ".duck_tmp"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect()
        self.con.execute(f"SET temp_directory={_lit(self.tmp)}")
        self.con.execute("SET memory_limit='6GB'")
        self.con.execute("SET threads TO 4")
        try:
            self.con.execute("LOAD spatial")
        except duckdb.Error:
            self.con.execute("INSTALL spatial")
            self.con.execute("LOAD spatial")
        self.incomplete: list[str] = []
        self.lads = self._discover(lads)
        self.full_lad = full_lad if full_lad in self.lads else self._smallest()

    def _discover(self, lads):
        found = []
        for d in sorted(self.out.glob("lad=*")):
            lad = d.name[4:]
            if lads is not None and lad not in lads:
                continue
            try:
                _lad(lad)
            except ValueError:
                self.incomplete.append(lad)
                continue
            if all((d / f).exists() for f in ("stats.json", "uprn.parquet", "buildings.parquet")):
                found.append(lad)
            else:
                self.incomplete.append(lad)
        return found

    def _smallest(self):
        if not self.lads:
            return None
        return min(self.lads, key=lambda lad: (self.out / f"lad={lad}" / "uprn.parquet").stat().st_size)

    def safe(self, cid: str, lad: str, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            self.checks[cid].errors.append(f"{lad}: {type(e).__name__}: {str(e).splitlines()[0][:160]}")

    # ------------------------------------------------------------------ per LAD
    def run_lad(self, lad: str):
        con, C = self.con, self.checks
        d = self.out / f"lad={lad}"
        s = self.staged / lad
        up, bp = d / "uprn.parquet", d / "buildings.parquet"
        con.execute(f"CREATE OR REPLACE TEMP VIEW u AS SELECT * FROM read_parquet({_lit(up)}, hive_partitioning=false)")
        con.execute(f"CREATE OR REPLACE TEMP VIEW b AS SELECT * FROM read_parquet({_lit(bp)}, hive_partitioning=false)")
        ucols = _cols(con, up)
        missing = [c for c in REQUIRED_UPRN_COLS if c not in ucols]
        C["schema_columns"].add(lad, len(missing), len(REQUIRED_UPRN_COLS), missing)
        if missing:
            return
        n = _q(con, "SELECT count(*) FROM u")[0][0]
        row = self.lad_rows.setdefault(lad, {"lad": lad, "uprns": n})
        gu, gb = _gx(con, up), _gx(con, bp)

        def optional(name):
            p = d / name
            return p if p.exists() else None

        # Integrity
        def integrity():
            (dup,) = _q(con, "SELECT count(*) - count(DISTINCT uprn) FROM u")[0]
            ex = [r[0] for r in _q(con, "SELECT uprn FROM u GROUP BY uprn HAVING count(*) > 1 LIMIT 5")]
            C["uprn_unique_lad"].add(lad, dup, n, ex)
            con.execute("INSERT INTO uall SELECT DISTINCT uprn, ? FROM u", [lad])
            if "lad" in ucols:
                bad = _q(con, "SELECT count(*) FROM u WHERE lad IS DISTINCT FROM ?", [lad])[0][0]
                ex = [r[0] for r in _q(con, "SELECT uprn FROM u WHERE lad IS DISTINCT FROM ? LIMIT 5", [lad])]
                C["lad_column"].add(lad, bad, n, ex)
            on = s / "onsud.parquet"
            if on.exists():
                con.execute(
                    f"CREATE OR REPLACE TEMP VIEW o AS SELECT DISTINCT uprn FROM read_parquet({_lit(on)}, hive_partitioning=false)"
                )
                no = _q(con, "SELECT count(*) FROM o")[0][0]
                miss = _q(con, "SELECT count(*) FROM o ANTI JOIN u USING (uprn)")[0][0]
                ex = [r[0] for r in _q(con, "SELECT uprn FROM o ANTI JOIN u USING (uprn) LIMIT 5")]
                C["onsud_present"].add(lad, miss, no, ex)
                extra = _q(con, "SELECT count(*) FROM (SELECT DISTINCT uprn FROM u) x ANTI JOIN o USING (uprn)")[0][0]
                ex = [
                    r[0]
                    for r in _q(
                        con, "SELECT uprn FROM (SELECT DISTINCT uprn FROM u) x ANTI JOIN o USING (uprn) LIMIT 5"
                    )
                ]
                C["onsud_extra"].add(lad, extra, n, ex)
                C["row_count"].add(lad, int(n != no), 1, [])
                if n != no:
                    C["row_count"].per_lad[lad]["examples"] = [f"uprn.parquet={n} onsud={no}"]
            for name in ("provenance", "epc", "poi"):
                p = optional(f"{name}.parquet")
                if p:
                    q = f"SELECT count(*), count(*) FILTER (WHERE uprn IS NOT NULL) FROM read_parquet({_lit(p)}, hive_partitioning=false)"
                    t, tn = _q(con, q)[0]
                    bad = _q(
                        con,
                        f"SELECT count(*) FROM read_parquet({_lit(p)}, hive_partitioning=false) x WHERE uprn IS NOT NULL AND "
                        "uprn NOT IN (SELECT uprn FROM u)",
                    )[0][0]
                    ex = [
                        r[0]
                        for r in _q(
                            con,
                            f"SELECT uprn FROM read_parquet({_lit(p)}, hive_partitioning=false) x WHERE uprn IS NOT NULL "
                            "AND uprn NOT IN (SELECT uprn FROM u) LIMIT 5",
                        )
                    ]
                    C["provenance_subset"].add(lad, bad, tn, ex)

        self.safe("uprn_unique_lad", lad, integrity)

        # Geometry
        def geometry():
            gb_x, gb_y = GB_EXTENT[2], GB_EXTENT[3]
            bad_expr = (
                f"(x IS NOT NULL AND (x < 0 OR x > {gb_x} OR y < 0 OR y > {gb_y})) OR "
                f"(x IS NOT NULL AND geometry IS NULL) OR (x IS NULL AND geometry IS NOT NULL) OR "
                f"(geometry IS NOT NULL AND (abs(ST_X({gu}) - x) > 0.01 OR abs(ST_Y({gu}) - y) > 0.01))"
            )
            bad = _q(con, f"SELECT count(*) FROM u WHERE {bad_expr}")[0][0]
            ex = [r[0] for r in _q(con, f"SELECT uprn FROM u WHERE {bad_expr} LIMIT 5")]
            C["geo_extent"].add(lad, bad, n, ex)
            nul = _q(con, "SELECT count(*) FROM u WHERE x IS NULL")[0][0]
            C["geo_null_rate"].add(lad, nul, n, [r[0] for r in _q(con, "SELECT uprn FROM u WHERE x IS NULL LIMIT 5")])
            out_sql = (
                "WITH m AS (SELECT median(x) mx, median(y) my FROM u WHERE x IS NOT NULL) "
                "SELECT {sel} FROM u, m WHERE x IS NOT NULL AND sqrt((x-mx)^2 + (y-my)^2) > 300000"
            )
            bad = _q(con, out_sql.format(sel="count(*)"))[0][0]
            ex = [r[0] for r in _q(con, out_sql.format(sel="uprn") + " LIMIT 5")]
            C["geo_outliers"].add(lad, bad, n, ex)
            ou = s / "open_uprn.parquet"
            if ou.exists():
                con.execute(
                    f"CREATE OR REPLACE TEMP VIEW ou AS SELECT * FROM read_parquet({_lit(ou)}, hive_partitioning=false)"
                )
                cond = (
                    "(abs(u.x - ou.x) > 0.01 OR abs(u.y - ou.y) > 0.01 OR u.x < (SELECT min(x) - 1 FROM ou) OR "
                    "u.x > (SELECT max(x) + 1 FROM ou) OR u.y < (SELECT min(y) - 1 FROM ou) OR "
                    "u.y > (SELECT max(y) + 1 FROM ou))"
                )
                q = f"FROM u JOIN ou USING (uprn) WHERE u.x IS NOT NULL AND {cond}"
                bad = _q(con, f"SELECT count(*) {q}")[0][0]
                ex = [r[0] for r in _q(con, f"SELECT u.uprn {q} LIMIT 5")]
                C["geo_open_uprn"].add(lad, bad, n, ex)

        self.safe("geo_extent", lad, geometry)

        def buildings():
            nb = _q(con, "SELECT count(*) FROM b")[0][0]
            C["bld_unique"].add(
                lad,
                _q(con, "SELECT count(*) - count(DISTINCT building_id) FROM b")[0][0],
                nb,
                [r[0] for r in _q(con, "SELECT building_id FROM b GROUP BY 1 HAVING count(*) > 1 LIMIT 5")],
            )
            empty = (
                f"({gb} IS NULL OR ST_IsEmpty({gb}) OR ST_Area({gb}) <= 0 OR "
                f"ST_GeometryType({gb}) NOT IN ('POLYGON', 'MULTIPOLYGON'))"
            )
            C["bld_nonempty"].add(
                lad,
                _q(con, f"SELECT count(*) FROM b WHERE {empty}")[0][0],
                nb,
                [r[0] for r in _q(con, f"SELECT building_id FROM b WHERE {empty} LIMIT 5")],
            )
            inv = f"({gb} IS NOT NULL AND NOT ST_IsEmpty({gb}) AND NOT ST_IsValid({gb}))"
            C["bld_valid"].add(
                lad,
                _q(con, f"SELECT count(*) FROM b WHERE {inv}")[0][0],
                nb,
                [r[0] for r in _q(con, f"SELECT building_id FROM b WHERE {inv} LIMIT 5")],
            )
            q = "FROM u WHERE building_id IS NOT NULL AND building_id NOT IN (SELECT building_id FROM b)"
            nref = _q(con, "SELECT count(*) FROM u WHERE building_id IS NOT NULL")[0][0]
            C["bld_referenced"].add(
                lad, _q(con, f"SELECT count(*) {q}")[0][0], nref, [r[0] for r in _q(con, f"SELECT uprn {q} LIMIT 5")]
            )

        self.safe("bld_unique", lad, buildings)

        def point_in():
            full = lad == self.full_lad
            samp = "" if full else "AND hash(u.uprn) % 100 = 0"
            base = (
                f"FROM (SELECT u.uprn, u.match_method, u.toid_x, u.toid_y, "
                f"{gu.replace('geometry', 'u.geometry')} AS pg, "
                f"{gb.replace('geometry', 'b.geometry')} AS bg FROM u JOIN b USING (building_id) "
                f"WHERE u.match_method <> 'none' {samp}) t"
            )
            bad_c = (
                "((match_method = 'uprn_point_in_polygon' AND (pg IS NULL OR NOT ST_Intersects(pg, bg))) OR "
                "(match_method = 'toid_point_in_polygon' AND (toid_x IS NULL OR "
                "NOT ST_Intersects(ST_Point(toid_x, toid_y), bg))))"
            )
            tot, bad = _q(con, f"SELECT count(*), count(*) FILTER (WHERE {bad_c}) {base}")[0]
            ex = [r[0] for r in _q(con, f"SELECT uprn {base} WHERE {bad_c} LIMIT 5")]
            C["bld_point_in"].add(lad, bad, tot, ex)
            c2 = "match_method = 'toid_point_in_polygon' AND pg IS NOT NULL AND NOT ST_Intersects(pg, bg)"
            tot2 = _q(con, f"SELECT count(*) {base} WHERE match_method = 'toid_point_in_polygon' AND pg IS NOT NULL")
            C["bld_uprn_pt_out"].add(
                lad,
                _q(con, f"SELECT count(*) {base} WHERE {c2}")[0][0],
                tot2[0][0],
                [r[0] for r in _q(con, f"SELECT uprn {base} WHERE {c2} LIMIT 5")],
            )

        self.safe("bld_point_in", lad, point_in)

        def meta():
            errs = [f"{p.stem}: {e}" for p in (up, bp) if (e := _check_geo_meta(p))]
            C["geoparquet_meta"].add(lad, len(errs), 2, errs)

        self.safe("geoparquet_meta", lad, meta)

        # Logic
        def logic():
            bad_mm = (
                "(match_method IS NULL OR match_method NOT IN ('toid_point_in_polygon', "
                "'uprn_point_in_polygon', 'none') OR (match_method = 'none') <> (building_id IS NULL))"
            )
            C["method_vs_building"].add(
                lad,
                _q(con, f"SELECT count(*) FROM u WHERE {bad_mm}")[0][0],
                n,
                [r[0] for r in _q(con, f"SELECT uprn FROM u WHERE {bad_mm} LIMIT 5")],
            )
            q = (
                "FROM u LEFT JOIN (SELECT building_id, count(*) AS n FROM u WHERE building_id IS NOT NULL "
                "GROUP BY 1) c USING (building_id) WHERE u.uprns_in_building IS DISTINCT FROM c.n"
            )
            C["uprns_in_building"].add(
                lad, _q(con, f"SELECT count(*) {q}")[0][0], n, [r[0] for r in _q(con, f"SELECT u.uprn {q} LIMIT 5")]
            )
            exp = (
                "CASE WHEN uprns_in_building = 1 THEN 'single' WHEN uprns_in_building IS NULL THEN NULL "
                "WHEN uprns_in_parcel < uprns_in_building THEN 'parcel_finer' ELSE 'shared' END"
            )
            q = f"FROM u WHERE outline_granularity IS DISTINCT FROM ({exp})"
            C["outline_granularity"].add(
                lad, _q(con, f"SELECT count(*) {q}")[0][0], n, [r[0] for r in _q(con, f"SELECT uprn {q} LIMIT 5")]
            )
            bad_md = (
                "(methods_disagree IS NOT NULL AND (match_method <> 'toid_point_in_polygon' OR x IS NULL OR "
                "toid_x IS NULL OR building_id IS NULL))"
            )
            C["methods_disagree"].add(
                lad,
                _q(con, f"SELECT count(*) FROM u WHERE {bad_md}")[0][0],
                n,
                [r[0] for r in _q(con, f"SELECT uprn FROM u WHERE {bad_md} LIMIT 5")],
            )
            both, dis = _q(
                con,
                "SELECT count(*) FILTER (WHERE methods_disagree IS NOT NULL), "
                "count(*) FILTER (WHERE methods_disagree) FROM u",
            )[0]
            C["methods_disagree_rate"].add(
                lad, dis, both, [r[0] for r in _q(con, "SELECT uprn FROM u WHERE methods_disagree LIMIT 5")]
            )
            st, pa, ma = _q(
                con,
                "SELECT count(*) FILTER (WHERE street_name IS NOT NULL), "
                "count(*) FILTER (WHERE parcel_inspire_id IS NOT NULL), "
                "count(*) FILTER (WHERE building_id IS NOT NULL) FROM u",
            )[0]
            row.update(
                street_pct=st / n if n else None, parcel_pct=pa / n if n else None, match_pct=ma / n if n else None
            )

        self.safe("method_vs_building", lad, logic)

        # Cross-source
        def cross():
            al = optional("address_local.parquet")
            if al:
                on = s / "onsud.parquet"
                if on.exists():
                    src = (
                        f"FROM read_parquet({_lit(al)}, hive_partitioning=false) a JOIN (SELECT DISTINCT ON (uprn) uprn, postcode AS opc "
                        f"FROM read_parquet({_lit(on)}, hive_partitioning=false) ORDER BY uprn) o USING (uprn)"
                    )
                    col = "opc"
                else:
                    src, col = f"FROM read_parquet({_lit(al)}, hive_partitioning=false) a", "postcode_onsud"
                base = f"{src} WHERE a.postcode IS NOT NULL AND {col} IS NOT NULL"
                bad_c = f"{_norm_pc('a.postcode')} <> {_norm_pc(col)}"
                tot, bad = _q(con, f"SELECT count(*), count(*) FILTER (WHERE {bad_c}) {base}")[0]
                ex = [r[0] for r in _q(con, f"SELECT a.uprn {base} AND {bad_c} LIMIT 5")]
                C["epc_postcode"].add(lad, bad, tot, ex)
                lic_bad_c = (
                    "(licence IS NULL OR licence NOT IN ('EPC-restricted', 'PPD-restricted', 'open-partial') OR "
                    "(address_line1 IS NOT NULL AND licence NOT IN ('EPC-restricted', 'PPD-restricted')))"
                )
                lic_bad = _q(
                    con, f"SELECT count(*) FROM read_parquet({_lit(al)}, hive_partitioning=false) WHERE {lic_bad_c}"
                )[0][0]
                C["lic_address_flag"].add(
                    lad,
                    lic_bad,
                    n,
                    [
                        r[0]
                        for r in _q(
                            con,
                            f"SELECT uprn FROM read_parquet({_lit(al)}, hive_partitioning=false) WHERE {lic_bad_c} LIMIT 5",
                        )
                    ],
                )
            od = optional("streets_odbl.parquet")
            if od:
                nz = "regexp_replace(lower({}), '[^a-z0-9]', '', 'g')"
                base = (
                    f"FROM u JOIN read_parquet({_lit(od)}, hive_partitioning=false) o USING (uprn) WHERE u.street_name IS NOT NULL "
                    "AND o.osm_street_name IS NOT NULL"
                )
                bad_c = (
                    f"{nz.format('u.street_name')} <> {nz.format('o.osm_street_name')} AND "
                    f"coalesce({nz.format('u.street_name_alt')}, '') <> {nz.format('o.osm_street_name')}"
                )
                tot, bad = _q(con, f"SELECT count(*), count(*) FILTER (WHERE {bad_c}) {base}")[0]
                C["osm_street"].add(lad, bad, tot, [r[0] for r in _q(con, f"SELECT u.uprn {base} AND {bad_c} LIMIT 5")])
                fl, fb = _q(
                    con,
                    f"SELECT count(*) FILTER (WHERE osm_agrees_os IS NOT NULL), "
                    f"count(*) FILTER (WHERE NOT osm_agrees_os) FROM read_parquet({_lit(od)}, hive_partitioning=false)",
                )[0]
                self.osm_flag[0] += fb
                self.osm_flag[1] += fl
                lb = _q(
                    con,
                    f"SELECT count(*) FROM read_parquet({_lit(od)}, hive_partitioning=false) WHERE licence IS DISTINCT FROM 'ODbL-1.0'",
                )[0][0]
                C["lic_odbl"].add(
                    lad,
                    lb,
                    _q(con, f"SELECT count(*) FROM read_parquet({_lit(od)}, hive_partitioning=false)")[0][0],
                    [
                        r[0]
                        for r in _q(
                            con,
                            f"SELECT uprn FROM read_parquet({_lit(od)}, hive_partitioning=false) WHERE licence IS DISTINCT FROM 'ODbL-1.0' LIMIT 5",
                        )
                    ],
                )
            po = optional("poi.parquet")
            if po:
                tot = _q(con, f"SELECT count(*) FROM read_parquet({_lit(po)}, hive_partitioning=false)")[0][0]
                bad_c = (
                    f"((uprn IS NOT NULL AND match_confidence IS NULL) OR "
                    f"(match_confidence IS NOT NULL AND match_confidence NOT IN {CONFIDENCES}) OR "
                    "(uprn IS NOT NULL AND uprn NOT IN (SELECT uprn FROM u)) OR "
                    "(uprn IS NULL AND match_confidence IS NOT NULL))"
                )
                C["poi_values"].add(
                    lad,
                    _q(con, f"SELECT count(*) FROM read_parquet({_lit(po)}, hive_partitioning=false) WHERE {bad_c}")[0][
                        0
                    ],
                    tot,
                    [
                        r[0]
                        for r in _q(
                            con,
                            f"SELECT coalesce(uprn, -1) FROM read_parquet({_lit(po)}, hive_partitioning=false) "
                            f"WHERE {bad_c} LIMIT 5",
                        )
                    ],
                )
                for c, k in _q(
                    con,
                    f"SELECT match_confidence, count(*) FROM read_parquet({_lit(po)}, hive_partitioning=false) "
                    "WHERE match_confidence IS NOT NULL GROUP BY 1",
                ):
                    if c in self.poi_dist:
                        self.poi_dist[c] += k
            pv = optional("provenance.parquet")
            if pv:
                tot, far = _q(
                    con,
                    f"SELECT count(*) FILTER (WHERE usrn_link_flag IS NOT NULL), "
                    f"count(*) FILTER (WHERE usrn_link_flag = 'far') FROM read_parquet({_lit(pv)}, hive_partitioning=false)",
                )[0]
                self.far[lad] = far / tot if tot else None
                row["far_pct"] = self.far[lad]
                C["usrn_far_rate"].add(
                    lad,
                    far,
                    tot,
                    [
                        r[0]
                        for r in _q(
                            con,
                            f"SELECT uprn FROM read_parquet({_lit(pv)}, hive_partitioning=false) WHERE usrn_link_flag = 'far' LIMIT 3",
                        )
                    ],
                )
            pp = s / "hmlr_ppd_uprn.parquet"
            if pp.exists():
                base = (
                    f"FROM (SELECT DISTINCT uprn FROM read_parquet({_lit(pp)}, hive_partitioning=false) WHERE uprn IS NOT NULL) p "
                    f"WHERE uprn NOT IN (SELECT uprn FROM read_parquet({_lit(s / 'onsud.parquet')}, hive_partitioning=false))"
                    if (s / "onsud.parquet").exists()
                    else ""
                )
                if base:
                    tot = _q(
                        con, f"SELECT count(DISTINCT uprn) FROM read_parquet({_lit(pp)}, hive_partitioning=false)"
                    )[0][0]
                    C["ppd_in_onsud"].add(
                        lad,
                        _q(con, f"SELECT count(*) {base}")[0][0],
                        tot,
                        [r[0] for r in _q(con, f"SELECT uprn {base} LIMIT 5")],
                    )

        self.safe("epc_postcode", lad, cross)

        # Licence (columns)
        def licence():
            files = {"uprn": up, "buildings": bp}
            for name in ("epc", "poi", "provenance"):
                if p := optional(f"{name}.parquet"):
                    files[name] = p
            for name, p in files.items():
                cols = _cols(con, p)
                bad = [c for c in cols if FORBIDDEN.search(c) and c not in ALLOWED_NAMES]
                if name == "provenance":
                    pc = [c for c in bad if c.startswith("postcode")]
                    bad = [c for c in bad if c not in pc]
                    C["lic_prov_postcode"].add(lad, len(pc), len(cols), pc)
                C[f"lic_cols_{name}"].add(lad, len(bad), len(cols), bad)
            osm = [c for c in ucols if c.startswith("osm_") or c in OSM_ADDRESS_COLS]
            C["lic_osm_cols"].add(lad, len(osm), len(ucols), osm)

        self.safe("lic_cols_uprn", lad, licence)

        def licence_more():
            # resolved_open: OGL-only; no address text, no ODbL values
            if ro := optional("resolved_open.parquet"):
                cols = _cols(con, ro)
                bad = [
                    c
                    for c in cols
                    if (FORBIDDEN.search(c) and c not in OPEN_ALLOWED) or c.startswith("osm_") or c in OSM_ADDRESS_COLS
                ]
                n_bad_vals = 0
                for c in (
                    "best_outline_source",
                    "street_source",
                    "address_source",
                    "best_outline_licence",
                    "poi_licence",
                ):
                    if c in cols:
                        k = _q(
                            con,
                            f"SELECT count(*) FROM read_parquet({_lit(ro)}, hive_partitioning=false) "
                            f"WHERE lower(CAST({c} AS VARCHAR)) LIKE '%osm%' OR lower(CAST({c} AS VARCHAR)) LIKE '%odbl%' "
                            f"OR lower(CAST({c} AS VARCHAR)) LIKE '%openstreetmap%'",
                        )[0][0]
                        if k:
                            n_bad_vals += k
                            bad.append(f"{c}:{k} ODbL value(s)")
                C["lic_resolved_open"].add(lad, len(bad), len(cols), bad)
            # outline_odbl: licence column must be ODbL on every row
            if oo := optional("outline_odbl.parquet"):
                if "licence" in _cols(con, oo):
                    lb, tot = _q(
                        con,
                        f"SELECT count(*) FILTER (WHERE licence IS DISTINCT FROM 'ODbL-1.0'), count(*) "
                        f"FROM read_parquet({_lit(oo)}, hive_partitioning=false)",
                    )[0]
                    C["lic_odbl"].add(lad, lb, tot, ["outline_odbl"] if lb else [])
                else:
                    C["lic_odbl"].add(lad, 1, 1, ["outline_odbl: no licence column"])
            # sidecars
            for name, need_odbl in SIDECARS.items():
                if not (d / f"{name}.parquet").exists():
                    continue
                sc = d / f"{name}.LICENCE.txt"
                if not sc.exists():
                    C["lic_sidecars"].add(lad, 1, 1, [f"{name}: missing {sc.name}"])
                elif need_odbl and "ODbL" not in sc.read_text(errors="replace"):
                    C["lic_sidecars"].add(lad, 1, 1, [f"{name}: sidecar does not mention ODbL"])
                else:
                    C["lic_sidecars"].add(lad, 0, 1)

        self.safe("lic_resolved_open", lad, licence_more)

    # ------------------------------------------------------------------ national / aggregate
    def finish(self):
        con, C = self.con, self.checks
        if self.lads:
            tot = _q(con, "SELECT count(*), count(DISTINCT uprn) FROM uall")[0]
            C["uprn_unique_national"].add("national", tot[0] - tot[1], tot[0])
            q = "SELECT uprn FROM uall GROUP BY uprn HAVING count(DISTINCT lad) > 1"
            n2 = _q(con, f"SELECT count(*) FROM ({q})")[0][0]
            ex = _q(con, f"{q} LIMIT 5")
            C["uprn_two_lads"].add("national", n2, tot[1], [r[0] for r in ex])
            for r in ex:
                lads = [x[0] for x in _q(con, "SELECT lad FROM uall WHERE uprn = ?", [r[0]])]
                C["uprn_two_lads"].per_lad.setdefault("national", {"bad": n2, "total": tot[1], "examples": []})
                C["uprn_two_lads"].per_lad["national"]["examples"].append(f"{r[0]} in {','.join(lads)}")

        for cid, key, label in (
            ("lad_outlier_street", "street_pct", "street coverage"),
            ("lad_outlier_parcel", "parcel_pct", "parcel coverage"),
            ("lad_outlier_match", "match_pct", "building match rate"),
            ("usrn_far_lad", "far_pct", "usrn 'far' rate"),
        ):
            rates = {lad: r.get(key) for lad, r in self.lad_rows.items() if r.get(key) is not None}
            self._zcheck(cid, rates, label)
        rng = {
            lad: r
            for lad, r in self.lad_rows.items()
            if (r.get("street_pct") is not None and r["street_pct"] < 0.5)
            or (r.get("match_pct") is not None and r["match_pct"] < 0.8)
        }
        if self.lad_rows:
            for lad in self.lad_rows:
                bad = int(lad in rng)
                C["coverage_range"].add(
                    lad,
                    bad,
                    1,
                    [f"street={rng[lad].get('street_pct', 0):.3f} match={rng[lad].get('match_pct', 0):.3f}"]
                    if bad
                    else [],
                )
        # POI distribution
        tot = sum(self.poi_dist.values())
        if tot:
            C["poi_confidence"].add("national", self.poi_dist["low"], tot)
            C["poi_confidence"].note = ", ".join(f"{k}: {v} ({v / tot:.1%})" for k, v in self.poi_dist.items())
        self._licence_files()
        if self.osm_flag[1]:
            C["osm_street"].note = (
                f"osm_agrees_os (nearest way vs OS name, stricter than the matched name) is false for "
                f"{self.osm_flag[0]}/{self.osm_flag[1]} rows. "
            )
        for cid in ("usrn_far_rate", "epc_postcode", "osm_street", "ppd_in_onsud", "methods_disagree_rate"):
            c = C[cid]
            if c.ran and c.total:
                c.note = (c.note + f" overall rate {c.bad / c.total:.4%} ({c.bad}/{c.total})").strip()

    def _zcheck(self, cid: str, rates: dict[str, float], label: str):
        c = self.checks[cid]
        flagged, evaluated = _zflags(rates)
        if not evaluated:
            c.note = "needs >= 5 LADs (per country) to evaluate"
            return
        c.ran = True
        c.total = evaluated
        c.bad = len(flagged)
        c.examples = [
            f"{lad}: {r:.3f} (z={z:+.1f})" for lad, r, z in sorted(flagged, key=lambda t: -abs(t[2]))[:MAX_EX]
        ]
        c.per_lad = {lad: {"bad": 1, "total": 1, "examples": [f"{label} {r:.4f}, z={z:+.1f}"]} for lad, r, z in flagged}

    def _licence_files(self):
        c, found_bad = self.checks["lic_address_local"], []
        scanned = sum(1 for _ in self.out.glob("lad=*"))
        for p in self.out.rglob("address_local*"):
            rel = p.relative_to(self.out)
            if not (len(rel.parts) == 2 and rel.parts[0].startswith("lad=")):
                found_bad.append(str(rel))
        for root in [*self.release_dirs, self.staged, self.pq]:
            if root.exists():
                found_bad += [str(p.relative_to(root.parent)) for p in root.rglob("address_local*")]
        c.ran = True
        c.total = scanned
        c.bad = len(found_bad)
        c.examples = found_bad[:MAX_EX]
        # resolved_local carries address text: same placement rule as address_local
        c, found_bad = self.checks["lic_resolved_local"], []
        for p in self.out.rglob("resolved_local*"):
            rel = p.relative_to(self.out)
            if not (len(rel.parts) == 2 and rel.parts[0].startswith("lad=")):
                found_bad.append(str(rel))
        for root in [*self.release_dirs, self.staged, self.pq]:
            if root.exists():
                found_bad += [str(p.relative_to(root.parent)) for p in root.rglob("resolved_local*")]
        c.ran = True
        c.total = scanned
        c.bad = len(found_bad)
        c.examples = found_bad[:MAX_EX]

    # ------------------------------------------------------------------ spot check
    def spot_check(self, k: int = 50) -> list[dict]:
        if not self.lads:
            return []
        rnd = random.Random(self.seed)
        slots: dict[str, int] = {}
        for _ in range(k):
            lad = rnd.choice(self.lads)
            slots[lad] = slots.get(lad, 0) + 1
        out = []
        for lad, m in slots.items():
            d = self.out / f"lad={lad}"
            try:
                bp, up = d / "buildings.parquet", d / "uprn.parquet"
                gb = _gx(self.con, bp).replace("geometry", "b.geometry")
                joins, sel = (
                    "",
                    [
                        "u.uprn",
                        "u.x",
                        "u.y",
                        "u.uprns_in_building",
                        "u.parcel_area_m2",
                        "u.street_name",
                        "u.epc_property_type",
                        f"round(ST_Area({gb}), 1) AS building_area_m2",
                    ],
                )
                po = d / "poi.parquet"
                if po.exists():
                    joins = (
                        f"LEFT JOIN (SELECT uprn, first(name ORDER BY poi_id) AS poi_name FROM "
                        f"read_parquet({_lit(po)}, hive_partitioning=false) WHERE uprn IS NOT NULL GROUP BY 1) p ON p.uprn = u.uprn"
                    )
                    sel.append("p.poi_name")
                else:
                    sel.append("NULL AS poi_name")
                sql = (
                    f"SELECT {', '.join(sel)} FROM read_parquet({_lit(up)}, hive_partitioning=false) u LEFT JOIN read_parquet({_lit(bp)}, hive_partitioning=false) b "
                    f"USING (building_id) {joins} ORDER BY hash(u.uprn + {self.seed}) LIMIT {m}"
                )
                cur = self.con.execute(sql)
                names = [x[0] for x in cur.description]
                for r in cur.fetchall():
                    rec = dict(zip(names, r, strict=True))
                    rec["lad"] = lad
                    out.append(rec)
            except Exception as e:  # noqa: BLE001
                out.append({"lad": lad, "error": f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"})
        return out


# ---------------------------------------------------------------------- report
CSS = """
:root{--bg:#fff;--fg:#1c2330;--mut:#667085;--line:#e4e7ec;--card:#f8f9fb;--pass:#067647;--warn:#b54708;--fail:#b42318;--skip:#667085}
@media(prefers-color-scheme:dark){:root{--bg:#14171c;--fg:#e6e9ee;--mut:#98a2b3;--line:#2a2f38;--card:#1b1f26;--pass:#47cd89;--warn:#fdb022;--fail:#f97066;--skip:#98a2b3}}
body{font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg);margin:0 auto;max-width:1200px;padding:16px}
h1{font-size:22px}h2{font-size:17px;margin-top:28px}table{border-collapse:collapse;width:100%;margin:8px 0}
th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top;font-size:13px}
th{color:var(--mut);font-weight:600}.chip{font-weight:700;font-size:12px;padding:1px 8px;border-radius:10px;border:1px solid currentColor}
.PASS{color:var(--pass)}.WARN{color:var(--warn)}.FAIL{color:var(--fail)}.SKIP{color:var(--skip)}
.cards{display:flex;gap:10px;flex-wrap:wrap}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 14px}
.mut{color:var(--mut)}code{font-size:12px}details{margin:2px 0}.scroll{overflow-x:auto}
"""


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.3f}" if abs(v) < 1000 else f"{v:,.0f}"
    return html.escape(str(v))


def _render(res: dict) -> str:
    e = html.escape
    s = res["summary"]
    parts = [
        f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,"
        f"initial-scale=1'><title>UPRN validation report</title><style>{CSS}</style></head><body>",
        f"<h1>UPRN validation report <span class='chip {res['status']}'>{res['status']}</span></h1>",
        f"<p class=mut>Generated {e(res['generated'])}. {len(res['lads'])} LADs validated"
        f"{', ' + str(len(res['incomplete_lads'])) + ' incomplete (build in progress) skipped' if res['incomplete_lads'] else ''}"
        f"; {res['uprn_total']:,} UPRNs; full point-in-polygon LAD: {e(str(res['full_lad']))}; "
        f"elapsed {res['elapsed_s']:.0f}s.</p>",
        "<div class=cards>"
        + "".join(f"<div class=card><span class='{k}'><b>{v}</b></span> {k}</div>" for k, v in s.items())
        + "</div>",
    ]
    group = None
    for c in res["checks"]:
        if c["group"] != group:
            if group:
                parts.append("</table>")
            group = c["group"]
            parts.append(
                f"<h2>{e(group)}</h2><table><tr><th>Status</th><th>Check</th><th>Bad / total</th>"
                "<th>Rate</th><th>Detail</th></tr>"
            )
        rate = "" if c["rate"] is None else f"{c['rate']:.4%}"
        det = []
        if c["note"]:
            det.append(f"<div class=mut>{e(c['note'])}</div>")
        if c["errors"]:
            det.append("<div class=FAIL>errors: " + e("; ".join(c["errors"][:5])) + "</div>")
        if c["examples"]:
            det.append("<div>examples: <code>" + e(", ".join(c["examples"])) + "</code></div>")
        if c["per_lad"]:
            rows = "".join(
                f"<tr><td>{e(k)}</td><td>{v['bad']:,}</td><td>{v['total']:,}</td>"
                f"<td><code>{e(', '.join(v['examples']))}</code></td></tr>"
                for k, v in list(c["per_lad"].items())[:30]
            )
            det.append(
                f"<details><summary>{c['lads_affected']} LADs affected</summary><table><tr><th>LAD</th>"
                f"<th>bad</th><th>total</th><th>examples</th></tr>{rows}</table></details>"
            )
        parts.append(
            f"<tr><td><span class='chip {c['status']}'>{c['status']}</span></td><td>{e(c['name'])}"
            f"<br><code class=mut>{c['id']}</code></td><td>{c['bad']:,} / {c['total']:,}</td>"
            f"<td>{rate}</td><td>{''.join(det)}</td></tr>"
        )
    parts.append("</table>")
    cols = ("lad", "uprns", "match_pct", "street_pct", "parcel_pct", "far_pct")
    parts.append(
        "<h2>Per-LAD coverage</h2><div class=scroll><table><tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>"
    )
    for r in res["lad_summary"]:
        parts.append(
            "<tr>"
            + "".join(
                f"<td>{_fmt(r.get(c) if c in ('lad', 'uprns') or r.get(c) is None else round(r[c] * 100, 2))}</td>"
                for c in cols
            )
            + "</tr>"
        )
    parts.append(
        "</table></div><h2>Random spot check (human eyeballing)</h2><p class=mut>Seeded random sample; "
        "x,y are EPSG:27700.</p><div class=scroll><table>"
    )
    keys = (
        "lad",
        "uprn",
        "x",
        "y",
        "building_area_m2",
        "uprns_in_building",
        "parcel_area_m2",
        "street_name",
        "epc_property_type",
        "poi_name",
    )
    parts.append("<tr>" + "".join(f"<th>{k}</th>" for k in keys) + "</tr>")
    for r in res["spot_check"]:
        parts.append(
            "<tr>"
            + (
                f"<td>{e(r['lad'])}</td><td colspan=9 class=FAIL>{e(r['error'])}</td>"
                if "error" in r
                else "".join(f"<td>{_fmt(r.get(k))}</td>" for k in keys)
            )
            + "</tr>"
        )
    parts.append("</table></div></body></html>")
    return "".join(parts)


def validate(
    out_dir, staged_dir, parquet_dir, report_dir, lads=None, *, release_dirs=None, full_lad="E09000019", seed: int = 42
) -> dict:
    """Run every check, write ``report_dir/report.html`` + ``results.json`` and return the results dict."""
    t0 = time.time()
    out_dir, staged_dir, parquet_dir, report_dir = (Path(p) for p in (out_dir, staged_dir, parquet_dir, report_dir))
    report_dir.mkdir(parents=True, exist_ok=True)
    if release_dirs is None:
        cands = [out_dir.parent / "release", out_dir.parent / "publish", out_dir.parent.parent / "publish"]
        release_dirs = [p for p in cands if p.exists()]
    run = _Run(
        out_dir,
        staged_dir,
        parquet_dir,
        report_dir,
        set(lads) if lads else None,
        [Path(p) for p in release_dirs],
        full_lad,
        seed,
    )
    try:
        run.con.execute("CREATE TEMP TABLE uall (uprn BIGINT, lad VARCHAR)")
        for lad in run.lads:
            run.run_lad(lad)
        run.finish()
        spot = run.spot_check()
        checks = [c.to_dict() for c in run.checks.values()]
        summary = {k: sum(c["status"] == k for c in checks) for k in ("PASS", "WARN", "FAIL", "SKIP")}
        status = "FAIL" if summary["FAIL"] else "WARN" if summary["WARN"] else "PASS"
        res = {
            "status": status,
            "summary": summary,
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "lads": run.lads,
            "incomplete_lads": run.incomplete,
            "full_lad": run.full_lad,
            "uprn_total": sum(r["uprns"] for r in run.lad_rows.values()),
            "elapsed_s": time.time() - t0,
            "checks": checks,
            "lad_summary": list(run.lad_rows.values()),
            "spot_check": spot,
        }
    finally:
        run.con.close()
        shutil.rmtree(run.tmp, ignore_errors=True)
    (report_dir / "results.json").write_text(json.dumps(res, indent=1, default=str))
    (report_dir / "report.html").write_text(_render(res))
    return res


def main(argv=None) -> int:  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="data/out")
    ap.add_argument("--staged", default="data/staged")
    ap.add_argument("--parquet", default="data/parquet")
    ap.add_argument("--report", default="data/validation")
    ap.add_argument("--lad", action="append")
    a = ap.parse_args(argv)
    r = validate(a.out, a.staged, a.parquet, a.report, a.lad)
    print(r["status"], r["summary"])
    for c in r["checks"]:
        if c["status"] in ("FAIL", "WARN"):
            print(f"{c['status']} {c['id']} bad={c['bad']}/{c['total']} lads={c['lads_affected']} {c['examples'][:3]}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
