# Pipeline contract (internal, for parallel development)

Stages: download -> stage -> build -> extras (address, provenance, poi, osm, outlines) -> resolve -> national -> validate -> publish.
All paths relative to repo root. Optional inputs are left-joined: when absent their columns are NULL and the step warns and continues.

## Directories
- `data/raw/<dataset>/` — transient download/extract area, one directory per dataset. Emptied after conversion.
- `data/parquet/<dataset>/` — national (GB) source data converted to zstd Parquet as each download lands,
  columns as in the source (snake_case), geometry as WKB EPSG:27700. `data/parquet/manifest.json` records
  each source file's name, size, md5 and release so downloads are skipped without keeping the zip.
  Staging reads from here, not from raw.
- `data/staged/<lad>/` — normalised Parquet, filtered to one local authority (LAD code, e.g. E09000019).
- `data/out/lad=<code>/` — per-LAD outputs (hive-style, `read_parquet('out/*/uprn.parquet', hive_partitioning=true)`); `data/out/national/` — roll-up; `data/out/ni/` — NI layer; `data/release/<release>/` — assembled release; `data/validation/` — validation report; `data/proj/` — OSTN15 grid; `data/tmp/` — DuckDB spill.

Coordinates are British National Grid (EPSG:27700) metres. Polygons are WKB in EPSG:27700. WGS84 sources (OSM, Overture, Microsoft) are
converted with the OSTN15 grid (`bng.py`, `download --sources ostn15`); there is no Helmert fallback. NI geometry stays in EPSG:29903.

## Staged schemas (`data/staged/<lad>/`)
| File | Columns | Owner |
|---|---|---|
| `onsud.parquet` | uprn BIGINT, lad VARCHAR, oa VARCHAR, lsoa VARCHAR, msoa VARCHAR, postcode VARCHAR | stage.py |
| `open_uprn.parquet` | uprn BIGINT, x DOUBLE, y DOUBLE, lat DOUBLE, lon DOUBLE | stage.py |
| `lids_uprn_toid.parquet` | uprn BIGINT, toid VARCHAR, lids_confidence VARCHAR | stage.py |
| `open_toid.parquet` | toid VARCHAR, toid_x DOUBLE, toid_y DOUBLE | stage.py |
| `buildings.parquet` | building_id VARCHAR, geom_wkb BLOB | stage.py |
| `hmlr_ppd_uprn.parquet` (optional) | transaction_id VARCHAR, uprn BIGINT | hmlr.py |
| `hmlr_inspire.parquet` (optional) | transaction_id VARCHAR, inspire_id VARCHAR | hmlr.py |
| `streets.parquet` (optional) | uprn BIGINT, usrn BIGINT, street_name, street_name_alt, locality, street_method VARCHAR, usrn_count INTEGER | streets.py |
| `epc.parquet` (optional) | uprn BIGINT (one row per UPRN), ~100 non-address EPC columns incl. built_form, property_type, epc_count | epc.py |
| `inspire_parcel.parquet` (optional) | uprn BIGINT, parcel_inspire_id VARCHAR, parcel_area_m2 DOUBLE, uprns_in_parcel INTEGER (+ `parcel_source='ros'` for Scottish LADs from RoS INSPIRE; never overwrites a non-empty E&W file) | inspire.py, ros.py |
| `inspire_polygons.parquet` (optional) | parcel_inspire_id VARCHAR, geom_wkb BLOB (EPSG:27700). Matched parcels only; source of the published `parcels.parquet` (`parcels.py`). | inspire.py, ros.py |

The UPRN set for a LAD is defined by ONSUD. Other UPRN-keyed tables are filtered to those UPRNs;
`open_toid` to the TOIDs referenced; `buildings` to OpenMap Local buildings intersecting the LAD's
UPRN extent (bbox plus a 200 m buffer). `building_id` = sha1 hex of the polygon WKB (not persistent across releases).

