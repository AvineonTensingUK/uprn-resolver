"""WGS84 <-> British National Grid (EPSG:27700) using the OSTN15 grid, with no silent fallback.

Why: DuckDB spatial's ``ST_Transform('EPSG:4326', 'EPSG:27700')`` uses a 7-parameter Helmert (no grid), which is
~2 m out in London (up to ~5 m elsewhere; the national OSM / Overture Parquet files were converted from Helmert
to OSTN15 in place, see ``crs_transform`` in the manifest).
OS's own transformation is OSTN15 (EPSG:7709 "OSGB36 to ETRS89 (OSTN15)"),
distributed for PROJ as the GeoTIFF ``uk_os_OSTN15_NTv2_OSGBtoETRS.tif`` (https://cdn.proj.org/, 3.0 MB; data (c) Crown
copyright / Ordnance Survey, released under the OS OpenData licence / OGL v3).

Grid file location (first hit wins): ``$UPRN_PROJ_GRID`` (full path), else ``<repo>/data/proj/<GRID_NAME>``. If it is
missing we raise ``FileNotFoundError`` (run ``python -m uprn_resolver.bng --fetch`` once to download it).

WGS84 is treated as identical to ETRS89 (null transformation), which is the standard OS-endorsed approximation
(they differ by <~1 m from plate motion; OSM/GPS coordinates are not realised at the ETRS89 epoch anyway).

Great Britain only: OSTN15 covers GB (and Isle of Man / Channel Islands are NOT covered). Points outside the grid
raise ``ValueError`` rather than yielding silently-wrong numbers. Northern Ireland uses the Irish Grid (EPSG:29903) and
ITM, which OSTN15 does not cover.

Two routes, same numbers (agree to <1e-9 m):

* pyproj (``wgs84_to_bng``, ``transform_geometry_wkb``): vectorised, explicit pipeline with the grid.
* DuckDB (``duckdb_bng_crs`` / ``duckdb_st_transform_sql``): a PROJ CRS string with ``+nadgrids=<abs path>`` as the
  target CRS of ``ST_Transform``. NB: DuckDB's PROJ silently returns ``inf`` if the grid cannot be found (relative
  name without PROJ_DATA), so always use the absolute path and check ``isfinite`` (``duckdb_st_transform_sql`` asserts
  the file exists; use ``assert_finite`` afterwards).
"""

from __future__ import annotations

import os
import urllib.request
from functools import lru_cache
from pathlib import Path

import numpy as np
import shapely
from pyproj import Transformer

GRID_NAME = "uk_os_OSTN15_NTv2_OSGBtoETRS.tif"
GRID_URL = "https://cdn.proj.org/" + GRID_NAME
GRID_SHA256 = "5d6ed64d2119952c4c559fa1fccbc594b6520fc3ec3ef2fc10be13202c4384fa"
_TMERC = "+proj=tmerc +lat_0=49 +lon_0=-2 +k=0.9996012717 +x_0=400000 +y_0=-100000 +ellps=airy"


def grid_path() -> Path:
    """Absolute path to the OSTN15 grid; raises FileNotFoundError if it is not present."""
    env = os.environ.get("UPRN_PROJ_GRID")
    p = Path(env) if env else Path(__file__).resolve().parent.parent / "data" / "proj" / GRID_NAME
    if not p.is_file():
        raise FileNotFoundError(
            f"OSTN15 grid not found at {p}. Download {GRID_URL} there (python -m uprn_resolver.bng --fetch) "
            "or set UPRN_PROJ_GRID. Refusing to fall back to a grid-less (Helmert) transform."
        )
    return p.resolve()


def fetch_grid(dest: Path | None = None) -> Path:
    """Download the grid (3 MB) to ``data/proj/`` and verify its sha256."""
    import hashlib

    dest = dest or Path(__file__).resolve().parent.parent / "data" / "proj" / GRID_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    urllib.request.urlretrieve(GRID_URL, tmp)  # noqa: S310 - fixed https URL
    if hashlib.sha256(tmp.read_bytes()).hexdigest() != GRID_SHA256:
        tmp.unlink()
        raise ValueError("OSTN15 grid sha256 mismatch")
    tmp.replace(dest)
    return dest


def _pipeline(forward: bool) -> str:
    g = grid_path()
    if forward:  # ETRS89/WGS84 lon,lat (deg) -> BNG E,N
        return (
            "+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
            f"+step +inv +proj=hgridshift +grids={g} +step {_TMERC}"
        )
    return (
        f"+proj=pipeline +step +inv {_TMERC} +step +proj=hgridshift +grids={g} "
        "+step +proj=unitconvert +xy_in=rad +xy_out=deg"
    )


@lru_cache(maxsize=2)
def _transformer(forward: bool) -> Transformer:
    return Transformer.from_pipeline(_pipeline(forward))


