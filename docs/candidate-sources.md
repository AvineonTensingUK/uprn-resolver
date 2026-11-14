# Candidate sources to close the outline and address gaps

Research date: 22 October 2026. Scope: only sources that improve (a) the building outline or (b) the address
(house number/name, flat, street, locality, town, postcode). Baseline is in [DATASOURCES.md](../DATASOURCES.md)
and [RESEARCH.md](../RESEARCH.md). Items marked **measured** were tested on Islington (E09000019, 169,184 UPRNs)
with samples in `data/samples/` (git-ignored). Everything else is from the publisher's own page, cited inline.
"Unverified" means the publisher page did not settle the point.

## Top 5 (ranked by expected gain)

| # | Candidate | Gap closed | Gain | Licence route | Effort |
|---|---|---|---|---|---|
| 1 | OSM building polygons (already in the local GB PBF) | Block-level outlines | **Measured**: 37,619 polygons in the Islington bbox, median 78 m² (OpenMap Local: 376 m²). 78.9% of UPRNs fall inside one. Only 52% of UPRNs share their polygon with more than 20 others (OpenMap Local: 85%). Terraces are mostly split per house | ODbL, separate table | Low (osmium + point-in-polygon; 1 to 2 days) |
| 2 | Council Tax address lists (council-released, OGL) | House number, flat, street for ~all domestic UPRNs | Full address text for every dwelling, not just the ~51% with an EPC. Islington is listed as released under OGL | Local-only (OS/GeoPlace/Royal Mail rights claim, May 2026) | Medium to high (address-to-UPRN matching; 1 to 2 weeks) |
| 3 | Relax OSM address matching (building and parcel level) | House numbers | **Measured**: 30,241 OSM address objects with a house number in the Islington bbox, 6,305 with `ref:GB:uprn`. The current conservative matcher yields house numbers for only 9,569 of 169,121 UPRNs. Matching to the containing building or parcel should lift this a lot, since a house number is a building-level fact | ODbL, separate table | Low (change to `uprn_resolver/osm.py` matching; 2 to 3 days) |
| 4 | Microsoft GlobalML Building Footprints | Per-building outlines, including NI | **Measured** on the Islington tile: 15,753 polygons (OpenMap Local: 4,968), median 246 m², 85.7% of UPRNs inside one (94.8% within 10 m), 76% of UPRNs share a polygon with more than 20 others (OpenMap Local: 85%). Modest gain in dense terraces, better in suburbs; Belfast is covered | CDLA-Permissive-2.0 on Microsoft's own page, so it can sit beside OGL data | Low to medium (download by quadkey, reproject, spatial join; 2 to 3 days) |
| 5 | Registers of Scotland cadastral parcels | Parcel outline for Scotland (no INSPIRE equivalent in the pipeline) | Land-parcel polygons for registered Scottish property. Record notes ~32% of items missing and a data range ending 2017 | OGL since 2020 per [ROS news](https://www.ros.gov.uk/about/news/2020/hmlr-and-ros-to-share-inspire-data); the record's licence link was not readable | Medium (ATOM or CSV by registration county; 3 to 4 days) |

Honourable mentions: Price Paid Data as local address anchors (row 9 below) and Historic England listed-building
outlines (row 8).

Combination note: OSM polygons (1) and Microsoft polygons (4) are complementary. Pick per UPRN the smallest
polygon that contains the UPRN or TOID point and keep a `outline_source` column.

## Outlines (a)

| # | Source | Gives | Join key | Coverage | Licence (verified where) | Access / size | Update | Verdict | Effort |
|---|---|---|---|---|---|---|---|---|---|
| 1 | OSM buildings, [Geofabrik GB](https://download.geofabrik.de/europe/great-britain.html) | Per-building polygons, often per terrace house in London | Spatial | GB and NI; strong in cities | ODbL 1.0; already accepted for the ODbL companion table in DATASOURCES.md | Local PBF 2.3 GB (already downloaded) | Daily | **Adopt (ODbL table)** | Low |
| 4 | [Microsoft GlobalMLBuildingFootprints](https://github.com/microsoft/GlobalMLBuildingFootprints) | ML footprints from 2014-2024 imagery | Spatial | UK incl. NI (Belfast tile under `UnitedKingdom`); Islington tile 113 MB | "licensed by Microsoft under the CDLA Permissive 2.0" (Microsoft repo, release 2026-08-13) | Quadkey tiles; line-delimited GeoJSON in `.csv.gz` | Irregular (2023-09, 2024-09, 2026-08) | **Adopt (open, permissive)**; precision for Europe 94.3%, recall 85.9%, "not perfect in dense urban areas" (Microsoft) | Low to medium |
| 5 | [ROS cadastral parcels](https://spatialdata.gov.scot/geonetwork/srv/api/records/2c17e031-5b71-4580-bf44-dd5250cc92a6) | Scottish land parcels | Spatial | Scotland, Land Register only; ~32% items missing | OGL per ROS 2020 news; attribution: ROS + OS data credit | [ROS INSPIRE download](https://ros-inspire.themapcloud.com/), quarterly | Quarterly | **Adopt (Scotland parcels)** after confirming the licence link | Medium |
| 8 | Historic England listed building outlines via [planning.data.gov.uk](https://www.planning.data.gov.uk/dataset/listed-building-outline) | Authoritative polygons for listed buildings (132,279) | Spatial | England; "work in progress", not full coverage, may be curtilage-like | OGL v3, "Crown copyright and database right 2026" (dataset page) | JSON / GeoJSON / Parquet from the entity API | Rolling | **Adopt (cheap overlay)**; small share of UPRNs | Low |
| 11 | [Overture Maps buildings](https://docs.overturemaps.org/guides/buildings/) | Conflation of OSM, Microsoft, Google, Esri | Spatial | Global; no UK-specific source | Theme under ODbL (Overture [attribution page](https://docs.overturemaps.org/attribution/)) | GeoParquet | Monthly | **Reject**: adds nothing beyond sources 1 and 4, forces ODbL on Microsoft data | n/a |
| 12 | [VIDA Google-Microsoft-OSM combined](https://source.coop/vida/google-microsoft-osm-open-buildings) | Same inputs, pre-merged | Spatial | Google covers Africa, Asia, Latin America only | ODbL 1.0 | GeoParquet, 2.7 billion footprints | Irregular | **Reject**: same reason | n/a |
| 13 | Google Open Buildings | ML footprints | Spatial | Not UK | CC BY 4.0 (Overture page) | n/a | n/a | **Reject** (no UK coverage) | n/a |
| 14 | OS NGD Buildings / MasterMap | Authoritative polygons | TOID, UPRN | GB | PSGA or premium | n/a | n/a | Already rejected (closed) | n/a |
| 15 | Verisk, Geomni and similar | Commercial footprints | n/a | n/a | Commercial | n/a | n/a | Note only | n/a |

## Addresses (b)

| # | Source | Gives | Join key | Coverage | Licence (verified where) | Access / size | Update | Verdict | Effort |
|---|---|---|---|---|---|---|---|---|---|
| 2 | Council Tax address lists, index at [datadaptive.com/addr](https://www.datadaptive.com/addr/) | House number/name, flat, street, postcode for every council-tax dwelling | Address text; some geocoded versions carry UPRN | ~117 councils; Islington "Y (geocoded)", also Camden, Haringey, Hackney, Southwark, Lewisham, Newham, Ealing, Richmond; Welsh and Scottish councils too | Councils confirm OGL v3. **But** OS and GeoPlace claimed on 8 May 2026 that the data contains OS, GeoPlace and Royal Mail IP ([Boswarva](https://www.owenboswarva.com/blog/post-addr86.htm)). Boswarva removed 57 authorities' downloads on 12 May 2026; Dorset, East Riding and Isle of Wight say permission was given "in error"; Rushcliffe's legal advice says the data is the council's own | Council FOI zips (about 8 MB each); Islington's geocoded link is down | One-off snapshots | **Adopt local-only** with a legal-risk flag; never publish | Medium to high: match to UPRN via postcode + street + number against EPC anchors, then parcel and street-order interpolation for the rest |
| 3 | OSM `addr:*` | House number/name, flat, `ref:GB:uprn` | UPRN tag, building, spatial | Patchy; 31,556 address objects in the Islington bbox, 6,305 with a UPRN tag | ODbL | Local PBF | Daily | **Adopt (ODbL table)**, relax matching | Low |
| 6 | [HMLR Price Paid Data](https://www.gov.uk/guidance/about-the-price-paid-data) | PAON (number/name), SAON (flat), street, locality, town, postcode for sales since 1995 | Address text (no UPRN; UPRN lookup only from Aug 2026) | England and Wales residential sales | OGL v3. Page says address data is "mostly derived from Royal Mail address data" and does not clear those rights | Single CSV, monthly | Monthly | **Adopt local-only** as address anchors and validation. Cannot fill gaps alone because it has no UPRN | Medium |
| 7 | [GIAS (schools)](https://www.get-information-schools.service.gov.uk/) | Street, locality, town, postcode, easting/northing, UPRN | UPRN | England; I fetched today's file: 52,595 establishments, 39,866 with a UPRN | OGL v3 "except where otherwise stated" (site footer) | CSV, 65 MB, daily | Daily | **Adopt (small)**: school UPRNs only | Low |
| 9 | [FSA Food Hygiene Ratings](https://ratings.food.gov.uk/open-data/) | Business name, 4 address lines, postcode, lat/lon | Address text, spatial. API response has **no UPRN** | UK-wide food businesses; private addresses may be absent | Page says only "terms and conditions apply"; data.gov.uk listings show OGL v3 | API (JSON/XML) and daily CSV | Daily | **Adopt local-only** for non-domestic address text; low priority | Low |
| 10 | [VOA / HMRC rating list](https://voaratinglists.blob.core.windows.net/html/rlidata.htm) | Full non-domestic address, UARN; UPRN column unverified (spec PDF not read) | UARN | England and Wales non-domestic | Page states "An open government licence does not apply"; use limited to non-domestic rating purposes, no onward disclosure, delete when not needed ([terms](https://www.tax.service.gov.uk/business-rates-find/terms-and-conditions)) | Zip of asterisk-delimited CSV, 2M+ rows | 2-monthly epochs, weekly changes | **Reject**: purpose limitation rules out this use | n/a |
| 16 | Brownfield land register via [planning.data.gov.uk](https://www.planning.data.gov.uk/dataset/brownfield-land) | Free-text site address, point | Spatial | 40,099 sites, 296 providers, no UPRN field | OGL v3 | CSV/JSON/Parquet | Rolling | **Reject**: tiny and not property-level | n/a |
| 17 | planning.data.gov.uk `planning-application` (has a `uprn` field) | Application address and UPRN | UPRN | **0 records** today ("incomplete, not ready for use") | OGL v3 | API | n/a | **Reject for now**; recheck in 2027 | n/a |
| 18 | Council LLPG extracts on data.gov.uk (e.g. [Wyre](https://www.data.gov.uk/dataset/827229dd-6e20-483d-b598-7090748d9fd1/llpg1)) | UPRN-keyed address | UPRN | One council, listing about 11 years old | OGL v3 on the listing, but LLPG feeds the NLPG, so the same OS/GeoPlace rights question applies | CSV | Stale | **Reject**: stale, one council, rights unclear | n/a |
| 19 | [Companies House BasicCompanyData](https://download.companieshouse.gov.uk/en_output.html) | Registered office address lines | Address text, no UPRN | UK companies; many are agent addresses | Page states no licence | 471 MB zip, monthly | Monthly | **Reject**: not property-level, no UPRN, no licence stated | n/a |
| 20 | Overture Addresses ([guide](https://docs.overturemaps.org/guides/addresses/)) | OpenAddresses-derived points | Spatial | 41 countries; **GB not included** | Mixed per source | GeoParquet | Monthly | **Reject** (no UK) | n/a |
| 21 | OpenAddresses | Community address points | Spatial | UK coverage not verifiable from its site (page 403 / generic) | "Most sources only require attribution" (generic) | n/a | n/a | **Reject** until a GB source is shown | n/a |
| 22 | One Scotland Gazetteer, Scottish Address Directory | Authoritative Scottish addresses | UPRN | Scotland | Public sector (PSGA or One Scotland Mapping Agreement) members only ([NRS SAD page](https://nrscotland.gov.uk/statistics-and-data/geography/our-products/scottish-address-directory)) | n/a | n/a | **Reject** (closed) | n/a |
| 23 | Scottish Assessors valuation roll and Council Tax list | Address text | Address text | Scotland | Licensed on request; £100 per dataset, "temporarily suspended" at last notice ([SAA](https://www.saa.gov.uk/2022/07/)); no OGL | CSV | n/a | **Reject** (not open) | n/a |
| 24 | Royal Mail PAF | Full postal addresses | n/a | UK | Paid licence; terms not checked | n/a | n/a | **Reject** (closed) | n/a |
| 25 | Ofcom or Openreach UPRN-level data | n/a | n/a | n/a | No open UPRN-level release found | n/a | n/a | **Reject** | n/a |

## Northern Ireland

There is still no open UPRN source. [OSNI Pointer](https://www.nidirect.gov.uk/ordnance-survey-of-northern-ireland/pointer)
carries UPRNs and building IDs but is a paid product (full NI CSV about £9,224 ex VAT, via
[mapshop](https://mapshop.nidirect.gov.uk/Catalogue/Digital-products/Address-data-and-gazetteers/Pointer-data)).
LPS open data under OGL is limited to boundaries, townlands, DTMs and similar ([OSNI townlands](https://www.data.gov.uk/dataset/18e1bd0f-1829-496e-9b21-a5d8f6159940/osni-open-data-largescale-boundaries-townlands6)),
with no buildings and no addresses.

| Source | Gives | Licence | Verdict |
|---|---|---|---|
| Microsoft footprints (above) | Building outlines, Belfast covered | CDLA-Permissive-2.0 | **Adopt**: the only open NI outline route besides OSM |
| OSM (above) | Outlines and `addr:*` points, no UPRN | ODbL | **Adopt (ODbL table)**, keyed spatially to a point set, not UPRNs |
| OSNI townland polygons | Locality (townland) name for an address | OGL v3 (OpenDataNI pages; one page cites the LPS Open Government Data Licence) | **Adopt for locality only**, needs a point source first |
| NI EPC data from the Department of Finance FOI response [DOF 2026-0254](https://www.finance-ni.gov.uk/sites/default/files/2026-06/DOF%202026-0254%20EIR%20Response._0.pdf) | All domestic and non-domestic EPCs, 2008 to March 2026, via a Box link | Not stated; fields and UPRN presence unverified (Box page needs a browser) | **Investigate**: if it has addresses and UPRNs it is the only candidate NI UPRN-to-address route; treat as local-only |

Honest conclusion for NI: outlines can be improved (Microsoft, OSM); UPRN-keyed addresses cannot be had openly.

## Notes on rights

- **Council Tax lists**: the open permission comes from councils, but OS and GeoPlace contest it. Use for local
  address resolution only and record `address_source = council_tax_ogl_disputed` so it is never collected by
  `publish.py`. Re-check the Boswarva [index](https://www.datadaptive.com/addr/) before each build, since
  councils may withdraw.
- **Price Paid Data**: OGL, but treat the address fields as Royal Mail-derived, consistent with the existing
  EPC/PPD rule in RESEARCH.md.
- **Microsoft vs Overture**: Microsoft's own repo says CDLA-Permissive-2.0, while Overture republishes the same
  data inside an ODbL theme. Use Microsoft's original files so the footprints are not pulled under share-alike.
- **Verify before relying**: ROS licence link, VOA UPRN column, NI EPC fields. None of these could be read from
  the publisher pages today.

## Measurements (Islington)

| Outline source | Polygons | Median area | UPRNs inside a polygon | UPRNs in a polygon shared by more than 20 UPRNs |
|---|---|---|---|---|
| OS OpenMap Local (current) | 4,968 | 376 m² | ~94.8% | 85% of matched UPRNs |
| Microsoft ML (tile 031313131) | 15,753 | 246 m² | 85.7% (94.8% within 10 m) | 76% |
| OSM buildings (Geofabrik 2026-10-20) | 37,619 | 78 m² | 78.9% | 52% |

Caveats: Microsoft and OSM polygon counts cover the Islington bounding box (slightly larger than the borough),
so they are indicative. The ">20 UPRNs" figure overstates the problem because flats legitimately share a
building, so it measures relative sharpness, not error. No accuracy check against MasterMap was possible.
