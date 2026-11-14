# Resolving a UPRN to a building outline and address — what exists openly?

Research notes, October 2026.

## Question

Is there an openly licensed service or dataset that resolves a UPRN to **both** a postal address and a
building footprint polygon?

## Answer

**No.** Every individual link in the chain exists, but two of them are closed, so no all-open
end-to-end resolver is available — and no single product provides both attributes.

- **UPRN → address** is closed. OS Open UPRN deliberately carries a coordinate and nothing else.
  The only clean national resolver is the OS Places API (`/search/places/v1/uprn`), which is premium,
  or AddressBase Core, which is PSGA-only. Open proxies (EPC, ONSUD, Price Paid lookups) carry a UPRN
  incidentally and are partial.
- **UPRN → building polygon** is closed. The open route is `UPRN → OS Open Linked Identifiers → TOID`,
  and it stops there as an *identifier*: OS Open TOID supplies a generalised point, not the polygon.
  The real polygons live in OS NGD Buildings / OS MasterMap Topography Layer, which are premium.
  The open polygon products (OS OpenMap Local, OS Open Zoomstack) carry no TOID or UPRN, so they
  cannot be joined — only geometrically approximated. The Open TOID point is a useful anchor for that
  approximation (see break 2). HMLR INSPIRE Index Polygons (open, no login) give a land *parcel*
  polygon for nearly every England & Wales UPRN by spatial join, but a parcel is not a building.

