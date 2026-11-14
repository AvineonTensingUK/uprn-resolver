# UPRN resolver: goal and data sources

## Goal

Resolve every UPRN in Great Britain to a **building outline** and an **address**, using open data
wherever it exists, and say honestly how good each answer is.

The open part is published as a GeoParquet dataset (one row per UPRN, partitioned by local
authority) intended for source.coop. Anything that is not openly licensed stays local.

What open data can and cannot do:

| Attribute | Open result | Limit |
|---|---|---|
| Point | Authoritative, ~100% | — |
| Building outline | ~95% matched | OS OpenMap Local is generalised: in dense areas an "outline" is often a whole block |
| Land parcel | ~98% (England & Wales) | Registered freehold extent, not a building |
| Street name + locality | ~93% | No street name for private/estate roads |
| Postcode | ~98% | Republication pending ONS confirmation |
| House number / name, flat | **None** | Closed (AddressBase, PAF, Places API) |

Every row carries provenance and confidence columns (`match_method`, `uprns_in_building`,
`uprns_in_parcel`, `outline_granularity`) so users can tell a per-property answer from a
block-level one.

## Open data sources (published output)

| Source | Publisher | Licence | Join key | Used for |
|---|---|---|---|---|
| OS Open UPRN | Ordnance Survey | OGL v3 | UPRN | Point (BNG + lat/lon) |
| OS Open Linked Identifiers — `BLPU_UPRN_TopographicArea_TOID_5` | Ordnance Survey | OGL v3 | UPRN → TOID | Building TOID (1:1 per UPRN) |
| OS Open Linked Identifiers — `BLPU_UPRN_Street_USRN_11`, `Road_TOID_Street_USRN_10` | Ordnance Survey | OGL v3 | UPRN → USRN → Road TOID | Street link |
| OS Open TOID | Ordnance Survey | OGL v3 | TOID | Point guaranteed inside the MasterMap feature (filtered to UPRN-linked TOIDs) |
| OS OpenMap Local — Building layer | Ordnance Survey | OGL v3 | Spatial (point-in-polygon) | Approximate building outline |
| OS Open Names — Named Road | Ordnance Survey | OGL v3 | Road TOID | Street name (incl. Welsh/Gaelic), locality |
| ONS UPRN Directory (ONSUD) | ONS | OGL v3 (postcode use: to confirm) | UPRN | Local authority, OA/LSOA/MSOA, postcode |
| INSPIRE Index Polygons | HM Land Registry | OGL v3 + OS attribution | Spatial | Land parcel polygon, ID, area, UPRNs per parcel (`parcels.parquet`: polygons of parcels holding a UPRN) |
| Price Paid transactions (no address) for UPRNs with an HMLR lookup, `ppd_open.parquet` | HM Land Registry | OGL v3 + attribution | Transaction ID | Price, date, type, tenure per UPRN (Aug 2026 lookups only) |
| Price Paid UPRN and INSPIRE ID lookups (Aug 2026 onwards) | HM Land Registry | OGL v3 | Transaction ID → UPRN / INSPIRE ID | Sale transaction and parcel IDs |
| EPC — non-address fields (England & Wales, Scotland) | MHCLG; Scottish Government | OGL v3 (address fields excluded) | UPRN | Property type, built form, floor area, ratings, etc. |
| OS Open USRN | Ordnance Survey | OGL v3 (OS OpenData licence; "Contains OS data (c) Crown copyright and database right 2026") | USRN | Street line geometry + street type; UPRN-to-street distance check (`provenance.parquet`) |
| Code-Point Open | Ordnance Survey | OGL v3 (OS OpenData licence). Must carry all three: "Contains OS data (c) Crown copyright and database right", "Contains Royal Mail data (c) Royal Mail copyright and database right", "Contains National Statistics data (c) Crown copyright and database right" | Postcode unit | Postcode centroid, positional quality, admin codes; postcode sanity check and nearest-centroid postcode estimate |
| OS Open Roads (RoadLink, named links) | Ordnance Survey | OGL v3 | Spatial (USRN line vs road link) | Fallback street name for USRNs with no Open Names entry |
| Overture Maps Places | Overture Maps Foundation | Per provider: CDLA-Permissive-2.0, Apache-2.0, CC0 | Spatial (same building/parcel/nearest) | Business / organisation names for non-residential UPRNs |

## Local-only sources (never published)

