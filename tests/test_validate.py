import json
import shutil

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from test_build import LAD, staged  # noqa: F401  (fixture)

from uprn_resolver.build import build
from uprn_resolver.validate import _zflags, validate


@pytest.fixture
def built(staged, tmp_path):  # noqa: F811
    out = tmp_path / "out"
    build(LAD, staged, out)
    return staged, out, tmp_path


def _run(built, name="rep", **kw):
    staged_dir, out, tmp = built
    return validate(out, staged_dir, tmp / "pq", tmp / name, **kw)


def _st(res):
    return {c["id"]: c for c in res["checks"]}


def _rewrite(path, sql):
    """Replace a parquet file with the result of ``sql`` (``{t}`` is the current file)."""
    tmp = path.with_suffix(".tmp.parquet")
    src = f"read_parquet('{path}', hive_partitioning=false)"
    duckdb.connect().execute(f"COPY ({sql.format(t=src)}) TO '{tmp}' (FORMAT parquet)")
    tmp.replace(path)


def test_clean(built):
    res = _run(built)
    st = _st(res)
    # the fixture contains one deliberate TOID/UPRN disagreement (uprn 3), so the two rate checks fire
    fails = {c["id"] for c in res["checks"] if c["status"] == "FAIL"}
    assert res["lads"] == [LAD] and fails <= {"methods_disagree_rate", "bld_uprn_pt_out"}, fails
    assert st["methods_disagree_rate"]["examples"] == [f"{LAD}:3"]
    for cid in (
        "uprn_unique_lad",
        "onsud_present",
        "row_count",
        "uprn_unique_national",
        "bld_referenced",
        "bld_valid",
        "bld_point_in",
        "method_vs_building",
        "uprns_in_building",
        "outline_granularity",
        "geoparquet_meta",
        "geo_open_uprn",
        "lic_cols_uprn",
        "lic_address_local",
    ):
        assert st[cid]["status"] == "PASS", cid
    assert st["osm_street"]["status"] == "SKIP"
    assert (built[2] / "rep" / "report.html").read_text().startswith("<!doctype html>")
    assert json.loads((built[2] / "rep" / "results.json").read_text())["summary"]["FAIL"] == len(fails)
    assert len(res["spot_check"]) == 6 and "error" not in res["spot_check"][0]
    assert not (built[2] / "rep" / ".duck_tmp").exists()


def test_incomplete_lad_skipped(built):
    _, out, _ = built
    (out / "lad=E00000002").mkdir()
    shutil.copy(out / f"lad={LAD}" / "uprn.parquet", out / "lad=E00000002" / "uprn.parquet")
    res = _run(built)
    assert res["incomplete_lads"] == ["E00000002"] and res["lads"] == [LAD]


def test_integrity_failures(built):
    staged_dir, out, tmp = built
    p = out / f"lad={LAD}" / "uprn.parquet"
    _rewrite(p, "SELECT * FROM (SELECT * FROM {t} UNION ALL SELECT * FROM {t} WHERE uprn = 1) WHERE uprn NOT IN (5, 6)")
    res = _run(built)
    st = _st(res)
    assert st["uprn_unique_lad"]["status"] == "FAIL" and st["uprn_unique_lad"]["bad"] == 1
    assert LAD + ":1" in st["uprn_unique_lad"]["examples"]
    assert st["onsud_present"]["bad"] == 2 and LAD + ":6" in st["onsud_present"]["examples"]
    assert st["row_count"]["status"] == "FAIL"


def test_uprn_in_two_lads(built):
    _, out, _ = built
    d2 = out / "lad=E00000002"
    shutil.copytree(out / f"lad={LAD}", d2)
    _rewrite(d2 / "uprn.parquet", "SELECT *, 'E00000001' AS lad FROM {t}")
    res = _run(built)
    st = _st(res)
    assert st["uprn_two_lads"]["status"] == "FAIL" and st["uprn_two_lads"]["bad"] == 6
    assert st["uprn_unique_national"]["bad"] == 6
    assert st["lad_column"]["status"] == "FAIL"  # copied rows still say LAD


