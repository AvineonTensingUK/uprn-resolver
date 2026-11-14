"""Static STAC catalogue for a release directory (``<release>/stac/``).

One Collection per table, one Item per LAD per table. Items carry a WGS84 ``geometry``/``bbox`` (the LAD's data
extent converted from British National Grid with OSTN15, see ``uprn_resolver.bng``), ``proj:code`` =
``EPSG:27700`` with the native ``proj:bbox``, the release date as ``datetime``, and one ``data`` asset pointing at
the Parquet file (``application/vnd.apache.parquet``, relative href) with the STAC *table* extension
(``table:columns`` on the Collection, ``table:row_count`` / ``table:primary_geometry`` on the Item).

The catalogue is SELF_CONTAINED (relative links only), so it can be synced to Source Cooperative / any object
store as is and browsed with STAC Browser or ``pystac.Catalog.from_file``.

Spec references: STAC 1.1 https://github.com/radiantearth/stac-spec ; table extension
https://github.com/stac-extensions/table ; projection extension https://github.com/stac-extensions/projection ;
GeoParquet distribution guidance
https://github.com/opengeospatial/geoparquet/blob/main/format-specs/distributing-geoparquet.md ;
Source Cooperative data products https://docs.source.coop/ .
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pystac

from . import bng

PARQUET_MEDIA_TYPE = "application/vnd.apache.parquet"
TABLE_EXT = "https://stac-extensions.github.io/table/v1.2.0/schema.json"
PROJ_EXT = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"
GEO_KINDS = (
    "uprn", "buildings", "resolved_open", "parcels", "outline_ms_buildings", "outline_odbl_buildings",
    "uprn_lookup", "uprn_links", "os_buildings", "land_parcels", "microsoft_buildings",
)  # fmt: skip
OGL_URL = "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"

# table -> (title, description, license id, licence file relative to the release root, spdx-ish URL or None)
# SPDX ids: OGL-UK-3.0, ODbL-1.0, CDLA-Permissive-2.0. "various" = per-row mixed (Overture); "other" = OGL core +
# third-party columns, see the linked LICENCE.txt.
TABLES: dict[str, tuple[str, str, str, str]] = {
    # published names (single-file release, see names.PUBLIC_TABLES)
    "uprn_lookup": (
        "UPRN lookup (start here)",
        "One row per UPRN: point, best outline and its confidence, street, local authority and LSOA, EPC type, "
        "business name, quality. OGL core plus labelled Overture (poi_*) columns; no OSM.",
        "other",
        "uprn_lookup/LICENCE.txt",
    ),
    "uprn_links": (
        "UPRN links",
        "One row per UPRN: TOID, OS building, land parcel, USRN, street, OA / LSOA / MSOA, EPC and Price Paid ids.",
        "OGL-UK-3.0",
        "LICENCE.md",
    ),
    "os_buildings": (
        "OS buildings",
        "OS OpenMap Local building outlines.",
        "OGL-UK-3.0",
        "LICENCE.md",
    ),
    "land_parcels": (
        "Land parcels",
        "HM Land Registry INSPIRE Index Polygons and Registers of Scotland cadastral parcels holding a UPRN.",
        "OGL-UK-3.0",
        "land_parcels/LICENCE.txt",
    ),
    "microsoft_buildings": (
        "Microsoft buildings",
        "Microsoft building footprints that are some UPRN's best outline.",
        "CDLA-Permissive-2.0",
        "microsoft_buildings/LICENCE.txt",
    ),
    # internal names (per-LAD release, publish --per-lad)
    "uprn": (
        "UPRN points",
        "One row per UPRN: point, TOID, building match, street, parcel, geographies.",
        "OGL-UK-3.0",
        "LICENCE.md",
    ),
    "buildings": (
        "Building outlines",
        "OS-derived building polygons, deduplicated per LAD.",
        "OGL-UK-3.0",
        "LICENCE.md",
    ),
    "epc": ("EPC summary by UPRN", "Non-address EPC fields, one row per UPRN.", "OGL-UK-3.0", "LICENCE.md"),
    "provenance": ("Match provenance", "QA and provenance fields per UPRN.", "OGL-UK-3.0", "LICENCE.md"),
    "resolved_open": (
        "Resolved open table",
        "Best answer per UPRN. OGL core plus Overture (poi_*) and Microsoft (outline ids) columns; no OSM.",
        "other",
        "resolved_open/LICENCE.txt",
    ),
    "parcels": (
        "Land parcel polygons",
        "HM Land Registry INSPIRE Index Polygons and Registers of Scotland cadastral parcels holding a UPRN.",
        "OGL-UK-3.0",
        "parcels/LICENCE.txt",
    ),
    "poi": (
        "Overture Places by UPRN",
        "Overture Places matched to UPRNs; licence per row/provider.",
        "various",
        "poi/LICENCE.txt",
    ),
    "outline_ms": (
        "Microsoft footprint match",
        "Per-UPRN Microsoft building footprint match.",
        "CDLA-Permissive-2.0",
        "outline_ms/LICENCE.txt",
    ),
    "outline_ms_buildings": (
        "Microsoft building footprints",
        "Microsoft footprints used by outline_ms.",
        "CDLA-Permissive-2.0",
        "outline_ms_buildings/LICENCE.txt",
    ),
    "streets_odbl": (
        "OSM street and house number",
        "OpenStreetMap derived addresses; ODbL share-alike.",
        "ODbL-1.0",
        "streets_odbl/LICENCE.txt",
    ),
    "outline_odbl": (
        "OSM building match",
        "Per-UPRN OpenStreetMap building match; ODbL share-alike.",
        "ODbL-1.0",
        "outline_odbl/LICENCE.txt",
    ),
    "outline_odbl_buildings": (
        "OSM building footprints",
        "OSM footprints used by outline_odbl.",
        "ODbL-1.0",
        "outline_odbl_buildings/LICENCE.txt",
    ),
}


def release_datetime(release: str) -> dt.datetime:
    """``2026-10`` -> 2026-10-01T00:00Z; ``2026-10-23`` -> that day."""
    try:
        parts = [int(p) for p in release.split("-")[:3]]
        while len(parts) < 3:
            parts.append(1)
        return dt.datetime(*parts, tzinfo=dt.UTC)
    except ValueError:  # not a date-like release name
        return dt.datetime.now(dt.UTC).replace(microsecond=0)


def bng_bbox_to_wgs84(bbox, n: int = 21) -> tuple[list[float], list[list[float]]]:
    """BNG bbox -> (WGS84 [w, s, e, n], closed polygon ring). Edges are densified to n points because a BNG
    rectangle is not a lon/lat rectangle; the ring follows the transformed edges, the bbox is its envelope."""
    x0, y0, x1, y1 = (float(v) for v in bbox)
    t = np.linspace(0.0, 1.0, n)
    xs = np.concatenate([x0 + (x1 - x0) * t, np.full(n, x1), x1 - (x1 - x0) * t, np.full(n, x0)])
    ys = np.concatenate([np.full(n, y0), y0 + (y1 - y0) * t, np.full(n, y1), y1 - (y1 - y0) * t])
    try:
        lon, lat = bng.bng_to_wgs84(xs, ys)
    except ValueError:  # a national extent's edges cross sea outside the OSTN15 grid: EPSG's Helmert is ample here
        from pyproj import Transformer

        lon, lat = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True).transform(xs, ys)
    ring = [[round(float(a), 6), round(float(b), 6)] for a, b in zip(lon, lat, strict=True)]
    ring.append(ring[0])
    return [
        round(float(lon.min()), 6),
        round(float(lat.min()), 6),
        round(float(lon.max()), 6),
        round(float(lat.max()), 6),
    ], ring


def _file_info(path: Path) -> dict:
    """Row count, columns, primary geometry and BNG bbox (from GeoParquet metadata) of one Parquet file."""
    pf = pq.ParquetFile(path)
    schema = pf.schema_arrow
    info: dict = {
        "rows": pf.metadata.num_rows,
        "columns": [(n, str(schema.field(n).type)) for n in schema.names],
        "bbox": None,
        "geom": None,
    }
    raw = (schema.metadata or {}).get(b"geo")
    if raw:
        geo = json.loads(raw)
        info["geom"] = geo.get("primary_column")
        info["bbox"] = (geo.get("columns", {}).get(info["geom"], {}) or {}).get("bbox")
    return info


def _lad_extents(root: Path) -> dict[str, list[float]]:
    f = root / "lad_index.parquet"
    if not f.exists():
        return {}
    out = {}
    for r in pq.read_table(f).to_pylist():
        if r.get("xmin") is not None:
            out[r["lad"]] = [r["xmin"], r["ymin"], r["xmax"], r["ymax"]]
    return out


def _table_files(root: Path, table: str) -> list[Path]:
    """The table's data files: ``<table>/<table>.parquet`` in a single-file release, else one per LAD."""
    single = root / table / f"{table}.parquet"
    return [single] if single.exists() else sorted((root / table).glob("lad=*/*.parquet"))