Northern Ireland is outside every *open* product: OS Open UPRN, LIDS and OpenMap Local are GB-only.
[AddressBase Islands](https://ordnancesurvey.co.uk/products/addressbase-islands) (Plus/Premium)
does cover Northern Ireland (built partly from LPS Pointer), the Channel Islands and the Isle of Man,
with UPRNs, but it is free only to PSGA members and premium otherwise.

## The open building blocks

| Dataset | Key | Contains | Licence | Caveat |
|---|---|---|---|---|
| [OS Open UPRN](https://www.ordnancesurvey.co.uk/products/os-open-uprn) | UPRN | UPRN, BNG `X_COORDINATE`/`Y_COORDINATE`, `LATITUDE`/`LONGITUDE`, point geometry | OGL | No address attribution; **no classification**, so a UPRN may be street furniture, a land parcel or a parent record, not a dwelling |
| [OS Open Linked Identifiers](https://www.ordnancesurvey.co.uk/products/os-open-linked-identifiers) (LIDS) | UPRN ↔ USRN ↔ TOID | Cross-reference CSV, 6-weekly, plus confidence value | OGL | *"Geometry is not included"*; UPRN→TOID is itself a point-in-polygon (see break 2) |
| [OS Open TOID](https://docs.os.uk/os-downloads/identifiers/os-open-toid/os-open-toid-technical-specification) | TOID | TOID, source product, version number/date, XY | OGL | Point only. For polygon features the point is a pole of inaccessibility, guaranteed inside the polygon, rounded to 1 m |
| OS Open USRN | USRN | Street identifiers | OGL | Not needed: LIDS already carries the UPRN-to-USRN link |
| [OS Open Names](https://www.ordnancesurvey.co.uk/products/os-open-names) | ID (Road TOID for roads) | Place and road names; `Named Road` features | OGL | Gives street name and locality, not house number. Joins to the Road TOID (96.6% of USRNs) |
| [OS OpenMap Local — Building](https://docs.os.uk/os-downloads/products/maps-and-imagery-portfolio/os-openmap-local/os-openmap-local-technical-specification/feature-types/building) | none | Generalised building polygons, `featureCode` only | OGL | No TOID, no UPRN — unjoinable by key. Amalgamates terraced rows. The only open OS building layer |
| [OS Open Zoomstack — `local_buildings`](https://docs.os.uk/os-downloads/products/maps-and-imagery-portfolio/os-open-zoomstack/os-open-zoomstack-technical-specification/geopackage-schema) | `id`, `uuid` | Generalised polygons | OGL | Identifier *"will not be persistent between product versions"* |
| [HMLR INSPIRE Index Polygons](https://www.gov.uk/guidance/inspire-index-polygons-spatial-data) | INSPIRE ID | Freehold land parcel polygons (England & Wales) | OGL (since July 2020) | Land parcel, not building. Downloaded without login from the [use-land-property-data service](https://use-land-property-data.service.gov.uk/datasets/inspire/download): 318 council zips, GML in EPSG:27700, 24,453,254 polygons (4.1 GB as Parquet). The GML `schemaLocation` points at an unreachable internal host; strip it or GDAL hangs. Attribution: Crown copyright and database right + OS (check pending for redistributing polygons) |

No open release of OS NGD building footprints was found as of October 2026; OpenMap Local remains
the only OS open building layer.

## The closed building blocks

| Dataset | Key | Contains | Access |
|---|---|---|---|
| [OS Places API `GET /search/places/v1/uprn`](https://docs.os.uk/os-apis/accessing-os-apis/os-places-api/technical-specification/uprn) | UPRN | Full DPA/LPI address, postcode, `X_COORDINATE`/`Y_COORDINATE`, classification, `LOCAL_CUSTODIAN_CODE` | Premium (£/transaction); the [OS Data Hub Premium plan](https://www.ordnancesurvey.co.uk/developers/os-data-hub) includes £1,000/month of free API transactions. Free to PSGA members |
| [AddressBase Core](https://docs.os.uk/os-downloads/products/addresses-and-names-portfolio/addressbase-core/addressbase-core-overview) | UPRN | Address elements, single-line address, property point, secondary classification, `TOID`, `USRN`, UDPRN | PSGA only — free at point of use for public sector, not open data |
| [AddressBase Islands](https://ordnancesurvey.co.uk/products/addressbase-islands) | UPRN | Addresses for Northern Ireland, Channel Islands, Isle of Man | Free to PSGA members, otherwise premium |
| [OS NGD Buildings](https://docs.os.uk/osngd/data-structure/buildings/building-features/building-part) | UPRN via `UPRN Reference` | Building and Building Part polygons, `TOID`, status, height/roof attribution, address counts | Premium / PSGA; 6-month Data Exploration Licence trial |
| OS MasterMap Topography Layer | TOID | Building polygons (TopographicArea) | Premium / PSGA |

The `UPRN Reference` cross-reference table in OS NGD Buildings is the licensed UPRN ↔ building
polygon join. It exists — it is just not open.

## The two breaks in the chain

### 1. UPRN → address

OS Open UPRN is explicitly designed as an identifier-plus-position bridge:

> *"OS Open UPRN is an open dataset that contains the position of each record held within
> AddressBase Premium along with the unique identifier."*

There is no open national gazetteer keyed on UPRN. The nearest open proxies carry a UPRN
incidentally, and are partial:

| Source | Coverage | UPRN field | Licence | Limitations |
|---|---|---|---|---|
| **ONSUD / NSUL** — ONS | Every GB UPRN, 6-weekly (Sept 2026 epoch 128, 41.6M rows; the `ONS_LATEST` portal item is stale, use the newest dated item) | `UPRN` mapped to statistical geographies, plus a postcode (since ~Oct 2020) | Verify. Believed OGL, but ONS FOI-2024-2429 says there is *"no formal agreement"* on inclusion/use of postcodes; current release licence not clearly stated | Best open partial address: postcode only, no street or building name |
| **EPC (England & Wales)** — [new service](https://get-energy-performance-data.communities.gov.uk/), [old csvw schema](https://epc.opendatacommunities.org/docs/csvw) | Dwellings certified since 2008 | `UPRN` (added Nov 2021), plus `UPRN_SOURCE` (`Energy Assessor` / `Address Matched`) | All fields **except address and postcode** are OGL v3. Address fields published with OS/Royal Mail consent, restricted use | ~92% of domestic records had a UPRN at release (~1.7M assessor-supplied); weaker for new builds, non-domestic and DECs. Bulk EPC data has moved to `get-energy-performance-data.communities.gov.uk`, which now requires a GOV.UK One Login for bulk download (not yet obtained, so E&W EPC is not in the pilot) |
| **EPC (Scotland)** — [data.gov.scot](https://data.gov.scot/) | 3.67M certificates with a UPRN (`OSG_REFERENCE_NUMBER`) | `OSG_REFERENCE_NUMBER` | Open, no login | Separate register, does not fill the England/Wales gap. Address and free-text columns are dropped; some lodgement dates are invalid (e.g. 2108) and are nulled outside 2007 to today |
| **HMLR Price Paid lookup tables** — [dataset](https://www.gov.uk/government/statistical-data-sets/transaction-unique-identifier-and-uprn-look-up-table-dataset) | England & Wales, transactions from **28 August 2026** | Monthly tables: transaction ID ↔ UPRN, and transaction ID ↔ INSPIRE ID | OGL, but excludes third-party (Royal Mail/OS) rights; PPD rows contain address text | Only August 2026 published so far (84,149 UPRN rows, 74,017 INSPIRE rows nationally; Islington coverage 0.1%). Headerless CSVs at `price-paid-data.publicdata.landregistry.gov.uk/pp-uprn-lookup-<mon>-<year>.csv` and `pp-inspire-id-lookup-…`. Not back-dated; UPRN only where a reliable match exists |
| **OSM addresses** | Partial, uneven | `ref:GB:uprn` tag where mapped | **ODbL** (share-alike) | Sparse coverage; no completeness guarantee. Mixing with OGL outputs has share-alike consequences |

**Street name is the exception: it can be derived openly.** The chain is UPRN → USRN (LIDS
`BLPU_UPRN_Street_USRN_11`, 42.4M rows) → Road TOID (LIDS `Road_TOID_Street_USRN_10`) → OS Open
Names `Named Road` (its ID is the Road TOID; 96.6% join). Combined with locality (OS Open Names) and
postcode (ONSUD), this gives an *open address component*: street + locality + postcode, never house
number or name. See the Islington pilot for coverage.

None of these is a full resolver. They are enrichment, and each fails silently on a different
subset of properties. For redistribution, treat the address text in EPC and PPD as unavailable:
use them as keys only.

### 2. UPRN → polygon

The open chain reaches a TOID and an Open TOID point:

```
UPRN ──(OS Open UPRN)──────────────► point (seed point)
UPRN ──(LIDS)──────────────────────► TOID (+ confidence)
TOID ──(OS Open TOID)──────────────► point inside the feature (pole of inaccessibility, 1 m)
point ──(point-in-polygon, OpenMap Local)──► generalised building outline   ← approximate
TOID ──(NGD Buildings / MasterMap)──► authoritative polygon                  ← premium
```

Because OS OpenMap Local and Zoomstack polygons have no TOID, the open route is geometric.
The Open TOID point is a better anchor than the raw UPRN point: for polygon features it is
guaranteed to lie inside the TopographicArea polygon, whereas a UPRN seed point may sit in a
garden or on the boundary. The approach still degrades in predictable ways:

- **Flats and sub-buildings collapse.** OS Open UPRN points for multiple UPRNs in one
  building stack on the same location, so point-in-polygon resolves every flat to the same
  outline (and every flat shares one TOID point). NGD Building Part would distinguish them.
- **Terraces collapse.** OpenMap Local amalgamates terraced rows, so a whole terrace becomes
  one outline and many houses resolve to it.
- **Generalisation error.** OpenMap Local buildings are amalgamations at 1:10 000-ish
  generalisation; boundaries will not line up with the cadastral footprint.
- **LIDS is itself geometric.** OS documents `BLPU_UPRN_TopographicArea_TOID` as formed by
  point-in-polygon of the AddressBase seed point against the MasterMap TopographicArea polygon
  (done by GeoPlace). UPRN→TOID therefore inherits seed-point problems (stacked flats, seeds in
  gardens), and a TopographicArea need not be a building. The LIDS confidence value indicates
  whether the feature has changed since the relationship was formed — it is **not** a match
  quality score. So LIDS is the *official* link, but not a guarantee of a correct building.
- **Non-building UPRNs.** With no classification in OS Open UPRN, some UPRNs have no building at all.

**INSPIRE parcel route (land parcel, all UPRNs).** The HMLR INSPIRE Index Polygons are downloadable
openly without login, so a point-in-polygon of the UPRN against the parcels gives an open UPRN →
land **parcel** for essentially every England & Wales UPRN (98.3% in Islington). This is a parcel,
not a building, but it is much finer than OpenMap Local in dense areas. The parcel IDs match the
format of HMLR's Price Paid INSPIRE lookup, so the two can be cross-checked. HMLR's monthly
transaction ID ↔ INSPIRE ID / UPRN lookups (since 28 August 2026) remain a sparse, recent-only
supplement. Only parcel IDs are published, not polygons, pending the OS attribution check.

## What a resolver can honestly claim

| Capability | Open-only | With PSGA / premium key |
|---|---|---|
| UPRN → point | Yes (OS Open UPRN) | Yes |
| UPRN → TOID | Yes (LIDS; itself a seed-point join) | Yes |
| UPRN → building polygon | Approximate (Open TOID point + OpenMap Local) | Yes (NGD `UPRN Reference`) |
| UPRN → land parcel polygon | Yes, open, all UPRNs via spatial join (HMLR INSPIRE; E&W) | Yes (INSPIRE / Land Registry products) |
| UPRN → street name | Yes, open, ~93% (UPRN → USRN → Road TOID → OS Open Names) | Yes |
| UPRN → full address | No — street, locality and postcode only; never house number or name | Yes (Places API / AddressBase) |
| Sub-building / flat resolution | No | Yes (LPI + Building Part) |
| Northern Ireland | No (outside all open products) | Yes (AddressBase Islands, PSGA/premium) |

Suggested public wording: the open path resolves a UPRN to **a point and a building TOID**, and
*estimates* the outline. It does not resolve **authoritative outline plus address**. That last
mile is precisely the AddressBase Core / NGD / Places API tier.

Note that Ordnance Survey markets OS Open Linked Identifiers with the line *"link data
related to an address with its building footprint"* — which is accurate only if you
already licence MasterMap or NGD for the geometry.

## Prior art

| Project | What it does | Status / limits |
|---|---|---|
| [GeoPlace FindMyAddress](https://www.geoplace.co.uk/addresses-streets/address-data-and-services/findmyaddress) | Official UPRN ↔ address lookup | Personal non-commercial use; small daily search limit (sources say 10 or 30 per day); no API, no bulk |
| [OSM proposed import of UK postcodes, UPRNs and INSPIRE IDs](https://wiki.openstreetmap.org/wiki/Proposed_import_of_UK_postcodes,_UPRNs_and_INSPIRE_IDs_(England_%26_Wales)) | Tag buildings only where ≥95% lie within an INSPIRE polygon containing exactly one UPRN | Proposed as of Oct 2025; no evidence it started. Terrace-offset concerns |
| [OSM UPRN pilot](https://www.loomio.com/d/s5BWLiFX) | Small trial of UPRN tagging, ~3,500 buildings | Pilot scale only |
| Colouring Britain | Building attribute database | ODbL; OS footprints used under arrangement; footprint geometry excluded from downloads |
| [Glasgow EPC→UPRN lookup](https://researchdata.gla.ac.uk/2211) | EPC records matched to UPRN (96% match) | Research dataset, Glasgow only |
| Ideal Postcodes, Loqate | Commercial address/UPRN lookup | Licensed, paid |

**Conclusion:** no open project combines these into one provenance-tagged dataset.

## Publishing plan: source.coop

Publish a 6-weekly **GeoParquet dataset** rather than run a service. OS already publishes there
([ordnancesurvey/ngd-boundaries](https://s2.source.coop/ordnancesurvey/ngd-boundaries)).

| Table | Columns |
|---|---|
| `uprn` | `uprn`, point, `toid`, `lids_confidence`, TOID point, `building_id`, `match_method`, `methods_disagree`, `uprns_in_building`, `outline_granularity`, `usrn`, `street_name`, `street_name_alt`, `locality`, `parcel_inspire_id`, `parcel_area_m2`, `uprns_in_parcel`, statistical geography codes, PPD transaction IDs, INSPIRE IDs, `epc_built_form`, `epc_property_type`, `epc_count`, optional postcode (pending ONS clarification) |
| `buildings` | `building_id`, polygon |
| `epc` (separate `epc.parquet`) | Scottish EPC certificates keyed by UPRN: all non-address columns (domestic 103, non-domestic 40); address and free-text columns dropped by denylist |

Rules:

- No address text beyond street name, locality and (optionally) postcode; never house number or name (EPC/PPD/OS/Royal Mail rights).
- Parcel IDs only, not parcel polygons, until the OS attribution check is complete.
- No OSM or Overture geometry in the main table; if ever used, a separate ODbL table.
- Attribute: Contains OS data © Crown copyright and database right (and HMLR where used).

National inputs as filtered Parquet total about 7 GB (Open TOID filtered to LIDS-linked TOIDs: 149M → 8.3M rows).

## Pilot results: Islington

London Borough of Islington (E09000019), October 2026 data, 169,184 UPRNs.

**Building outline (OS OpenMap Local)**

| Measure | Result |
|---|---|
| UPRNs with a LIDS TOID | 99.93% |
| Matched via Open TOID point | 94.33% |
| Matched via UPRN point (fallback) | 0.51% |
| Unmatched | 5.16% |
| TOID-point vs UPRN-point disagree | 0 of 158,106 (verified independently) |
| Distinct outlines | 4,968 |
| Max UPRNs in one outline | 833 |
| UPRNs sharing an outline with more than 20 others | 80% |
| UPRN-to-TOID-point distance | median 3 m, p90 21 m, p99 60 m |
| OpenMap Local polygon area | median 376 m², p90 2,142 m² |

The two methods never disagree because OpenMap Local polygons are block-sized: a UPRN and its TOID
point, a few metres apart, always fall in the same polygon. LIDS is strictly 1:1 UPRN → TopographicArea
TOID (41,633,824 rows, all distinct UPRNs). The matching is therefore fine; OpenMap Local
generalisation is the limiting factor, and in dense urban areas "outline" ≈ block.

**Land parcel (HMLR INSPIRE)**

| Measure | Result |
|---|---|
| UPRNs inside a parcel | 98.3% |
| UPRNs alone in a parcel | 12,150 |
| UPRNs alone in an OpenMap Local outline | 349 |

Parcels separate individual properties far better than OpenMap Local outlines. Parcel IDs match the HMLR
Price Paid INSPIRE lookup format.

**Street name (open address component)**

| Measure | Result |
|---|---|
| UPRNs with a USRN | 99.8% |
| UPRNs with a street name | 93.5% |
| Distinct street names | 1,154 |
| Corner UPRNs with 2 USRNs | 275 |
| UPRNs with a postcode (ONSUD) | 98.2% |

The 6% gap is 311 USRNs not in the Highways road network (likely estate or private roads). A spot check
of 19 UPRNs was 19/19 plausible against the postcode. Locality is mostly "London".

**Other**

- HMLR Price Paid lookup coverage is 0.1% (August 2026 only).
- E&W EPC not included: bulk download now needs a GOV.UK One Login. Scottish EPC is available openly.

## Design implication

Structure the resolver as a provider chain so the open and licensed paths share one
interface:

1. `PointProvider` — OS Open UPRN (open, complete GB).
2. `IdentifierProvider` — LIDS / Linked Identifiers API (open, free, unlimited transactions).
3. `GeometryProvider`
   - open: point-in-polygon over OS OpenMap Local using the **Open TOID point first**, falling
     back to the UPRN point; flagged `approximate`. OSM footprints are an optional separate
     (ODbL) source
   - licensed: NGD Buildings `UPRN Reference`, flagged `authoritative`
4. `AddressProvider`
   - open: street name (LIDS USRN → OS Open Names), locality, ONSUD postcode; PPD/EPC as **keys
     only** (address text is not redistributable), flagged `partial` with provenance per field
   - licensed: Places API `?uprn=` or AddressBase Core, flagged `authoritative`

Every response should carry per-field provenance and a confidence flag, so callers can
tell an authoritative outline from an interpolated one. Remember that LIDS confidence is a
staleness signal, not a match-quality score.

## Sources

- OS Open UPRN — product page and technical specification (attributes: UPRN, X/Y, lat/lon only)
- OS Open Linked Identifiers — product page and OS Data Hub download documentation
- [BLPU_UPRN_TopographicArea_TOID technical specification](https://docs.os.uk/os-downloads/products/buildings-and-infrastructure-portfolio/os-open-linked-identifiers/os-open-linked-identifiers-technical-specification/blpu_uprn_topographicarea_toid_5)
- [OS Open TOID technical specification](https://docs.os.uk/os-downloads/identifiers/os-open-toid/os-open-toid-technical-specification)
- OS OpenMap Local — Building feature type specification
- OS Open Zoomstack — GeoPackage schema
- OS NGD Buildings — Building, Building Part, and `UPRN Reference` cross-reference documentation
- OS Places API — `/search/places/v1/uprn` technical specification
- [OS Data Hub](https://www.ordnancesurvey.co.uk/developers/os-data-hub) — Premium plan, £1,000/month free transactions
- [AddressBase Islands](https://ordnancesurvey.co.uk/products/addressbase-islands)
- AddressBase Core — product overview; GOV.UK "Access free address data using AddressBase"
- [HMLR announcement: property identifiers for Price Paid Data from 28 August](https://www.gov.uk/government/news/hm-land-registry-to-provide-property-identifiers-for-price-paid-data-from-28-august)
- [HMLR Transaction unique identifier and UPRN look up table dataset](https://www.gov.uk/government/statistical-data-sets/transaction-unique-identifier-and-uprn-look-up-table-dataset)
- HM Land Registry — Price Paid Data single file; INSPIRE Index Polygons
- EPC England & Wales — CSVW table schema (`UPRN`, `UPRN_SOURCE`); [new bulk service](https://get-energy-performance-data.communities.gov.uk/) (One Login); Scottish EPC on [data.gov.scot](https://data.gov.scot/)
- [HMLR INSPIRE Index Polygons download](https://use-land-property-data.service.gov.uk/datasets/inspire/download)
- [OS Open Names](https://www.ordnancesurvey.co.uk/products/os-open-names); LIDS `BLPU_UPRN_Street_USRN_11` and `Road_TOID_Street_USRN_10`
- [EPC now includes UPRN](https://news.opendatacommunities.org/energy-performance-certificates-now-include-uprn)
- [EPC copyright and licensing](https://epc.opendatacommunities.org/docs/copyright)
- [Open Data Communities relaunch, July 2026](https://mhclgdigital.blog.gov.uk/2026/07/09/shaping-the-future-of-open-data-the-new-open-data-communities-service/)
- [ONS FOI: inclusion of postcodes in ONSUD and NSUL](https://www.ons.gov.uk/aboutus/transparencyandgovernance/freedomofinformationfoi/inclusionofpostcodesintheonsudandnsulproducts)
- [GeoPlace FindMyAddress](https://www.geoplace.co.uk/addresses-streets/address-data-and-services/findmyaddress)
- [OSM proposed UK postcodes/UPRNs/INSPIRE IDs import](https://wiki.openstreetmap.org/wiki/Proposed_import_of_UK_postcodes,_UPRNs_and_INSPIRE_IDs_(England_%26_Wales)); [OSM UPRN pilot](https://www.loomio.com/d/s5BWLiFX)
- [Glasgow EPC→UPRN lookup](https://researchdata.gla.ac.uk/2211)
- [OS on source.coop](https://s2.source.coop/ordnancesurvey/ngd-boundaries)
