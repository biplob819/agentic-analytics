-- LOD-02: Snowflake load for the UK price paid dataset, templated with {catalog}
-- (database), {database} (DATABASE.SCHEMA), {local_file} (the gzipped snapshot on this
-- machine) and {stage_file} (its name on the stage). Generated from transforms.py; do
-- not edit by hand. CREATE statements are setup and always run: the file format, stage
-- and raw table are replaced each time; the typed table is created if missing and the
-- loader refuses it if its columns differ (unless --force, which replaces it). The rest
-- is the load, skipped when the typed table already has rows (unless --force).
-- Empty fields stay empty strings (EMPTY_FIELD_AS_NULL = FALSE, NULL_IF = ()), as in
-- ClickHouse's String columns.
CREATE DATABASE IF NOT EXISTS {catalog};

CREATE SCHEMA IF NOT EXISTS {database};

CREATE OR REPLACE FILE FORMAT {database}.uk_price_paid_csv
    TYPE = CSV
    FIELD_OPTIONALLY_ENCLOSED_BY = '"'
    COMPRESSION = GZIP
    EMPTY_FIELD_AS_NULL = FALSE
    NULL_IF = ()
    ENCODING = 'UTF8';

CREATE OR REPLACE STAGE {database}.uk_price_paid_stage
    FILE_FORMAT = {database}.uk_price_paid_csv;

CREATE OR REPLACE TABLE {database}.uk_price_paid_raw
(
    uuid_string VARCHAR,
    price_string VARCHAR,
    time VARCHAR,
    postcode VARCHAR,
    a VARCHAR,
    b VARCHAR,
    c VARCHAR,
    addr1 VARCHAR,
    addr2 VARCHAR,
    street VARCHAR,
    locality VARCHAR,
    town VARCHAR,
    district VARCHAR,
    county VARCHAR,
    d VARCHAR,
    e VARCHAR
);

CREATE TABLE IF NOT EXISTS {database}.uk_price_paid
(
    price NUMBER(10, 0),
    date DATE,
    postcode1 VARCHAR,
    postcode2 VARCHAR,
    type VARCHAR,
    is_new NUMBER(1, 0),
    duration VARCHAR,
    addr1 VARCHAR,
    addr2 VARCHAR,
    street VARCHAR,
    locality VARCHAR,
    town VARCHAR,
    district VARCHAR,
    county VARCHAR
);

PUT 'file://{local_file}' @{database}.uk_price_paid_stage AUTO_COMPRESS = FALSE OVERWRITE = TRUE;

COPY INTO {database}.uk_price_paid_raw
    FROM @{database}.uk_price_paid_stage/{stage_file}
    FILE_FORMAT = (FORMAT_NAME = '{database}.uk_price_paid_csv')
    ON_ERROR = ABORT_STATEMENT
    FORCE = TRUE;

TRUNCATE TABLE {database}.uk_price_paid;

INSERT INTO {database}.uk_price_paid
SELECT
    TO_NUMBER(price_string, 10, 0) AS price,
    TRY_TO_DATE(LEFT(time, 10), 'YYYY-MM-DD') AS date,
    SPLIT_PART(postcode, ' ', 1) AS postcode1,
    SPLIT_PART(postcode, ' ', 2) AS postcode2,
    CASE a WHEN 'T' THEN 'terraced' WHEN 'S' THEN 'semi-detached' WHEN 'D' THEN 'detached' WHEN 'F' THEN 'flat' WHEN 'O' THEN 'other' ELSE a END AS type,
    IFF(b = 'Y', 1, 0) AS is_new,
    CASE c WHEN 'F' THEN 'freehold' WHEN 'L' THEN 'leasehold' WHEN 'U' THEN 'unknown' ELSE c END AS duration,
    addr1,
    addr2,
    street,
    locality,
    town,
    district,
    county
FROM {database}.uk_price_paid_raw;
