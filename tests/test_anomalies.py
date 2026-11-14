import csv
import json

import duckdb
import pytest

from uprn_resolver.anomalies import PUBLISHERS, SPECS, main, render_markdown, run

CLUSTER = 120


@pytest.fixture(scope="module")
def pq(tmp_path_factory):
    root = tmp_path_factory.mktemp("w") / "parquet"
    con = duckdb.connect()
    con.execute("LOAD spatial") if False else None
    try:
        con.execute("LOAD spatial")
    except duckdb.Error:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
    for d in (
        "onsud",
        "open_uprn",
        "lids",
        "open_toid",
        "open_usrn",
        "streets",
        "open_roads",
        "hmlr",
        "inspire",
        "codepoint",
    ):
        (root / d).mkdir(parents=True)
    (root / "epc_ew" / "domestic").mkdir(parents=True)
    (root / "epc_ew" / "non_domestic").mkdir(parents=True)
    (root / "epc").mkdir()

    def w(sql, path):
        con.execute(f"COPY ({sql}) TO '{root / path}' (FORMAT parquet)")

    # uprns 1..120 in LAD E1 around (100000,100000) with a 20 m spacing, 1001..1120 in E2 around (500000,400000)
    # 999: ONSUD says E1 but the point sits in E2's cluster; 888: in ONSUD only; 777: Open UPRN only
    pts = (
        "SELECT i AS uprn, 'E1' AS lad, 100000.0 + (i % 12) * 20 AS x, 100000.0 + (i // 12) * 20 AS y, 'AB1 1AA' AS pc "
        f"FROM range(1, {CLUSTER + 1}) t(i) UNION ALL "
        "SELECT i, 'E2', 500000.0 + (i % 12) * 20, 400000.0 + ((i - 1000) // 12) * 20, 'ZZ9 9ZZ' "
        f"FROM range(1001, {1000 + CLUSTER + 1}) t(i) UNION ALL SELECT 999, 'E1', 500100.0, 400100.0, 'AB1 1AA'"
    )
    w(f"SELECT uprn, lad, pc AS postcode FROM ({pts}) UNION ALL SELECT 888, 'E2', 'ZZ9 9ZZ'", "onsud/onsud.parquet")
    w(
        f"SELECT uprn, x, y, 0.0 AS lat, 0.0 AS lon FROM ({pts}) UNION ALL SELECT 777, 400000.0, 300000.0, 1.0, 1.0",
        "open_uprn/open_uprn.parquet",
    )
    # LIDS: uprn 1 -> T1 (point 10 m away), uprn 2 -> T2 (point 500 m away), uprn 3 -> T3 (absent from Open TOID)
    w("SELECT * FROM (VALUES (1, 'T1'), (2, 'T2'), (3, 'T3')) v(uprn, toid)", "lids/lids.parquet")
    w(
        "SELECT * FROM (VALUES ('T1', 100000.0 + 10, 100000.0), ('T2', 100000.0 + 20 + 500, 100000.0)) v(toid, toid_x, toid_y)",
        "open_toid/sy.parquet",
    )
    # streets: uprn 1 -> USRN 10 (line 30 m away), uprn 2 -> 10 too but we move it far via usrn 11 line; uprn 3 -> USRN 12 (absent)
    w("SELECT * FROM (VALUES (1, 10), (2, 11), (3, 12)) v(uprn, usrn)", "streets/uprn_usrn.parquet")
    w("SELECT * FROM (VALUES ('R1', 10), ('R2', 13)) v(toid, usrn)", "streets/road_usrn.parquet")
    w("SELECT 'R1' AS toid, 'High St' AS name1", "streets/open_names.parquet")
    w(
        "SELECT 'R2' AS toid, 'Named Rd' AS name_1, NULL AS name_2, 0.0 AS xmin",
        "open_roads/open_roads.parquet",
    )
    w(
        "SELECT usrn, 'x' AS street_type, ST_AsWKB(ST_GeomFromText(wkt)) AS geom_wkb FROM (VALUES "
        "(10, 'LINESTRING(99000 100030, 101000 100030)'), (11, 'LINESTRING(300000 300000, 300100 300000)'), "
        "(13, 'LINESTRING(0 0, 1 1)')) v(usrn, wkt)",
        "open_usrn/open_usrn.parquet",
    )
    # HMLR
    w(
        "SELECT * FROM (VALUES ('{A}', '1'), ('{B}', '55555')) v(transaction_id, value)",
        "hmlr/pp-uprn-lookup-test.parquet",
    )
    w(
        "SELECT * FROM (VALUES ('{A}', 'P1'), ('{B}', 'P2')) v(transaction_id, value)",
        "hmlr/pp-inspire-id-lookup-test.parquet",
    )
    w("SELECT 'P1' AS inspire_id, ST_AsWKB(ST_Point(0, 0)) AS geom_wkb", "inspire/a.parquet")
    # Code-Point: AB1 1AA near the E1 cluster; ZZ9 9ZZ is not published (ONSUD-only)
    w("SELECT 'AB1 1AA' AS postcode, 100100.0 AS x, 100100.0 AS y", "codepoint/codepoint.parquet")
    # EPC: uprn 1 correct postcode; uprn 2 lodged with AB1 1AA but... point is 100 m away so make uprn 1001 far (400 km)
    w(
        "SELECT * FROM (VALUES ('1', 'ab11aa', 'ADDR SHOULD NEVER LEAK'), ('1001', 'AB1 1AA', 'ADDR2'), ('424242', 'AB1 1AA', 'ADDR3')) v(uprn, postcode, address)",
        "epc_ew/domestic/certificates-2020.parquet",
    )
    w("SELECT '2' AS uprn, 'AB1 1AA' AS postcode", "epc_ew/non_domestic/certificates-2020.parquet")
    w("SELECT 1 AS uprn", "epc/s_domestic_2020.parquet")
    return root


