import duckdb
import pytest

from uprn_resolver.inspire import _slug, stage_inspire


def _sq(x, y, s):
    return f"POLYGON(({x} {y},{x + s} {y},{x + s} {y + s},{x} {y + s},{x} {y}))"


def _setup(tmp_path, polys):
    data = tmp_path / "data"
    raw = data / "raw"
    raw.mkdir(parents=True)
    staged = data / "staged"
    (staged / "E1").mkdir(parents=True)
    con = duckdb.connect()
    con.execute("LOAD spatial")
    if polys is not None:
        (data / "parquet" / "inspire").mkdir(parents=True)
        rows = ", ".join(f"('{i}', '{w}')" for i, w in polys)
        con.execute(
            f"COPY (SELECT inspire_id, ST_AsWKB(ST_GeomFromText(w)) AS geom_wkb FROM (VALUES {rows}) t(inspire_id, w)) "
            f"TO '{data}/parquet/inspire/a.parquet' (FORMAT parquet)"
        )
    con.execute(
        "COPY (SELECT * FROM (VALUES (1::BIGINT,5.0,5.0,0.0,0.0),(2,6.0,6.0,0,0),(3,50.0,50.0,0,0),(4,500.0,500.0,0,0)) "
        f"t(uprn,x,y,lat,lon)) TO '{staged}/E1/open_uprn.parquet' (FORMAT parquet)"
    )
    return con, raw, staged


def test_smallest_area_and_counts(tmp_path):
    big, small, other = ("100", _sq(0, 0, 20)), ("200", _sq(4, 4, 4)), ("300", _sq(40, 40, 20))
    con, raw, staged = _setup(tmp_path, [big, small, other])
    stage_inspire("E1", raw, staged)
    rows = {
        r[0]: r[1:] for r in con.execute(f"SELECT * FROM read_parquet('{staged}/E1/inspire_parcel.parquet')").fetchall()
    }
    assert rows[1][0] == "200" and rows[1][1] == pytest.approx(16.0) and rows[1][2] == 2
    assert rows[2][0] == "200" and rows[2][2] == 2  # both points inside the small parcel
    assert 4 not in rows  # outside everything
    assert rows[3][0] == "300"


def test_uprns_in_parcel(tmp_path):
    con, raw, staged = _setup(tmp_path, [("100", _sq(0, 0, 20))])
    stage_inspire("E1", raw, staged)
    n = con.execute(f"SELECT list(uprns_in_parcel) FROM read_parquet('{staged}/E1/inspire_parcel.parquet')").fetchone()[
        0
    ]
    assert n == [2, 2]
    g = con.execute(f"SELECT count(*) FROM read_parquet('{staged}/E1/inspire_polygons.parquet')").fetchone()[0]
    assert g == 1


def test_no_data_noop(tmp_path):
    _, raw, staged = _setup(tmp_path, None)
    stage_inspire("E1", raw, staged)
    assert not (staged / "E1" / "inspire_parcel.parquet").exists()


def test_slug():
    assert _slug("London_Borough_of_Islington.zip") == "london_borough_of_islington"