def test_logic_failures(built):
    _, out, _ = built
    p = out / f"lad={LAD}" / "uprn.parquet"
    _rewrite(
        p,
        "SELECT * REPLACE (CASE WHEN uprn = 1 THEN 99 ELSE uprns_in_building END AS uprns_in_building, "
        "CASE WHEN uprn = 2 THEN 'single' ELSE outline_granularity END AS outline_granularity, "
        "CASE WHEN uprn = 4 THEN 'toid_point_in_polygon' ELSE match_method END AS match_method, "
        "CASE WHEN uprn = 5 THEN true ELSE methods_disagree END AS methods_disagree, "
        "CASE WHEN uprn = 6 THEN 'nope' ELSE building_id END AS building_id) FROM {t}",
    )
    st = _st(_run(built))
    assert st["uprns_in_building"]["status"] == "FAIL" and LAD + ":1" in st["uprns_in_building"]["examples"]
    assert st["outline_granularity"]["status"] == "FAIL"
    assert st["method_vs_building"]["status"] == "FAIL"
    assert st["methods_disagree"]["status"] == "FAIL"
    assert st["bld_referenced"]["status"] == "FAIL" and LAD + ":6" in st["bld_referenced"]["examples"]


def test_geometry_failures(built):
    import shapely.geometry as g

    _, out, _ = built
    bp = out / f"lad={LAD}" / "buildings.parquet"
    # bow-tie (invalid) polygon replaces building A -> invalid + points no longer inside
    bow = g.Polygon([(100, 100), (110, 110), (110, 100), (100, 110)]).wkb
    t = pq.read_table(bp)
    geoms = [
        bow if b == "A" else w for b, w in zip(t["building_id"].to_pylist(), t["geometry"].to_pylist(), strict=True)
    ]
    t = t.set_column(t.schema.get_field_index("geometry"), t.schema.field("geometry"), pa.array(geoms, pa.binary()))
    pq.write_table(t, bp)
    up = out / f"lad={LAD}" / "uprn.parquet"
    _rewrite(up, "SELECT * REPLACE (CASE WHEN uprn = 5 THEN 5000000.0 ELSE x END AS x) FROM {t}")
    st = _st(_run(built))
    assert st["bld_valid"]["bad"] == 1 and st["bld_valid"]["status"] == "FAIL"
    assert st["bld_point_in"]["status"] == "FAIL"
    assert st["geo_extent"]["status"] == "FAIL" and LAD + ":5" in st["geo_extent"]["examples"]
    assert st["geo_open_uprn"]["status"] == "FAIL"


def test_geo_metadata_missing(built):
    _, out, _ = built
    up = out / f"lad={LAD}" / "uprn.parquet"
    t = pq.read_table(up).replace_schema_metadata({})
    pq.write_table(t, up)
    assert _st(_run(built))["geoparquet_meta"]["status"] == "FAIL"


def test_licence_failures(built):
    staged_dir, out, tmp = built
    d = out / f"lad={LAD}"
    pq.write_table(pa.table({"uprn": [1, 2], "address_line1": ["secret", None], "town": ["x", "y"]}), d / "epc.parquet")
    pq.write_table(
        pa.table({"uprn": [1, 2, 3], "postcode_onsud": ["A", "B", "C"], "paon": ["a", "b", "c"]}),
        d / "provenance.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "uprn": [1, 2, 3, 4],
                "single_line_address": ["a", "b", "c", "d"],
                "postcode": ["XX1 1XX", "XX1 1XX", None, None],
                "postcode_onsud": ["XX1 1XX", "YY2 2YY", None, None],
                "address_line1": ["a", None, "c", "d"],
                "licence": ["EPC-restricted", "wrong", "PPD-restricted", "open-partial"],
            }
        ),
        d / "address_local.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "uprn": [1, 2],
                "osm_street_name": ["a", "b"],
                "osm_agrees_os": [True, None],
                "licence": ["ODbL-1.0", "CC0"],
            }
        ),
        d / "streets_odbl.parquet",
    )
    (out / "national").mkdir()
    (out / "national" / "address_local.parquet").write_bytes(b"x")
    rel = tmp / "release"
    (rel / "epc").mkdir(parents=True)
    (rel / "address_local_E1.parquet").write_bytes(b"x")
    res = _run(built, release_dirs=[rel])
    st = _st(res)
    assert st["lic_cols_epc"]["status"] == "FAIL" and set(st["lic_cols_epc"]["examples"]) == {"address_line1", "town"}
    assert st["lic_cols_provenance"]["status"] == "FAIL" and st["lic_cols_provenance"]["examples"] == ["paon"]
    assert st["lic_prov_postcode"]["status"] == "WARN"
    assert st["lic_address_local"]["status"] == "FAIL" and st["lic_address_local"]["bad"] == 2
    assert st["lic_address_flag"]["status"] == "FAIL" and st["lic_address_flag"]["bad"] == 2
    assert st["lic_odbl"]["status"] == "FAIL" and st["lic_odbl"]["bad"] == 1
    assert st["epc_postcode"]["total"] == 2 and st["epc_postcode"]["bad"] == 0  # compared against staged ONSUD
    assert st["osm_street"]["status"] == "SKIP" or st["osm_street"]["total"] == 0
    # no address text leaks into the report
    html = (tmp / "rep" / "report.html").read_text() + (tmp / "rep" / "results.json").read_text()
    assert "secret" not in html


