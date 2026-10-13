SELECT EXISTS (
    SELECT 1
    FROM information_schema.tables
    WHERE table_catalog = current_database()
        AND table_schema = ?
        AND table_name = ?
        AND table_type = 'BASE TABLE'
)
