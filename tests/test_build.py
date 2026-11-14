import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from uprn_resolver.build import build, build_all, staged_lads

LAD = "E00000001"


def sq(x0, y0, s=10):
    import shapely.geometry as g

    return g.box(x0, y0, x0 + s, y0 + s).wkb


def _w(d, name, cols):
    pq.write_table(pa.table(cols), d / f"{name}.parquet")


@pytest.fixture
def staged(tmp_path):
    d = tmp_path / "staged" / LAD
    d.mkdir(parents=True)
    # Buildings A (0-10), B (100-110), C (200-210)
    _w(d, "buildings", {"building_id": ["A", "B", "C"], "geom_wkb": [sq(0, 0), sq(100, 0), sq(200, 0)]})
    uprns = [1, 2, 3, 4, 5, 6]
    _w(
        d,
        "onsud",
        {
            "uprn": uprns,
            "lad": [LAD] * 6,
            "oa": ["oa"] * 6,
            "lsoa": ["ls"] * 6,
            "msoa": ["ms"] * 6,
            "postcode": ["XX1 1XX"] * 6,
        },
    )
    pts = {
        1: (5, 5),
        2: (6, 6),  # stacked in A
        3: (105, 5),  # in B, TOID point in C -> disagree
        4: (50, 50),  # outside, TOID outside
        5: (500, 500),  # no TOID, outside
        6: (205, 5),
    }  # in C, TOID missing coords-> uprn method
    _w(
        d,
        "open_uprn",
        {
            "uprn": list(pts),
            "x": [float(v[0]) for v in pts.values()],
            "y": [float(v[1]) for v in pts.values()],
            "lat": [0.0] * 6,
            "lon": [0.0] * 6,
        },
    )
    _w(
        d,
        "lids_uprn_toid",
        {"uprn": [1, 2, 3, 4, 6, 6], "toid": ["t1", "t1", "t3", "t4", "t6b", "t6a"], "lids_confidence": ["hi"] * 6},
    )
    # t6a has no coordinates, t6b does (in C): rule prefers TOID with coords
    _w(
        d,
        "open_toid",
        {"toid": ["t1", "t3", "t4", "t6b"], "toid_x": [5.0, 205.0, 60.0, 205.0], "toid_y": [5.0, 5.0, 60.0, 5.0]},
    )
    return tmp_path / "staged"


def test_build(staged, tmp_path):
    out = tmp_path / "out"
    s = build(LAD, staged, out)
    rows = {r["uprn"]: r for r in pq.read_table(out / f"lad={LAD}" / "uprn.parquet").to_pylist()}
    assert rows[1]["match_method"] == "toid_point_in_polygon"
    assert rows[1]["building_id"] == rows[2]["building_id"] == "A"
    assert rows[2]["match_method"] == "toid_point_in_polygon"
    assert rows[1]["uprns_in_building"] == 2
    assert rows[3]["building_id"] == "C" and rows[3]["methods_disagree"] is True
    assert rows[4]["match_method"] == "none" and rows[4]["building_id"] is None
    assert rows[5]["toid"] is None and rows[5]["toid_count"] == 0
    assert rows[6]["toid"] == "t6b" and rows[6]["toid_count"] == 2
    assert rows[6]["building_id"] == "C" and rows[3]["uprns_in_building"] == 2
    assert rows[1]["methods_disagree"] is False
    assert rows[2]["methods_disagree"] is False
    assert rows[5]["methods_disagree"] is None
    assert "postcode" not in rows[1]
    assert rows[1]["ppd_transaction_ids"] is None and rows[1]["inspire_ids"] is None
    assert s["total_uprns"] == 6 and s["with_point"] == 6 and s["with_toid"] == 5
    assert s["match_method"]["none"]["count"] == 2  # uprns 4, 5
    assert s["methods_disagree"] == {"count": 1, "of_both_matched": 4, "pct": 25.0}
    assert s["uprns_in_building"]["2-5"] == 4
    assert s["uprns_in_building"]["1"] == 0
    assert json.loads((out / f"lad={LAD}" / "stats.json").read_text())["total_uprns"] == 6