| Source | Why local only | Used for |
|---|---|---|
| EPC address fields (England & Wales via GOV.UK One Login bulk download; Scotland) | Address data published with OS / Royal Mail consent, not OGL | Full address text for ~50% of UPRNs (`address_local.parquet`, flagged `EPC-restricted`) |
| Price Paid Data address fields (`ppd.parquet`: PAON, SAON, street, locality, town, postcode) | OGL v3 for the data but address fields are Royal Mail / OS third-party data (personal/non-commercial use only) | Historic transaction to UPRN matching (`ppd_local.parquet`, `match_method='address_match'`), plus full PPD address text for UPRNs with no EPC address |

`publish.py` rejects address columns and never collects `address_local.parquet`.

## ODbL companion table (separate file, never merged into the OGL `uprn.parquet`)

| Source | Licence | Used for |
|---|---|---|
| OpenStreetMap, Geofabrik Great Britain extract (`great-britain-latest.osm.pbf`, md5-verified; converted by `uprn_resolver/osm.py` to `data/parquet/osm/{highways,addresses}.parquet`, PBF then deleted) | ODbL-1.0, attribution "© OpenStreetMap contributors" | `out/lad=<code>/streets_odbl.parquet` (+ `streets_odbl.LICENCE.txt`): OSM street name (nearest named highway within 50 m) where OS Open Names has none or to cross-check it, and OSM `addr:housenumber` / `addr:housename` (the only open route to house numbers), matched conservatively: exact `ref:GB:uprn`, UPRN inside a single-UPRN building polygon, or mutual nearest addr node within 10 m |

ODbL share-alike applies to this derivative database, so it is published (if at all) as its own file under ODbL and
is excluded from the OGL release. `publish.py` does not collect it.

### ODbL companion: OSM building outlines

