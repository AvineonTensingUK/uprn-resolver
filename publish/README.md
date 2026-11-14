# UK UPRN open resolver

Open lookup from UPRN to point, TOID, approximate building outline, land parcel ID, street name,
statistical geographies and HM Land Registry identifiers, for Great Britain (41,593,127 UPRNs in 350 local
authorities). Only street name and locality are included in the OGL tables, never house number or name
(Royal Mail / OS third-party rights); house numbers exist only in the separately licensed `streets_odbl`
table. Parcels come from HMLR INSPIRE (England & Wales) and Registers of Scotland. Not Northern Ireland, which
has no open UPRNs. Built by [uprn-resolver](https://github.com/AvineonTensingUK/uprn-resolver) (the repository's
`RESEARCH.md` explains why this approach).

## Layout

One file per table per local authority (LAD), hive-partitioned, so `lad=<code>` is a queryable column:

```
README.md  LICENCE.md  stats.json  lad_index.parquet   (lad, n_uprn, xmin, ymin, xmax, ymax, bytes)
release_manifest.json                      every file with size, sha256, row count; source versions
stac/                                      static STAC catalogue (catalog.json, collections, one item per LAD)
tiles/                                     PMTiles vector tiles for web maps, one archive per licence
resolved_open/lad=<code>/resolved_open.parquet + LICENCE.txt  GeoParquet 1.1, Point  OGL v3 + labelled Overture/Microsoft columns
parcels/lad=<code>/parcels.parquet + LICENCE.txt  GeoParquet 1.1, Polygon  OGL v3 (HMLR INSPIRE, Registers of Scotland)
uprn/lad=<code>/uprn.parquet               GeoParquet 1.1, Point    OGL v3
buildings/lad=<code>/buildings.parquet     GeoParquet 1.1, Polygon  OGL v3
epc/lad=<code>/epc.parquet                 plain Parquet            OGL v3
provenance/lad=<code>/provenance.parquet   plain Parquet            OGL v3
poi/lad=<code>/poi.parquet  + poi/LICENCE.txt                      Overture, per provider
streets_odbl/lad=<code>/streets_odbl.parquet + LICENCE.txt         ODbL-1.0
ppd_open/lad=<code>/ppd_open.parquet + LICENCE.txt                 plain Parquet, OGL v3 (HMLR)
ppd_open_summary/lad=<code>/ppd_open_summary.parquet + LICENCE.txt plain Parquet, OGL v3 (HMLR)
outline_ms/lad=<code>/outline_ms.parquet + LICENCE.txt            plain Parquet            CDLA-Permissive-2.0
outline_ms_buildings/lad=<code>/outline_ms_buildings.parquet      GeoParquet 1.1, Polygon  CDLA-Permissive-2.0
outline_odbl/lad=<code>/outline_odbl.parquet + LICENCE.txt        plain Parquet            ODbL-1.0
outline_odbl_buildings/lad=<code>/outline_odbl_buildings.parquet  GeoParquet 1.1, Polygon  ODbL-1.0
```

At the product root (next to the release folders), `latest.json` names the newest release and gives the paths of
its `release_manifest.json` and `stac/catalog.json`. A release folder is immutable once published; check the
manifest's sha256 values to verify a download.

Spatial files are sorted along a Hilbert curve and carry a per-row `bbox` covering column (GeoParquet 1.1);
filter on `bbox.xmin/xmax/ymin/ymax` to skip row groups, and use `lad_index.parquet` to choose files by
extent. Coordinates are British National Grid, EPSG:27700; geometry is WKB. Details: `docs/format.md` in the repository.

Licences differ by table and must not be mixed: the OGL tables (`uprn`, `buildings`, `epc`, `provenance`) are
OGL v3.0 (`ppd_open*` and `parcels` also OGL v3.0, the latter with its own attribution statements); `poi` follows each Overture provider's licence (CDLA-Permissive-2.0 for most, Apache-2.0 for
Foursquare, CC0-1.0 for AllThePlaces); `streets_odbl` is OpenStreetMap data under ODbL-1.0 (share-alike,
"© OpenStreetMap contributors") and is kept in its own folder, as are the OSM building outlines (`outline_odbl*`, ODbL-1.0).
`outline_ms*` (Microsoft footprints) are CDLA-Permissive-2.0, and `resolved_open` is OGL v3.0 with labelled
Overture and Microsoft columns (see `LICENCE.md`). `address_local`, `resolved_local` and `ppd_local` (full addresses, postcode) are never published.

