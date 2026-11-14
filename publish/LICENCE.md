# Licence

Almost everything in this release is under the Open Government Licence v3.0. Two labelled parts have their own
open licences: Microsoft building footprints (`microsoft_buildings/`, CDLA-Permissive-2.0) and the business name
columns of `uprn_lookup/` (Overture Maps, per provider). No share-alike data is included.

| Folder | Licence | Notes |
|---|---|---|
| `uprn_links/`, `os_buildings/` | OGL v3.0 | OGL core (OS, ONS, HMLR, RoS, EPC non-address fields) |
| `land_parcels/` | OGL v3.0 | Land parcel polygons: HM Land Registry INSPIRE Index Polygons (England and Wales) and Registers of Scotland cadastral parcels. Needs the HM Land Registry, Ordnance Survey (AC0000851063) and Registers of Scotland statements below. `LICENCE.txt` |
| `uprn_lookup/` | OGL v3.0 core + third-party columns | `poi_*` columns are Overture Places (per-provider licence in `poi_licence`); any Microsoft-derived outline ids/areas are CDLA-Permissive-2.0. Contains no OSM/ODbL data. `LICENCE.txt` |
| `microsoft_buildings/` | CDLA-Permissive-2.0 | Microsoft building footprints used as a best outline; `LICENCE.txt` |
| `tiles/` | OGL v3.0 | Rendered from `uprn_lookup/`, `os_buildings/` and `land_parcels/`; same statements |
| `accuracy.json` | ODbL-1.0 (Produced Work) | Aggregate statistics derived from OpenStreetMap; see section 2 |

`address_local` and `resolved_local` (EPC address text, postcodes) are never part of a release.

## 1. OGL v3.0 core

