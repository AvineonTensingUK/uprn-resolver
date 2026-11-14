# Release format

Cloud-native GeoParquet 1.1, written by `uprn_resolver.publish.assemble` (design notes at the end).

## Layout

One file per local authority (LAD) per table, hive-partitioned by `lad`. Coordinates are EPSG:27700 (metres).

```
<release>/
  README.md  LICENCE.md  stats.json  release_manifest.json  [CHANGELOG.json  CHANGELOG.md]
  stac/catalog.json ...           static STAC catalogue (see "STAC" below)
  national/<table>/part-NNNN.parquet   optional national files (see "National files" below)
  lad_index.parquet               lad, n_uprn, xmin, ymin, xmax, ymax, bytes (one row per LAD, to pick files by bbox)

  OGL v3 core
  uprn/lad=<code>/uprn.parquet            GeoParquet 1.1, Point, Hilbert sorted, bbox covering
  buildings/lad=<code>/buildings.parquet  GeoParquet 1.1, Polygon, Hilbert sorted, bbox covering
  epc/lad=<code>/epc.parquet              plain Parquet, sorted by uprn
  provenance/lad=<code>/provenance.parquet plain Parquet, sorted by uprn (postcode text columns dropped)

  OGL core + third-party columns
  resolved_open/lad=<code>/resolved_open.parquet + LICENCE.txt
        GeoParquet 1.1, Point; best answer per UPRN. street_name/locality are OS (OGL); poi_* are Overture
        (poi_licence per row); Microsoft-derived outline ids/areas are CDLA-Permissive-2.0. No OSM, no addresses.

  Other licences, each in its own folder with LICENCE.txt
  poi/lad=<code>/poi.parquet                         Overture Places (CDLA-Permissive-2.0 / Apache-2.0 / CC0)
  outline_ms/lad=<code>/outline_ms.parquet           per-UPRN Microsoft footprint match   CDLA-Permissive-2.0
  outline_ms_buildings/lad=<code>/...parquet         GeoParquet 1.1, Polygon              CDLA-Permissive-2.0
  streets_odbl/lad=<code>/streets_odbl.parquet       OSM street/house number              ODbL-1.0
  outline_odbl/lad=<code>/outline_odbl.parquet       per-UPRN OSM building match          ODbL-1.0
  outline_odbl_buildings/lad=<code>/...parquet       GeoParquet 1.1, Polygon              ODbL-1.0
```

`resolved_open`, `poi`, `outline_*` and `streets_odbl` exist only if built for that LAD; a table that exists must
have its `<table>.LICENCE.txt` sidecar in the LAD output (the build fails otherwise) and the first one found is
copied to `<table>/LICENCE.txt` (`outline_ms_buildings` and `outline_odbl_buildings` use the `outline_ms` and
`outline_odbl` sidecars). `address_local` and `resolved_local` (addresses, postcodes) are never read or released
(asserted in `assemble`). ODbL tables stay in their own folders and are never joined into the OGL tables: the writer
rejects any `osm_*` column, and an OSM-named `best_outline_source`, in an OGL table.
Forbidden-column filtering (address, street, locality, postcode ...) has explicit per-table allowances in
`publish.TABLE_ALLOW` (e.g. `resolved_open`: `street_name`, `street_source`, `locality`); ODbL, Overture and
Microsoft tables are exempt from the name filter because their licences allow those fields.
`buildings`, `outline_ms_buildings` and `outline_odbl_buildings` straddling two LADs appear in both files (same id);
dedupe on `building_id` / `ms_building_id` / `osm_building_id` for national use.
Column schemas: see `publish/README.md` (`uprn`, `buildings`) and `CONTRACT.md`.

## STAC catalogue

`<release>/stac/` is a static, self-contained (relative links) STAC catalogue written by `uprn_resolver.stac.build_stac`
(called by `assemble`; validated with pystac, which also checks that every asset href resolves). One Collection per
table (`stac/<table>/collection.json`), one Item per LAD per table (`stac/<table>/<lad>/<lad>.json`).

