"""Tests for uprn_resolver.provenance on synthetic data."""

import duckdb
import pytest

from uprn_resolver import provenance as pv


def _line(x0, y0, x1, y1):
    return f"ST_AsWKB(ST_GeomFromText('LINESTRING({x0} {y0}, {x1} {y1})'))"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "MIN_FREE_GB", 0)
    pq, staged = tmp_path / "parquet", tmp_path / "staged" / "E1"
    staged.mkdir(parents=True)
    for d in ("open_usrn", "codepoint", "open_roads"):
        (pq / d).mkdir(parents=True)
    con = duckdb.connect()
    try:
        con.execute("LOAD spatial")
    except duckdb.Error:
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")

    def copy(sql, path):
        con.execute(f"COPY ({sql}) TO '{path}' (FORMAT parquet)")

    copy(
        f"""SELECT * FROM (VALUES
        (1::BIGINT, 'Designated Street Name', {_line(0, 0, 1000, 0)}),
        (2::BIGINT, 'Officially Described Street', {_line(0, 100, 1000, 100)}),
        (3::BIGINT, 'Numbered Street', {_line(0, 200, 1000, 200)}),
        (4::BIGINT, 'Numbered Street', {_line(0, 300, 1000, 300)})) t(usrn, street_type, geom_wkb)""",
        pq / "open_usrn" / "open_usrn.parquet",
    )
    # roads: Foo Road covers USRN 2; Bar Lane/Baz Lane each half of USRN 3; nothing near USRN 4
    copy(
        f"""SELECT *, ST_XMin(g) xmin, ST_YMin(g) ymin, ST_XMax(g) xmax, ST_YMax(g) ymax, ST_AsWKB(g) geom_wkb
        FROM (SELECT *, ST_GeomFromWKB({{wkb}}) g FROM (VALUES
          ('t1', 'Foo Road', NULL, NULL, {_line(0, 103, 1000, 103)}),
          ('t2', 'Bar Lane', NULL, NULL, {_line(0, 202, 500, 202)}),
          ('t3', 'Baz Lane', NULL, NULL, {_line(500, 202, 1000, 202)}),
          ('t4', 'Heol Fawr', 'cym', 'High Road', {_line(0, 2000, 10, 2000)}))
          t(toid, name_1, name_1_lang, name_2, wkb))""".replace("{wkb}", "wkb"),
        pq / "open_roads" / "open_roads.parquet",
    )
    copy(
        """SELECT * FROM (VALUES
        ('AB1 2CD', 500.0, 10.0, 10), ('AB1 2CE', 3000.0, 3000.0, 10), ('AB1 2CF', 520.0, 112.0, 50),
        ('AB1 2CG', NULL, NULL, 90)) t(postcode, x, y, positional_quality)""",
        pq / "codepoint" / "codepoint.parquet",
    )
    copy(
        """SELECT * FROM (VALUES
        (1::BIGINT, 500.0, 10.0), (2::BIGINT, 500.0, 110.0), (3::BIGINT, 500.0, 210.0),
        (4::BIGINT, 500.0, 5000.0), (5::BIGINT, 3000.0, 3010.0), (6::BIGINT, 520.0, 120.0),
        (7::BIGINT, NULL::DOUBLE, NULL::DOUBLE), (8::BIGINT, NULL::DOUBLE, NULL::DOUBLE)) t(uprn, x, y)""",
        staged / "open_uprn.parquet",
    )
    copy(
        """SELECT * FROM (VALUES
        (1::BIGINT, 'E1', 'ab1  2cd'), (2::BIGINT, 'E1', 'AB1 2CF'), (3::BIGINT, 'E1', 'AB1 2CG'),
        (4::BIGINT, 'E1', 'AB1 2CD'), (5::BIGINT, 'E1', 'ZZ9 9ZZ'), (6::BIGINT, 'E1', NULL),
        (7::BIGINT, 'E1', 'AB1 2CD'), (8::BIGINT, 'E1', 'AB1 2CF')) t(uprn, lad, postcode)""",
        staged / "onsud.parquet",
    )
    copy(
        """SELECT * FROM (VALUES
        (1::BIGINT, 1::BIGINT, 'High Street'), (2::BIGINT, 2::BIGINT, NULL), (3::BIGINT, 3::BIGINT, NULL),
        (4::BIGINT, 1::BIGINT, 'High Street'), (5::BIGINT, 4::BIGINT, NULL))
        t(uprn, usrn, street_name)""",
        staged / "streets.parquet",
    )
    return tmp_path