## uprn.parquet

| Column | Type | Description |
|---|---|---|
| uprn | BIGINT | UPRN (ONSUD defines the UPRN set) |
| x, y | DOUBLE | UPRN coordinates (OS Open UPRN) |
| geometry | BLOB | UPRN point, WKB |
| toid | VARCHAR | TOID from OS Open Linked Identifiers (one chosen if several) |
| toid_count | INTEGER | Number of TOIDs linked to the UPRN |
| lids_confidence | VARCHAR | LIDS confidence value |
| toid_x, toid_y | DOUBLE | OS Open TOID point (rounded to 1 m) |
| building_id | VARCHAR | Matched building in buildings.parquet |
| match_method | VARCHAR | `toid_point` or `uprn_point` (fallback), or unmatched |
| methods_disagree | BOOLEAN | TOID point and UPRN point fall in different buildings |
| uprns_in_building | INTEGER | UPRNs matched to this building |
| outline_granularity | VARCHAR | `single` (only UPRN in its outline), `parcel_finer` (its INSPIRE parcel holds fewer UPRNs than its outline), `shared` (otherwise, incl. no parcel); NULL when no outline matched |
| usrn | BIGINT | Street identifier (LIDS) |
| street_name | VARCHAR | Street name (OS Open Names, via Road TOID) |
| street_name_alt | VARCHAR | Second street name for corner UPRNs with two USRNs |
| locality | VARCHAR | Locality (OS Open Names), mostly the town or city |
| street_method, usrn_count | VARCHAR, INTEGER | How the street name was derived; number of USRNs linked to the UPRN |
| parcel_inspire_id | VARCHAR | Parcel containing the UPRN: HMLR INSPIRE (England & Wales) or Registers of Scotland; the polygon is in `parcels.parquet` (`parcel_id`) |
| parcel_area_m2 | DOUBLE | Area of that parcel |
| uprns_in_parcel | INTEGER | UPRNs in that parcel |
| oa, lsoa, msoa | VARCHAR | Statistical geography codes (ONSUD) |
| ppd_transaction_ids | LIST(VARCHAR) | HMLR Price Paid transaction IDs (where available) |
| inspire_ids | LIST(VARCHAR) | HMLR INSPIRE polygon IDs (where available) |
| epc_built_form, epc_property_type | VARCHAR | EPC summary fields (see epc.parquet for coverage) |
| epc_count | BIGINT | Number of EPC certificates for the UPRN |

Postcode is only present in builds made with `--include-postcode`.

## buildings.parquet

| Column | Type | Description |
|---|---|---|
| building_id | VARCHAR | sha1 hex of the polygon WKB |
| geometry | BLOB | Building polygon, WKB, EPSG:27700 |

## parcels.parquet

Land parcel polygons: HM Land Registry INSPIRE Index Polygons (England and Wales) and Registers of Scotland INSPIRE
Cadastral Parcels. One row per parcel that contains at least one UPRN of the local authority; a parcel straddling
two authorities appears in both files. This is the geometry behind `uprn.parcel_inspire_id`, and behind
`resolved_open.best_outline_id` where `best_outline_source` is `inspire_parcel` or `ros_parcel`.

| Column | Type | Description |
|---|---|---|
| parcel_id | VARCHAR | INSPIRE ID (HMLR) or Registers of Scotland parcel ID |
| parcel_source | VARCHAR | `inspire_parcel` or `ros_parcel` (the `resolved_open.best_outline_source` values) |
| parcel_area_m2 | DOUBLE | Polygon area |
| uprns_in_parcel | INTEGER | UPRNs of this local authority inside the parcel |
| geometry | BLOB | Parcel polygon, WKB, EPSG:27700 |

A parcel shows the indicative ground-level extent of a registered freehold title. It is not a building outline, and
the legal extent of a title can only be established from the title plan. OGL v3.0; the HM Land Registry, Ordnance
Survey and Registers of Scotland statements in `parcels/LICENCE.txt` must be carried with any reuse.