- Item `geometry`/`bbox` are WGS84: the LAD's data extent (GeoParquet `geo.columns.geometry.bbox`, or the LAD extent
  from `lad_index.parquet` for plain tables) converted from BNG with OSTN15 (`uprn_resolver.bng`), edges densified
  because a BNG rectangle is not a lon/lat rectangle. The native box is in `proj:bbox`, CRS in `proj:code` = `EPSG:27700`.
- `datetime` is the release date (`2026-10` is 2026-10-01). One asset `data` per Item, media type
  `application/vnd.apache.parquet`, relative href. When `national/` exists each Collection also has `national_NNNN` assets.
- STAC *table* extension: `table:columns` (name, Arrow type) on the Collection, `table:row_count` and
  `table:primary_geometry` on Items.
- Collection `license`: `OGL-UK-3.0` (uprn, buildings, epc, provenance), `ODbL-1.0` (streets_odbl, outline_odbl*),
  `CDLA-Permissive-2.0` (outline_ms*), `various` (poi: per-provider Overture licences), `other` (resolved_open: OGL
  core plus Overture and Microsoft columns). Each Collection has a `license` link to the release `LICENCE.md` or the
  table's `LICENCE.txt`.

STAC items give catalogue-style discovery (STAC Browser, `pystac-client` against a static catalogue, spatial search by
LAD). Source Cooperative serves the files over plain HTTPS/S3, so the static catalogue works as is.

## National files (optional, off by default)

`assemble(national_files=True)` (or `publish.write_national(root)` afterwards) writes each geo table once more as
`national/<table>/part-NNNN.parquet`: one Hilbert-sorted GeoParquet 1.1 file with the bbox covering, for remote
readers that prefer few requests over per-LAD pruning. Above ~2 GB of LAD input the table is split into contiguous
Hilbert ranges (spatial partitions, each with its own `geo.bbox`), as the GeoParquet distribution guidance
recommends for large datasets. Features straddling LADs (buildings tables) are deduplicated on their id. It roughly
doubles the release size and needs scratch disk about the size of the largest table (DuckDB spills to
`<stage>/.duck_tmp`), hence off by default.

## Versioning, manifest and changelog

- Releases are immutable. `assemble` refuses an existing `<stage>/<release>` unless `force=True`; a failed run removes
  its partial folder.
- `release_manifest.json` (written last, not listing itself): every file with `bytes`, `sha256` and (Parquet) `rows`,
  `row_groups`; per-table totals; a digest of the source manifest (`data/parquet/manifest.json`: sha256, downloaded
  files and converted row counts per dataset). Verify a download with `sha256sum`.
- `<stage>/latest.json` points at the newest release (`latest`, `path`, `manifest`, `stac`). It belongs at the product
  root; `publish` uploads it after the release sync so readers never see a pointer to a half-uploaded release.
- `publish.diff_releases(old_dir, new_dir, write=True)` writes `CHANGELOG.json` (per table: UPRNs old/new/added/removed,
  up to 20 sample UPRNs each, and counts of changed values: `uprn` building_id/street_name/toid/parcel_inspire_id,
  `resolved_open` best_outline_id/outline_confidence/street_name) and a short `CHANGELOG.md`. A UPRN that moves LAD is
  not an add/remove because the comparison is national.

## Parquet / GeoParquet properties

| Property | Value |
|---|---|
| `geo` metadata | version `1.1.0`, primary column `geometry`, encoding `WKB`, `geometry_types` computed from data, `bbox` = file extent |
| CRS | full PROJJSON for EPSG:27700 (`id: {authority: EPSG, code: 27700}`) |
| Covering | root column `bbox STRUCT(xmin, ymin, xmax, ymax DOUBLE)`, per row, NULL iff geometry NULL; `geo.columns.geometry.covering.bbox` points at it |
| Row order | Hilbert curve of the bbox centre over the BNG extent (0,0,700000,1300000), tie-break by id (`uprn`, `building_id`, `ms_building_id`, `osm_building_id`) |
| Row groups | 100,000 rows (DuckDB writes 100,352 = 49 x 2048); last group smaller |
| Compression | zstd level 9 |