def test_provenance_for_lad(env):
    stats = pv.provenance_for_lad("E1", env / "staged", env / "out", env / "parquet")
    assert stats["uprns"] == 8
    con = duckdb.connect()
    rows = {
        r[0]: r
        for r in con.execute(f"""
        SELECT uprn, usrn_link_flag, usrn_distance_m, street_name_fallback, street_name_fallback_method,
               postcode_onsud, postcode_flag, postcode_centroid_distance_m, codepoint_positional_quality,
               postcode_nearest_cpo, nearest_agrees_onsud, postcode_nearest_cpo_distance_m
        FROM read_parquet('{env}/out/lad=E1/provenance.parquet')""").fetchall()
    }
    assert len(rows) == 8
    # uprn 1: 10 m from its USRN, named, onsud postcode matches nearest centroid
    # uprns 7, 8: NULL coordinates -> every nearest-centroid field is NULL (not a (0, 0) lookup)
    for u in (7, 8):
        assert rows[u][9] is None and rows[u][10] is None and rows[u][11] is None
        assert rows[u][1] == "no_geometry"
    assert rows[1][1:3] == ("ok", 10.0) and rows[1][3] is None
    assert rows[1][5:9] == ("AB1 2CD", "ok", 0.0, 10) and rows[1][9:11] == ("AB1 2CD", True)
    # uprn 2: unnamed USRN fully covered by Foo Road -> fallback name; postcode centroid ~20 m away
    assert rows[2][3:5] == ("Foo Road", "open_roads_overlap")
    assert rows[2][6] == "ok"
    # uprn 3: USRN 3 split between two names (50% each) -> no fallback; postcode with no coords
    assert rows[3][3] is None and rows[3][6] == "not_in_codepoint" and rows[3][8] == 90
    # uprn 4: 4990 m from its USRN -> far; also postcode far from the point
    assert rows[4][1] == "far" and rows[4][6] == "far"
    # uprn 5: USRN 4 has no road nearby; postcode unknown to Code-Point; nearest centroid disagrees (NULL)
    assert rows[5][3] is None and rows[5][6] == "not_in_codepoint" and rows[5][9] == "AB1 2CE"
    assert rows[5][10] is False
    # uprn 6: no ONSUD postcode, no streets row
    assert rows[6][1] == "no_geometry" and rows[6][5] is None and rows[6][10] is None


def test_provenance_all_resumable(env):
    r = pv.provenance_all(env / "staged", env / "out", env / "parquet")
    assert r["built"] == ["E1"] and not r["failed"]
    r = pv.provenance_all(env / "staged", env / "out", env / "parquet")
    assert r["skipped"] == ["E1"] and not r["built"]


def test_missing_inputs(env):
    (env / "parquet" / "codepoint" / "codepoint.parquet").unlink()
    with pytest.raises(FileNotFoundError):
        pv.provenance_for_lad("E1", env / "staged", env / "out", env / "parquet")
    r = pv.provenance_all(env / "staged", env / "out", env / "parquet")
    assert "E1" in r["failed"]


def test_convert_codepoint(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "MIN_FREE_GB", 0)
    csv = tmp_path / "src" / "Data" / "CSV"
    csv.mkdir(parents=True)
    (csv / "n.csv").write_text(
        '"N1 0AA",10,530626,183961,"E92000001","E19000003","E18000007","","E09000019","E05013700"\n'
        '"N10AB",90,0,0,"E92000001","E19000003","E18000007","","E09000019","E05013700"\n'
    )
    n = pv.convert_provenance("codepoint", tmp_path / "src", tmp_path / "parquet")
    assert n == 2
    rows = (
        duckdb.connect()
        .execute(
            f"SELECT postcode, x, positional_quality, admin_county_code, admin_district_code "
            f"FROM read_parquet('{tmp_path}/parquet/codepoint/codepoint.parquet') ORDER BY postcode"
        )
        .fetchall()
    )
    assert rows == [("N1 0AA", 530626.0, 10, None, "E09000019"), ("N1 0AB", None, 90, None, "E09000019")]
    assert "codepoint/codepoint.parquet" in (tmp_path / "parquet" / "manifest.json").read_text()