def _check_geo(path, gtypes):
    md = pq.read_schema(path).metadata
    geo = json.loads(md[b"geo"])
    assert geo["version"] == "1.1.0" and geo["primary_column"] == "geometry"
    col = geo["columns"]["geometry"]
    assert col["encoding"] == "WKB" and col["crs"]["id"]["code"] == 27700
    assert set(col["geometry_types"]) == set(gtypes) and len(col["bbox"]) == 4
    assert pq.read_schema(path).field("geometry").type == pa.binary()


def test_geoparquet(staged, tmp_path):
    out = tmp_path / "out"
    build(LAD, staged, out)
    _check_geo(out / f"lad={LAD}" / "uprn.parquet", ["Point"])
    _check_geo(out / f"lad={LAD}" / "buildings.parquet", ["Polygon"])
    ids = pq.read_table(out / f"lad={LAD}" / "buildings.parquet").column("building_id").to_pylist()
    assert sorted(ids) == ["A", "C"]  # B loses to the TOID match for UPRN 3


def test_postcode_opt_in(staged, tmp_path):
    build(LAD, staged, tmp_path / "o", include_postcode=True)
    t = pq.read_table(tmp_path / "o" / f"lad={LAD}" / "uprn.parquet")
    assert t.column("postcode").to_pylist()[0] == "XX1 1XX"


def test_hmlr(staged, tmp_path):
    d = staged / LAD
    _w(d, "hmlr_ppd_uprn", {"transaction_id": ["t-a", "t-b", "t-c"], "uprn": [1, 1, 3]})
    _w(d, "hmlr_inspire", {"transaction_id": ["t-a", "t-b"], "inspire_id": ["i1", "i2"]})
    s = build(LAD, staged, tmp_path / "o")
    rows = {r["uprn"]: r for r in pq.read_table(tmp_path / "o" / f"lad={LAD}" / "uprn.parquet").to_pylist()}
    assert rows[1]["ppd_transaction_ids"] == ["t-a", "t-b"]
    assert rows[1]["inspire_ids"] == ["i1", "i2"]
    assert rows[3]["ppd_transaction_ids"] == ["t-c"] and rows[3]["inspire_ids"] is None
    assert s["with_ppd"]["count"] == 2 and s["with_inspire"]["count"] == 1


def test_settings_and_layout(staged, tmp_path):
    tmp = tmp_path / "ddbtmp"
    build(LAD, staged, tmp_path / "o", threads=2, memory_limit="1GB", temp_directory=tmp)
    assert tmp.is_dir() and (tmp_path / "o" / f"lad={LAD}" / "uprn.parquet").exists()


def test_build_all_resumable(staged, tmp_path):
    import shutil

    shutil.copytree(staged / LAD, staged / "E00000002")
    (staged / LAD / ".stage_done").write_text("ok\n")  # old-style marker: complete
    (staged / "E00000002" / ".stage_done").write_text("ok\n")
    (staged / "E00000003").mkdir()  # incomplete: ignored
    (staged / "E00000003" / "onsud.parquet").write_bytes(b"")
    assert staged_lads(staged) == [LAD, "E00000002"]
    out = tmp_path / "out"
    r = build_all(staged, out, threads=2)
    assert sorted(r["built"]) == [LAD, "E00000002"] and not r["skipped"] and not r["failed"]
    assert (out / "lad=E00000002" / "stats.json").exists()
    r = build_all(staged, out)
    assert r["skipped"] == [LAD, "E00000002"] and not r["built"]
    r = build_all(staged, out, lads=[LAD], skip_existing=False)
    assert list(r["built"]) == [LAD]
    r = build_all(staged, out, lads=["nope"])
    assert "nope" in r["failed"]