```sql
SELECT r.uprn, r.outline_confidence, p.geometry
FROM read_parquet('<release>/resolved_open/lad=E09000019/resolved_open.parquet') r
JOIN read_parquet('<release>/parcels/lad=E09000019/parcels.parquet') p
  ON p.parcel_id = r.best_outline_id AND p.parcel_source = r.best_outline_source;
```

## epc.parquet

EPC certificates keyed by UPRN, one row per UPRN: Scotland (data.gov.scot) and England & Wales (GOV.UK bulk
download, where loaded). All non-address columns are kept; address and free-text columns are dropped and
lodgement dates outside 2007 to today are nulled. 54.9% of UPRNs nationally have an EPC.

## resolved_open.parquet

Best answer per UPRN. EPC bridge columns, all non-address OGL fields:

| Column | Type | Description |
|---|---|---|
| property_type_epc, built_form_epc | VARCHAR | EPC property type and built form (latest certificate) |
| epc_lmk_key | VARCHAR | Identifier of the UPRN's latest EPC certificate (`lmk_key` / certificate number); NULL when none |
| epc_certificate_type | VARCHAR | Type of that certificate (e.g. domestic, non-domestic, Scotland) |
| epc_lodgement_date | DATE | Lodgement date of that certificate (2007 to today) |
| epc_count | BIGINT | Number of EPC certificates for the UPRN |

EPC address data is not included; join on epc_lmk_key under the EPC data's own terms.

## ppd_open.parquet and ppd_open_summary.parquet

HM Land Registry Price Paid Data, England & Wales, keyed by UPRN. They contain only sales that HMLR itself links
to a UPRN in its published transaction-to-UPRN lookup (`match_method = 'hmlr_lookup'`). The lookup has been
published monthly since August 2026 and is not back-dated; in release 2026-10 it links 84,141 sales, mostly
completed in 2025 and 2026, with a few older ones. There are no address fields (no PAON, SAON, street, locality, town or postcode), and no
sales linked by comparing address text. Sales before the lookup began, or without a lookup entry, are absent, so
this is not the complete Price Paid dataset.

`ppd_open`: one row per sale: `uprn`, `transaction_id`, `price`, `date`, `property_type`, `old_new`, `duration`,
`ppd_category_type`. `ppd_open_summary`: one row per UPRN: `sales_count`, `last_price`, `last_sale_date`,
`first_sale_date`.

