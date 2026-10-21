-- Permanent tables for the AI Data Profiling Agent.
-- The app runs this before every save (IF NOT EXISTS, so it is safe to re-run).
-- Re-profiling a file replaces that file's rows only (DELETE ... WHERE SOURCE_FILE = ? + INSERT, one transaction).

CREATE SCHEMA IF NOT EXISTS HC_MIGRATION_DB.PROFILING
  COMMENT = 'Results of the AI Data Profiling Agent - one set of rows per source file';

CREATE TABLE IF NOT EXISTS HC_MIGRATION_DB.PROFILING.PROFILE_RUN (
    RUN_ID               VARCHAR(32)   NOT NULL,
    SOURCE_FILE          VARCHAR(255)  NOT NULL,   -- e.g. SRC_MEMBER.txt  (re-run key)
    TABLE_NAME           VARCHAR(255)  NOT NULL,   -- e.g. SRC_MEMBER
    ROW_COUNT            NUMBER        NOT NULL,
    COLUMN_COUNT         NUMBER        NOT NULL,
    CHECKS_RUN           NUMBER        NOT NULL,
    CHECKS_FAILED        NUMBER        NOT NULL,
    ISSUES_FOUND         NUMBER        NOT NULL,
    COLUMNS_WITH_ISSUES  NUMBER        NOT NULL,
    RULES_PROPOSED_BY    VARCHAR(50),              -- AI model name or 'built-in rules'
    RUN_BY               VARCHAR(255),
    RUN_AT               TIMESTAMP_NTZ NOT NULL,
    DURATION_SECONDS     NUMBER(10,1)
);

CREATE TABLE IF NOT EXISTS HC_MIGRATION_DB.PROFILING.PROFILE_COLUMN (
    RUN_ID               VARCHAR(32)   NOT NULL,
    SOURCE_FILE          VARCHAR(255)  NOT NULL,
    TABLE_NAME           VARCHAR(255)  NOT NULL,
    COLUMN_POSITION      NUMBER        NOT NULL,
    COLUMN_NAME          VARCHAR(255)  NOT NULL,
    DATA_TYPE            VARCHAR(30)   NOT NULL,   -- profiling type: NAME, DATE, PHONE, SSN, CODE ...
    LOADED_AS            VARCHAR(30)   NOT NULL,   -- VARCHAR - every value is loaded as text
    IS_REQUIRED          BOOLEAN,
    IS_UNIQUE            BOOLEAN,
    MIN_LENGTH_RULE      NUMBER,
    MAX_LENGTH_RULE      NUMBER,
    DATE_FORMAT          VARCHAR(30),
    ALLOWED_VALUES       ARRAY,
    RULE_SET_BY          VARCHAR(20),              -- AI / BUILT-IN / EDITED
    RULE_REASON          VARCHAR(1000),
    ROW_COUNT            NUMBER,
    NULL_COUNT           NUMBER,
    NULL_PCT             NUMBER(7,2),
    DISTINCT_COUNT       NUMBER,
    MIN_LENGTH           NUMBER,                   -- actual, trimmed
    MAX_LENGTH           NUMBER,
    TOP_VALUES           ARRAY                     -- [{"value":..., "count":...}], SSN / tax id masked
);

CREATE TABLE IF NOT EXISTS HC_MIGRATION_DB.PROFILING.PROFILE_CHECK_RESULT (
    RUN_ID               VARCHAR(32)   NOT NULL,
    SOURCE_FILE          VARCHAR(255)  NOT NULL,
    TABLE_NAME           VARCHAR(255)  NOT NULL,
    CHECK_ID             NUMBER        NOT NULL,
    COLUMN_NAME          VARCHAR(255)  NOT NULL,
    DATA_TYPE            VARCHAR(30)   NOT NULL,
    CHECK_NAME           VARCHAR(255)  NOT NULL,
    SEVERITY             VARCHAR(10),              -- High / Medium / Low (NULL when passed)
    STATUS               VARCHAR(4)    NOT NULL,   -- PASS / FAIL
    FAILED_ROWS          NUMBER        NOT NULL,
    FAILED_PCT           NUMBER(7,2)   NOT NULL,
    EXAMPLE_VALUES       ARRAY,                    -- up to 5, SSN / tax id masked
    CHECK_CONDITION      VARCHAR(4000)             -- the SQL WHERE clause that finds the failing rows
);