## National Parquet layout (`data/parquet/`)
| Path | Columns |
|---|---|
| `lids/lids.parquet` | uprn BIGINT, toid VARCHAR, lids_confidence VARCHAR (+ any version fields) |
| `open_uprn/open_uprn.parquet` | uprn BIGINT, x DOUBLE, y DOUBLE, lat DOUBLE, lon DOUBLE |
| `open_toid/<tile>.parquet` | toid VARCHAR, toid_x DOUBLE, toid_y DOUBLE — only TOIDs present in lids.parquet |
| `openmap_local/<tile>.parquet` | building_id VARCHAR, geom_wkb BLOB — Building layer only |
| `onsud/onsud.parquet` | uprn BIGINT, lad VARCHAR, oa VARCHAR, lsoa VARCHAR, msoa VARCHAR, postcode VARCHAR |

`uprn_resolver.convert.convert(dataset: str, src: Path, parquet_dir: Path) -> int` converts one extracted
source file (CSV / shapefile / GPKG) to the layout above, appends to `parquet_dir/manifest.json`, returns
row count; the caller deletes the source afterwards. Open TOID conversion requires `lids.parquet` to exist.

## Commands (`uprn_resolver.cli`)
`download [--datasets D..] [--areas A..] [--sources S..] [--pbf F]` (default sources core, hmlr, streets, epc, inspire, ros; opt-in provenance, ostn15, osm, osm_buildings, msbuildings, overture),
`stage (--lad X | --all [--restage] [--no-epc])`, `build (--lad X | --all [--rebuild] [--threads --memory-limit --temp-dir --include-postcode])`,
`address`, `provenance [--download]`, `poi [--download] [--assign]`, `osm [--convert --pbf]`, `outlines [--source osm|ms|both] [--convert]`,
`resolve [--include-postcode]` (each: `--lad X | --all [--rebuild]`), `national [--report]`, `validate [--lad X] [--report-dir]`, `qa --lad`,
`publish --release --dest [--execute] [--stage-dir] [--allow]` (dry run by default), `ni extract|addresses`, `lookup UPRN [--local]`,
`pipeline (--lad X | --all) [--restage] [--rebuild] [--skip-extras]`, `all --lad`. Lazy imports; optional sources warn and continue; `--all` loops skip finished LADs unless `--rebuild`/`--restage`.

## Function signatures
Per-LAD functions are `f(lad, staged_dir, out_dir, parquet_dir)`; `*_all(staged_dir, out_dir, parquet_dir, lads=None, skip_existing=True)` returns `{built|done, skipped, failed}`.
- `download.download(datasets, raw_dir, areas=None)` — `open_uprn`, `lids`, `open_toid`, `openmap_local`, `onsud`; Parquet goes to the sibling `parquet/`. Other sources: `hmlr.download_hmlr`, `streets.download_streets`, `epc.download_epc`, `inspire.download_inspire`, `ros.download_ros`, `provenance.download_provenance_sources`, `osm.download_osm` + `osm.convert_osm`, `osm_buildings.convert_osm_buildings`, `msbuildings.fetch_links` + `convert_all_tiles`, `poi.download_places`, `bng.fetch_grid`.
- `stage.stage(lad, raw_dir, staged_dir)`; `stage.stage_all(raw_dir, staged_dir, lads=None, skip_existing=True, include_epc=True)`; optional `hmlr/streets/epc/inspire/ros.stage_*(lad, raw_dir, staged_dir)` (`hmlr.stage_hmlr` reads `staged/<lad>/onsud.parquet`).
- `build.build(lad, staged_dir, out_dir, include_postcode=False, *, threads=None, memory_limit=None, temp_directory=None) -> dict` writes `out/lad=<lad>/{uprn,buildings}.parquet`, `stats.json`, plus `epc.parquet` when EPC is staged. `build.build_all(...)` skips LADs with `stats.json`. Buildings straddling LADs appear in both (same `building_id`); `uprns_in_building` is per LAD.
- Extras: `address.address_local` (needs staged streets), `provenance.provenance_for_lad`, `poi.poi_for_lad` (after `download_places` + `assign_places_lad`), `osm.osm_for_lad`, `osm_buildings.osm_outline_for_lad`, `msbuildings.ms_outline_for_lad`.
- `resolve.resolve_lad(lad, out_dir, include_postcode=False, *, staged_dir=None)`, `resolve.resolve_all(out_dir, lads=None, ...)`, `resolve.lookup(uprn, out_dir, local=False)`.
- `national.national(out_dir, report_path)`, `validate.validate(out_dir, staged_dir, parquet_dir, report_dir, lads=None)`, `qa.qa(lad, out_dir, report_path)`.
- `publish.assemble(out_dir, release, stage_dir, allow=())`, `publish.publish(out_dir, release, dest, dry_run=True, stage_dir=None, allow=())`.
- `ni.extract_ni(pbf, parquet_dir)`, `ni.ni_address_points(parquet_dir, out_dir)`.