def assert_finite(*arrays: np.ndarray) -> None:
    for a in arrays:
        if not np.all(np.isfinite(a)):
            n = int((~np.isfinite(a)).sum())
            raise ValueError(f"{n} coordinate(s) outside OSTN15 coverage or invalid (inf/nan)")


def wgs84_to_bng(lon, lat):
    """WGS84/ETRS89 lon,lat (degrees; scalars or arrays) -> BNG (x, y) metres via OSTN15. Raises if out of coverage."""
    lon_a = np.asarray(lon, dtype="float64")
    lat_a = np.asarray(lat, dtype="float64")
    x, y = _transformer(True).transform(lon_a, lat_a, errcheck=False)
    x, y = np.asarray(x), np.asarray(y)
    assert_finite(x, y)
    return (x, y) if lon_a.ndim else (float(x), float(y))


def bng_to_wgs84(x, y):
    """BNG E,N (metres) -> WGS84/ETRS89 (lon, lat) degrees via OSTN15 (inverse; iterative in PROJ)."""
    xa = np.asarray(x, dtype="float64")
    ya = np.asarray(y, dtype="float64")
    lon, lat = _transformer(False).transform(xa, ya, errcheck=False)
    lon, lat = np.asarray(lon), np.asarray(lat)
    assert_finite(lon, lat)
    return (lon, lat) if xa.ndim else (float(lon), float(lat))


def transform_geometry_wkb(wkb: bytes | None, *, inverse: bool = False) -> bytes | None:
    """WKB geometry in WGS84 lon/lat -> WKB in BNG (or the reverse with ``inverse=True``); None passes through."""
    if wkb is None:
        return None
    geom = shapely.from_wkb(wkb)
    fn = bng_to_wgs84 if inverse else wgs84_to_bng
    return shapely.to_wkb(shapely.transform(geom, lambda c: np.column_stack(fn(c[:, 0], c[:, 1]))))


def transform_geometries(geoms: np.ndarray, *, inverse: bool = False) -> np.ndarray:
    """Vectorised over an array of shapely geometries (one pass over all coordinates)."""
    fn = bng_to_wgs84 if inverse else wgs84_to_bng
    return shapely.transform(geoms, lambda c: np.column_stack(fn(c[:, 0], c[:, 1])))


def duckdb_bng_crs() -> str:
    """PROJ CRS string (BNG + OSTN15 via +nadgrids) usable as the target CRS in DuckDB ``ST_Transform``."""
    return f"{_TMERC} +units=m +nadgrids={grid_path()} +no_defs"


def duckdb_st_transform_sql(geom_expr: str) -> str:
    """SQL expression: ``geom_expr`` (WGS84 lon/lat) -> BNG with OSTN15. Check ST_X/ST_Y finite afterwards."""
    crs = duckdb_bng_crs().replace("'", "''")
    return f"ST_Transform({geom_expr}, 'EPSG:4326', '{crs}', always_xy := true)"


def duckdb_st_transform_inverse_sql(geom_expr: str) -> str:
    """SQL expression: ``geom_expr`` (BNG metres) -> WGS84 lon/lat with OSTN15 (inverse of the above)."""
    crs = duckdb_bng_crs().replace("'", "''")
    return f"ST_Transform({geom_expr}, '{crs}', 'EPSG:4326', always_xy := true)"


def assert_parquet_finite(con, path, *cols: str) -> None:
    """Raise if any of ``cols`` (numeric coordinate columns) in the Parquet ``path`` is NULL-free but inf/NaN.

    Used by the converters after an OSTN15 ``ST_Transform``: DuckDB's PROJ yields ``inf`` outside the grid (or when
    the grid cannot be found) rather than raising.
    """
    q = str(path).replace("'", "''")
    pred = " OR ".join(f"({c} IS NOT NULL AND NOT isfinite({c}))" for c in cols)
    n = con.execute(f"SELECT count(*) FROM read_parquet('{q}') WHERE {pred}").fetchone()[0]
    if n:
        raise ValueError(f"{n} row(s) in {path} have non-finite coordinates (outside OSTN15 coverage?)")


