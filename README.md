# uprn-resolver

An open, OGL-first dataset and pipeline that resolves a Great Britain UPRN to a point, a TOID, an approximate
building outline, a land parcel, a street name and statistical geographies, and says honestly how good each
answer is. Full addresses are never published: they are built only on the local machine.

National build, release 2026-11 (350 local authorities, 41,593,127 UPRNs from ONSUD):

| Measure | Share of UPRNs |
|---|---|
| Matched to an OpenMap Local outline | 85.6% (85.2% via TOID point, 0.4% via UPRN point) |
| Street name (OS Open Names) | 91.1% |
| In a land parcel (HMLR INSPIRE for England & Wales, RoS for Scotland) | 91.9% |
| EPC property type / built form | 54.9% |
| Best outline of any kind (building or parcel) | 97.0% (`property` level: 47.5% of all UPRNs) |
| `property` outlines holding no other mapped property (checked against OpenStreetMap) | 98.4% |

`outline_granularity`: `single` 4,580,345, `parcel_finer` 19,807,622, `shared` 11,228,688, `none` 5,976,472.
Outlines are approximate: OS OpenMap Local is generalised and amalgamates terraces, so in dense areas an
"outline" is a block (Islington: median polygon 376 m2). Use `uprns_in_building`, `uprns_in_parcel` and
`outline_granularity` to judge each match. See [RESEARCH.md](RESEARCH.md) for why this approach.