| Source | Licence | Used for |
|---|---|---|
| OpenStreetMap `building=*` polygons (ways + multipolygon relations) from the local `united-kingdom-261020.osm.pbf` (read-only; Northern Ireland and `building=no` dropped), converted by `uprn_resolver/osm_buildings.py` to `data/parquet/osm/buildings.parquet` (EPSG:27700 WKB + bbox columns, Hilbert sorted) | ODbL-1.0, attribution "© OpenStreetMap contributors" | `out/lad=<code>/outline_odbl.parquet` (+ `outline_odbl_buildings.parquet`, `outline_odbl.LICENCE.txt`): per UPRN the OSM building holding it (Open TOID point first, then UPRN point, then nearest polygon within 5 m), UPRNs per OSM building, area, and comparison with the OGL OpenMap Local outline (`ogl_uprns_in_building`, area ratio). OSM polygons are much finer in dense areas (Islington median 78 m2 vs 376 m2). WGS84 to EPSG:27700 uses the OSTN15 grid (`uprn_resolver/bng.py`; the OSM and Overture Parquet files were originally converted with DuckDB's Helmert-only transform, ~1.7 m east in London, and rewritten in place to OSTN15, recorded as `crs_transform: OSTN15` in the manifest) |

Share-alike applies: keep these files separate from the OGL `uprn.parquet`/`buildings.parquet`; `publish.py` does not collect them.

## Not used, and why

| Source | Reason |
|---|---|
| AddressBase, OS Places API, PAF | Closed / licensed — the only full-address sources |
| OS MasterMap Topography, OS NGD Buildings | Closed — the only per-property outlines |
| HMLR Title Number ↔ UPRN lookup | Paid (National Polygon Service) |
| Overture Addresses | Alpha; no UK coverage |
| OSM / Overture Buildings | ODbL share-alike; would only be a separate table |
| source.coop EPC copy (addresscloud) | Only 7 columns, no built form |

## Coverage and caveats

- Great Britain only. Northern Ireland has UPRNs (AddressBase Islands) but no open data.
- Price Paid lookups exist only from August 2026 and are not back-dated.
- `building_id` is a hash of the OpenMap Local geometry and is not stable across releases.
- Pilot (Islington, E09000019): 94.8% outline match; 61% of UPRNs have a parcel finer than
  their outline; of UPRNs with only a shared outline, most are flats (correctly shared).

See [RESEARCH.md](RESEARCH.md) for the full research, sources and pilot results, and
[publish/LICENCE.md](publish/LICENCE.md) for attribution statements.

## Northern Ireland (open data only; no UPRNs)

NI UPRNs exist only in OSNI Pointer (paid, about £9.2k) and nothing open carries them. `uprn_resolver/ni.py` builds
what open data allows from the OSM UK PBF (ODbL-1.0, `© OpenStreetMap contributors`), cut with the OSM
admin_level=4 relation 156393 (same vintage as the data; no bbox spill into the Republic):
`data/parquet/ni/{buildings,addresses,highways}.parquet` (GeoParquet 1.1, EPSG:29903 `geometry` + WGS84
`geometry_wgs84`) and `data/out/ni/ni_address_buildings.parquet` (+ `.LICENCE.txt`): one row per OSM address object,
matched to its building polygon, with street (tag or nearest highway within 50 m) and postcode (OSM tags only).
ODbL share-alike: keep separate from OGL outputs.

| Source | Licence | Verdict |
|---|---|---|
| [OSNI Largescale Boundaries - Townlands](https://www.data.gov.uk/dataset/da45a2ca-eb36-45a7-b933-230313db54a0/osni-open-data-largescale-boundaries-townlands1) (OpenDataNI) | OGL v3, "Contains LPS Intellectual Property © Crown copyright and database right" | Open; locality polygons only, no addresses or UPRNs |
| OSNI Open Data mid-scale raster / other OSNI open boundaries | OGL / OpenDataNI | Open; raster or boundaries, no building vectors found |
| ONS Postcode Directory BT rows ([ONSPD licensing](https://ideal-postcodes.co.uk/guides/onspd-licensing)) | NI data under the ONSPD licence with LPS/Royal Mail terms, not plain OGL (commercial use needs an LPS licence) | Not adopted; centroids only, no UPRNs |
| [OSNI Pointer](https://www.nidirect.gov.uk/ordnance-survey-of-northern-ireland/pointer) | Paid | The only UPRN source for NI |
| NI EPC register via [DoF FOI DOF 2026-0254](https://www.finance-ni.gov.uk/sites/default/files/2026-06/DOF%202026-0254%20EIR%20Response._0.pdf) | No licence stated (an FOI/EIR release is not a reuse licence); addresses are personal data | Not downloaded. Box folder (link in the PDF's link annotation) holds `NI_Domestic_Master_to 03_2026.csv` (~596 MB) and `NI_Non_Dom_Master_to_03_2026.csv` (~8 MB); fields and UPRN presence unverified |

## Scotland: Registers of Scotland INSPIRE Cadastral Parcels (`uprn_resolver/ros.py`)

| Source | Licence | Notes |
|---|---|---|
| [RoS INSPIRE download](https://ros-inspire.themapcloud.com/) (33 registration counties, shapefile zips, EPSG:27700 `_bng` read; catalogue record [spatialdata.gov.scot](https://spatialdata.gov.scot/geonetwork/srv/api/records/2c17e031-5b71-4580-bf44-dd5250cc92a6)) | Download page links "Open Government Licence" to OGL v3. Attribution (catalogue record): "© Crown copyright. Reproduced with the permission of Registers of Scotland." and "Contains OS data © Crown copyright and database right (year)." | `data/parquet/ros/<county>.parquet` (1,573,146 polygons, data date 2026-08-13). Land Register only, so gaps. Staged by `stage_ros` as `inspire_parcel.parquet` with `parcel_source='ros'` for LADs starting `S`; never overwrites an existing non-empty file. Matched polygons are published in `parcels.parquet` with both statements. |

## Microsoft building footprints (alternative outline, CDLA-Permissive-2.0)

| Source | Licence | Used for |
|---|---|---|
| Microsoft GlobalMLBuildingFootprints, `UnitedKingdom` region, 194 quadkey tiles, release 2026-08-13 (index: `dataset-links.csv`, <https://github.com/microsoft/GlobalMLBuildingFootprints>; converted by `uprn_resolver/msbuildings.py` to `data/parquet/msbuildings/{gb,ni}/<quadkey>.parquet`, raw tiles deleted) | CDLA-Permissive-2.0 ("licensed by Microsoft under the CDLA Permissive 2.0", repo README). Attribution: "Contains Microsoft Building Footprints (GlobalMLBuildingFootprints), licensed under CDLA-Permissive-2.0" | `out/lad=<code>/outline_ms.parquet` (per-UPRN `ms_building_id`, `uprns_in_ms_building`, `ms_area_m2`, `match_method`, comparison with the OGL outline) and `outline_ms_buildings.parquet` (matched polygons), with `outline_ms.LICENCE.txt`. Matching order: Open TOID point, UPRN point, nearest within 5 m. Height is not used. Used from Microsoft's own files, not Overture's ODbL copy. GB polygons are EPSG:27700 (reprojected with the OSTN15 grid via `bng.py`); NI polygons are EPSG:29903 in `ni/` for the NI work |
