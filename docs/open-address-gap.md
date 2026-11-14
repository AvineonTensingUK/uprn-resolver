# The open address and building-outline gap in Great Britain

Evidence summary for policy makers. National build of 23 October 2026: **41,593,127 UPRNs** (ONSUD, 350 local authorities).
All figures are measured on that build (`data/out/national/stats.json` and the per-authority `resolved.stats.json` files).

## What a UPRN is, and why it matters

A UPRN is the unique number for every addressable property in Britain. Ordnance Survey publishes every UPRN openly, but only
as a point on a map. The two things people usually want next, which building it is and what its address is, are not openly available.

## What open data achieves today

Per property, open data (licences that allow free reuse) gives:

| Question | Open result | Note |
|---|---|---|
| Where is it? | 99.99% (41,587,313 points) | Authoritative |
| Which building outline? | 47.5% a per-property match, 20.5% a small group (2 to 5 or so), 29.0% a whole block, 3.0% none | "Per property" is mostly a land parcel, not a building. 47.8% of UPRNs use an HM Land Registry parcel as their best outline, 35.1% an OS generalised building |
| Same, adding OpenStreetMap | 50.9% per property, 2.9% none | OpenStreetMap is share-alike (ODbL), so this cannot be released with the main open data |
| Street name | 91.1% | From OS Open Names |
| Full address (house number, street, town) | **0% openly released** | Only on the local machine |
| Full address, using EPC address text | 54.9% | EPC address fields are restricted; cannot be published |
| Full address, adding OSM | 58.7% | EPC 54.9% plus OSM 3.8%; OSM share-alike, local only |
| House numbers | Closed | Only EPC (restricted) and patchy OSM |

Anything the open data cannot answer is labelled in the dataset rather than guessed.

## What is blocked, and by whom

- **Addresses.** The address text of every property is held in AddressBase, owned in practice by Ordnance Survey and
  GeoPlace, with Royal Mail rights over the postal data. It is licensed (PSGA for public bodies, paid otherwise) and not
  open. The Energy Performance Certificate register carries addresses for about 55% of properties, but the department
  that runs it (MHCLG) publishes them only with OS and Royal Mail consent and restricted reuse. HM Land Registry's Price
  Paid Data has the same restriction. Council Tax lists released by councils under the OGL were disputed by OS and GeoPlace
  on 8 May 2026.
- **Building outlines.** The true per-building polygons are OS MasterMap Topography / NGD Buildings, premium products. The
  open OS product (OpenMap Local) is generalised and merges terraces. Free alternatives (OpenStreetMap, Microsoft) improve
  coverage but are licensed differently or are machine-drawn.
- **Postcodes.** The ONS says there is "no formal agreement" about redistributing postcodes inside its UPRN products, so we
  hold them back pending a reply (an enquiry is drafted, not yet sent).
- **Land parcels.** HM Land Registry's open INSPIRE polygons are open, but the exact attribution for redistributing geometry
  is unconfirmed (enquiry drafted).

## What would unlock it

1. Release the OS AddressBase Core address text, or at least house number and street per UPRN, under the OGL. This single
   change closes the address gap.
2. Release the OS NGD Building footprint with the UPRN cross-reference openly, replacing OpenMap Local.
3. Confirm in writing that postcodes may travel with UPRNs (ONS) and which attribution applies.
4. Have MHCLG and OS/Royal Mail agree that EPC address text may be reused openly.
5. Give HM Land Registry's INSPIRE polygons a clear redistribution statement.

## Northern Ireland

Northern Ireland has **no open UPRNs at all**. They exist only in OSNI Pointer (about 9.2 thousand pounds for the full
file). Open data there is limited to OpenStreetMap (address points and buildings, no UPRNs), Microsoft building footprints
and townland boundaries. NI EPC data, released following a Freedom of Information request, may carry addresses and UPRNs; this
is unverified.

## Caveats

Figures are measured on this build. The 47.5% and 50.9% are the share graded "property" for the open and local-use builds;
accuracy against OS MasterMap was not measured. Three authorities (E07000046, E08000025, E08000035) lack Microsoft outline
input, which slightly lowers the 47.5% there. The 54.9% EPC share equals EPC certificate presence nationally.