CREATE TABLE IF NOT EXISTS HC_MIGRATION_DB.PROFILING.PROFILE_FAILED_ROW (
    RUN_ID               VARCHAR(32)   NOT NULL,
    SOURCE_FILE          VARCHAR(255)  NOT NULL,
    TABLE_NAME           VARCHAR(255)  NOT NULL,
    CHECK_ID             NUMBER        NOT NULL,
    COLUMN_NAME          VARCHAR(255)  NOT NULL,
    CHECK_NAME           VARCHAR(255)  NOT NULL,
    ROW_NO               NUMBER        NOT NULL,   -- 1 = first data row after the header
    KEY_COLUMN           VARCHAR(255),
    KEY_VALUE            VARCHAR(1000),
    COLUMN_VALUE         VARCHAR(4000),            -- as in the file (SSN / tax id masked)
    VALUE_LENGTH         NUMBER                    -- spaces included
);

-- Export query 1: one row per check, with the column details next to it
CREATE OR REPLACE VIEW HC_MIGRATION_DB.PROFILING.V_PROFILE_RESULTS AS
SELECT r.TABLE_NAME, r.SOURCE_FILE, c.COLUMN_POSITION, k.COLUMN_NAME, k.DATA_TYPE, c.LOADED_AS,
       c.IS_REQUIRED, c.MIN_LENGTH_RULE, c.MAX_LENGTH_RULE, c.DATE_FORMAT, ARRAY_TO_STRING(c.ALLOWED_VALUES, ', ') AS ALLOWED_VALUES,
       c.MIN_LENGTH AS ACTUAL_MIN_LENGTH, c.MAX_LENGTH AS ACTUAL_MAX_LENGTH, c.NULL_COUNT, c.DISTINCT_COUNT,
       k.CHECK_NAME, k.STATUS, k.SEVERITY, k.FAILED_ROWS, k.FAILED_PCT,
       ARRAY_TO_STRING(k.EXAMPLE_VALUES, ' | ') AS EXAMPLE_VALUES, k.CHECK_CONDITION,
       r.ROW_COUNT AS FILE_ROW_COUNT, r.RUN_AT, r.RUN_BY, r.RULES_PROPOSED_BY, r.RUN_ID
FROM HC_MIGRATION_DB.PROFILING.PROFILE_RUN r
JOIN HC_MIGRATION_DB.PROFILING.PROFILE_CHECK_RESULT k ON k.RUN_ID = r.RUN_ID
JOIN HC_MIGRATION_DB.PROFILING.PROFILE_COLUMN c ON c.RUN_ID = r.RUN_ID AND c.COLUMN_NAME = k.COLUMN_NAME;

-- Export query 2: the exact failing rows, with the column details
CREATE OR REPLACE VIEW HC_MIGRATION_DB.PROFILING.V_PROFILE_FAILED_ROWS AS
SELECT f.TABLE_NAME, f.SOURCE_FILE, f.COLUMN_NAME, c.DATA_TYPE, c.LOADED_AS, c.MAX_LENGTH_RULE,
       f.CHECK_NAME, k.SEVERITY, f.ROW_NO, f.KEY_COLUMN, f.KEY_VALUE, f.COLUMN_VALUE, f.VALUE_LENGTH, f.RUN_ID
FROM HC_MIGRATION_DB.PROFILING.PROFILE_FAILED_ROW f
JOIN HC_MIGRATION_DB.PROFILING.PROFILE_CHECK_RESULT k ON k.RUN_ID = f.RUN_ID AND k.CHECK_ID = f.CHECK_ID
JOIN HC_MIGRATION_DB.PROFILING.PROFILE_COLUMN c ON c.RUN_ID = f.RUN_ID AND c.COLUMN_NAME = f.COLUMN_NAME;
