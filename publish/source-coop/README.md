# GB UPRN Open Resolver

Every Unique Property Reference Number (UPRN) in Great Britain, 41,593,127 of them, resolved to a point, a TOID,
an approximate building outline, a land parcel, a street name and statistical geographies. Each answer comes with
a measure of how specific it is, so you can tell a house's own outline from a city block.

Built only from open data, almost all under the Open Government Licence v3.0. Two small parts carry their own
open licences, both labelled: Microsoft building footprints, used where no OS building or land parcel fits, and
business names from Overture Maps. No house numbers, building names or postcodes are published.

- **Coverage:** England, Scotland and Wales, 350 local authorities. Northern Ireland has no open UPRNs (see below).
- **Format:** GeoParquet 1.1, one file per table, plus a STAC catalogue and PMTiles.
- **Release:** `2026-11`, a one-off snapshot. There may be further releases; if so, `latest.json` will name the
  newest.
- **Code and method:** [github.com/AvineonTensingUK/uprn-resolver](https://github.com/AvineonTensingUK/uprn-resolver), open source under the
  Apache License 2.0

## Why this exists

Since 2020 the UPRN has been openly available, which makes it a natural common key for property data in Great
Britain. The open products each cover part of the picture: OS Open UPRN gives the location, OS Open Linked
Identifiers links a UPRN to a building TOID and a street, and OS OpenMap Local provides building outlines. Putting
them together to answer "which building, which parcel, which street?" for a UPRN still takes a fair amount of
work, which can be a barrier for researchers, civic tech groups, students and open-source projects.

This dataset brings those open pieces together in one place, uses geometry to bridge the gaps between them, and
records how confident each answer is. The repository's `RESEARCH.md` describes the approach in detail.

**Ordnance Survey's own products remain the recommended choice for authoritative or production use.** OS NGD
Buildings and OS MasterMap Topography Layer provide the definitive building polygon for each TOID, and
AddressBase and the OS Places API provide full addresses and classification for each UPRN. Public sector
organisations can access these under the Public Sector Geospatial Agreement (PSGA). This dataset is an open
approximation intended to complement them, not to stand in for them.

This is a side project by staff at Avineon Tensing, for open-source and research activities, and is not an
official Avineon Tensing product. It is not produced or endorsed by Ordnance Survey or any of the other data
publishers, whose open data releases made it possible.

## Quick start

Look up one UPRN with DuckDB:

```sql
INSTALL spatial; LOAD spatial; INSTALL httpfs; LOAD httpfs;

SELECT uprn, street_name, locality, outline_confidence, best_outline_source, lsoa, quality
FROM read_parquet('https://data.source.coop/avineon-tensing/uprn-resolver/2026-11/uprn_lookup/uprn_lookup.parquet')
WHERE uprn = 5300000000;
```

Every UPRN points to the shape of its best outline. `best_outline_source` says which table holds it, and
`best_outline_id` is the key:

| `best_outline_source` | Table | Key |
|---|---|---|
| `land_parcel` | `land_parcels/` | `parcel_id` |
| `os_building` | `os_buildings/` | `building_id` |
| `microsoft_building` | `microsoft_buildings/` | `ms_building_id` |

For example, the land parcels behind UPRNs in Islington:

```sql
SELECT r.uprn, r.outline_confidence, p.parcel_area_m2, p.geometry
FROM read_parquet('https://data.source.coop/avineon-tensing/uprn-resolver/2026-11/uprn_lookup/uprn_lookup.parquet') r
JOIN read_parquet('https://data.source.coop/avineon-tensing/uprn-resolver/2026-11/land_parcels/land_parcels.parquet') p
  ON p.parcel_id = r.best_outline_id
WHERE r.lad = 'E09000019' AND p.lad = 'E09000019'
  AND r.best_outline_source = 'land_parcel';
```

Each table is a single file, read over HTTP without downloading it: DuckDB, GDAL, QGIS and pandas fetch only the
parts a query needs. Rows are sorted spatially (Hilbert curve) in blocks of 100,000, and each block records its
extent, so filtering on `lad` (the local authority code) or on the `bbox` column skips most of the file. Looking
up a UPRN without a `lad` or area filter reads the whole `uprn` column, about 80 MB; add `WHERE lad = '…'` when
you know the authority. `lad_index.parquet` gives each authority's extent and UPRN count.

Coordinates are British National Grid (EPSG:27700). WGS84 sources were converted with the OSTN15 grid, not a
Helmert approximation.

## What's in a release

| Folder | One row per | Contents | Licence |
|---|---|---|---|
| `uprn_lookup/` | UPRN | **Start here.** Best answer per UPRN: point, best outline and its confidence, street, local authority and LSOA, EPC property type and built form, business name, quality score and flags | OGL v3.0; the business name columns are Overture Maps (per provider) |
| `uprn_links/` | UPRN | More detail per UPRN: TOID, matched OS building, land parcel, USRN, OA / LSOA / MSOA, EPC certificate and Price Paid sale IDs | OGL v3.0 |
| `os_buildings/` | building | OS OpenMap Local building outlines | OGL v3.0 |
| `land_parcels/` | land parcel | Land parcels that contain a UPRN: HM Land Registry INSPIRE (England and Wales) and Registers of Scotland | OGL v3.0, with HMLR, OS and RoS statements |
| `microsoft_buildings/` | footprint | Microsoft building footprints that are some UPRN's best outline (11% of UPRNs) | CDLA-Permissive-2.0 |
| `tiles/` | | PMTiles for web maps: `uprn_points`, `os_buildings` and `land_parcels` | OGL v3.0 |
| `stac/` | | Static STAC catalogue: one collection and one item per table | |

Also in each release: `README.md` (column-by-column schemas), `LICENCE.md`, `accuracy.json` (the OpenStreetMap
check below), `stats.json` (per-authority statistics), `lad_index.parquet` (each authority's extent), and
`release_manifest.json` (size, sha256 and row count of every file, plus source dataset versions).

To keep the release simple, some things are left out. Each is free from its publisher, and the
[code](https://github.com/AvineonTensingUK/uprn-resolver) can rebuild them locally: full EPC certificates, HM
Land Registry Price Paid records, Overture Maps places, the complete Microsoft footprint set, and OpenStreetMap
streets, house numbers and buildings.

## Statistics

Figures for release `2026-11`, over every UPRN in the ONS UPRN Directory. Per-authority figures are in `stats.json`.

### Coverage

| | England | Scotland | Wales | **Great Britain** |
|---|---|---|---|---|
| Local authorities | 296 | 32 | 22 | **350** |
| UPRNs | 35,630,027 | 3,882,711 | 2,080,389 | **41,593,127** |
| Has a point (OS Open UPRN) | 99.98% | 99.99% | 99.96% | **99.98%** |
| Has a TOID (OS Open Linked Identifiers) | 99.87% | 99.89% | 99.88% | **99.87%** |
| Has a best outline of any kind | 97.5% | 93.6% | 96.2% | **97.0%** |
| Matched to an OS OpenMap Local building | 86.0% | 82.8% | 84.6% | **85.6%** |
| In a land parcel | 94.8% | 65.6% | 92.1% | **91.9%** |
| Has a street name | 92.3% | 85.4% | 81.3% | **91.1%** |
| Has an EPC property type | 55.7% | 49.8% | 51.8% | **54.9%** |
| Has a matched business name (Overture) | 3.9% | 3.0% | 3.8% | **3.8%** |
| Has a linked Price Paid sale | 0.2% | n/a | 0.2% | **0.2%** |
| Mean `quality_score` (0 to 100) | 84.7 | 79.2 | 84.5 | **84.2** |

Scotland's lower parcel coverage is expected: Registers of Scotland publish Land Register parcels only, and estimate
that 32% of ownership polygons are missing. Price Paid Data covers England and Wales only.

### Where the best outline comes from

`uprn_lookup.best_outline_source`, share of UPRNs:

| Source | England | Scotland | Wales | **Great Britain** |
|---|---|---|---|---|
| HM Land Registry INSPIRE parcel | 52.5% | n/a | 52.3% | **19,810,608 (47.6%)** |
| Registers of Scotland parcel | n/a | 33.3% | n/a | **1,293,403 (3.1%)** |
| OS OpenMap Local building | 33.4% | 50.1% | 35.0% | **14,582,801 (35.1%)** |
| Microsoft footprint | 11.5% | 10.1% | 9.0% | **4,672,139 (11.2%)** |
| None | 2.5% | 6.4% | 3.8% | **1,234,176 (3.0%)** |

Parcels often separate neighbouring properties better than OpenMap Local, which merges terraces into one shape.

### How specific the best outline is

`outline_confidence` says how many UPRNs share the best outline: `property` means none other, `small_group` two to
five, `block` more than five.

| | `property` | `small_group` | `block` | `none` |
|---|---|---|---|---|
| England | 48.4% | 20.2% | 28.9% | 2.5% |
| Scotland | 35.4% | 22.8% | 35.3% | 6.4% |
| Wales | 56.1% | 21.1% | 19.0% | 3.8% |
| **Great Britain** | **47.5%** | **20.5%** | **29.0%** | **3.0%** |

By source, in UPRNs:

| Source | `property` | `small_group` | `block` |
|---|---|---|---|
| HM Land Registry INSPIRE parcel | 12,376,110 | 3,421,171 | 4,013,327 |
| Registers of Scotland parcel | 690,734 | 207,430 | 395,239 |
| OS OpenMap Local building | 4,580,345 | 3,885,175 | 6,117,281 |
| Microsoft footprint | 2,128,955 | 1,008,743 | 1,534,441 |
| **All sources** | **19,776,144** | **8,522,519** | **12,060,288** |

Parcels supply two thirds of all `property`-level answers.

### OS OpenMap Local building match

From the `uprn_links` table, which uses OpenMap Local only:

| How the building was found | UPRNs |
|---|---|
| Building containing the TOID's point (OS Open TOID) | 35,443,787 (85.2%) |
| Building containing the UPRN's own point (fallback) | 172,868 (0.4%) |
| No building | 5,976,472 (14.4%) |

| UPRNs sharing the outline (`uprns_in_building`) or parcel (`uprns_in_parcel`) | Building | Parcel |
|---|---|---|
| 1 | 11.0% | 44.1% |
| 2 to 5 | 33.5% | 13.7% |
| 6 to 20 | 22.2% | 11.7% |
| More than 20 | 18.9% | 22.4% |
| None | 14.4% | 8.1% |

`outline_granularity`: `parcel_finer` (the parcel holds fewer UPRNs than the building) 47.6%, `shared` 27.0%,
`single` 11.0%, no outline 14.4%.

### Streets

| `street_source` | UPRNs |
|---|---|
| OS Open Names | 37,883,866 (91.1%) |
| OS Open Roads (fallback) | 27,483 (0.1%) |
| None | 3,681,778 (8.9%) |

Most UPRNs without a street name sit on roads missing from the open road network, usually private or estate roads.

### Energy Performance Certificates

| EPC property type | Share of all UPRNs |
|---|---|
| House | 32.4% |
| Flat | 14.4% |
| Bungalow | 4.7% |
| Maisonette | 1.2% |
| Non-domestic and other | 2.3% |
| No EPC | 45.1% |

Built form, for the 21.6 million UPRNs that have one: semi-detached 31.7%, mid-terrace 26.4%, detached 23.5%,
end-terrace 14.0%, enclosed terrace 2.3%, not recorded 2.1%.

### Quality

`quality_score` starts at 100 and loses points for each issue, listed in `quality`:

| `quality_score` | UPRNs |
|---|---|
| 100 | 41.4% |
| 90 to 95 | 16.6% |
| 70 to 85 | 29.2% |
| 50 to 65 | 10.3% |
| Below 50 | 2.5% |

| Flag in `quality` | UPRNs |
|---|---|
| ok | 15,391,734 (37.0%) |
| block outline | 12,060,288 (29.0%) |
| small-group outline | 8,522,519 (20.5%) |
| street far from USRN | 4,903,724 (11.8%) |
| outline from Microsoft footprints | 4,672,139 (11.2%) |
| no street name | 3,681,778 (8.9%) |
| postcode far from unit centroid | 1,876,426 (4.5%) |
| no outline | 1,234,176 (3.0%) |
| postcode not in Code-Point | 549,656 (1.3%) |
| USRN has no geometry | 150,524 (0.4%) |
| no USRN | 140,132 (0.3%) |
| street name from Open Roads fallback | 27,483 (0.1%) |
| building match methods disagree | 960 (0.0%) |

A UPRN can carry several flags. The postcode flags come from checks against Code-Point Open; no postcodes are
published.

### Release size

| Table | Rows | Size |
|---|---|---|
| `uprn_lookup` | 41,593,127 | 1.9 GB |
| `uprn_links` | 41,593,127 | 1.8 GB |
| `os_buildings` | 11,070,598 | 0.8 GB |
| `land_parcels` | 21,118,570 | 3.5 GB |
| `microsoft_buildings` | 2,914,012 | 0.4 GB |
| `tiles/` (3 PMTiles archives) | | 7.2 GB |
| **Whole release** | 3,518 files | **15.7 GB** |

### Checked against OpenStreetMap

About 3.4 million UPRNs carry an OpenStreetMap tag (`ref:GB:uprn`) on a building outline drawn by a mapper.
No OpenStreetMap data goes into `uprn_lookup`, so these give an independent check of what the confidence labels
claim. For the 2.8 million tagged buildings that hold a single UPRN, mostly houses:

| `outline_confidence` | Tagged UPRNs | Mapped building lies inside our outline | Our outline holds no other mapped property |
|---|---|---|---|
| `property` | 2,468,840 | 98.5% | **98.4%** |
| `small_group` | 256,056 | 97.6% | 9.9% |
| `block` | 81,550 | 98.1% | 4.6% |

So a `property` outline almost always belongs to that property alone, while `small_group` and `block` outlines
almost always take in neighbours, as their labels say. Where a `property` outline is an OS OpenMap Local or
Microsoft building, its area is typically within 5% of the mapped building's. Where it is a land parcel, it is
typically three to four times larger, because a parcel includes the garden.

Two caveats. Mappers often place the tag using the same OS Open UPRN point this dataset uses, so "lies inside" mainly
confirms we picked the right outline around that point rather than independently surveying where each property is.
And tagged buildings are concentrated where OpenStreetMap is well mapped, so they are not a random sample. Full
results by outline source, including buildings with several UPRNs, are in `accuracy.json` (aggregate statistics
derived from OpenStreetMap: © OpenStreetMap contributors, ODbL-1.0).

## Limitations

- **Outlines are approximate.** OS OpenMap Local is generalised and amalgamates terraces. In dense areas an
  "outline" is a block: in Islington the median outline is 376 m² and 80% of UPRNs share one with more than 20
  others. Check `outline_confidence`, `uprns_in_building`, `uprns_in_parcel` and `outline_granularity` before
  treating an outline as one property.
- **No addresses.** House numbers, building names and postcodes are not published (Royal Mail and OS third-party
  rights).
- **Not every UPRN is a dwelling.** OS Open UPRN carries no classification.
- **A parcel is not a building.** It shows the indicative ground-level extent of a registered freehold title; the
  legal extent can only be established from the title plan.
- **Scottish parcels are partial.** Registers of Scotland publish Land Register parcels only.
- **Price Paid links are recent sales only.** `uprn_links.ppd_transaction_ids` holds only sales in HMLR's UPRN lookup,
  published since August 2026, so most UPRNs have none.
- **`building_id` is not stable between releases.** It is a hash of the outline geometry.
- **ONSUD sets the UPRN list and local authority**, and can disagree with OS Open UPRN on location.

## Northern Ireland

Northern Ireland has no open UPRNs, so it is not covered.

## Source data

Every input to this dataset is open data: published under an open licence and free to download and reuse,
including commercially, provided the attribution terms are met. No premium, PSGA or paid-for data was used at any
stage, and nothing here was derived from AddressBase, OS MasterMap, OS NGD, the OS Places API or the Royal Mail
PAF. All sources are free to access; England and Wales EPC data needs only a free GOV.UK One Login account.

| Dataset | Publisher | Licence | Used for |
|---|---|---|---|
| [OS Open UPRN](https://www.ordnancesurvey.co.uk/products/os-open-uprn) | Ordnance Survey | OGL v3.0 | UPRN point |
| [OS Open Linked Identifiers](https://www.ordnancesurvey.co.uk/products/os-open-linked-identifiers) | Ordnance Survey | OGL v3.0 | UPRN to TOID and USRN links |
| [OS Open TOID](https://www.ordnancesurvey.co.uk/products/os-open-toid) | Ordnance Survey | OGL v3.0 | Point inside each TOID's feature, used to match outlines |
| [OS OpenMap Local](https://www.ordnancesurvey.co.uk/products/os-open-map-local) (buildings) | Ordnance Survey | OGL v3.0 | Building outlines |
| [OS Open Names](https://www.ordnancesurvey.co.uk/products/os-open-names) | Ordnance Survey | OGL v3.0 | Street name and locality, including Welsh and Gaelic names |
| [OS Open USRN](https://www.ordnancesurvey.co.uk/products/os-open-usrn) | Ordnance Survey | OGL v3.0 | Street geometry for distance checks |
| [OS Open Roads](https://www.ordnancesurvey.co.uk/products/os-open-roads) | Ordnance Survey | OGL v3.0 | Fallback street names |
| [Code-Point Open](https://www.ordnancesurvey.co.uk/products/code-point-open) | Ordnance Survey | OGL v3.0 | Postcode quality checks only; no postcodes published |
| [ONS UPRN Directory (ONSUD)](https://geoportal.statistics.gov.uk/) | Office for National Statistics | OGL v3.0 | UPRN list, local authority, OA / LSOA / MSOA |
| [INSPIRE Index Polygons](https://use-land-property-data.service.gov.uk/datasets/inspire) | HM Land Registry | OGL v3.0 | Land parcels, England and Wales |
| [Price Paid Data and UPRN lookup](https://www.gov.uk/government/collections/price-paid-data) | HM Land Registry | OGL v3.0 | Price Paid transaction IDs for linked UPRNs |
| [INSPIRE Cadastral Parcels](https://ros-inspire.themapcloud.com/) | Registers of Scotland | OGL v3.0 | Land parcels, Scotland |
| [EPC register, England and Wales](https://get-energy-performance-data.communities.gov.uk/) | MHCLG | OGL v3.0 (non-address fields) | Property type, built form and certificate ID |
| [EPC register, Scotland](https://statistics.gov.scot/data/domestic-energy-performance-certificates) | Scottish Government | OGL v3.0 (non-address fields) | As above, Scotland |
| [GlobalMLBuildingFootprints](https://github.com/microsoft/GlobalMLBuildingFootprints) | Microsoft | CDLA-Permissive-2.0 | Building footprints where no OS building or parcel fits |
| [Overture Maps Places](https://docs.overturemaps.org/guides/places/) | Overture Maps Foundation | CDLA-Permissive-2.0, Apache-2.0 or CC0-1.0, per provider | Business and organisation names |
| [OpenStreetMap](https://www.openstreetmap.org/) (Geofabrik extracts) | OpenStreetMap contributors | ODbL-1.0 | The independent accuracy check only (aggregate statistics in `accuracy.json`); no OpenStreetMap data is in the tables |

Exact source versions for this release are recorded in `release_manifest.json`. EPC address fields and Price Paid
address text are excluded, because they contain third-party address data that the open licences do not cover.

## Licences and attribution

Everything is under the OGL v3.0 except two labelled parts: `microsoft_buildings/` (CDLA-Permissive-2.0,
attribution only) and the business name columns of `uprn_lookup/` (Overture Maps, per provider; see
`uprn_lookup/LICENCE.txt`). There is no share-alike data in the release. Full terms and every required statement
are in `<release>/LICENCE.md`. HM Land Registry INSPIRE data is
reused under its [conditions of use](https://use-land-property-data.service.gov.uk/datasets/inspire/#conditions).

For the OGL v3.0 tables, carry these statements with any reuse:

- Contains OS data © Crown copyright and database right 2026.
- Contains Royal Mail data © Royal Mail copyright and database right 2026.
- Contains GeoPlace data © Local Government Information House Limited copyright and database right 2026.
- Source: Office for National Statistics licensed under the Open Government Licence v.3.0.
- Contains HM Land Registry data © Crown copyright and database right 2026. This data is licensed under the Open Government Licence v3.0.
- This information is subject to Crown copyright and database rights 2026 and is reproduced with the permission of HM Land Registry.
- The polygons (including the associated geometry, namely x, y co-ordinates) are subject to Crown copyright and database rights 2026 Ordnance Survey AC0000851063.
- © Crown copyright. Reproduced with the permission of Registers of Scotland.
- Contains EPC register data (non-address fields) from MHCLG and the Scottish Government under the Open Government Licence v3.0.

For `microsoft_buildings/`: "Building footprints © Microsoft, CDLA-Permissive-2.0". `accuracy.json` holds
aggregate statistics derived from OpenStreetMap: "© OpenStreetMap contributors, ODbL-1.0". This README and the STAC metadata are
available under CC0-1.0.

## Contact

Questions and feedback: [uk@avineon-tensing.com](mailto:uk@avineon-tensing.com)