def test_optional_tables_absent(staged, tmp_path):
    s = build(LAD, staged, tmp_path / "o")
    o = tmp_path / "o" / f"lad={LAD}"
    rows = {r["uprn"]: r for r in pq.read_table(o / "uprn.parquet").to_pylist()}
    assert rows[1]["street_name"] is None and rows[1]["parcel_inspire_id"] is None
    assert rows[1]["epc_count"] is None and not (o / "epc.parquet").exists()
    assert rows[1]["outline_granularity"] == "shared" and rows[4]["outline_granularity"] is None
    assert s["with_street_name"]["count"] == 0 and s["with_epc"]["count"] == 0
    assert s["outline_granularity"] == {"single": 0, "parcel_finer": 0, "shared": 4, "none": 2}


def test_optional_tables_present(staged, tmp_path):
    d = staged / LAD
    _w(
        d,
        "streets",
        {
            "uprn": [1, 2, 3],
            "usrn": [10, 10, 11],
            "street_name": ["A St", "A St", "B Rd"],
            "street_name_alt": [None, None, None],
            "locality": ["L", "L", None],
            "street_method": ["lids"] * 3,
            "usrn_count": [1, 1, 1],
        },
    )
    # UPRN 1 and 2 are in building A; parcels split them -> parcel_finer
    _w(
        d,
        "inspire_parcel",
        {
            "uprn": [1, 2, 3],
            "parcel_inspire_id": ["p1", "p2", "p3"],
            "parcel_area_m2": [50.0, 60.0, 70.0],
            "uprns_in_parcel": [1, 1, 2],
        },
    )
    _w(
        d,
        "epc",
        {
            "uprn": [1, 3],
            "built_form": ["Detached", "Terrace"],
            "property_type": ["House", "Flat"],
            "total_floor_area": [80.0, 50.0],
            "epc_count": [2, 1],
        },
    )
    s = build(LAD, staged, tmp_path / "o")
    o = tmp_path / "o" / f"lad={LAD}"
    rows = {r["uprn"]: r for r in pq.read_table(o / "uprn.parquet").to_pylist()}
    assert rows[1]["street_name"] == "A St" and rows[3]["usrn"] == 11
    assert rows[1]["outline_granularity"] == "parcel_finer"
    assert rows[3]["outline_granularity"] == "shared"  # building C has 2 UPRNs, parcel has 2
    assert rows[1]["epc_built_form"] == "Detached" and rows[1]["epc_count"] == 2
    assert "total_floor_area" not in rows[1]
    epc = pq.read_table(o / "epc.parquet")
    assert sorted(epc.column("uprn").to_pylist()) == [1, 3] and "total_floor_area" in epc.column_names
    assert s["with_street_name"]["count"] == 3 and s["with_parcel"]["count"] == 3
    assert s["with_epc"]["count"] == 2 and s["epc_built_form"] == {"Detached": 1, "Terrace": 1}
    assert s["uprns_in_parcel"] == {"1": 2, "2-5": 1, "6-20": 0, ">20": 0}


# --------------------------------------------------------------------------- review fixes
def _mark(staged, lad=LAD):
    (staged / lad / ".stage_done").write_text("ok\n")


def test_staged_lads_requires_marker_and_reports_unstaged(staged, tmp_path):
    from uprn_resolver.build import unstaged_lads

    assert staged_lads(staged) == [] and unstaged_lads(staged) == [LAD]
    r = build_all(staged, tmp_path / "out")
    assert not r["built"] and LAD in r["not_staged"] and ".stage_done" in r["not_staged"][LAD]
    r = build_all(staged, tmp_path / "out", lads=[LAD])  # explicit request: failure with a clear message
    assert not r["built"] and ".stage_done" in r["failed"][LAD]
    _mark(staged)  # old empty marker counts as complete
    assert staged_lads(staged) == [LAD] and unstaged_lads(staged) == []
    assert list(build_all(staged, tmp_path / "out")["built"]) == [LAD]


def test_failed_optional_steps_surface_as_warnings(staged, tmp_path):
    (staged / LAD / ".stage_done").write_text(json.dumps({"version": 2, "steps": {}, "failed_steps": ["EPC"]}))
    r = build_all(staged, tmp_path / "out")
    assert list(r["built"]) == [LAD] and "EPC" in r["warnings"][LAD][0]