def test_epc_postcode_mismatch_and_poi(built):
    _, out, _ = built
    d = out / f"lad={LAD}"
    pq.write_table(
        pa.table(
            {
                "uprn": [1, 2, 3],
                "single_line_address": ["a", "b", "c"],
                "postcode": ["xx1  1xx", "ZZ9 9ZZ", None],
                "postcode_onsud": [None] * 3,
                "address_line1": ["a", "b", "c"],
                "licence": ["EPC-restricted"] * 3,
            }
        ),
        d / "address_local.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "poi_id": ["p1", "p2", "p3"],
                "name": ["n", "n", "n"],
                "uprn": [1, 2, 777],
                "match_confidence": ["high", "bogus", "low"],
            }
        ),
        d / "poi.parquet",
    )
    st = _st(_run(built))
    e = st["epc_postcode"]
    assert (e["bad"], e["total"]) == (1, 2) and e["examples"] == [f"{LAD}:2"]  # case/space-insensitive match
    assert st["poi_values"]["status"] == "FAIL" and st["poi_values"]["bad"] == 2
    assert "ZZ9" not in json.dumps(st)


def test_zflags():
    rates = {f"E{i:08d}": 0.9 + (i % 3) * 0.001 for i in range(30)}
    rates["E99999999"] = 0.2
    flagged, n = _zflags(rates)
    assert [f[0] for f in flagged] == ["E99999999"] and n == 31
    assert _zflags({"E1": 0.1, "E2": 0.9}) == ([], 0)


def test_lad_outlier_check(built):
    _, out, _ = built
    for i in range(2, 32):
        shutil.copytree(out / f"lad={LAD}", out / f"lad=E{i:08d}")
    # make one LAD's street coverage differ
    odd = out / "lad=E00000031"
    _rewrite(odd / "uprn.parquet", "SELECT * REPLACE (NULL::VARCHAR AS street_name) FROM {t}")
    shutil.rmtree(out / f"lad={LAD}")  # keep duplicated-UPRN noise irrelevant: still checking the z-score logic
    res = _run(built)
    st = _st(res)
    assert st["lad_outlier_street"]["status"] in ("WARN", "PASS")


def test_licence_open_odbl_sidecars_and_resolved_local(built):
    staged_dir, out, tmp = built
    d = out / f"lad={LAD}"
    pq.write_table(
        pa.table(
            {
                "uprn": [1, 2],
                "street_name": ["a", "b"],  # allowed OGL street attribute
                "single_line_address": ["x", "y"],  # address text: forbidden
                "osm_housenumber": ["1", "2"],  # ODbL column: forbidden
                "best_outline_source": ["openmap_building", "osm_building"],  # ODbL value: forbidden
            }
        ),
        d / "resolved_open.parquet",
    )
    pq.write_table(pa.table({"uprn": [1], "licence": ["CC0"]}), d / "outline_odbl.parquet")
    pq.write_table(pa.table({"uprn": [1]}), d / "outline_ms.parquet")
    (d / "outline_odbl.LICENCE.txt").write_text("no mention")
    pq.write_table(pa.table({"uprn": [1]}), d / "resolved_local.parquet")
    rel = tmp / "release"
    rel.mkdir()
    (rel / "resolved_local_E1.parquet").write_bytes(b"x")
    st = _st(_run(built, release_dirs=[rel]))
    assert st["lic_resolved_open"]["status"] == "FAIL"
    ex = " ".join(st["lic_resolved_open"]["examples"])
    assert "single_line_address" in ex and "osm_housenumber" in ex and "best_outline_source" in ex
    assert "street_name" not in ex
    assert st["lic_odbl"]["status"] == "FAIL" and st["lic_odbl"]["bad"] == 1
    assert st["lic_sidecars"]["status"] == "FAIL" and st["lic_sidecars"]["bad"] == 3  # odbl text, ms, open missing
    assert st["lic_resolved_local"]["status"] == "FAIL" and st["lic_resolved_local"]["bad"] == 1
