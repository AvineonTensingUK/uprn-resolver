-- DuckDB convenience views over the hive-partitioned resolved products.
-- Usage:  duckdb -c ".read uprn_resolver/sql/resolver.sql"   (after replacing {OUT_DIR}, default data/out)
--     or  uprn_resolver.resolve.create_views(con, 'data/out')
-- Then:   SELECT * FROM resolved WHERE uprn = 5300001234;
--         SELECT * FROM resolved_local WHERE uprn = 5300001234;   -- local only, never publish
CREATE OR REPLACE VIEW resolved AS
SELECT * EXCLUDE (geometry) FROM read_parquet('{OUT_DIR}/lad=*/resolved_open.parquet',
    hive_partitioning = true, union_by_name = true);
CREATE OR REPLACE VIEW resolved_local AS
SELECT * EXCLUDE (geometry) FROM read_parquet('{OUT_DIR}/lad=*/resolved_local.parquet',
    hive_partitioning = true, union_by_name = true);
-- Open view columns include best_outline_source in {openmap_building, ms_building, inspire_parcel, ros_parcel}
-- and best_outline_licence ('OGL-3.0' | 'CDLA-Permissive-2.0').  resolved_local adds best_outline_*_local
-- (OSM/ODbL candidates included) and outline_confidence_local.
-- Example: SELECT best_outline_source, count(*) FROM resolved GROUP BY 1;
