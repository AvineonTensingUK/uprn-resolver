"""Stage national Parquet (data/parquet/) down to one LAD (see CONTRACT.md).

Reads ``raw_dir.parent / "parquet"`` and writes the five staged files to
``staged_dir/<lad>/`` using set-based DuckDB SQL.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb

BUFFER_M = 200.0


def _require(path: Path, what: str) -> None:
    if any(c in path.name for c in "*?"):
        ok = any(path.parent.glob(path.name))
    else:
        ok = path.exists()
    if not ok:
        raise FileNotFoundError(f"Missing national parquet for {what}: {path} (run `uprn-resolver download` first)")


PART_DIR = "_partitioned"


def _newest_mtime(paths) -> float:
    """Newest mtime (seconds) over files/dirs (directories recursively, glob patterns expanded); 0.0 if none."""
    best = 0.0

    def visit(p: Path) -> None:
        nonlocal best
        try:
            best = max(best, p.stat().st_mtime)
        except OSError:
            return
        if p.is_dir():
            for c in p.iterdir():
                visit(c)

    for p in paths:
        p = Path(p)
        if any(c in p.name for c in "*?"):
            if p.parent.is_dir():
                best = max(best, p.parent.stat().st_mtime)  # a file added/removed changes the dir mtime
            for c in p.parent.glob(p.name):
                visit(c)
        else:
            visit(p)
    return best


def _bbox_index_fresh(con, idx: Path, files: list[Path]) -> bool:
    """True when the cached extent index covers exactly ``files`` and is newer than every one of them."""
    if not idx.exists():
        return False
    try:
        have = {r[0] for r in con.execute(f"SELECT file FROM read_parquet('{idx}')").fetchall()}
    except Exception:  # noqa: BLE001 - unreadable index counts as stale
        return False
    return have == {str(f) for f in files} and idx.stat().st_mtime >= _newest_mtime(files)


def _bbox_index(con, idx: Path, files: list[Path], geom_col: str, id_col: str) -> Path:
    """Cache each file's geometry extent (EPSG:27700) once; used to prune files per LAD.

    The cache is rebuilt when the file set changed or any file is newer than the index."""
    if _bbox_index_fresh(con, idx, files):
        return idx
    idx.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for f in files:
        b = con.execute(
            f"""SELECT min(ST_XMin(g)), min(ST_YMin(g)), max(ST_XMax(g)), max(ST_YMax(g)) FROM
                (SELECT ST_GeomFromWKB({geom_col}) g FROM read_parquet('{f}'))"""
        ).fetchone()
        rows.append((str(f), *b))
    con.execute("CREATE OR REPLACE TEMP TABLE _bb(file VARCHAR, x0 DOUBLE, y0 DOUBLE, x1 DOUBLE, y1 DOUBLE)")
    con.executemany("INSERT INTO _bb VALUES (?,?,?,?,?)", rows)
    tmp = idx.with_name(f"{idx.name}.{os.getpid()}.tmp")
    con.execute(f"COPY _bb TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    tmp.replace(idx)
    return idx


def overlapping_files(con, idx: Path, bbox: tuple[float, float, float, float]) -> list[str]:
    x0, y0, x1, y1 = bbox
    return [
        r[0]
        for r in con.execute(
            f"SELECT file FROM read_parquet('{idx}') WHERE x1 >= ? AND x0 <= ? AND y1 >= ? AND y0 <= ? ORDER BY file",
            [x0, x1, y0, y1],
        ).fetchall()
    ]


def stage(lad: str, raw_dir: Path, staged_dir: Path) -> None:
    # (re)staging invalidates the LAD first: no marker until every required step has succeeded again
    (Path(staged_dir) / lad / DONE_MARKER).unlink(missing_ok=True)
    pq = Path(raw_dir).parent / "parquet"
    onsud = pq / "onsud" / "onsud.parquet"
    uprn = pq / "open_uprn" / "open_uprn.parquet"
    lids = pq / "lids" / "lids.parquet"
    toid_glob = pq / "open_toid" / "*.parquet"
    bld_glob = pq / "openmap_local" / "??.parquet"  # Building layer only, not *_important
    for path, what in (
        (onsud, "onsud"),
        (uprn, "open_uprn"),
        (lids, "lids"),
        (toid_glob, "open_toid"),
        (bld_glob, "openmap_local"),
    ):
        _require(path, what)

    part = Path(staged_dir) / PART_DIR
    bld_idx = part / "bbox_openmap.parquet"
    p = {
        n: next((part / n / f"lad={lad}").glob("data*.parquet"), None)
        for n in ("onsud", "open_uprn", "lids", "open_toid")
    }
    pq_root = Path(raw_dir).parent / "parquet"
    # national tables pre-partitioned by LAD (partition_national), used only when complete and newer than the inputs
    if all(v is not None for v in p.values()) and all(_part_fresh(part, pq_root, n) for n in p):
        onsud, uprn, lids, toid_glob = p["onsud"], p["open_uprn"], p["lids"], p["open_toid"]
    out = Path(staged_dir) / lad
    out.mkdir(parents=True, exist_ok=True)
    for stray in out.glob("*.tmp"):
        stray.unlink(missing_ok=True)
    tmp = Path(staged_dir).resolve().parent / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(f"SET temp_directory='{tmp}'")
    con.execute("SET preserve_insertion_order=false")
    if os.environ.get("UPRN_RESOLVER_THREADS"):
        con.execute(f"SET threads={int(os.environ['UPRN_RESOLVER_THREADS'])}")

    def write(query: str, name: str, params: list | None = None) -> None:
        dest = out / f"{name}.parquet"
        part = out / f"{name}.parquet.tmp"
        con.execute(f"COPY ({query}) TO '{part}' (FORMAT PARQUET, COMPRESSION ZSTD)", params or [])
        part.replace(dest)

    con.execute(
        f"""
        CREATE TEMP TABLE s_onsud AS
        SELECT CAST(uprn AS BIGINT) uprn, CAST(lad AS VARCHAR) lad, CAST(oa AS VARCHAR) oa,
               CAST(lsoa AS VARCHAR) lsoa, CAST(msoa AS VARCHAR) msoa,
               CAST(postcode AS VARCHAR) postcode
        FROM read_parquet('{onsud}') WHERE lad = ?""",
        [lad],
    )
    n = con.execute("SELECT count(*) FROM s_onsud").fetchone()[0]
    if n == 0:
        raise ValueError(f"No UPRNs for LAD {lad} in {onsud}")
    write("SELECT * FROM s_onsud ORDER BY uprn", "onsud")

    con.execute(f"""
        CREATE TEMP TABLE s_uprn AS
        SELECT CAST(u.uprn AS BIGINT) uprn, CAST(u.x AS DOUBLE) x, CAST(u.y AS DOUBLE) y,
               CAST(u.lat AS DOUBLE) lat, CAST(u.lon AS DOUBLE) lon
        FROM read_parquet('{uprn}') u SEMI JOIN s_onsud o ON o.uprn = u.uprn""")
    write("SELECT * FROM s_uprn ORDER BY uprn", "open_uprn")

    con.execute(f"""
        CREATE TEMP TABLE s_lids AS
        SELECT CAST(l.uprn AS BIGINT) uprn, CAST(l.toid AS VARCHAR) toid,
               CAST(l.lids_confidence AS VARCHAR) lids_confidence
        FROM read_parquet('{lids}') l SEMI JOIN s_onsud o ON o.uprn = l.uprn""")
    write("SELECT * FROM s_lids ORDER BY uprn, toid", "lids_uprn_toid")

    write(
        f"""
        SELECT CAST(t.toid AS VARCHAR) toid, CAST(t.toid_x AS DOUBLE) toid_x,
               CAST(t.toid_y AS DOUBLE) toid_y
        FROM read_parquet('{toid_glob}') t
        SEMI JOIN (SELECT DISTINCT toid FROM s_lids) l ON l.toid = t.toid
        ORDER BY toid""",
        "open_toid",
    )

    bb = con.execute("SELECT min(x), min(y), max(x), max(y) FROM s_uprn").fetchone()
    if bb[0] is None:
        raise ValueError(f"No Open UPRN coordinates for LAD {lad}")
    x0, y0, x1, y1 = bb[0] - BUFFER_M, bb[1] - BUFFER_M, bb[2] + BUFFER_M, bb[3] + BUFFER_M
    bld_src = f"'{bld_glob}'"
    if _bbox_index_fresh(con, bld_idx, sorted(bld_glob.parent.glob(bld_glob.name))):
        fl = overlapping_files(con, bld_idx, (x0, y0, x1, y1))
        bld_src = (
            "[" + ", ".join(f"'{f}'" for f in fl) + "]" if fl else f"(SELECT * FROM read_parquet('{bld_glob}') LIMIT 0)"
        )
    write(
        f"""
        SELECT DISTINCT ON (building_id) CAST(building_id AS VARCHAR) building_id,
               CAST(geom_wkb AS BLOB) geom_wkb
        FROM (
            SELECT building_id, geom_wkb, ST_GeomFromWKB(geom_wkb) g
            FROM read_parquet({bld_src})
        )
        WHERE ST_XMax(g) >= {x0} AND ST_XMin(g) <= {x1}
          AND ST_YMax(g) >= {y0} AND ST_YMin(g) <= {y1}
        ORDER BY building_id""",
        "buildings",
    )
    con.close()


CORE_FILES = ("onsud", "open_uprn", "lids_uprn_toid", "open_toid", "buildings")
DONE_MARKER = ".stage_done"
_GB_LAD = r"^[EWS][0-9]{8}$"


def _part_inputs(pq: Path, part: Path, name: str) -> list[Path]:
    onsud = pq / "onsud" / "onsud.parquet"
    return {
        "onsud": [onsud],
        "open_uprn": [pq / "open_uprn" / "open_uprn.parquet", onsud],
        "lids": [pq / "lids" / "lids.parquet", onsud],
        "open_toid": [pq / "open_toid" / "*.parquet", part / "lids" / "_ok"],
    }[name]


def _part_fresh(part: Path, pq: Path, name: str) -> bool:
    """A partitioned national table is usable when its ``_ok`` exists and is newer than every input."""
    ok = part / name / "_ok"
    return ok.exists() and ok.stat().st_mtime >= _newest_mtime(_part_inputs(pq, part, name))


def partition_national(raw_dir: Path, staged_dir: Path) -> None:
    """One pass over each national table: write onsud/open_uprn/lids/open_toid hive-partitioned by LAD under
    ``staged_dir/_partitioned/<table>/lad=<lad>/data.parquet`` (GB LADs only), plus OpenMap tile and INSPIRE
    file extents for bbox pruning. Idempotent: finished tables (``_ok`` marker newer than their national inputs)
    are not redone; a re-converted national table is re-partitioned."""
    pq = Path(raw_dir).parent / "parquet"
    part = Path(staged_dir) / PART_DIR
    part.mkdir(parents=True, exist_ok=True)
    tmp = Path(staged_dir).resolve().parent / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute(f"SET temp_directory='{tmp}'")
    con.execute("SET preserve_insertion_order=false")
    onsud = pq / "onsud" / "onsud.parquet"
    jobs = {
        "onsud": f"""SELECT CAST(uprn AS BIGINT) uprn, CAST(lad AS VARCHAR) lad, CAST(oa AS VARCHAR) oa,
                  CAST(lsoa AS VARCHAR) lsoa, CAST(msoa AS VARCHAR) msoa, CAST(postcode AS VARCHAR) postcode
                  FROM read_parquet('{onsud}') WHERE regexp_matches(lad, '{_GB_LAD}')""",
        "open_uprn": f"""SELECT CAST(u.uprn AS BIGINT) uprn, CAST(u.x AS DOUBLE) x, CAST(u.y AS DOUBLE) y,
                  CAST(u.lat AS DOUBLE) lat, CAST(u.lon AS DOUBLE) lon, o.lad
                  FROM read_parquet('{pq / "open_uprn" / "open_uprn.parquet"}') u
                  JOIN (SELECT DISTINCT uprn, lad FROM read_parquet('{onsud}')) o ON o.uprn = u.uprn
                  WHERE regexp_matches(o.lad, '{_GB_LAD}')""",
        "lids": f"""SELECT CAST(l.uprn AS BIGINT) uprn, CAST(l.toid AS VARCHAR) toid,
                  CAST(l.lids_confidence AS VARCHAR) lids_confidence, o.lad
                  FROM read_parquet('{pq / "lids" / "lids.parquet"}') l
                  JOIN (SELECT DISTINCT uprn, lad FROM read_parquet('{onsud}')) o ON o.uprn = l.uprn
                  WHERE regexp_matches(o.lad, '{_GB_LAD}')""",
    }
    for name, q in jobs.items():
        d = part / name
        if _part_fresh(part, pq, name):
            continue
        shutil.rmtree(d, ignore_errors=True)
        con.execute(f"""COPY ({q}) TO '{d}' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (lad),
                        FILENAME_PATTERN 'data', OVERWRITE_OR_IGNORE)""")
        (d / "_ok").write_text("ok\n")
    d = part / "open_toid"
    toid_fresh = _part_fresh(part, pq, "open_toid")
    if not toid_fresh and not any((part / "lids").glob("*/data*.parquet")):
        d.mkdir(parents=True, exist_ok=True)
        (d / "_ok").write_text("ok\n")
        toid_fresh = True
    if not toid_fresh:
        shutil.rmtree(d, ignore_errors=True)
        con.execute(f"""COPY (
            SELECT CAST(t.toid AS VARCHAR) toid, CAST(t.toid_x AS DOUBLE) toid_x, CAST(t.toid_y AS DOUBLE) toid_y, l.lad
            FROM read_parquet('{pq / "open_toid" / "*.parquet"}') t
            JOIN (SELECT DISTINCT toid, lad FROM read_parquet('{part / "lids" / "*" / "data*.parquet"}',
                  hive_partitioning=true)) l ON l.toid = t.toid)
            TO '{d}' (FORMAT PARQUET, COMPRESSION ZSTD, PARTITION_BY (lad), FILENAME_PATTERN 'data',
            OVERWRITE_OR_IGNORE)""")
        (d / "_ok").write_text("ok\n")
    _bbox_index(
        con, part / "bbox_openmap.parquet", sorted((pq / "openmap_local").glob("??.parquet")), "geom_wkb", "building_id"
    )
    insp = sorted((pq / "inspire").glob("*.parquet"))
    if insp:
        _bbox_index(con, part / "bbox_inspire.parquet", insp, "geom_wkb", "inspire_id")
    con.close()


def list_lads(raw_dir: Path) -> tuple[list[str], dict[str, int]]:
    """GB LAD codes in national ONSUD, plus skipped odd codes -> UPRN count (NULL as 'NULL')."""
    import re

    onsud = Path(raw_dir).parent / "parquet" / "onsud" / "onsud.parquet"
    _require(onsud, "onsud")
    con = duckdb.connect()
    rows = con.execute(f"SELECT lad, count(*) FROM read_parquet('{onsud}') GROUP BY lad ORDER BY lad").fetchall()
    con.close()
    ok = lambda c: c is not None and re.match(_GB_LAD, c)  # noqa: E731
    good = [r[0] for r in rows if ok(r[0])]
    skipped = {("NULL" if r[0] is None else r[0]): r[1] for r in rows if not ok(r[0])}
    return good, skipped


def core_input_mtime(raw_dir: Path) -> float:
    """Newest mtime of the national inputs ``stage`` reads (a LAD staged before this is stale)."""
    pq = Path(raw_dir).parent / "parquet"
    return _newest_mtime(
        [
            pq / "onsud" / "onsud.parquet",
            pq / "open_uprn" / "open_uprn.parquet",
            pq / "lids" / "lids.parquet",
            pq / "open_toid" / "*.parquet",
            pq / "openmap_local" / "??.parquet",
        ]
    )


def step_input_mtime(step: str, raw_dir: Path) -> float:
    """Newest mtime of the source files/directories an optional staging step reads."""
    from uprn_resolver.streets import STREETS

    raw = Path(raw_dir)
    pq = raw.parent / "parquet"
    paths = {
        "HMLR": [raw / "hmlr"],
        "streets": [pq / STREETS],
        "INSPIRE": [pq / "inspire"],
        "ROS": [pq / "ros"],
        "EPC": [pq / "epc", pq / "epc_ew"],
    }.get(step, [])
    return _newest_mtime(paths)


def read_marker(lad_dir: Path) -> dict | None:
    """The LAD's stage marker, or None if absent.

    New markers are JSON ``{"version", "staged_at", "core_input_mtime", "steps": {name: {"status": "ok"|"failed",
    "error", "input_mtime"}}, "failed_steps": [...]}``. Any other content (the old ``ok\\n`` marker) is a *legacy*
    marker: the LAD is treated as complete and its input times default to the marker file's mtime."""
    f = Path(lad_dir) / DONE_MARKER
    if not f.exists():
        return None
    try:
        m = json.loads(f.read_text())
    except ValueError:
        m = None
    if not isinstance(m, dict) or "steps" not in m:
        return {"legacy": True, "core_input_mtime": f.stat().st_mtime, "steps": {}, "failed_steps": []}
    m.setdefault("failed_steps", [])
    return m