Attribution (also in each folder's `LICENCE.txt`): "Contains HM Land Registry data © Crown copyright and database
right 2021. This data is licensed under the Open Government Licence v3.0." The UPRNs contain OS data © Crown
copyright and database right; see `LICENCE.md`. The address-bearing local tables (`ppd_local`, `ppd_local_summary`)
are never published.

## stac/, release_manifest.json, latest.json, tiles/

- `stac/`: static STAC catalogue (`stac/catalog.json`) with one collection per table and one item per LAD,
  linking each Parquet file with its row count, schema (table extension) and extent. Browse it with any STAC client.
- `accuracy.json`: an independent check of `resolved_open` against OpenStreetMap buildings tagged with a UPRN
  (`ref:GB:uprn`), by `best_outline_source` x `outline_confidence`, split into buildings with one UPRN (`single`)
  or several (`multi`). `inside_pct`: at least half of the mapped building lies inside our outline.
  `exclusive_pct`: that, and no other tagged property has half its area inside our outline. Also the median
  area ratio (our outline / mapped building) and median intersection over union. Rows with a null `source`,
  `confidence` or `building` are totals. Aggregate statistics derived from OpenStreetMap
  (© OpenStreetMap contributors, ODbL-1.0); built with `uprn-resolver accuracy`.
- `release_manifest.json`: path, bytes and sha256 of every file in the release (plus rows and row groups for
  Parquet), per-table totals, and the versions of the source datasets used. It does not list itself.
- `latest.json` (product root): `{"latest": "<release>", "path", "manifest", "stac", "updated"}`; read it to find
  the newest release.
- `tiles/`: PMTiles vector tiles for web maps, one archive per licence: `uprn_points` (OGL, zoom 5-16),
  `outlines_ogl` (OGL), `parcels` (OGL, HMLR/RoS statements), `outlines_ms` (CDLA-Permissive-2.0) and `outlines_odbl` (ODbL-1.0), all zoom 13-16, plus
  `ni_addresses` (Northern Ireland OSM address points, ODbL-1.0, no UPRNs). The Parquet files remain the source of
  truth.
- `CHANGELOG.json` / `CHANGELOG.md` and `national/<table>/part-NNNN.parquet` appear when a release is built with
  `--changelog-against` / `--national-files`.

## provenance.parquet

Per-UPRN QA from Open USRN, Code-Point Open and Open Roads: `usrn_street_type`, `usrn_distance_m`,
`usrn_link_flag`, fallback street name (`street_name_fallback*`), `postcode_flag`, `codepoint_positional_quality`.
Postcode text columns are removed from the release.

## poi.parquet

Overture Maps Places matched to UPRNs (same building, then same parcel, then nearest within 30 m): `name`,
`category`, `brand`, `overture_confidence`, `uprn` (nullable), `match_method`, `match_distance_m`,
`match_confidence`. Attribution and per-provider licences are in `poi/LICENCE.txt`.

## streets_odbl.parquet

One row per UPRN with OpenStreetMap-derived `osm_street_name`, `osm_housenumber`, `osm_housename` and match
details. ODbL-1.0: a derived database that must stay under ODbL if redistributed; see `streets_odbl/LICENCE.txt`.

## Caveats

- Outlines are approximate: OS OpenMap Local is generalised and carries no TOID or UPRN.
- `building_id` is not persistent across releases (hash of geometry; OS re-generalises).
- Stacked flats and terraces: many UPRNs share one outline, and amalgamated terraces merge dwellings.
- In dense urban areas an "outline" is effectively a block (Islington: median polygon 376 m², 80% of UPRNs
  share an outline with more than 20 others). Use `uprns_in_building`, `uprns_in_parcel` and
  `outline_granularity` to judge how specific a match is; parcels are usually finer.
- Parcels: 91.9% of UPRNs nationally, with their polygons in `parcels.parquet`. Scottish parcels are Land Register only, so there are gaps; about 1.7% of Islington UPRNs fall in none.
- Street name covers 91.1% of UPRNs nationally; the rest sit on roads missing from the Highways network
  (likely estate or private roads). There are no house numbers or names.
- LIDS itself derives UPRN->TOID by point-in-polygon of the address seed, so TOID links inherit that error.
- UPRNs are not all dwellings (no classification in OS Open UPRN).
- ONSUD defines the UPRN set and LAD, and can disagree with OS Open UPRN: UPRN 10093645993 is in Torridge per ONSUD but its point is near Cambridge.
- WGS84 sources (OSM, Overture, Microsoft) are converted to EPSG:27700 with the OSTN15 grid, not a Helmert approximation.

## Example

```sql
INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;
SELECT u.uprn, u.lsoa, u.match_method, ST_AsText(ST_GeomFromWKB(b.geometry))
FROM read_parquet('https://data.source.coop/<account>/uk-uprn-open/<release>/uprn/lad=E09000019/uprn.parquet') u
JOIN read_parquet('https://data.source.coop/<account>/uk-uprn-open/<release>/buildings/lad=E09000019/buildings.parquet') b
  USING (building_id)
WHERE u.uprn = 5300000000;
```

(DuckDB 1.5 or later reads the geo-annotated column as `GEOMETRY` already; on older versions wrap it in `ST_GeomFromWKB`.)

## Releases

This is a one-off release. Further releases may follow; if so, `latest.json` at the product root will name the newest.

## National results

350 LADs, 41,593,127 UPRNs (ONSUD).

| Measure | Result |
|---|---|
| Have a point / a TOID | 41,587,313 / 41,545,932 |
| Outline matched (TOID point + UPRN point) | 85.6% (85.2% + 0.4%); 14.4% none |
| `methods_disagree` | 982 of 35,079,063 matched by both routes |
| Street name | 91.1% |
| In a parcel (INSPIRE or RoS) | 91.9% |
| Have an EPC | 54.9% |
| `outline_granularity` single / parcel_finer / shared / none | 4,579,585 / 19,807,866 / 11,230,386 / 5,975,290 |

Pilot, Islington (E09000019), 169,184 UPRNs: 80% of UPRNs share an outline with more than 20 others
(median polygon 376 m2), 98.3% fall in a parcel, 93.5% have a street name.

## Licence

OGL v3.0 for the OGL tables, with required attribution; ODbL and Overture terms as above. See [LICENCE.md](LICENCE.md).
