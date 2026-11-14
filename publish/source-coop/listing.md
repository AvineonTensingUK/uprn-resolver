# Source Cooperative listing: GB UPRN Open Resolver

Everything needed to set up the product on Source Cooperative, plus intro text to reuse on the page or when
announcing it. The full landing page (schemas, statistics, licences) is [README.md](README.md) in this folder,
uploaded to the product root.

## Product form

| Field | Value |
|---|---|
| Title | GB UPRN Open Resolver: points, buildings, land parcels and streets for 41.6 million UPRNs |
| Product ID | `uk-uprn-open` (used in the README example URLs; cannot be renamed later) |
| Owner account | `<account>` (your Source username or organisation) |
| Data connection | Source-managed storage, us-west-2 |
| Visibility | Unlisted until the upload is verified, then Listed |

**Description** (456 characters, limit 500):

> Every Great Britain UPRN (41.6 million) resolved to a point, TOID, building outline or land parcel, street name
> and statistical geographies, with a tested confidence label on each answer. Built only from open data: OS
> OpenData, ONS, HM Land Registry, Registers of Scotland and EPC under OGL v3, plus optional Microsoft, Overture and
> OpenStreetMap layers under their own licences. GeoParquet per local authority, STAC catalogue and PMTiles. No
> address text.

**Tags** (18 of 20): `opendata`, `uprn`, `geoparquet`, `ogl`, `great-britain`, `united-kingdom`, `addresses`,
`buildings`, `building-footprints`, `land-parcels`, `inspire`, `epc`, `ordnance-survey`, `toid`, `stac`, `pmtiles`,
`openstreetmap`, `geocoding`

## Short intro

For the top of the page, a post or an email:

> The UPRN is the identifier for every addressable place in Great Britain, and it has been open since 2020, but on
> its own it is just a point. This dataset turns all 41.6 million UPRNs into something you can map and join: the
> building or land parcel each one sits in, its street, its census areas and its energy certificate, each with an
> honest, tested measure of how specific the answer is. Built entirely from open data, free to use under the Open
> Government Licence.

## What it is

An open lookup table, one row per UPRN, covering England, Scotland and Wales. For each UPRN it gives:

- **Where it is:** the OS Open UPRN point, in British National Grid and WGS84.
- **What it sits in:** the best available outline, either an OS OpenMap Local building, a Microsoft building
  footprint, or a land parcel from HM Land Registry or Registers of Scotland, with the polygon itself.
- **How specific that is:** `property` (the outline holds this UPRN alone), `small_group` (two to five UPRNs) or
  `block` (more than five), plus a 0 to 100 quality score and plain-language quality flags.
- **Context:** the TOID and street (USRN and street name), census output area, LSOA and MSOA, local authority, EPC
  property type and built form, a business name where one matches, and recent house sale prices.

It ships as GeoParquet, one file per table per local authority, with a STAC catalogue for discovery and PMTiles for
web maps. A typical lookup reads one small file over HTTPS; nothing needs to be downloaded in full.

## Why it exists

The UPRN is meant to be the common key for property data in Great Britain. Since 2020 it has been open, and many
open datasets now carry it: EPCs, HM Land Registry price paid lookups, council and housing data. But the open UPRN
product is a coordinate and nothing more. The authoritative building polygons and full addresses behind it are in
Ordnance Survey's premium products (OS NGD, OS MasterMap, AddressBase), which public bodies can use under the PSGA
but most researchers, charities, start-ups and open-source projects cannot.

Every piece needed to go further is open, but scattered: OS Open Linked Identifiers links a UPRN to a TOID, OS Open
TOID gives a point inside that feature, OS OpenMap Local has building shapes with no identifiers, and HM Land
Registry and Registers of Scotland publish land parcels. Joining them correctly for 41.6 million UPRNs takes weeks of
work and careful licence reading. This dataset does that once, openly, and says how good each answer is.

Ordnance Survey's own products remain the right choice for authoritative or production use. This is an open
approximation that complements them.

## How it may be useful

- **Mapping UPRN-keyed data.** Many datasets carry a UPRN and nothing else. Join on `uprn` to put them on a map as
  buildings or parcels rather than dots, and aggregate them to census areas.
- **Energy and retrofit.** Combine EPC built form and property type with a footprint or parcel to estimate roof
  and wall areas, or to target areas for insulation, heat pumps or solar.
- **Housing and planning.** Count dwellings per building or parcel, find properties sharing a footprint (flats,
  conversions), and link sale prices to the land they relate to.
- **Risk and resilience.** Overlay flood, subsidence or heat maps on building outlines and parcels instead of
  points, to see which properties a hazard actually touches.
- **Networks and services.** Count premises per building or street for broadband, utilities, waste collection or
  emergency planning.
- **Research and teaching.** A complete, documented, open GB property base layer that can be cited and shared,
  with no licence fees or sign-ups.
- **Data quality.** The quality flags pick out UPRNs whose street, postcode or building links disagree, which is
  useful for cleaning other UPRN-keyed data.

## How good it is

- 97.0% of UPRNs have a best outline, and 47.5% are at `property` level: an outline belonging to that UPRN alone.
- Checked against 2.5 million OpenStreetMap buildings that mappers have tagged with a UPRN, 98.4% of `property`
  outlines cover that property and no neighbouring one. `small_group` and `block` outlines take in neighbours, as
  their labels say.
- 91.1% of UPRNs have a street name, 91.9% fall in a land parcel and 54.9% have an EPC.

Full statistics, by nation and by source, are in the README.

## What it does not do

- **No full addresses.** House numbers, flat numbers and postcodes are not open in Great Britain; they are only in
  AddressBase and Royal Mail's PAF. OpenStreetMap house numbers are offered separately where they exist (15% of
  UPRNs).
- **Outlines are approximate.** In dense areas the best open outline can be a whole block or a terrace; the
  confidence label says when.
- **Great Britain only.** Northern Ireland has no open UPRNs.

## Licence and contact

The core is under the Open Government Licence v3.0, with attribution to Ordnance Survey, ONS, HM Land Registry,
Registers of Scotland and the EPC registers. Microsoft, Overture and OpenStreetMap layers sit in separate folders
under their own licences (CDLA-Permissive-2.0, per-provider Overture terms, ODbL). The code is open source under
the Apache License 2.0: [github.com/AvineonTensingUK/uprn-resolver](https://github.com/AvineonTensingUK/uprn-resolver).

A side project by staff at Avineon Tensing, not an official Avineon Tensing product, and not produced or endorsed by
Ordnance Survey or the other data publishers. Questions and feedback: uk@avineon-tensing.com
