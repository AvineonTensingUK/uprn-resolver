import json
from pathlib import Path

import duckdb
import pytest

from uprn_resolver import accuracy

try:
    duckdb.connect().execute("LOAD spatial")
except Exception:  # pragma: no cover
    pytest.skip("duckdb spatial extension unavailable", allow_module_level=True)

LAD = "E09000001"
BLOCK = "POLYGON((0 0,40 0,40 20,0 20,0 0))"  # holds houses A and B
HOUSE_A = "POLYGON((1 1,9 1,9 9,1 9,1 1))"
HOUSE_B = "POLYGON((21 1,29 1,29 9,21 9,21 1))"
HOUSE_C = "POLYGON((101 1,109 1,109 9,101 9,101 1))"
PARCEL_C = "POLYGON((100 0,110 0,110 20,100 20,100 0))"


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    rel, pqd, work = tmp_path / "release" / "r1", tmp_path / "parquet", tmp_path / "work"
    con = duckdb.connect()
    con.execute("LOAD spatial")

    def write(path: Path, sql: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet)")

    write(
        rel / "resolved_open" / f"lad={LAD}" / "resolved_open.parquet",
        """SELECT * FROM (VALUES (1, 'openmap_building', 'blk', 'block'), (2, 'openmap_building', 'blk', 'block'),
           (3, 'inspire_parcel', 'pc', 'property'), (4, 'none', NULL, 'none'))
           t(uprn, best_outline_source, best_outline_id, outline_confidence)""",
    )
    write(
        rel / "buildings" / f"lad={LAD}" / "buildings.parquet",
        f"SELECT 'blk' AS building_id, ST_AsWKB(ST_GeomFromText('{BLOCK}')) AS geometry",
    )
    write(
        pqd / "osm" / "addresses.parquet",
        f"""SELECT * FROM (VALUES ('way', 10, '1', true, ST_AsWKB(ST_GeomFromText('{HOUSE_A}'))),
           ('way', 11, '2', true, ST_AsWKB(ST_GeomFromText('{HOUSE_B}'))),
           ('way', 12, '3', true, ST_AsWKB(ST_GeomFromText('{HOUSE_C}'))),
           ('node', 13, '4', false, NULL)) t(osm_type, osm_id, ref_gb_uprn, is_building, geom_wkb)""",
    )
    write(
        pqd / "inspire" / "x.parquet", f"SELECT 'pc' AS inspire_id, ST_AsWKB(ST_GeomFromText('{PARCEL_C}')) AS geom_wkb"
    )
    write(pqd / "ros" / "x.parquet", "SELECT 'none' AS inspire_id, NULL::BLOB AS geom_wkb")
    return rel, pqd, work


def test_block_is_not_exclusive_and_parcel_is(tmp_path):
    rel, pqd, work = _setup(tmp_path)
    assert accuracy.prepare(rel, pqd, work) == {"osm_building_tags": 3, "tagged_uprns": 3}
    assert accuracy.lad_rows(LAD, rel, pqd, work) == 3
    rows = {
        r[0]: r[1:]
        for r in duckdb.sql(
            f"SELECT uprn, inside, no_neighbour, round(outline_area / osm_area, 2) FROM '{work}/rows/lad={LAD}.parquet'"
        ).fetchall()
    }
    assert rows[1] == (True, False, 12.5)  # inside the block, but so is house B
    assert rows[3] == (True, True, 3.13)  # the parcel holds house C only, with a garden


def test_run_writes_summary_with_attribution(tmp_path):
    rel, pqd, work = _setup(tmp_path)
    out = tmp_path / "validation" / "accuracy.json"
    res = accuracy.run(rel, pqd, work, out)
    assert res["done"] == [LAD] and not res["failed"]
    s = json.loads(out.read_text())
    assert "OpenStreetMap" in s["attribution"] and s["release"] == "r1"
    by = {(g["source"], g["confidence"], g["building"]): g for g in s["groups"]}
    assert by[(None, "block", "single")]["exclusive_pct"] == 0.0
    assert by[(None, "property", "single")]["exclusive_pct"] == 100.0
    assert by[(None, None, None)]["n"] == 3
    assert accuracy.run(rel, pqd, work, out)["skipped"] == [LAD]


def test_lad_without_tags_matches_row_schema(tmp_path):
    rel, pqd, work = _setup(tmp_path)
    other = rel / "resolved_open" / "lad=E09000002" / "resolved_open.parquet"
    other.parent.mkdir(parents=True)
    duckdb.sql(
        f"""COPY (SELECT 99 AS uprn, 'none' AS best_outline_source, NULL AS best_outline_id,
            'none' AS outline_confidence) TO '{other}' (FORMAT parquet)"""
    )
    res = accuracy.run(rel, pqd, work, tmp_path / "a.json")
    assert sorted(res["done"]) == [LAD, "E09000002"]
    cols = lambda lad: [d[0] for d in duckdb.sql(f"SELECT * FROM '{work}/rows/lad={lad}.parquet'").description]  # noqa: E731
    assert cols("E09000002") == cols(LAD)
