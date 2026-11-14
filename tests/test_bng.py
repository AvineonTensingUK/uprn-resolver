"""OSTN15 conversion tests against Ordnance Survey's published OSTN15 developer-pack test points.

Source: OSTN15-NTv2.zip (OSTN15_TestInput/Output_ETRStoOSGB.txt) from
https://www.ordnancesurvey.co.uk/documents/resources/OSTN15-NTv2.zip ; rounded to the mm in OS's output.
"""

import duckdb
import numpy as np
import pytest
import shapely

from uprn_resolver import bng

try:
    bng.grid_path()
except FileNotFoundError:  # pragma: no cover
    pytest.skip("OSTN15 grid not downloaded (data/proj)", allow_module_level=True)

# (id, lat, lon, E, N) ETRS89 -> OSGB (OSTN15 only; no OSGM15 height)
TP = [
    ("TP01", 49.92226393730, -6.29977752014, 91492.146, 11318.804),
    ("TP04", 50.57563665000, -1.29782277240, 449816.371, 75335.861),
    ("TP09", 51.48936564950, -0.11992557180, 530624.974, 178388.464),
    ("TP12", 52.25529381630, -2.15458614387, 389544.190, 261912.153),
    ("TP14", 52.75136687170, 0.40153547065, 562180.547, 319784.995),
    ("TP20", 53.80021519630, -1.66379168242, 422242.186, 433818.701),
    ("TP26", 55.85399952950, -4.29649016251, 256340.925, 664697.269),
    ("TP29", 57.13902518960, -2.04856030746, 397160.491, 805349.736),
    ("TP31", 57.81351838410, -8.57854456076, 9587.909, 899448.996),
    ("TP38", 59.53470794490, -1.62516966058, 421300.525, 1072147.239),
    ("TP40", 60.13308091660, -2.07382822798, 395999.668, 1138728.951),
]
LAT, LON, E, N = (np.array(c, dtype=float) for c in zip(*[t[1:] for t in TP], strict=True))


def test_forward_matches_os_test_points_to_mm():
    x, y = bng.wgs84_to_bng(LON, LAT)
    assert np.abs(x - E).max() < 0.004  # observed max 2.8 mm (PROJ GeoTIFF interpolation vs OS rounding)
    assert np.abs(y - N).max() < 0.004  # observed max 2.8 mm (PROJ GeoTIFF interpolation vs OS rounding)


def test_scalar_and_roundtrip():
    x, y = bng.wgs84_to_bng(float(LON[2]), float(LAT[2]))
    assert isinstance(x, float)
    lon, lat = bng.bng_to_wgs84(x, y)
    assert abs(lon - LON[2]) < 1e-9 and abs(lat - LAT[2]) < 1e-9


def test_differs_from_grid_less_helmert_in_london():
    c = duckdb.connect()
    c.sql("install spatial; load spatial")
    hx = c.sql(
        "select ST_X(ST_Transform(ST_Point(-0.1199, 51.4894),'EPSG:4326','EPSG:27700',always_xy:=true))"
    ).fetchall()[0][0]
    x, _ = bng.wgs84_to_bng(-0.1199, 51.4894)
    assert 1.0 < hx - x < 4.0  # Helmert is east of OSTN15 by ~2 m in London


def test_duckdb_route_matches_pyproj():
    c = duckdb.connect()
    c.sql("install spatial; load spatial")
    rows = ", ".join(f"({a}, {b})" for a, b in zip(LON, LAT, strict=True))
    sql = bng.duckdb_st_transform_sql("ST_Point(lon, lat)")
    got = c.sql(f"select ST_X({sql}), ST_Y({sql}) from (values {rows}) t(lon, lat)").fetchnumpy()
    x, y = bng.wgs84_to_bng(LON, LAT)
    assert np.abs(list(got.values())[0] - x).max() < 1e-6
    assert np.abs(list(got.values())[1] - y).max() < 1e-6


def test_out_of_coverage_raises():
    with pytest.raises(ValueError):
        bng.wgs84_to_bng(np.array([2.0, -0.12]), np.array([40.0, 51.5]))


def test_polygon_wkb_roundtrip():
    poly = shapely.box(-0.1200, 51.4890, -0.1199, 51.4891)
    out = shapely.from_wkb(bng.transform_geometry_wkb(shapely.to_wkb(poly)))
    assert out.is_valid and 5 < out.area < 100
    back = shapely.from_wkb(bng.transform_geometry_wkb(shapely.to_wkb(out), inverse=True))
    assert shapely.hausdorff_distance(back, poly) < 1e-8
    assert bng.transform_geometry_wkb(None) is None


def test_missing_grid_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setenv("UPRN_PROJ_GRID", str(tmp_path / "nope.tif"))
    with pytest.raises(FileNotFoundError):
        bng.grid_path()


# --------------------------------------------------------------------------- Irish Grid (EPSG:29903)
NI_PTS = np.array([(-5.9301, 54.5966), (-6.6556, 54.3523), (-7.3086, 54.9966), (-5.4800, 54.6600), (-7.8, 54.2)])