Made available under the
[Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
You are free to copy, publish, distribute, adapt and use it commercially, provided you acknowledge the
sources below, and carry the same acknowledgements into any onward re-use or sub-licence.

### Required attribution (data release year: 2026)

Contains OS data © Crown copyright and database right 2026

Contains GeoPlace data © Local Government Information House Limited copyright and database right 2026

Source: Office for National Statistics licensed under the Open Government Licence v.3.0

Contains HM Land Registry data © Crown copyright and database right 2026. This data is licensed under the Open Government Licence v3.0.

UPRNs contain OS data © Crown copyright and database rights 2026. This data is licensed under the Open Government Licence v3.0.

This information is subject to Crown copyright and database rights 2026 and is reproduced with the permission of HM Land Registry.
The polygons (including the associated geometry, namely x, y co-ordinates) are subject to Crown copyright and database rights 2026 Ordnance Survey AC0000851063.
(England and Wales parcel polygons, identifiers, areas and counts; HM Land Registry INSPIRE Index Polygons, OGL v3.0. Conditions:
<https://use-land-property-data.service.gov.uk/datasets/inspire/#conditions>)

© Crown copyright. Reproduced with the permission of Registers of Scotland. (Scottish parcel polygons, identifiers and areas;
Registers of Scotland INSPIRE Cadastral Parcels, OGL v3, with the OS statement above.)

Contains Royal Mail data © Royal Mail copyright and database right 2026
(required in all cases by this release, the safe option; strictly needed only if postcode-related data ships, see
the uncertainty notes). If postcode-related columns ship (`--include-postcode` builds, `provenance` postcode text
columns, or Code-Point Open derived values), also add:
"Contains National Statistics data © Crown copyright and database right 2026" (Code-Point Open requires all three
statements: OS, Royal Mail, National Statistics; DATASOURCES.md).

Contains EPC register data (non-address fields) from MHCLG (England and Wales) and the Scottish Government under the
OGL v3.0. Address fields are not included.

Use the year of the release you are using (HM Land Registry: "year of supply or date of publication").

### Which statement comes from where

| Statement | Required by | Applies to |
|---|---|---|
| Contains OS data | OS OpenData (OGL); ONS (UPRN products); HMLR; RoS | All OS-derived columns (UPRN, coordinates, TOID, LIDS, street names, building outlines) |
| Contains Royal Mail data | ONS; Code-Point Open | ONSUD-derived columns; any postcode-related columns |
| Contains GeoPlace data | ONS (UPRN products) | ONSUD-derived columns |
| Source: Office for National Statistics ... | ONS | ONSUD-derived columns (oa, lsoa, msoa, lad) |
| Contains HM Land Registry data ... | HMLR | `ppd_transaction_ids`, `inspire_ids` (look-up tables) |
| This information is subject to Crown copyright ... permission of HM Land Registry. | HMLR INSPIRE conditions | England and Wales rows of `land_parcels/`; `parcel_inspire_id`, `parcel_area_m2`, `uprns_in_parcel` |
| The polygons ... Ordnance Survey AC0000851063. | HMLR INSPIRE conditions (OS statement) | England and Wales rows of `land_parcels/` (the polygons themselves) and the columns derived from them |
| UPRNs contain OS data ... | HMLR (Transaction ID <-> UPRN table) | `ppd_transaction_ids` |
| © Crown copyright. Reproduced with the permission of Registers of Scotland. | RoS (catalogue record, spatialdata.gov.scot) | Scottish rows of `land_parcels/`; Scottish `parcel_inspire_id`, `parcel_area_m2`, `uprns_in_parcel` |
| Contains National Statistics data | OS Code-Point Open terms | Only if Code-Point Open derived postcode columns ship |

`uprn_lookup` `street_name` / `locality` / `street_source` are OS Open Names / Open Roads values (OGL, OS
statement); they are not address text from EPC, OSM or Royal Mail.

### Conditional or uncertain statements

- **Royal Mail statement and postcode.** The ONS licences page lists the Royal Mail statement for
  postcode products. Its listing for UPRN products (ONSUD) also appeared to include it, but the page
  summary I retrieved was ambiguous on whether Royal Mail is strictly required when no postcode is
  included. **Unverified.** This dataset carries the Royal Mail statement in all cases (the safe option),
  and builds made with `--include-postcode` certainly require it. ONS postcodes are derived from OS
  AddressBase and checked against Code-Point Open, and ONS says no formal agreement covers postcode
  inclusion in its UPRN products (ONS FOI-2024-2429). Postcodes starting "BT" are excluded from free
  re-use and are not covered by this dataset (Great Britain only).
- **Downstream users.** ONS states the statements "must be acknowledged or displayed whenever the
  owners data is used"; it does not separately address redistribution. OS guidance (per search results,
  not read directly from an OS page) says the acknowledgement should be included in any sub-licences
  and further re-use. Treat the obligation as passing downstream.
- **OS OpenData wording.** "Contains OS data © Crown copyright and database right [year]" is confirmed
  only via third-party pages and search snippets quoting OS guidance; the OS pages I could fetch
  (product page, technical specification) contain no attribution text, and the OS licensing pages
  returned errors. **Unverified against an OS-hosted page.** Check the OS Data Hub download page for
  each product before release.
- **Code-Point Open.** Not redistributed; only (optionally) ONSUD postcodes, which ONS validated against it, and
  QA flags derived from it in `provenance`. Whether that triggers the extra OS statements is **unverified**; the
  statements above cover them in substance.
- **Registers of Scotland.** The RoS wording is from the catalogue record
  (<https://spatialdata.gov.scot/geonetwork/srv/api/records/2c17e031-5b71-4580-bf44-dd5250cc92a6>) and the OGL link
  on the download page (<https://ros-inspire.themapcloud.com/>, "Terms and Conditions: Open Government Licence OGL3",
  read 2026-11-14). The catalogue record asks for both statements "on any product or report created from the
  dataset". The Scottish polygons are shipped in `land_parcels/` on that basis.
- **ONSUD user guide.** The per-release user guide PDF (licence section and postcode attribution table)
  was not read; only the ONS licences page and FOI response were. **Unverified.**

## 2. OpenStreetMap (`accuracy.json` only)

No OpenStreetMap data is in the tables or tiles. `accuracy.json` holds aggregate statistics from comparing
`uprn_lookup` with OpenStreetMap buildings tagged with a UPRN; as a Produced Work under the
[ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/) (section 4.3) it carries the notice "Contains
information from OpenStreetMap, © OpenStreetMap contributors, ODbL 1.0"
(<https://www.openstreetmap.org/copyright>).

## 3. Overture Maps Places (`poi_*` columns in `uprn_lookup/`)

Contains Places data from Overture Maps Foundation (<https://overturemaps.org>). Source:
<https://docs.overturemaps.org/guides/places/>; per-provider licences and attribution:
<https://docs.overturemaps.org/attribution/>. Licence depends on the provider of each record
(`poi_licence`, and `uprn_lookup/LICENCE.txt` for the release in use):

- CDLA-Permissive-2.0: Meta, Microsoft, PinMeTo, Krick, RenderSEO, DAC, BrightQuery, Birdeye, Uberall,
  OpenTravel Alliance.
- Apache-2.0: Foursquare (Copyright Foursquare Labs, Inc. 2024; data transformed to the Overture schema). Apache-2.0
  requires carrying the licence text and the NOTICE with redistributed copies: include the Foursquare NOTICE from the
  Overture attribution page (<https://docs.overturemaps.org/attribution/>) when redistributing Foursquare-sourced rows.
- CC0-1.0: AllThePlaces.

The Places theme contains no OpenStreetMap data (per the Overture attribution page; **re-check each Overture release**).
The match (`poi_match_confidence`) is derived by uprn-resolver and is heuristic. The provider list above is from the repository's `poi.LICENCE.txt`; the
Apache-2.0 NOTICE text itself was not retrieved here (**unverified**).

## 4. Microsoft building footprints (`microsoft_buildings/`, and ids in `uprn_lookup/`)

Contains Microsoft Building Footprints (GlobalMLBuildingFootprints), licensed under CDLA-Permissive-2.0
(<https://cdla.dev/permissive-2-0/>). Source: <https://github.com/microsoft/GlobalMLBuildingFootprints>
(the repository README states the data is licensed by Microsoft under the CDLA Permissive 2.0). CDLA-Permissive-2.0
requires that the licence text (or a link) accompany redistribution of the data; it places no condition on the
results of analysis. Polygons are reprojected to EPSG:27700. Only footprints that are some UPRN's best outline are included; the
link to each UPRN is derived by spatial join with OS Open UPRN / Open TOID, so it also contains OS data (OGL
statement above applies). Attribution: "Building footprints © Microsoft, CDLA-Permissive-2.0".
The Microsoft data is taken from Microsoft's own files, not Overture's ODbL building copy.

## 5. INSPIRE index polygons

The polygons of parcels that contain at least one UPRN are included in `land_parcels/`, with the INSPIRE ID, parcel area
and UPRN count per parcel. HM Land Registry's
conditions (<https://use-land-property-data.service.gov.uk/datasets/inspire/#conditions>, read 2026-11-14) put the INSPIRE Index Polygons "and attributes" under the OGL and require
the HM Land Registry statement above; anyone reusing the polygons or their geometry must also display the Ordnance
Survey statement above (AC0000851063). This release carries both. HM Land Registry confirmed (enquiry ref 261023-6878290) that the same terms apply to anyone receiving
this dataset and must be included in any sub-licence.

If you combine this dataset with address text from Price Paid Data, your use of that address data
remains subject to the Price Paid Data licensing conditions. This dataset contains no address text.
HMLR notes that transaction IDs and UPRNs are linked only where AddressBase and the transaction are related.

## 6. Sources of attribution wording

- ONS geography licences: <https://www.ons.gov.uk/methodology/geography/licences> (OS, Royal Mail, GeoPlace, ONS statements)
- ONS FOI-2024-2429 on postcodes in ONSUD/NSUL: <https://www.ons.gov.uk/aboutus/transparencyandgovernance/freedomofinformationfoi/inclusionofpostcodesintheonsudandnsulproducts>
- HMLR Transaction ID and UPRN look-up table: <https://www.gov.uk/government/statistical-data-sets/transaction-unique-identifier-and-uprn-look-up-table-dataset>
- HMLR Transaction ID and INSPIRE ID look-up table: <https://www.gov.uk/government/statistical-data-sets/transaction-unique-identifier-and-inspire-id-look-up-table-dataset>
- OS OpenData (OGL; attribution wording from secondary sources, see note above): <https://www.os.uk/opendata>
  and <https://www.ordnancesurvey.co.uk/business-government/licensing-agreements/copyright-acknowledgements>
- Open Government Licence v3.0: <https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/>

## 7. Sources

OS Open UPRN, OS Open Linked Identifiers, OS Open TOID, OS OpenMap Local, OS Open Names, OS Open Roads, OS Open USRN (OGL); Registers of Scotland INSPIRE parcels (OGL); ONSUD (ONS, OGL);
HM Land Registry Transaction ID to UPRN and Transaction ID to INSPIRE ID look-up tables (OGL).

## 8. Disclaimer

Provided as is, with no warranty. Building outlines are approximate. The release contains no address text: no house numbers, building names or postcodes.