def test_crash_mid_rebuild_leaves_no_marker(staged, tmp_path, monkeypatch):
    import uprn_resolver.build as B

    _mark(staged)
    out = tmp_path / "out"
    build(LAD, staged, out)
    d = out / f"lad={LAD}"
    assert (d / "stats.json").exists()

    real = B._write

    def dying(con, query, path, geo):
        if path.name == "buildings.parquet":
            raise MemoryError("OOM")  # uprn.parquet was already rewritten
        return real(con, query, path, geo)

    monkeypatch.setattr(B, "_write", dying)
    r = build_all(staged, out, skip_existing=False)
    assert LAD in r["failed"]
    assert not (d / "stats.json").exists()  # marker removed BEFORE any output was written
    monkeypatch.setattr(B, "_write", real)
    r = build_all(staged, out)  # default (skip_existing) run must rebuild the LAD
    assert list(r["built"]) == [LAD] and (d / "stats.json").exists()
    assert not list(d.glob("*.tmp"))


def test_build_cleans_stray_tmp_and_writes_marker_last(staged, tmp_path, monkeypatch):
    import uprn_resolver.build as B

    out = tmp_path / "out"
    d = out / f"lad={LAD}"
    d.mkdir(parents=True)
    for n in ("uprn.parquet.tmp", "buildings.parquet.tmp", "stats.json.tmp", "epc.parquet.tmp"):
        (d / n).write_text("junk")
    (d / "stats.json").write_text("{}")
    seen = {}
    real = B.write_text_atomic

    def spy(path, text):
        seen["files"] = sorted(p.name for p in d.iterdir())
        return real(path, text)

    monkeypatch.setattr(B, "write_text_atomic", spy)
    build(LAD, staged, out)
    assert not list(d.glob("*.tmp"))
    assert (
        "uprn.parquet" in seen["files"] and "buildings.parquet" in seen["files"] and "stats.json" not in seen["files"]
    )


def test_stage_marker_newer_than_stats_triggers_rebuild(staged, tmp_path):
    import os
    import time

    _mark(staged)
    out = tmp_path / "out"
    assert list(build_all(staged, out)["built"]) == [LAD]
    assert build_all(staged, out)["skipped"] == [LAD]
    t = time.time() + 100
    os.utime(staged / LAD / ".stage_done", (t, t))  # LAD re-staged (e.g. a failed optional step retried)
    assert list(build_all(staged, out)["built"]) == [LAD]


def test_deterministic_duplicate_choice(staged, tmp_path):
    """Duplicate ONSUD rows: the same row wins whatever the input order."""
    d = staged / LAD
    t = pq.read_table(d / "onsud.parquet").to_pylist()
    dup = dict(t[0], postcode="AA0 0AA", oa="oa0")
    results = []
    for order in (t + [dup], [dup] + t):
        pq.write_table(pa.Table.from_pylist(order), d / "onsud.parquet")
        out = tmp_path / f"o{len(results)}"
        build(LAD, staged, out, include_postcode=True)
        row = next(r for r in pq.read_table(out / f"lad={LAD}" / "uprn.parquet").to_pylist() if r["uprn"] == 1)
        results.append((row["postcode"], row["oa"]))
    assert results[0] == results[1] == ("AA0 0AA", "oa0")


def test_overlapping_buildings_smallest_area_wins(staged, tmp_path):
    d = staged / LAD
    t = pq.read_table(d / "buildings.parquet").to_pylist()
    t += [
        {"building_id": "AA", "geom_wkb": sq(450, 450, 100)},  # big, smaller id
        {"building_id": "ZZ", "geom_wkb": sq(495, 495, 10)},  # small, larger id: UPRN 5 at (500, 500) is in both
    ]
    pq.write_table(pa.Table.from_pylist(t), d / "buildings.parquet")
    out = tmp_path / "o"
    build(LAD, staged, out)
    rows = {r["uprn"]: r for r in pq.read_table(out / f"lad={LAD}" / "uprn.parquet").to_pylist()}
    assert rows[5]["building_id"] == "ZZ"
