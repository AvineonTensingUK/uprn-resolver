"""Public names used in a published release. The pipeline keeps its internal table names (``resolved_open``,
``uprn``, ...); ``publish.apply_public_names`` renames the tables and the ``best_outline_source`` values when a
single-file release is written, so users see names that say what each table holds."""

# internal table -> published table (folder and file name)
PUBLIC_TABLES = {
    "resolved_open": "uprn_lookup",
    "uprn": "uprn_links",
    "buildings": "os_buildings",
    "parcels": "land_parcels",
    "outline_ms_buildings": "microsoft_buildings",
}
INTERNAL_TABLES = {v: k for k, v in PUBLIC_TABLES.items()}

# internal best_outline_source value -> published value (England/Wales vs Scotland stays in parcel_source)
PUBLIC_SOURCES = {
    "openmap_building": "os_building",
    "inspire_parcel": "land_parcel",
    "ros_parcel": "land_parcel",
    "ms_building": "microsoft_building",
}


def public(table: str) -> str:
    return PUBLIC_TABLES.get(table, table)


def internal(table: str) -> str:
    return INTERNAL_TABLES.get(table, table)