## Output columns
`uprn.parquet` (GeoParquet, Point, EPSG:27700): uprn, x, y, geometry, toid, toid_count, lids_confidence, toid_x, toid_y,
building_id, match_method, methods_disagree, uprns_in_building, outline_granularity, oa, lsoa, msoa,
usrn, street_name, street_name_alt, locality, street_method, usrn_count (Open Names/Open Roads, OGL),
parcel_inspire_id, parcel_area_m2, uprns_in_parcel, epc_built_form, epc_property_type, epc_count,
ppd_transaction_ids, inspire_ids (HMLR), postcode (opt-in only).

`outline_granularity` (NULL when no building): `single` if uprns_in_building = 1; else `parcel_finer` if
uprns_in_parcel < uprns_in_building (the INSPIRE parcel is a finer outline than the building); else `shared`
(includes UPRNs with no parcel).

`epc.parquet` (plain Parquet, not geo; one row per UPRN): uprn + every staged EPC column. Only written when
EPC is staged. `uprn.parquet` carries just epc_built_form, epc_property_type, epc_count.

`stats.json` adds: with_street_name, with_parcel, with_epc ({count, pct}), uprns_in_parcel (1, 2-5, 6-20, >20),
outline_granularity (single/parcel_finer/shared/none counts), epc_built_form distribution, *_present flags.

## Companion tables (`out/lad=<code>/`)
| File | Key columns | Licence |
|---|---|---|
| `provenance.parquet` | uprn, usrn, usrn_street_type, usrn_distance_m, usrn_link_flag, street_name_fallback*, postcode_flag, codepoint_positional_quality, ... | OGL |
| `poi.parquet` | poi_id, name, category, brand, overture_confidence, uprn (nullable), match_method, match_distance_m, match_confidence | Overture per provider (+ `poi.LICENCE.txt`) |
| `streets_odbl.parquet` | uprn, osm_street_name, osm_way_id, osm_agrees_os, osm_housenumber, osm_housename, osm_addr_*, building_level_address, licence | ODbL (+ `.LICENCE.txt`) |
| `outline_odbl.parquet`, `outline_odbl_buildings.parquet` | uprn, osm_building_id, uprns_in_osm_building, osm_match_method, ogl_building_id, osm_finer_than_ogl, ... / OSM polygons (GeoParquet) | ODbL |
| `outline_ms.parquet`, `outline_ms_buildings.parquet` | uprn, ms_building_id, uprns_in_ms_building, ms_area_m2, match_method, area_ratio_ms_to_ogl, ... / Microsoft polygons | CDLA-Permissive-2.0 |
| `resolved_open.parquet` | uprn, x, y, lat, lon, geometry, best_outline_id, best_outline_source, outline_confidence, street_name, street_source, property_type_epc, poi_*, lsoa, quality_score, quality | OGL + Overture attachment |
| `ppd_local.parquet` | uprn, transaction_id, price, date, property_type, old_new, duration, match_method (hmlr_lookup / address_match), match_confidence, am_confidence, am_agrees, ppd_category_type, paon, saon, street, locality, town_city, postcode, lad (one row per sale) | restricted (HMLR OGL v3.0 + Royal Mail / OS address restriction); NEVER published (+ `ppd_local.LICENCE.txt`) |
| `ppd_local_summary.parquet` | uprn, sales_count, last_price, last_sale_date, first_sale_date, lad | local only (same terms) |
| `ppd_open.parquet`, `ppd_open_summary.parquet` | transaction_id-level sales / per-UPRN summary without address text | OGL v3.0 (+ `ppd_open.LICENCE.txt`); not yet assembled or joined into `resolved_open` (pending HMLR confirmation) |
| `resolved_local.parquet`, `address_local.parquet` | resolved_open plus postcode and full address (EPC > PPD > OSM > partial); `address_source` in {epc_*, ppd, osm_high, osm_medium, open_partial}, `address_licence` in {EPC-restricted, PPD-restricted, ODbL-1.0, OGL}; `resolved_local` also has ppd_sales_count, ppd_last_price, ppd_last_sale_date, ppd_first_sale_date (left join of `ppd_local_summary`, NULL when none or no PPD file e.g. Scotland) | restricted; NEVER published |