# --------------------------------------------------------------------------- Northern Ireland / Irish Grid
# EPSG:29903 (TM75 / Irish Grid). PROJ ships only a ~1-2 m 7-parameter Helmert ("TM75 to ETRS89 (2)", EPSG:1953) as
# the default, and cdn.proj.org has NO TM75 grid (checked https://cdn.proj.org/files.geojson: the only Ireland files
# are the OSGM15 geoids). EPSG:6946 "TM75 to ETRS89 (3)" would use ``tm75_etrs89.gsb`` (ESRI Ireland), which is not
# freely downloadable. The official OSi/OSNI method (EPSG:1041, "recommended by OSi and OSNI for all horizontal
# coordinate transformations", 0.4 m at 95 %) is the OSi/LPS polynomial; its coefficients are published under a BSD
# licence (c) Ordnance Survey Ltd 2016, (c) Crown copyright / Land & Property Services 2016 and/or (c) Ordnance Survey
# Ireland 2016, as redistributed in Mark Wilton-Jones' Grid Reference Utilities
# (https://www.howtocreate.co.uk/jslibs/gridrefutils.js, "OSiLPS"). We evaluate that polynomial exactly
# (``osi_polynomial_shift``) and bake it into a regular NTv2 grid (``build_irish_grid``, 0.01 deg spacing, bilinear
# interpolation error ~1e-4 m) so that PROJ/DuckDB can use it through ``+nadgrids`` exactly as for OSTN15.
# Copyright statement to keep with derived data: (c) Copyright and database rights Ordnance Survey Limited 2016,
# (c) Crown copyright and database rights Land & Property Services 2016 and/or (c) Ordnance Survey Ireland, 2016.
IE_GRID_NAME = "ie_osilps_tm75_etrs89.gsb"
IE_TMERC = "+proj=tmerc +lat_0=53.5 +lon_0=-8 +k=1.000035 +x_0=200000 +y_0=250000 +ellps=mod_airy"
IE_BOUNDS = (51.3, 55.5, -10.7, -5.2)  # lat_min, lat_max, lon_min, lon_max of the baked grid (EPSG area of use + pad)
IE_STEP = 0.01
_POLY_LATM, _POLY_LONM, _POLY_K0 = 53.5, -7.7, 0.1
_POLY_A = (
    (0.763, 0.123, 0.183, -0.374),
    (-4.487, -0.515, 0.414, 13.110),
    (0.215, -0.570, 5.703, 113.743),
    (-0.265, 2.852, -61.678, -265.898),
)
_POLY_B = (
    (-2.810, -4.680, 0.170, 2.163),
    (-0.341, -0.119, 3.913, 18.867),
    (1.196, 4.877, -27.795, -284.294),
    (-0.887, -46.666, -95.377, -853.950),
)


def _poly_eval(coef, u, v):
    return sum(coef[i][j] * u**i * v**j for i in range(4) for j in range(4))


def osi_polynomial_shift(lat, lon):
    """OSi/LPS polynomial: TM75 (Airy Modified) lat,lon degrees -> (dlat, dlon) degrees to ADD to reach ETRS89."""
    lat = np.asarray(lat, dtype="float64")
    lon = np.asarray(lon, dtype="float64")
    u, v = _POLY_K0 * (lat - _POLY_LATM), _POLY_K0 * (lon - _POLY_LONM)
    return _poly_eval(_POLY_A, u, v) / 3600.0, _poly_eval(_POLY_B, u, v) / 3600.0


def ie_grid_path() -> Path:
    """Absolute path to the baked Irish NTv2 grid; raises FileNotFoundError (no silent Helmert fallback)."""
    env = os.environ.get("UPRN_PROJ_GRID_IE")
    p = Path(env) if env else Path(__file__).resolve().parent.parent / "data" / "proj" / IE_GRID_NAME
    if not p.is_file():
        raise FileNotFoundError(
            f"Irish Grid shift grid not found at {p}. Generate it with `python -m uprn_resolver.bng --build-ie` "
            "or set UPRN_PROJ_GRID_IE. Refusing to fall back to a grid-less (Helmert) transform."
        )
    return p.resolve()


def build_irish_grid(dest: Path | None = None) -> Path:
    """Write the NTv2 grid (TM75 -> ETRS89 shifts from the OSi/LPS polynomial); deterministic, atomic."""
    import struct

    dest = dest or Path(__file__).resolve().parent.parent / "data" / "proj" / IE_GRID_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    la0, la1, lo0, lo1 = IE_BOUNDS
    nlat = round((la1 - la0) / IE_STEP) + 1
    nlon = round((lo1 - lo0) / IE_STEP) + 1
    lats = la0 + IE_STEP * np.arange(nlat)
    lons_e = lo1 - IE_STEP * np.arange(nlon)  # NTv2 rows run east -> west (longitude positive west)
    lon_g, lat_g = np.meshgrid(lons_e, lats)
    dlat, dlon = osi_polynomial_shift(lat_g, lon_g)
    nodes = np.empty((nlat, nlon, 4), dtype="<f4")
    nodes[..., 0] = dlat * 3600.0
    nodes[..., 1] = -dlon * 3600.0  # positive west
    nodes[..., 2:] = 0.01  # nominal accuracy (arc-sec); PROJ ignores it

    def rec(key: str, val) -> bytes:
        k = key.ljust(8).encode()
        if isinstance(val, str):
            return k + val.ljust(8).encode()
        if isinstance(val, int):
            return k + struct.pack("<i4x", val)
        return k + struct.pack("<d", val)

    ov = [rec("NUM_OREC", 11), rec("NUM_SREC", 11), rec("NUM_FILE", 1), rec("GS_TYPE", "SECONDS"),
          rec("VERSION", "NTv2.0"), rec("SYSTEM_F", "TM75"), rec("SYSTEM_T", "ETRS89"),
          rec("MAJOR_F", 6377340.189), rec("MINOR_F", 6356034.447938534),
          rec("MAJOR_T", 6378137.0), rec("MINOR_T", 6356752.314140356)]  # fmt: skip
    sub = [rec("SUB_NAME", "IRELAND"), rec("PARENT", "NONE"), rec("CREATED", "20260101"), rec("UPDATED", "20260101"),
           rec("S_LAT", la0 * 3600.0), rec("N_LAT", la1 * 3600.0), rec("E_LONG", -lo1 * 3600.0),
           rec("W_LONG", -lo0 * 3600.0), rec("LAT_INC", IE_STEP * 3600.0), rec("LONG_INC", IE_STEP * 3600.0),
           rec("GS_COUNT", nlat * nlon)]  # fmt: skip
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(b"".join(ov) + b"".join(sub) + nodes.tobytes() + b"END     " + b"\x00" * 8)
    tmp.replace(dest)
    return dest


