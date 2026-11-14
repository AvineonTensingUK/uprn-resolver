# uprn-resolver

An open, OGL-first dataset and pipeline that resolves a Great Britain UPRN to a point, a TOID, an approximate
building outline, a land parcel, a street name and statistical geographies, and says honestly how good each
answer is. Full addresses are never published: they are built only on the local machine.

National build, release 2026-10 (350 local authorities, 41,593,127 UPRNs from ONSUD):

| Measure | Share of UPRNs |
|---|---|
| Matched to an OpenMap Local outline | 85.6% (85.2% via TOID point, 0.4% via UPRN point) |
| Street name (OS Open Names) | 91.1% |
| In a land parcel (HMLR INSPIRE for England & Wales, RoS for Scotland) | 91.9% |
| EPC property type / built form | 54.9% |
| Best outline of any kind (building or parcel) | 97.0% (`property` level: 47.5% of all UPRNs) |
| `property` outlines holding no other mapped property (checked against OpenStreetMap) | 98.4% |

`outline_granularity`: `single` 4,579,585, `parcel_finer` 19,807,866, `shared` 11,230,386, `none` 5,975,290.
Outlines are approximate: OS OpenMap Local is generalised and amalgamates terraces, so in dense areas an
"outline" is a block (Islington: median polygon 376 m2). Use `uprns_in_building`, `uprns_in_parcel` and
`outline_granularity` to judge each match. See [RESEARCH.md](RESEARCH.md) for why this approach.

The full statistics breakdown (coverage by nation, outline sources and confidence, streets, EPC, quality flags,
optional layers and table sizes) is in the [dataset README](publish/source-coop/README.md#statistics).

## Products

Per LAD under `data/out/lad=<code>/`. Coordinates are British National Grid (EPSG:27700); WGS84 inputs are
converted with the OSTN15 grid (`uprn_resolver/bng.py`), never DuckDB's Helmert-only transform (about 2 m out).

| Table | Content | Licence | Published |
|---|---|---|---|
| `uprn`, `buildings`, `epc` | Core OGL tables (see [publish/README.md](publish/README.md)) | OGL v3 | yes |
| `parcels` | Land parcel polygons holding a UPRN (HMLR INSPIRE, Registers of Scotland); the geometry behind a parcel `best_outline_id` | OGL v3, own attribution statements | yes, with `LICENCE.txt` |
| `provenance` | Street/postcode QA per UPRN (Open USRN, Code-Point Open, Open Roads); postcode text dropped from the release | OGL v3 | yes |
| `poi` | Overture Places matched to UPRNs (business names) | per provider: CDLA-Permissive-2.0, Apache-2.0 (Foursquare), CC0 | yes, with `LICENCE.txt` |
| `streets_odbl` | OSM street names and `addr:*` house numbers/names | ODbL-1.0 | yes, kept apart |
| `outline_odbl` (+ `_buildings`) | OSM building polygons per UPRN, finer than OpenMap Local | ODbL-1.0 | kept apart |
| `outline_ms` (+ `_buildings`) | Microsoft GlobalML building footprints per UPRN | CDLA-Permissive-2.0 | separate |
| `resolved_open` | Best answer per UPRN: point, outline id and confidence, street, EPC, POI, quality | OGL plus labelled Overture attachment | yes |
| `resolved_local` | `resolved_open` plus postcode and best full address (EPC > OSM > partial) | restricted | **never** |
| `address_local` | EPC address text | EPC-restricted | **never** |

`outline_odbl`, `outline_ms` and `resolved_open` are built per LAD but are not yet part of `publish.assemble`
(which ships `uprn`, `buildings`, `epc`, `provenance`, `poi`, `streets_odbl`). The release is hive-partitioned,
one file per table per LAD, GeoParquet 1.1 with Hilbert ordering and a `bbox` covering column; see
[docs/format.md](docs/format.md) and [CONTRACT.md](CONTRACT.md).

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
publish    --release R --dest s3://acct/product/ [--execute] [--stage-dir D] [--allow COL ...]   # dry run by default
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
