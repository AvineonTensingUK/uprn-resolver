from pathlib import Path

import duckdb
import pytest

from uprn_resolver.stage import stage


def _wkb(x0, y0, x1, y1):
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    return con.execute(f"SELECT ST_AsWKB(ST_MakeEnvelope({x0},{y0},{x1},{y1}))").fetchone()[0]


def _pq(con, path: Path, select: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({select}) TO '{path}' (FORMAT PARQUET)")


@pytest.fixture
def national(tmp_path):
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    pq = tmp_path / "parquet"
    _pq(
        con,
        pq / "onsud/onsud.parquet",
        """SELECT * FROM (VALUES
        (1::BIGINT,'L1','o','l','m','AA1 1AA'), (2::BIGINT,'L1','o','l','m','AA1 1AB'),
        (3::BIGINT,'L2','o','l','m','BB1 1AA')) v(uprn,lad,oa,lsoa,msoa,postcode)""",
    )
    _pq(
        con,
        pq / "open_uprn/open_uprn.parquet",
        """SELECT * FROM (VALUES
        (1::BIGINT,1000.0,1000.0,51.0,-1.0),(2::BIGINT,1100.0,1200.0,51.1,-1.1),
        (3::BIGINT,9000.0,9000.0,52.0,-2.0),(4::BIGINT,1050.0,1050.0,51.0,-1.0))
        v(uprn,x,y,lat,lon)""",
    )
    _pq(
        con,
        pq / "lids/lids.parquet",
        """SELECT * FROM (VALUES
        (1::BIGINT,'t1','High'),(2::BIGINT,'t2','Low'),(3::BIGINT,'t3','High'),
        (4::BIGINT,'t4','High')) v(uprn,toid,lids_confidence)""",
    )
    _pq(
        con,
        pq / "open_toid/tq.parquet",
        """SELECT * FROM (VALUES
        ('t1',1.0,2.0),('t2',3.0,4.0),('t3',5.0,6.0)) v(toid,toid_x,toid_y)""",
    )
    rows = {
        "in": (1000, 1000, 1010, 1010),
        "edge": (1300, 1300, 1310, 1310),
        "touch": (1250, 1399, 1500, 1500),
        "out": (1301, 1401, 1500, 1500),
        "far": (5000, 5000, 5010, 5010),
    }
    (pq / "openmap_local").mkdir(parents=True)
    con.execute("CREATE TABLE b(building_id VARCHAR, geom_wkb BLOB)")
    for k, v in rows.items():
        con.execute("INSERT INTO b VALUES (?, ?)", [k, _wkb(*v)])
    con.execute(f"COPY b TO '{pq / 'openmap_local/tq.parquet'}' (FORMAT PARQUET)")
    # ImportantBuilding layer must be ignored
    con.execute("DELETE FROM b")
    con.execute("INSERT INTO b VALUES ('important', ?)", [_wkb(1000, 1000, 1010, 1010)])
    con.execute(f"COPY b TO '{pq / 'openmap_local/tq_important.parquet'}' (FORMAT PARQUET)")
    return tmp_path


def test_stage_filters_and_schemas(national):
    raw = national / "raw"
    staged = national / "staged"
    stage("L1", raw, staged)
    d = staged / "L1"

    def q(sql):
        return duckdb.sql(sql).fetchall()

    assert q(f"select uprn from '{d}/onsud.parquet' order by 1") == [(1,), (2,)]
    assert q(f"select uprn from '{d}/open_uprn.parquet' order by 1") == [(1,), (2,)]
    assert q(f"select uprn,toid from '{d}/lids_uprn_toid.parquet' order by 1") == [(1, "t1"), (2, "t2")]
    assert q(f"select toid from '{d}/open_toid.parquet' order by 1") == [("t1",), ("t2",)]
    # bbox x 1000..1100, y 1000..1200, +200 -> x<=1300, y<=1400, min 800
    assert q(f"select building_id from '{d}/buildings.parquet' order by 1") == [("edge",), ("in",), ("touch",)]
    types = {
        "onsud": ["BIGINT", "VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR"],
        "open_uprn": ["BIGINT", "DOUBLE", "DOUBLE", "DOUBLE", "DOUBLE"],
        "lids_uprn_toid": ["BIGINT", "VARCHAR", "VARCHAR"],
        "open_toid": ["VARCHAR", "DOUBLE", "DOUBLE"],
        "buildings": ["VARCHAR", "BLOB"],
    }
    for name, t in types.items():
        got = [r[1] for r in q(f"describe select * from '{d}/{name}.parquet'")]
        assert got == t, name


def test_stage_missing_input(national):
    (national / "parquet/lids/lids.parquet").unlink()
    with pytest.raises(FileNotFoundError, match="lids"):
        stage("L1", national / "raw", national / "staged")


def test_stage_all_resumable(national):
    from uprn_resolver.stage import list_lads, stage_all

    raw = national / "raw"
    lads, ignored = list_lads(raw)
    assert lads == [] and set(ignored) == {"L1", "L2"}  # fixture codes are not GB codes
    res = stage_all(raw, national / "staged", lads=["L1"], include_epc=False)
    assert res["staged"] == ["L1"] and not res["failed"]
    assert (national / "staged/L1/.stage_done").exists()
    res = stage_all(raw, national / "staged", lads=["L1", "L9"], include_epc=False)
    assert res["skipped"] == ["L1"] and "L9" in res["failed"]


def test_stage_buildings_deduped(national):
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    # same building present in two tiles (tile-edge duplicate)
    con.execute(
        f"COPY (SELECT 'in' building_id, ST_AsWKB(ST_MakeEnvelope(1000,1000,1010,1010)) geom_wkb) "
        f"TO '{national / 'parquet/openmap_local/zz.parquet'}' (FORMAT PARQUET)"
    )
    stage("L1", national / "raw", national / "staged")
    ids = [
        r[0]
        for r in duckdb.connect()
        .execute(f"SELECT building_id FROM '{national / 'staged/L1/buildings.parquet'}'")
        .fetchall()
    ]
    assert len(ids) == len(set(ids)) and "in" in ids


# --------------------------------------------------------------------------- review fixes (marker / staleness)
import json  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402

from uprn_resolver import stage as stage_mod  # noqa: E402
from uprn_resolver.stage import DONE_MARKER, read_marker, stage_all  # noqa: E402


def _later(path, seconds=100):
    t = time.time() + seconds
    os.utime(path, (t, t))


def test_marker_records_failed_optional_step_and_retries_only_it(national, monkeypatch):
    from uprn_resolver import hmlr

    raw, staged = national / "raw", national / "staged"

    def boom(*a, **k):
        raise RuntimeError("transient")

    monkeypatch.setattr(hmlr, "stage_hmlr", boom)
    res = stage_all(raw, staged, lads=["L1"], include_epc=False)
    assert res["staged"] == ["L1"] and "HMLR: transient" in res["warnings"]["L1"][0]
    m = json.loads((staged / "L1" / DONE_MARKER).read_text())
    assert m["failed_steps"] == ["HMLR"]
    assert m["steps"]["HMLR"]["status"] == "failed" and "transient" in m["steps"]["HMLR"]["error"]
    assert m["steps"]["streets"]["status"] == "ok" and m["steps"]["INSPIRE"]["status"] == "ok"
    onsud_mtime = (staged / "L1" / "onsud.parquet").stat().st_mtime_ns

    monkeypatch.undo()  # the source recovers: only the failed step is retried, the core files are left alone
    res = stage_all(raw, staged, lads=["L1"], include_epc=False)
    assert res["staged"] == ["L1"] and res["retried"] == {"L1": ["HMLR"]}
    assert (staged / "L1" / "onsud.parquet").stat().st_mtime_ns == onsud_mtime
    assert read_marker(staged / "L1")["failed_steps"] == []
    assert stage_all(raw, staged, lads=["L1"], include_epc=False)["skipped"] == ["L1"]


def test_no_marker_when_required_step_fails_and_forced_restage_clears_it(national, monkeypatch):
    raw, staged = national / "raw", national / "staged"
    stage_all(raw, staged, lads=["L1"], include_epc=False)
    assert (staged / "L1" / DONE_MARKER).exists()

    def boom(*a, **k):
        raise RuntimeError("OOM")

    monkeypatch.setattr(stage_mod, "stage", boom)
    res = stage_all(raw, staged, lads=["L1"], skip_existing=False, include_epc=False)
    assert "L1" in res["failed"] and not res["staged"]
    assert not (staged / "L1" / DONE_MARKER).exists()  # the old marker was removed at the start of the re-stage


def test_stage_removes_marker_and_stray_tmp_first(national):
    raw, staged = national / "raw", national / "staged"
    d = staged / "L1"
    d.mkdir(parents=True)
    (d / DONE_MARKER).write_text("ok\n")
    (d / "buildings.parquet.tmp").write_text("junk")
    (national / "parquet/lids/lids.parquet").unlink()  # required input missing: stage raises immediately
    with pytest.raises(FileNotFoundError):
        stage("L1", raw, staged)
    assert not (d / DONE_MARKER).exists()


def test_legacy_marker_is_complete(national):
    raw, staged = national / "raw", national / "staged"
    stage_all(raw, staged, lads=["L1"], include_epc=False)
    (staged / "L1" / DONE_MARKER).write_text("ok\n")  # old empty-style marker
    assert read_marker(staged / "L1")["legacy"] is True
    assert stage_all(raw, staged, lads=["L1"], include_epc=False)["skipped"] == ["L1"]


def test_stale_core_input_restages(national):
    raw, staged = national / "raw", national / "staged"
    stage_all(raw, staged, lads=["L1"], include_epc=False)
    _later(national / "parquet/onsud/onsud.parquet")  # a new national release lands
    res = stage_all(raw, staged, lads=["L1"], include_epc=False)
    assert res["staged"] == ["L1"] and not res["skipped"] and not res["retried"]
    assert stage_all(raw, staged, lads=["L1"], include_epc=False)["skipped"] == ["L1"]


def test_stale_optional_source_reruns_only_that_step(national):
    raw, staged = national / "raw", national / "staged"
    stage_all(raw, staged, lads=["L1"], include_epc=False)
    ins = national / "parquet" / "inspire"
    ins.mkdir()
    (ins / "README").write_text("new source dir, no parquet yet")
    _later(ins / "README", 200)
    res = stage_all(raw, staged, lads=["L1"], include_epc=False)
    assert res["retried"] == {"L1": ["INSPIRE"]} and res["staged"] == ["L1"]


def test_bbox_index_refreshes_for_new_and_changed_tiles(national):
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    tiles = national / "parquet/openmap_local"
    idx = national / "staged/_partitioned/bbox_openmap.parquet"
    files = sorted(tiles.glob("??.parquet"))
    stage_mod._bbox_index(con, idx, files, "geom_wkb", "building_id")
    assert stage_mod._bbox_index_fresh(con, idx, files)
    far = (9000, 9000, 9010, 9010)
    con.execute(
        f"COPY (SELECT 'newb' building_id, ST_AsWKB(ST_MakeEnvelope{far}) geom_wkb) TO '{tiles / 'zz.parquet'}' "
        "(FORMAT PARQUET)"
    )
    files2 = sorted(tiles.glob("??.parquet"))
    assert not stage_mod._bbox_index_fresh(con, idx, files2)  # a new tile is not in the cached index
    stage_mod._bbox_index(con, idx, files2, "geom_wkb", "building_id")
    assert any(f.endswith("zz.parquet") for f in stage_mod.overlapping_files(con, idx, (8000, 8000, 9500, 9500)))
    _later(files2[0])  # a re-converted tile
    assert not stage_mod._bbox_index_fresh(con, idx, files2)


def test_partition_national_repartitions_when_input_changes(national):
    con = duckdb.connect()
    pq = national / "parquet"
    con.execute(
        f"COPY (SELECT uprn, CASE WHEN lad='L1' THEN 'E00000001' ELSE 'E00000002' END lad, oa, lsoa, msoa, postcode "
        f"FROM '{pq / 'onsud/onsud.parquet'}') TO '{pq / 'onsud/o2.parquet'}' (FORMAT PARQUET)"
    )
    (pq / "onsud/onsud.parquet").unlink()
    (pq / "onsud/o2.parquet").rename(pq / "onsud/onsud.parquet")
    raw, staged = national / "raw", national / "staged"
    stage_mod.partition_national(raw, staged)
    ok = staged / "_partitioned/onsud/_ok"
    assert ok.exists() and list((staged / "_partitioned/onsud/lad=E00000001").glob("data*.parquet"))
    first = ok.stat().st_mtime_ns
    stage_mod.partition_national(raw, staged)
    assert ok.stat().st_mtime_ns == first  # unchanged inputs: nothing redone
    _later(pq / "onsud/onsud.parquet")
    assert not stage_mod._part_fresh(staged / "_partitioned", pq, "onsud")
    assert not stage_mod._part_fresh(staged / "_partitioned", pq, "open_uprn")
    stage_mod.partition_national(raw, staged)
    assert ok.stat().st_mtime_ns > first


def test_optional_stage_steps_remove_stale_outputs_when_source_absent(tmp_path):
    from uprn_resolver import epc, hmlr, inspire, streets

    raw, staged = tmp_path / "data" / "raw", tmp_path / "data" / "staged"
    d = staged / "L1"
    d.mkdir(parents=True)
    con = duckdb.connect()
    con.execute(f"COPY (SELECT 1::BIGINT uprn) TO '{d / 'onsud.parquet'}' (FORMAT PARQUET)")
    stale = ("epc", "hmlr_ppd_uprn", "hmlr_inspire", "streets", "inspire_parcel", "inspire_polygons")
    for n in stale:
        con.execute(f"COPY (SELECT 1::BIGINT uprn) TO '{d / (n + '.parquet')}' (FORMAT PARQUET)")
    for fn in (epc.stage_epc, hmlr.stage_hmlr, streets.stage_streets, inspire.stage_inspire):
        fn("L1", raw, staged)
    assert [n for n in stale if (d / f"{n}.parquet").exists()] == []
    assert (d / "onsud.parquet").exists()


def test_stage_inspire_keeps_ros_parcels(tmp_path):
    from uprn_resolver import inspire

    d = tmp_path / "staged" / "S1"
    d.mkdir(parents=True)
    duckdb.connect().execute(
        f"COPY (SELECT 1::BIGINT uprn, 'ros' parcel_source) TO '{d / 'inspire_parcel.parquet'}' (FORMAT PARQUET)"
    )
    inspire.stage_inspire("S1", tmp_path / "raw", tmp_path / "staged")
    assert (d / "inspire_parcel.parquet").exists()
