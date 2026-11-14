# Changelog

Code versions of the `uprn-resolver` pipeline. Data releases are named by month (for example `2026-10`) and are
described in each release's `README.md` and `release_manifest.json`.

## 0.2.0 (2026-11-14)

- **Lean release by default.** `publish` now ships only `resolved_open`, `uprn`, `buildings`, `parcels` and the
  Microsoft footprints that are some UPRN's best outline, with `uprn_points`, `outlines_ogl` and `parcels` tiles.
  The release is OGL apart from the labelled Microsoft footprints and Overture business names, with no
  share-alike data. `publish --full` ships every table as before.
- **One file per table.** Each table ships as a single Hilbert-sorted GeoParquet file (`<table>/<table>.parquet`,
  with a `lad` column) instead of 350 per-authority files; `publish --per-lad` keeps the old layout.
- **Plain table names in the release:** `uprn_lookup` (was `resolved_open`), `uprn_links` (`uprn`), `os_buildings`
  (`buildings`), `land_parcels` (`parcels`) and `microsoft_buildings` (`outline_ms_buildings`), with matching
  `best_outline_source` values and tile names. Internal pipeline names are unchanged (`uprn_resolver/names.py`).
- `accuracy` reads the pipeline output (`data/out`), so it no longer depends on the release layout.
- `publish` syncs with `--delete`, so the uploaded release folder matches the local one exactly.
- First data release on Source Cooperative: `2026-11`, at `avineon-tensing/uprn-resolver`.

## 0.1.0 (2026-11-14)

First public release of the pipeline.

- **Core build** for every Great Britain UPRN in the ONS UPRN Directory (41.6 million, 350 local authorities): OS Open
  UPRN point, TOID (OS Open Linked Identifiers), OS OpenMap Local building matched through the OS Open TOID point,
  street name and USRN (OS Open Names, Open Roads fallback), land parcel (HM Land Registry INSPIRE for England and
  Wales, Registers of Scotland), census geographies, EPC fields and HM Land Registry Price Paid lookups.
- **`resolved_open`**: the best answer per UPRN, choosing between OpenMap Local buildings, Microsoft footprints and
  land parcels, with `outline_confidence` (`property`, `small_group`, `block`, `none`), a quality score and quality
  flags.
- **`parcels`**: land parcel polygons holding a UPRN, published once per parcel under the OGL with the HM Land
  Registry, Ordnance Survey and Registers of Scotland statements.
- **Optional layers** kept in separate folders under their own licences: Microsoft building footprints
  (CDLA-Permissive-2.0), Overture places (per provider) and OpenStreetMap streets, house numbers and buildings
  (ODbL-1.0). Northern Ireland OpenStreetMap address points (no UPRNs).
- **`accuracy`**: an independent check of `resolved_open` against OpenStreetMap buildings tagged with a UPRN,
  published as aggregate statistics (`accuracy.json`).
- **Publishing**: GeoParquet 1.1 releases with Hilbert ordering and bbox covering, a static STAC catalogue, PMTiles
  vector tiles, a release manifest with checksums and source versions, licence checks before upload, and a dry-run
  upload to Source Cooperative.
- **Maintenance**: a resumable `refresh` command for new source data and a monthly archive of HM Land Registry's
  Price Paid lookups.
- Coordinates in British National Grid (EPSG:27700); WGS84 sources converted with the OSTN15 grid.
- Code under the Apache License 2.0.