def _tables_present(root: Path) -> list[str]:
    return [t for t in TABLES if (root / t).is_dir() and _table_files(root, t)]


def build_stac(root: Path, release: str | None = None, *, validate: bool = True) -> dict:
    """Write ``<root>/stac/`` for the release at ``root`` (adds files only; parquet is read, never rewritten).

    Returns a summary ``{"collections", "items", "files", "validated", "validation_error"}``. ``validate`` runs
    ``pystac`` validation of every object (fetches the extension JSON schemas over HTTP; if that is impossible the
    error is reported in ``validation_error`` and the catalogue is still written).
    """
    root = Path(root)
    release = release or root.name
    when = release_datetime(release)
    stac_root = root / "stac"
    if stac_root.exists():
        shutil.rmtree(stac_root)
    extents = _lad_extents(root)
    tables = _tables_present(root)
    cat = pystac.Catalog(
        id=f"uk-uprn-open-{release}",
        title=f"UK UPRN open resolver {release}",
        description=(
            "Static STAC catalogue for a release of the UK UPRN open resolver: one Collection per table and one "
            "Item per data file (the whole table, or one local authority). Licences differ by table; see each "
            "Collection."
        ),
    )
    n_items = 0
    coll_list: list[tuple[pystac.Collection, list[tuple[pystac.Item, Path]]]] = []
    for table in tables:
        title, desc, lic, lic_file = TABLES[table]
        files = _table_files(root, table)
        single = len(files) == 1 and not files[0].parent.name.startswith("lad=")
        items: list[tuple[pystac.Item, Path]] = []
        boxes: list[list[float]] = []
        cols: list = []
        for f in files:
            lad = None if single else f.parent.name.removeprefix("lad=")
            info = _file_info(f)
            if not cols:
                cols = [{"name": n, "type": t} for n, t in info["columns"]]
            native = info["bbox"] or (extents.get(lad) if lad else None)
            if native:
                wbox, ring = bng_bbox_to_wgs84(native)
                geom = {"type": "Polygon", "coordinates": [ring]}
                boxes.append(wbox)
            else:
                wbox, geom = None, None
            props = {
                "title": title if single else f"{title} - {lad}",
                **({} if single else {"lad": lad}),
                "table:row_count": info["rows"],
                "proj:code": "EPSG:27700",
            }
            if info["geom"]:
                props["table:primary_geometry"] = info["geom"]
            if native:
                props["proj:bbox"] = [float(v) for v in native]
            item = pystac.Item(
                id=table if single else f"{table}-{lad}",
                geometry=geom,
                bbox=wbox,
                datetime=when,
                properties=props,
                stac_extensions=[TABLE_EXT, PROJ_EXT],
            )
            items.append((item, f))
        if boxes:
            arr = np.array(boxes)
            sbox = [float(arr[:, 0].min()), float(arr[:, 1].min()), float(arr[:, 2].max()), float(arr[:, 3].max())]
        else:
            sbox = [-180.0, -90.0, 180.0, 90.0]
        coll = pystac.Collection(
            id=table,
            title=title,
            description=f"{desc} "
            + (
                "One GeoParquet file, Hilbert sorted, with a lad column (EPSG:27700)."
                if single
                else "Hive-partitioned by LAD (EPSG:27700)."
            ),
            license=lic,
            extent=pystac.Extent(pystac.SpatialExtent([sbox]), pystac.TemporalExtent([[when, when]])),
            extra_fields={"table:columns": cols, "table:primary_geometry": "geometry"}
            if table in GEO_KINDS
            else {"table:columns": cols},
            stac_extensions=[TABLE_EXT],
        )
        coll.links.append(
            pystac.Link(
                "license",
                f"../../{lic_file}",
                media_type="text/markdown" if lic_file.endswith(".md") else "text/plain",
                title="Licence of this table",
            )
        )
        if lic == "OGL-UK-3.0":
            coll.links.append(pystac.Link("license", OGL_URL, title="Open Government Licence v3.0"))
        coll_list.append((coll, items))
        cat.add_child(coll)
        for item, _ in items:
            coll.add_item(item)
            n_items += 1
    cat.normalize_hrefs(str(stac_root))
    for coll, items in coll_list:
        nat = root / "national" / coll.id
        for i, p in enumerate(sorted(nat.glob("*.parquet")) if nat.is_dir() else []):
            href = os.path.relpath(p, Path(coll.get_self_href()).parent)
            coll.add_asset(
                f"national_{i:04d}",
                pystac.Asset(href, title=f"National file {p.name}", media_type=PARQUET_MEDIA_TYPE, roles=["data"]),
            )
        for item, f in items:
            href = os.path.relpath(f, Path(item.get_self_href()).parent)
            item.add_asset("data", pystac.Asset(href, title=f.name, media_type=PARQUET_MEDIA_TYPE, roles=["data"]))
    cat.save(pystac.CatalogType.SELF_CONTAINED)
    summary = {
        "collections": len(tables),
        "items": n_items,
        "files": sum(1 for p in stac_root.rglob("*") if p.is_file()),
        "validated": None,
        "validation_error": None,
    }
    if validate:
        summary.update(validate_stac(stac_root))
    return summary


def validate_stac(stac_root: Path) -> dict:
    """Validate every object of a written catalogue with pystac; also check asset hrefs resolve to files."""
    stac_root = Path(stac_root)
    out: dict = {"validated": None, "validation_error": None, "missing_assets": []}
    cat = pystac.Catalog.from_file(str(stac_root / "catalog.json"))
    try:
        n = cat.validate_all()
        out["validated"] = n if isinstance(n, int) else len(n)
    except pystac.errors.STACValidationError as e:  # genuine schema failure
        out["invalid"] = str(e)[:500]
    except Exception as e:  # noqa: BLE001 - e.g. extension schemas could not be fetched (offline)
        out["validation_error"] = f"{type(e).__name__}: {str(e)[:500]}"
    for it in cat.get_items(recursive=True):
        for a in it.assets.values():
            if not Path(a.get_absolute_href()).exists():
                out["missing_assets"].append(a.href)
    return out