@lru_cache(maxsize=2)
def _ie_transformer(forward: bool) -> Transformer:
    g = ie_grid_path()
    if forward:  # ETRS89/WGS84 lon,lat (deg) -> Irish Grid E,N
        pipe = (
            "+proj=pipeline +step +proj=unitconvert +xy_in=deg +xy_out=rad "
            f"+step +inv +proj=hgridshift +grids={g} +step {IE_TMERC}"
        )
    else:
        pipe = (
            f"+proj=pipeline +step +inv {IE_TMERC} +step +proj=hgridshift +grids={g} "
            "+step +proj=unitconvert +xy_in=rad +xy_out=deg"
        )
    return Transformer.from_pipeline(pipe)


def wgs84_to_irish_grid(lon, lat):
    """WGS84/ETRS89 lon,lat (degrees; scalars or arrays) -> Irish Grid EPSG:29903 (x, y) via the OSi/LPS grid."""
    lon_a = np.asarray(lon, dtype="float64")
    lat_a = np.asarray(lat, dtype="float64")
    x, y = _ie_transformer(True).transform(lon_a, lat_a, errcheck=False)
    x, y = np.asarray(x), np.asarray(y)
    assert_finite(x, y)
    return (x, y) if lon_a.ndim else (float(x), float(y))


def irish_grid_to_wgs84(x, y):
    """Irish Grid (EPSG:29903) E,N -> WGS84/ETRS89 (lon, lat) degrees via the OSi/LPS grid."""
    xa = np.asarray(x, dtype="float64")
    ya = np.asarray(y, dtype="float64")
    lon, lat = _ie_transformer(False).transform(xa, ya, errcheck=False)
    lon, lat = np.asarray(lon), np.asarray(lat)
    assert_finite(lon, lat)
    return (lon, lat) if xa.ndim else (float(lon), float(lat))


def transform_geometries_ie(geoms: np.ndarray, *, inverse: bool = False) -> np.ndarray:
    """Vectorised over shapely geometries: WGS84 -> Irish Grid (or the reverse with ``inverse=True``)."""
    fn = irish_grid_to_wgs84 if inverse else wgs84_to_irish_grid
    return shapely.transform(geoms, lambda c: np.column_stack(fn(c[:, 0], c[:, 1])))


def duckdb_irish_grid_crs() -> str:
    """PROJ CRS string (Irish Grid + OSi/LPS grid via +nadgrids) usable as the target CRS in DuckDB ``ST_Transform``."""
    return f"{IE_TMERC} +units=m +nadgrids={ie_grid_path()} +no_defs"


def duckdb_irish_st_transform_sql(geom_expr: str) -> str:
    """SQL: ``geom_expr`` (WGS84 lon/lat) -> Irish Grid via the grid. Check coordinates are finite afterwards."""
    crs = duckdb_irish_grid_crs().replace("'", "''")
    return f"ST_Transform({geom_expr}, 'EPSG:4326', '{crs}', always_xy := true)"


def duckdb_irish_st_transform_inverse_sql(geom_expr: str) -> str:
    """SQL: ``geom_expr`` (Irish Grid metres) -> WGS84 lon/lat via the grid."""
    crs = duckdb_irish_grid_crs().replace("'", "''")
    return f"ST_Transform({geom_expr}, '{crs}', 'EPSG:4326', always_xy := true)"


if __name__ == "__main__":
    import sys

    if "--fetch" in sys.argv:
        print(fetch_grid())
    elif "--build-ie" in sys.argv:
        print(build_irish_grid())
    else:
        print(grid_path())
