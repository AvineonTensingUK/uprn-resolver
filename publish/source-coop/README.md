# GB UPRN Open Resolver

Every Unique Property Reference Number (UPRN) in Great Britain, 41,593,127 of them, resolved to a point, a TOID,
an approximate building outline, a land parcel, a street name and statistical geographies. Each answer comes with
a measure of how specific it is, so you can tell a house's own outline from a city block.

Built only from open data. The core is under the Open Government Licence v3.0. Optional layers from Microsoft,
Overture Maps and OpenStreetMap sit in their own folders under their own licences. No house numbers, building
names or postcodes are published.

- **Coverage:** England, Scotland and Wales, 350 local authorities. Northern Ireland has no open UPRNs (see below).
- **Format:** GeoParquet 1.1, one file per table per local authority, plus a STAC catalogue and PMTiles.
- **Release:** `2026-10`, a one-off snapshot. There may be further releases; if so, `latest.json` will name the
  newest.
- **Code and method:** [github.com/AvineonTensingUK/uprn-resolver](https://github.com/AvineonTensingUK/uprn-resolver), open source under the
  MIT licence

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
FROM read_parquet('https://data.source.coop/<account>/uk-uprn-open/2026-10/resolved_open/lad=*/resolved_open.parquet',
                  hive_partitioning = true)
WHERE uprn = 5300000000;
```

Every file is partitioned by local authority (`lad=<code>`). Filtering on `lad` reads one file instead of 350, and
`lad_index.parquet` gives each authority's extent. Spatial files are Hilbert-sorted and carry a `bbox` column, so
bounding-box filters skip most of each file.

Coordinates are British National Grid (EPSG:27700). WGS84 sources were converted with the OSTN15 grid, not a
Helmert approximation.

## What's in a release

| Folder | One row per | Contents | Licence |
|---|---|---|---|
| `resolved_open/` | UPRN | **Start here.** Best answer per UPRN: point, best outline and its confidence, street, EPC type, business name, quality flags | OGL v3.0, plus labelled Overture and Microsoft columns |
| `uprn/` | UPRN | Core table: point, TOID, matched OS building, parcel, USRN, street, OA / LSOA / MSOA | OGL v3.0 |
| `buildings/` | building | OS OpenMap Local outlines | OGL v3.0 |
| `parcels/` | land parcel | Parcel polygons that contain a UPRN: HM Land Registry INSPIRE (England and Wales) and Registers of Scotland. The shape behind a parcel-based best outline | OGL v3.0, with HMLR, OS and RoS statements |
| `epc/` | UPRN | Energy Performance Certificate fields (no address columns) | OGL v3.0 |
| `provenance/` | UPRN | Street and postcode QA (Open USRN, Code-Point Open, Open Roads), with no postcode text | OGL v3.0 |
| `ppd_open/`, `ppd_open_summary/` | sale / UPRN | HM Land Registry Price Paid: only sales in HM Land Registry's own UPRN lookup, published since August 2026 (mostly 2025 and 2026 sales) | OGL v3.0 |
| `poi/` | place | Overture Maps places matched to UPRNs (business names) | CDLA-Permissive-2.0, Apache-2.0 or CC0, per provider |
| `outline_ms/`, `outline_ms_buildings/` | UPRN / footprint | Microsoft building footprints matched to UPRNs | CDLA-Permissive-2.0 |
| `streets_odbl/` | UPRN | OpenStreetMap street names and house numbers | **ODbL-1.0** |
| `outline_odbl/`, `outline_odbl_buildings/` | UPRN / building | OpenStreetMap building outlines matched to UPRNs | **ODbL-1.0** |
| `tiles/` | | PMTiles vector tiles for web maps, one archive per licence | as for the source table |
| `stac/` | | Static STAC catalogue: one collection per table, one item per local authority | |

Also in each release: `README.md` (full column-by-column schemas), `LICENCE.md`, `stats.json` (per-authority
statistics), `lad_index.parquet`, and `release_manifest.json` (size, sha256 and row count of every file, plus
source dataset versions).

Total size is about 12 GB of Parquet, plus about 7 GB of optional PMTiles.

## Statistics

Figures for release `2026-10`, over every UPRN in the ONS UPRN Directory. Per-authority figures are in `stats.json`.

### Coverage

| | England | Scotland | Wales | **Great Britain** |
|---|---|---|---|---|
| Local authorities | 296 | 32 | 22 | **350** |
| UPRNs | 35,630,027 | 3,882,711 | 2,080,389 | **41,593,127** |
| Has a point (OS Open UPRN) | 99.99% | 99.99% | 99.98% | **99.99%** |
| Has a TOID (OS Open Linked Identifiers) | 99.89% | 99.91% | 99.86% | **99.89%** |
| Has a best outline of any kind | 97.5% | 93.6% | 96.2% | **97.0%** |
| Matched to an OS OpenMap Local building | 86.0% | 82.8% | 84.6% | **85.6%** |
| In a land parcel | 94.8% | 65.6% | 92.1% | **91.9%** |
| Has a street name | 92.3% | 85.4% | 81.2% | **91.1%** |
| Has an EPC property type | 55.7% | 49.8% | 51.8% | **54.9%** |
| Has a matched business name (Overture) | 3.9% | 3.0% | 3.8% | **3.9%** |
| Has a linked Price Paid sale | 0.2% | n/a | 0.2% | **0.2%** |
| Mean `quality_score` (0 to 100) | 84.7 | 79.2 | 84.5 | **84.2** |

Scotland's lower parcel coverage is expected: Registers of Scotland publish Land Register parcels only, and estimate
that 32% of ownership polygons are missing. Price Paid Data covers England and Wales only.

### Where the best outline comes from

`resolved_open.best_outline_source`, share of UPRNs:

| Source | England | Scotland | Wales | **Great Britain** |
|---|---|---|---|---|
| HM Land Registry INSPIRE parcel | 52.6% | n/a | 52.3% | **19,812,087 (47.6%)** |
| Registers of Scotland parcel | n/a | 33.3% | n/a | **1,293,471 (3.1%)** |
| OS OpenMap Local building | 33.4% | 50.1% | 35.0% | **14,581,625 (35.1%)** |
| Microsoft footprint | 11.5% | 10.1% | 9.0% | **4,674,298 (11.2%)** |
| None | 2.5% | 6.4% | 3.8% | **1,231,646 (3.0%)** |

Parcels often separate neighbouring properties better than OpenMap Local, which merges terraces into one shape.

### How specific the best outline is

`outline_confidence` says how many UPRNs share the best outline: `property` means none other, `small_group` two to
five, `block` more than five.

| | `property` | `small_group` | `block` | `none` |
|---|---|---|---|---|
| England | 48.4% | 20.2% | 28.9% | 2.5% |
| Scotland | 35.4% | 22.8% | 35.3% | 6.4% |
| Wales | 56.1% | 21.1% | 19.1% | 3.8% |
| **Great Britain** | **47.5%** | **20.5%** | **29.0%** | **3.0%** |

By source, in UPRNs:

| Source | `property` | `small_group` | `block` |
|---|---|---|---|
| HM Land Registry INSPIRE parcel | 12,375,707 | 3,421,706 | 4,014,674 |
| Registers of Scotland parcel | 690,742 | 207,427 | 395,302 |
| OS OpenMap Local building | 4,579,094 | 3,884,723 | 6,117,808 |
| Microsoft footprint | 2,129,303 | 1,009,096 | 1,535,899 |
| **All sources** | **19,774,846** | **8,522,952** | **12,063,683** |

Parcels supply two thirds of all `property`-level answers.

### OS OpenMap Local building match

From the `uprn` table, which uses OpenMap Local only:

| How the building was found | UPRNs |
|---|---|
| Building containing the TOID's point (OS Open TOID) | 35,457,696 (85.2%) |
| Building containing the UPRN's own point (fallback) | 160,141 (0.4%) |
| No building | 5,975,290 (14.4%) |

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
| Non-domestic and other | 2.2% |
| No EPC | 45.1% |

Built form, for the 21.6 million UPRNs that have one: semi-detached 31.7%, mid-terrace 26.4%, detached 23.5%,
end-terrace 14.0%, enclosed terrace 2.4%, not recorded 2.1%.

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
| ok | 15,390,887 (37.0%) |
| block outline | 12,063,683 (29.0%) |
| small-group outline | 8,522,952 (20.5%) |
| street far from USRN | 4,903,971 (11.8%) |
| outline from Microsoft footprints | 4,674,298 (11.2%) |
| no street name | 3,681,778 (8.9%) |
| postcode far from unit centroid | 1,875,977 (4.5%) |
| no outline | 1,231,646 (3.0%) |
| postcode not in Code-Point | 547,076 (1.3%) |
| USRN has no geometry | 147,881 (0.4%) |
| no USRN | 140,132 (0.3%) |
| street name from Open Roads fallback | 27,483 (0.1%) |
| building match methods disagree | 982 (0.0%) |

A UPRN can carry several flags. The postcode flags come from checks against Code-Point Open; no postcodes are
published.

### Optional layers

| Layer | Coverage |
|---|---|
| Microsoft footprint matched (`outline_ms`) | 34,925,170 UPRNs (84.0%) |
| OpenStreetMap building matched (`outline_odbl`) | 27,559,961 UPRNs (66.3%) |
| OpenStreetMap street name (`streets_odbl`) | 37,459,014 UPRNs (90.1%) |
| OpenStreetMap house number / house name (`streets_odbl`) | 6,310,431 (15.2%) / 1,245,730 (3.0%) |
| Overture places (`poi`) | 4,161,267 places, naming 3.9% of UPRNs |
| Price Paid sales (`ppd_open`) | 84,141 sales for 82,993 UPRNs, median price £285,000 |

The most common Overture categories on matched UPRNs are home services, personal and beauty services, hardware and
garden stores, professional services and restaurants.

### Release size

| Table | Rows | Size |
|---|---|---|
| `uprn` | 41,593,127 | 1.8 GB |
| `resolved_open` | 41,593,127 | 1.9 GB |
| `buildings` | 11,069,743 | 0.8 GB |
| `epc` | 22,852,322 | 1.3 GB |
| `provenance` | 41,593,127 | 0.3 GB |
| `poi` | 4,161,267 | 0.2 GB |
| `ppd_open` / `ppd_open_summary` | 84,141 / 82,993 | under 0.01 GB |
| `outline_ms` / `outline_ms_buildings` | 41,593,127 / 12,668,838 | 0.9 / 1.8 GB |
| `outline_odbl` / `outline_odbl_buildings` | 41,593,127 / 13,838,437 | 0.8 / 1.7 GB |
| `streets_odbl` | 41,593,127 | 0.2 GB |
| `tiles/` (PMTiles) | | 7.2 GB |
| **Whole release** | 8,366 files | **18.9 GB** |

The `parcels` table is added from release `2026-11`.

### Checked against OpenStreetMap

About 3.4 million UPRNs carry an OpenStreetMap tag (`ref:GB:uprn`) on a building outline drawn by a mapper.
No OpenStreetMap data goes into `resolved_open`, so these give an independent check of what the confidence labels
claim. For the 2.8 million tagged buildings that hold a single UPRN, mostly houses:

| `outline_confidence` | Tagged UPRNs | Mapped building lies inside our outline | Our outline holds no other mapped property |
|---|---|---|---|
| `property` | 2,468,852 | 98.5% | **98.4%** |
| `small_group` | 256,046 | 97.6% | 9.9% |
| `block` | 81,549 | 98.1% | 4.6% |

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
- **No addresses.** House numbers, building names and postcodes are not in the open tables (Royal Mail and OS
  third-party rights). OpenStreetMap house numbers are in `streets_odbl/` only, where OSM has them.
- **Not every UPRN is a dwelling.** OS Open UPRN carries no classification.
- **A parcel is not a building.** It shows the indicative ground-level extent of a registered freehold title; the
  legal extent can only be established from the title plan.
- **Scottish parcels are partial.** Registers of Scotland publish Land Register parcels only.
- **Price Paid covers recent sales only.** It includes only sales in HMLR's UPRN lookup, so it is not the
  complete Price Paid dataset.
- **`building_id` is not stable between releases.** It is a hash of the outline geometry.
- **ONSUD sets the UPRN list and local authority**, and can disagree with OS Open UPRN on location.

## Northern Ireland

Northern Ireland has no open UPRNs, so it is not in the Parquet tables. `tiles/ni_addresses.pmtiles` carries
92,358 OpenStreetMap address points for Northern Ireland (ODbL-1.0) for mapping. They cannot be joined to UPRNs.

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
| [Price Paid Data and UPRN lookup](https://www.gov.uk/government/collections/price-paid-data) | HM Land Registry | OGL v3.0 | Sale prices and dates for linked UPRNs; no address fields |
| [INSPIRE Cadastral Parcels](https://ros-inspire.themapcloud.com/) | Registers of Scotland | OGL v3.0 | Land parcels, Scotland |
| [EPC register, England and Wales](https://get-energy-performance-data.communities.gov.uk/) | MHCLG | OGL v3.0 (non-address fields) | Property type, built form and other certificate fields |
| [EPC register, Scotland](https://statistics.gov.scot/data/domestic-energy-performance-certificates) | Scottish Government | OGL v3.0 (non-address fields) | As above, Scotland |
| [GlobalMLBuildingFootprints](https://github.com/microsoft/GlobalMLBuildingFootprints) | Microsoft | CDLA-Permissive-2.0 | Alternative building footprints |
| [Overture Maps Places](https://docs.overturemaps.org/guides/places/) | Overture Maps Foundation | CDLA-Permissive-2.0, Apache-2.0 or CC0-1.0, per provider | Business and organisation names |
| [OpenStreetMap](https://www.openstreetmap.org/) (Geofabrik extracts) | OpenStreetMap contributors | ODbL-1.0 | Street names, house numbers and building outlines in the ODbL folders; Northern Ireland address points |

Exact source versions for this release are recorded in `release_manifest.json`. EPC address fields and Price Paid
address text are excluded, because they contain third-party address data that the open licences do not cover.

## Licences and attribution

Licences differ by folder and must not be mixed. Most importantly, the ODbL folders are share-alike: anything
you derive from them and redistribute must stay under ODbL. Full terms and every required statement are in
`<release>/LICENCE.md`, and each non-OGL folder carries its own `LICENCE.txt`. HM Land Registry INSPIRE data is
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

For OpenStreetMap-derived folders: "© OpenStreetMap contributors, ODbL-1.0". This README and the STAC metadata are
available under CC0-1.0.

## Contact

Questions and feedback: [uk@avineon-tensing.com](mailto:uk@avineon-tensing.com)