Check any file: `uv run python -m uprn_resolver.publish path/to/file.parquet` (validator plus row-group report).

## Querying

DuckDB (>= 1.5 maps the geo-annotated column to `GEOMETRY` on read; on older versions wrap it in `ST_GeomFromWKB`).
Filter on the covering struct, not on the geometry, so row groups are skipped from Parquet statistics:

```sql
INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;
SELECT uprn, street_name, ST_AsText(geometry)
FROM read_parquet('https://data.source.coop/avineon-tensing/uprn-resolver/<release>/uprn/lad=*/uprn.parquet',
                  hive_partitioning = true)
WHERE lad = 'E09000019'
  AND bbox.xmin <= 531000 AND bbox.xmax >= 530000
  AND bbox.ymin <= 185000 AND bbox.ymax >= 184000;
```

(`lad=*` globbing needs S3 listing: with HTTP list files explicitly, or use `lad_index.parquet` to choose them.
S3 form: `s3://<region>.opendata.source.coop/avineon-tensing/uprn-resolver/<release>/...`.)

GeoPandas (`bbox` is a (xmin, ymin, xmax, ymax) tuple in the file CRS and uses the covering column):

```python
import geopandas as gpd

gdf = gpd.read_parquet("uprn/lad=E09000019/uprn.parquet", bbox=(530000, 184000, 531000, 185000))
```

QGIS / GDAL >= 3.5 (Parquet driver required): add the file as a vector layer (`Layer > Add Vector Layer`); the
CRS is read from the metadata and `bbox` is used for spatial filtering.
`ogr2ogr -spat 530000 184000 531000 185000 out.gpkg uprn/lad=E09000019/uprn.parquet`.

## Design decisions and evidence

- **Per-LAD files**, not one national file or 14 regional ones. The build is per LAD, so a rebuild or fix touches one
  ~10-30 MB file (national file: rewrite ~1 GB); LAD is the natural user filter and hive-prunes at file level; ~360
  files of ~110k rows (average) is well under the ~2 GB guidance for spatial partitioning, and Hilbert order inside a file
  gives the spatial pruning. Regions (~3M rows, 300 MB) would reduce per-file footer requests for national scans;
  `lad_index.parquet` covers that case by letting a client open only the intersecting files.
- **Hilbert + covering over geometry-only sorting**: 4.06M-row synthetic file (24 shifted Islington copies, same writer):
  bbox query touched 2 of 41 row groups (7 ms) vs 41 of 41 for a WKB `ST_Intersects` scan (406 ms) and 41 of 41 when the
  same file is randomly ordered (58 ms even with the covering). The covering is cheap for points (+10% size) and needed for polygons.
- **No WGS84 copy**: it would double storage and double the release's surface for inconsistency; EPSG:27700 is
  what OS data is natively in and every reader above reads the PROJJSON. `ST_Transform` or GeoPandas `to_crs` give WGS84.
- **WKB, not native geoarrow**: DuckDB cannot write the native encodings; WKB is read everywhere. (GeoParquet 2.0 native
  geometry statistics would make the `bbox` column unnecessary; revisit when DuckDB/GDAL write it.)
- DuckDB 2.0's own GeoParquet writer emits version 1.0.0 metadata with no CRS and no covering, so `publish.py` writes
  the geometry as plain WKB BLOB and supplies the `geo` JSON itself via `KV_METADATA`.

References: STAC spec https://stacspec.org/ ; table extension https://github.com/stac-extensions/table ;
Source Cooperative https://docs.source.coop/ ; GeoParquet 1.1 spec https://geoparquet.org/releases/v1.1.0/ ; distribution guidance (row groups 50k-150k,
spatial sort, partition above ~2 GB) https://github.com/opengeospatial/geoparquet/blob/main/format-specs/distributing-geoparquet.md ;
cloudnativegeo.org GeoParquet guide https://cloudnativegeo.org/geoparquet/ ; DuckDB spatial https://duckdb.org/docs/lts/core_extensions/spatial/overview .