@pytest.fixture(scope="module")
def result(pq, tmp_path_factory):
    out = tmp_path_factory.mktemp("o")
    s = run(pq, out, memory_limit="1GB")
    return s, {r["id"]: r for r in s["anomalies"]}, out


def _ids(out, name, col):
    with open(out / f"{name}.csv", newline="") as f:
        return {row[col] for row in csv.DictReader(f)}


def test_all_ok(result):
    s, by, _ = result
    assert {r["id"] for r in s["anomalies"]} == {sp.id for sp, _ in SPECS}
    assert all(r["status"] == "ok" for r in s["anomalies"]), {k: v.get("reason") for k, v in by.items()}


def test_onsud_open_uprn_sets(result):
    _, by, out = result
    assert _ids(out, "onsud_uprn_not_in_open_uprn", "uprn") == {"888"}
    assert _ids(out, "open_uprn_not_in_onsud", "uprn") == {"777"}
    assert by["open_uprn_no_coordinates"]["count"] == 0
    assert by["onsud_uprn_not_in_open_uprn"]["base"] == 2 * CLUSTER + 2


def test_lad_outliers(result):
    _, by, out = result
    assert _ids(out, "uprn_far_from_onsud_lad", "uprn") == {"999"}
    assert _ids(out, "uprn_inside_other_lad_territory", "uprn") == {"999"}
    assert by["uprn_far_from_onsud_lad"]["extra"]["over_300km_from_lad_median"] == 1


def test_lids_toid_and_street(result):
    _, by, out = result
    assert _ids(out, "lids_topo_toid_far_from_uprn", "uprn") == {"2"}
    assert by["lids_topo_toid_far_from_uprn"]["base"] == 2
    assert by["lids_topo_toid_far_from_uprn"]["extra"]["over_1km"] == 0
    assert _ids(out, "lids_toid_missing_from_open_toid", "toid") == {"T3"}
    # uprn 1 is 30 m from USRN 10; uprn 2 is ~200 km from USRN 11; USRN 12 has no geometry so is not in the base
    assert _ids(out, "uprn_far_from_usrn_street", "uprn") == {"2"}
    assert by["uprn_far_from_usrn_street"]["base"] == 2
    with open(out / "uprn_far_from_usrn_street.csv") as f:
        assert float(next(csv.DictReader(f))["dist_m"]) > 100_000
    assert _ids(out, "usrn_missing_from_open_usrn", "usrn") == {"12"}
    assert _ids(out, "road_toid_missing_from_open_names", "toid") == {"R2"}
    assert by["road_toid_missing_from_open_names"]["extra"]["named_in_open_roads"] == 1


def test_hmlr_epc_codepoint(result):
    _, by, out = result
    assert _ids(out, "hmlr_pp_uprn_not_in_onsud", "transaction_id") == {"{B}"}
    assert _ids(out, "hmlr_pp_inspire_not_in_polygons", "transaction_id") == {"{B}"}
    assert _ids(out, "epc_uprn_not_in_onsud", "uprn") == {"424242"}
    # 1001 is ~400 km from the AB1 1AA centroid; uprn 1 (postcode written 'ab11aa') is 100 m away
    assert _ids(out, "epc_uprn_far_from_postcode_centroid", "uprn") == {"1001"}
    cp = by["onsud_postcode_not_in_codepoint"]
    assert cp["count"] == 1 and cp["base"] == 2 and cp["extra"]["uprns_affected"] == CLUSTER + 1
    assert cp["examples"] == [] and "csv" not in cp
    assert not (result[2] / "onsud_postcode_not_in_codepoint.csv").exists()


def test_no_address_text_in_outputs(result):
    _, _, out = result
    blob = "".join(p.read_text() for p in out.iterdir())
    for needle in ("ADDR", "AB1 1AA", "ab11aa", "ZZ9 9ZZ", "NEVER LEAK"):
        assert needle not in blob


def test_missing_inputs_skipped(tmp_path):
    (tmp_path / "parquet").mkdir()
    s = run(tmp_path / "parquet", tmp_path / "out", only=["open_uprn_not_in_onsud"], memory_limit="1GB")
    assert s["anomalies"][0]["status"] == "skipped"


def test_render_and_cli(pq, tmp_path):
    doc = tmp_path / "d.md"
    main(["--parquet-dir", str(pq), "--out-dir", str(tmp_path / "o"), "--memory-limit", "1GB", "--doc", str(doc)])
    text = doc.read_text()
    assert "## Headline" in text and "Where to report" in text and "Covering note" in text
    s = json.loads((tmp_path / "o" / "summary.json").read_text())
    main(["--out-dir", str(tmp_path / "o"), "--doc", str(tmp_path / "e.md"), "--doc-only"])
    assert (tmp_path / "e.md").read_text() == render_markdown(s, PUBLISHERS)
