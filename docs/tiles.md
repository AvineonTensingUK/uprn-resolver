# Web tiles (PMTiles)

`uprn_resolver/tiles.py` builds PMTiles (vector, MVT, WGS84) from the release GeoParquet, one archive per licence:

| Archive | Source table | Layer | Zooms | Licence (in metadata `attribution`) |
|---|---|---|---|---|
| `uprn_points.pmtiles` | `resolved_open` | `uprn` (uprn, outline_confidence, best_outline_source, street_name, locality) | 5-16 | OGL v3.0 (Microsoft-derived ids CDLA-Permissive-2.0) |
| `outlines_ogl.pmtiles` | `buildings` (OpenMap) | `outlines` (building_id) | 13-16 | OGL v3.0 |
| `parcels.pmtiles` | `parcels` | `parcels` (parcel_id, parcel_source, uprns_in_parcel, parcel_area_m2) | 13-16 | OGL v3.0, with HM Land Registry, Ordnance Survey (AC0000851063) and Registers of Scotland statements |
| `outlines_ms.pmtiles` | `outline_ms_buildings` | `outlines` (ms_building_id, area_m2) | 13-16 | CDLA-Permissive-2.0 |
| `outlines_odbl.pmtiles` | `outline_odbl_buildings` | `outlines` (osm_building_id, osm_building_tag, osm_building_area_m2) | 13-16 | ODbL-1.0, share-alike; never mix into the others |
| `ni_addresses.pmtiles` | `data/out/ni/ni_address_buildings.parquet` (not in the release) | `ni_address` (osm_id, street, city, confidence, building_match) | 5-16 | ODbL-1.0; Northern Ireland, no UPRNs, already lon/lat |

Output: `<release>/tiles/`. Archives built for an LAD prefix get a suffix (`uprn_points_E09.pmtiles`).

## Toolchain

`tippecanoe` (Homebrew, `brew install tippecanoe pmtiles`; the `pmtiles` formula is the Protomaps CLI for `show`/`verify`).
Chosen over planetiler (JVM, needs a custom Java profile for GeoParquet-in-BNG) and a pure-Python tiler (slow,
no feature dropping): tippecanoe writes PMTiles directly and has density-based dropping for the point layer.
`tiles.py` streams LAD by LAD (DuckDB, coordinates through OSTN15 inverse `bng.bng_to_wgs84` /
`bng.transform_geometries`) as GeoJSONSeq into one tippecanoe stdin, so no per-LAD intermediates are written; only
tippecanoe's work files go under `data/tmp/tiles/` (auto-deleted). Every 10 LADs the build checks free disk and aborts
below 6 GB. UPRNs without coordinates (null x/y) are skipped. Points use `--drop-densest-as-needed` (thinned at z5-z15,
all points at z16 where tile size allows); outlines use drop/coalesce-densest and `--simplification=4`.

```
uv run python -m uprn_resolver.tiles --kind all --lad-prefix E09          # London
uv run python -m uprn_resolver.tiles --kind odbl                          # national, one kind
```

Suggested CLI wrapper: `uprn-resolver tiles --release DIR [--kind points|ogl|parcels|ms|odbl|ni|all] [--lad-prefix E09]`
calling `uprn_resolver.tiles.main`.

## Results (release 2026-10)

| Archive (London, 33 LADs, E09*) | Features | Size | Build time |
|---|---|---|---|
| uprn_points_E09 | 5,308,792 | 94 MB | 360 s |
| outlines_ogl_E09 | 509,574 | 108 MB | 66 s |
| outlines_ms_E09 | 685,658 | 120 MB | 124 s |
| outlines_odbl_E09 | 1,024,503 | 112 MB | 178 s |

Single LAD E09000019: points 2.5 MB (6 s), OGL 1.3 MB, MS 1.5 MB, ODbL 2.3 MB. All four pass `pmtiles verify`.

National estimate (tile bytes scaled by parquet bytes from London): points ~1.1 GB, OGL ~2.2 GB, MS ~2.0 GB,
ODbL ~1.5 GB, total roughly 5-7 GB and ~1.5-2 h, over the 4 GB budget for this disk, so only the London set was built.

## Viewer

`docs/viewer.html` (MapLibre GL JS 4.7.1 + pmtiles 3.2.1 from unpkg) loads archives from a base URL (default
`../data/release/2026-10/tiles/` with suffix `_E09`; serve the repo with `python -m http.server`, range requests are
needed) or from local files picked in the page. Layer toggles per archive; points are coloured by `outline_confidence`;
click for properties. The OSM raster basemap is for orientation only; the ODbL layer toggle is off by default.