def _write_marker(lad_dir: Path, core_mtime: float, steps: dict) -> None:
    f = Path(lad_dir) / DONE_MARKER
    doc = {
        "version": 2,
        "staged_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "core_input_mtime": core_mtime,
        "steps": steps,
        "failed_steps": sorted(k for k, v in steps.items() if v.get("status") != "ok"),
    }
    tmp = f.with_name(f"{f.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2))
    os.replace(tmp, f)


def _stage_current(d: Path, marker: dict | None, raw_dir: Path) -> bool:
    """Core files present and not older than the national inputs."""
    if marker is None or not all((d / f"{c}.parquet").exists() for c in CORE_FILES):
        return False
    return core_input_mtime(raw_dir) <= marker.get("core_input_mtime", 0.0)


def _step_needs_run(marker: dict, step: str, raw_dir: Path) -> bool:
    if marker.get("legacy"):
        return step_input_mtime(step, raw_dir) > marker["core_input_mtime"]
    st = marker["steps"].get(step)
    if st is None or st.get("status") != "ok":
        return True
    return step_input_mtime(step, raw_dir) > st.get("input_mtime", 0.0)


def stage_all(
    raw_dir: Path, staged_dir: Path, lads: list[str] | None = None, skip_existing: bool = True, include_epc: bool = True
) -> dict:
    """Stage every GB LAD (or ``lads``). Resumable: LADs with a current stage marker are skipped.

    Optional sources (HMLR, streets, INSPIRE, ROS, EPC) warn and continue on failure; the ``.stage_done`` JSON
    marker records each step's status. A LAD is *not* marked when the required ``stage`` step fails. With
    ``skip_existing`` a LAD is re-staged when the national inputs are newer than the marker, and only the optional
    steps that failed, are missing from the marker, or whose sources changed are re-run otherwise.
    Returns {"staged", "skipped", "failed" (lad -> msg), "ignored" (odd codes -> UPRN count),
    "warnings" (lad -> [msgs]), "retried" (lad -> [steps re-run on an already staged LAD])}.
    """
    import sys

    from uprn_resolver import hmlr, inspire, streets

    all_lads, ignored = list_lads(raw_dir)
    partition_national(raw_dir, staged_dir)
    todo = list(lads) if lads else all_lads
    insp_idx = Path(staged_dir) / PART_DIR / "bbox_inspire.parquet"
    pcon = duckdb.connect()

    def _inspire(lad, raw, staged):
        files = None
        if insp_idx.exists():  # per-file bbox pruning: only parcels files overlapping the LAD extent
            bb = pcon.execute(
                f"SELECT min(x), min(y), max(x), max(y) FROM read_parquet('{Path(staged) / lad / 'open_uprn.parquet'}')"
            ).fetchone()
            if bb[0] is not None:
                files = [
                    Path(f)
                    for f in overlapping_files(
                        pcon, insp_idx, (bb[0] - BUFFER_M, bb[1] - BUFFER_M, bb[2] + BUFFER_M, bb[3] + BUFFER_M)
                    )
                ]
        inspire.stage_inspire(lad, raw, staged, files=files)

    steps = [("HMLR", hmlr.stage_hmlr), ("streets", streets.stage_streets), ("INSPIRE", _inspire)]
    try:
        from uprn_resolver import ros

        steps.append(("ROS", ros.stage_ros))
    except ImportError:
        pass
    if include_epc:
        from uprn_resolver import epc

        steps.append(("EPC", epc.stage_epc))
    res = {"staged": [], "skipped": [], "failed": {}, "ignored": ignored, "warnings": {}, "retried": {}}
    for i, lad in enumerate(todo, 1):
        d = Path(staged_dir) / lad
        t0 = time.time()
        marker = read_marker(d) if skip_existing else None
        run_steps = steps
        if skip_existing and _stage_current(d, marker, raw_dir):
            run_steps = [(w, f) for w, f in steps if _step_needs_run(marker, w, raw_dir)]
            if not run_steps:
                res["skipped"].append(lad)
                continue
            res["retried"][lad] = [w for w, _ in run_steps]
            core_mtime = marker["core_input_mtime"]
            states = {k: v for k, v in marker["steps"].items() if isinstance(v, dict)}
            if marker.get("legacy"):  # legacy marker: every step it covered counts as done at marker time
                states = {w: {"status": "ok", "input_mtime": marker["core_input_mtime"]} for w, _ in steps}
            (d / DONE_MARKER).unlink(missing_ok=True)  # incomplete until the re-run steps are recorded
        else:
            core_mtime = core_input_mtime(raw_dir)  # captured before reading, so later changes are detected
            states = {}
            (d / DONE_MARKER).unlink(missing_ok=True)  # forced/stale re-stage: not complete until it succeeds
            try:
                stage(lad, raw_dir, staged_dir)
            except Exception as e:
                res["failed"][lad] = f"{type(e).__name__}: {e}"
                print(f"[{i}/{len(todo)}] {lad} FAILED: {e}", file=sys.stderr, flush=True)
                continue
        for what, fn in run_steps:
            in_mtime = step_input_mtime(what, raw_dir)
            try:
                fn(lad, raw_dir, staged_dir)
                states[what] = {"status": "ok", "input_mtime": in_mtime}
            except Exception as e:
                states[what] = {"status": "failed", "error": f"{type(e).__name__}: {e}", "input_mtime": in_mtime}
                res["warnings"].setdefault(lad, []).append(f"{what}: {e}")
                print(f"warning: {what} staging failed for {lad}: {e}", file=sys.stderr, flush=True)
        _write_marker(d, core_mtime, states)
        res["staged"].append(lad)
        print(f"[{i}/{len(todo)}] {lad} staged in {time.time() - t0:.1f}s", flush=True)
    return res