Release layout (`publish.assemble`), hive-partitioned, one file per table per LAD: `uprn/lad=<code>/uprn.parquet` and
`buildings/lad=<code>/buildings.parquet` (GeoParquet 1.1, Hilbert, `bbox` covering), `epc/`, `provenance/`, `poi/` (+`LICENCE.txt`) as plain Parquet
sorted by uprn, `streets_odbl/` (+`LICENCE.txt`, ODbL), `lad_index.parquet` (lad, n_uprn, bbox, bytes), `stats.json`, README, LICENCE.
`resolved_open`, `outline_odbl` and `outline_ms` are not yet assembled. Column denylist (address, street, thoroughfare, building_name,
saon, paon, locality, town, postcode) applies to every OGL file, except the exempt names in `publish.ALLOWED_NAMES`; postcode needs `allow`.
`streets_odbl` is checked with its own rules. `address_local`, `resolved_local` and `ppd_local*` are never read.

## Output rules
- No address text in OGL files (street names/localities from OS Open Names are OGL and allowed). House numbers and names exist only in `streets_odbl` (ODbL) and the local-only tables. INSPIRE/RoS polygons of parcels holding a UPRN are published in `parcels` (OGL, with the HMLR, OS AC0000851063 and RoS statements). OSM and Microsoft polygons live only in their own licence-labelled files; ODbL tables are never merged into OGL ones.
- Northern Ireland has no UPRNs: `out/ni/` and `data/parquet/ni/` are OSM-only (ODbL), EPSG:29903 plus WGS84.
- Postcode only when `include_postcode=True` (ONS licensing unclear).

## Decisions
- **Multi-TOID rule (build):** when a UPRN maps to several TOIDs, prefer a TOID that has Open TOID coordinates, then the lexicographically smallest. `toid_count` records the number of distinct TOIDs.
- **building_id:** sha1 of the polygon WKB. Identical geometries dedupe. Not persistent across releases.
- **DuckDB spatial:** the extension is installed on first use (needs network once; cached in `~/.duckdb`).
- **CLI areas:** the CLI passes `--areas` via the env var `UPRN_RESOLVER_AREAS`.
- **Known mismatch:** ONSUD and Open UPRN can disagree (UPRN 10093645993: ONSUD Torridge, point near Cambridge); the LAD comes from ONSUD.
- **HMLR files:** headerless CSV; transaction IDs normalised to `{UPPER-GUID}`.
- **HMLR coverage:** only August 2026 onward exists.