@pytest.fixture(scope="module")
def ie_grid(tmp_path_factory):
    """Bake the NTv2 grid into a temp dir and point UPRN_PROJ_GRID_IE at it (self-contained, no data/ needed)."""
    import os

    p = bng.build_irish_grid(tmp_path_factory.mktemp("proj") / bng.IE_GRID_NAME)
    old = os.environ.get("UPRN_PROJ_GRID_IE")
    os.environ["UPRN_PROJ_GRID_IE"] = str(p)
    bng._ie_transformer.cache_clear()
    yield p
    if old is None:
        del os.environ["UPRN_PROJ_GRID_IE"]
    else:
        os.environ["UPRN_PROJ_GRID_IE"] = old
    bng._ie_transformer.cache_clear()


def _exact_irish(lon, lat):
    """Reference: exact polynomial (fixed-point inverse) then the plain Irish Grid projection."""
    from pyproj import Transformer

    fw = Transformer.from_pipeline(
        f"+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad +step {bng.IE_TMERC}"
    )
    la, lo = lat.copy(), lon.copy()
    for _ in range(20):
        dla, dlo = bng.osi_polynomial_shift(la, lo)
        la, lo = lat - dla, lon - dlo
    return fw.transform(lo, la)


def test_polynomial_shift_is_small_and_plausible():
    dlat, dlon = bng.osi_polynomial_shift(NI_PTS[:, 1], NI_PTS[:, 0])
    # Airy Modified -> GRS80 datum shift in NI: ~0.1-0.5 arc-sec in lat (<15 m), ~3-4 arc-sec in lon (~65 m)
    assert 0.05 < np.abs(dlat * 3600).max() < 1.0 and 2.0 < np.abs(dlon * 3600).max() < 5.0


def test_irish_grid_matches_exact_polynomial(ie_grid):
    x, y = bng.wgs84_to_irish_grid(NI_PTS[:, 0], NI_PTS[:, 1])
    ex, ey = _exact_irish(NI_PTS[:, 0], NI_PTS[:, 1])
    assert np.hypot(x - ex, y - ey).max() < 0.002  # NTv2 bilinear interpolation at 0.01 deg spacing
    assert 300000 < x[0] < 400000 and 350000 < y[0] < 400000  # Belfast City Hall ~ J 338 740


def test_irish_grid_close_to_but_not_helmert(ie_grid):
    c = duckdb.connect()
    c.sql("install spatial; load spatial")
    rows = ", ".join(f"({a}, {b})" for a, b in NI_PTS)
    sql = "ST_Transform(ST_Point(a, b), 'EPSG:4326', 'EPSG:29903', always_xy := true)"
    hx, hy = c.sql(f"select ST_X({sql}), ST_Y({sql}) from (values {rows}) t(a, b)").fetchnumpy().values()
    x, y = bng.wgs84_to_irish_grid(NI_PTS[:, 0], NI_PTS[:, 1])
    d = np.hypot(hx - x, hy - y)
    assert d.max() < 2.0 and d.max() > 0.01  # Helmert is ~0.2 m (max ~1.5 m) away from the official polynomial


def test_irish_duckdb_route_matches_pyproj_and_roundtrips(ie_grid):
    c = duckdb.connect()
    c.sql("install spatial; load spatial")
    rows = ", ".join(f"({a}, {b})" for a, b in NI_PTS)
    sql = bng.duckdb_irish_st_transform_sql("ST_Point(a, b)")
    got = c.sql(f"select ST_X({sql}), ST_Y({sql}) from (values {rows}) t(a, b)").fetchnumpy()
    x, y = bng.wgs84_to_irish_grid(NI_PTS[:, 0], NI_PTS[:, 1])
    gx, gy = got.values()
    assert np.abs(gx - x).max() < 1e-6 and np.abs(gy - y).max() < 1e-6
    inv = bng.duckdb_irish_st_transform_inverse_sql("ST_Point(x, y)")
    r = c.sql(f"select ST_X({inv}), ST_Y({inv}) from (select {x[0]} x, {y[0]} y)").fetchall()[0]
    assert abs(r[0] - NI_PTS[0, 0]) < 1e-8 and abs(r[1] - NI_PTS[0, 1]) < 1e-8
    lon, lat = bng.irish_grid_to_wgs84(x, y)
    assert np.abs(lon - NI_PTS[:, 0]).max() < 1e-8 and np.abs(lat - NI_PTS[:, 1]).max() < 1e-8
    sx, sy = bng.wgs84_to_irish_grid(float(NI_PTS[0, 0]), float(NI_PTS[0, 1]))
    assert isinstance(sx, float) and abs(sx - x[0]) < 1e-9


def test_irish_geometries_roundtrip(ie_grid):
    poly = shapely.box(-5.931, 54.596, -5.930, 54.597)
    out = bng.transform_geometries_ie(np.array([poly]))[0]
    assert out.is_valid and 1000 < out.area < 10000
    back = bng.transform_geometries_ie(np.array([out]), inverse=True)[0]
    assert shapely.hausdorff_distance(back, poly) < 1e-8


def test_irish_out_of_coverage_raises(ie_grid):
    with pytest.raises(ValueError):
        bng.wgs84_to_irish_grid(np.array([-6.0, 10.0]), np.array([54.5, 54.5]))


def test_irish_missing_grid_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setenv("UPRN_PROJ_GRID_IE", str(tmp_path / "nope.gsb"))
    with pytest.raises(FileNotFoundError):
        bng.ie_grid_path()
    with pytest.raises(FileNotFoundError):
        bng.duckdb_irish_grid_crs()