The full statistics breakdown (coverage by nation, outline sources and confidence, streets, EPC, quality flags,
optional layers and table sizes) is in the [dataset README](publish/source-coop/README.md#statistics).

## Products

Per LAD under `data/out/lad=<code>/`. Coordinates are British National Grid (EPSG:27700); WGS84 inputs are
converted with the OSTN15 grid (`uprn_resolver/bng.py`), never DuckDB's Helmert-only transform (about 2 m out).

| Table | Content | Licence | In the lean release |
|---|---|---|---|
| `resolved_open` | Best answer per UPRN: point, outline id and confidence, street, EPC, POI name, quality | OGL plus labelled Overture columns | yes |
| `uprn` | Core table: TOID, matched OpenMap Local building, parcel, USRN, OA/LSOA/MSOA, EPC and Price Paid ids | OGL v3 | yes |
| `buildings` | OS OpenMap Local outlines | OGL v3 | yes |
| `parcels` | Land parcel polygons holding a UPRN (HMLR INSPIRE, Registers of Scotland); the geometry behind a parcel `best_outline_id` | OGL v3, own attribution statements | yes, with `LICENCE.txt` |
| `outline_ms_buildings` | Microsoft GlobalML building footprints | CDLA-Permissive-2.0 | only footprints used as a best outline |
| `outline_ms` | Microsoft footprint match per UPRN | CDLA-Permissive-2.0 | `--full` only |
| `epc` | EPC certificate fields (no address columns) | OGL v3 | `--full` only |
| `provenance` | Street/postcode QA per UPRN (Open USRN, Code-Point Open, Open Roads); postcode text dropped | OGL v3 | `--full` only |
| `poi` | Overture Places matched to UPRNs (business names) | per provider: CDLA-Permissive-2.0, Apache-2.0 (Foursquare), CC0 | `--full` only |
| `ppd_open` (+ `_summary`) | HMLR Price Paid sales in HMLR's UPRN lookup, no address fields | OGL v3 | `--full` only |
| `streets_odbl` | OSM street names and `addr:*` house numbers/names | ODbL-1.0 | `--full` only, kept apart |
| `outline_odbl` (+ `_buildings`) | OSM building polygons per UPRN, finer than OpenMap Local | ODbL-1.0 | `--full` only, kept apart |
| `resolved_local` | `resolved_open` plus postcode and best full address (EPC > OSM > partial) | restricted | **never** |
| `address_local` | EPC address text | EPC-restricted | **never** |

Published releases use plain names (`uprn_resolver/names.py`): `resolved_open` is `uprn_lookup`, `uprn` is
`uprn_links`, `buildings` is `os_buildings`, `parcels` is `land_parcels` and `outline_ms_buildings` is
`microsoft_buildings`; `best_outline_source` values become `os_building`, `land_parcel` and `microsoft_building`.

`publish` ships the lean release by default (`publish.LEAN_TABLES`): the answer per UPRN and the shapes its best
outline points to, plus `uprn_points`, `outlines_ogl` and `parcels` tiles. `publish --full` ships every table
present. Each table ships as one GeoParquet 1.1 file (`<table>/<table>.parquet`, Hilbert ordered, `bbox`
covering column, `lad` column); `--per-lad` keeps one file per table per LAD instead; see [docs/format.md](docs/format.md) and [CONTRACT.md](CONTRACT.md).

Scotland has parcels from Registers of Scotland INSPIRE (Land Register only, so gaps) and Scottish EPC;
England & Wales EPC needs a GOV.UK One Login download. Northern Ireland has no open UPRNs: `ni` builds an
OSM-only (ODbL) building and address layer that cannot be joined to GB UPRNs.

## Setup

```
uv sync
uv run uprn-resolver download --sources ostn15      # once: OSTN15 grid (3 MB) for any WGS84 -> BNG source
```

## Commands

`--data-dir` (default `./data`) is the base for `raw/`, `parquet/`, `staged/`, `out/`. Per-LAD loops are
resumable: finished LADs are skipped unless `--rebuild` (`--restage` for `stage`). `python -m uprn_resolver` also works.

```
download [--datasets ...] [--areas TQ] [--sources SRC ...] [--pbf FILE]
         default sources: core hmlr streets epc inspire ros; opt-in: provenance ostn15 osm osm_buildings msbuildings overture
stage      --lad X | --all [--restage] [--no-epc]
build      --lad X | --all [--rebuild] [--threads N --memory-limit S --temp-dir D] [--include-postcode]
address    --lad X | --all [--rebuild]              # local-only address_local
provenance [--download] [--lad X | --all] [--rebuild]
poi        [--download] [--assign] [--lad X | --all] [--rebuild]
osm        [--convert --pbf FILE] [--lad X | --all] [--rebuild]       # streets_odbl
outlines   [--source osm|ms|both] [--convert] --lad X | --all [--rebuild]
parcels    --lad X | --all [--rebuild]              # parcels.parquet: HMLR INSPIRE / RoS polygons holding a UPRN
accuracy   --release R [--lad X ...] [--rebuild]   # check resolved_open against OSM buildings tagged with a UPRN
resolve    --lad X | --all [--rebuild] [--include-postcode]
national   [--report FILE]                          # out/national/ roll-up and national.html
validate   [--lad X ...] [--report-dir DIR]         # data/validation/report.html + results.json
qa         --lad X [--report FILE]
publish    --release R --dest s3://acct/product/ [--execute] [--full] [--per-lad] [--stage-dir D] [--allow COL ...]   # dry run by default
ni         extract [--pbf UK.osm.pbf] | addresses
lookup     UPRN [--local]                           # resolved_open row as JSON; --local reads resolved_local
pipeline   --lad X | --all [--restage] [--rebuild] [--skip-extras]   # stage, build, extras, resolve, national, validate
all        --lad X                                  # download, stage, build, QA one LAD
```

Optional sources (HMLR, streets, EPC, INSPIRE, RoS, every extra) warn and continue on failure. `pipeline` runs
the extras in the order address, provenance, poi, osm, outlines (osm then ms), parcels and does not download.
`osm`, `osm_buildings` and `ni` read a local PBF (never modified): `ni` needs a UK extract
(`united-kingdom-*.osm.pbf`) because the Geofabrik GB extract has no Northern Ireland.

## Known anomalies

- ONSUD and OS Open UPRN can disagree: UPRN 10093645993 is in Torridge according to ONSUD, but its Open UPRN
  point is near Cambridge. LAD membership comes from ONSUD, so it lands in a LAD it is not inside.
- 5,814 ONSUD UPRNs have no Open UPRN point; 47,195 have no TOID.
- HMLR Price Paid and INSPIRE lookups cover August 2026 onward only (0.2% of UPRNs).
- `building_id` is a hash of the polygon and is not stable across releases; buildings straddling two LADs
  appear in both files.

## Licence and attribution

OGL v3.0 for the OGL tables; ODbL for OSM-derived tables; CDLA-Permissive-2.0 and the Overture provider
licences where stated. Attribution statements are in [publish/LICENCE.md](publish/LICENCE.md) and must be
carried with any reuse. Sources: [DATASOURCES.md](DATASOURCES.md).
