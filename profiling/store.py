"""
Save profiling results to permanent Snowflake tables (HC_MIGRATION_DB.PROFILING).

Called by run_checks() while the temporary table still exists, so the failing
rows are copied inside Snowflake (INSERT ... SELECT) - they never leave it.
Re-profiling a file replaces that file's rows only: DELETE WHERE SOURCE_FILE = ?
then INSERT, in one transaction. Results of other files are kept.
"""
import json
from pathlib import Path

from profiling.checks import MAX_ROWS, ROW_NO, ident, lit, masked
from profiling.report import severity

SCHEMA = "HC_MIGRATION_DB.PROFILING"
DDL = Path(__file__).resolve().parent.parent / "sql" / "01_profiling_tables.sql"
TABLES = ["PROFILE_FAILED_ROW", "PROFILE_CHECK_RESULT", "PROFILE_COLUMN", "PROFILE_RUN"]
RULE_SET_BY = {"ai": "AI", "rules": "BUILT-IN", "user": "EDITED"}


def ensure_tables(conn):
    """Create the schema, tables and views if they don't exist (safe to run every time).
    CREATE SCHEMA switches the session into the new schema, so switch back afterwards -
    the temporary table with the file's rows lives in the original schema."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT CURRENT_DATABASE(), CURRENT_SCHEMA()")
        database, schema = cur.fetchone()
        for c in conn.execute_string(DDL.read_text(), remove_comments=True):
            c.close()
        cur.execute(f"USE SCHEMA {ident(database)}.{ident(schema)}")
    finally:
        cur.close()


def _insert(cur, table, columns, rows, array_cols=()):
    """Multi-row INSERT with bind values; ARRAY columns are sent as JSON text."""
    if not rows:
        return
    select = ", ".join(f"PARSE_JSON(${i})::ARRAY" if c in array_cols else f"${i}" for i, c in enumerate(columns, 1))
    values = ", ".join("(" + ", ".join(["%s"] * len(columns)) + ")" for _ in rows)
    cur.execute(f"INSERT INTO {SCHEMA}.{table} ({', '.join(columns)}) SELECT {select} FROM VALUES {values}",
                [v for row in rows for v in row])


def export_queries(source_file):
    """[(name, sql)] - a list, so the order survives the trip to the page."""
    f = lit(source_file)
    return [
        ("Issues for this file", (
            f"SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, MAX_LENGTH_RULE, ACTUAL_MIN_LENGTH, ACTUAL_MAX_LENGTH,\n"
            f"       CHECK_NAME, SEVERITY, FAILED_ROWS, FAILED_PCT, EXAMPLE_VALUES\n"
            f"FROM {SCHEMA}.V_PROFILE_RESULTS\n"
            f"WHERE SOURCE_FILE = {f} AND STATUS = 'FAIL'\n"
            f"ORDER BY DECODE(SEVERITY, 'High', 1, 'Medium', 2, 3), FAILED_ROWS DESC;")),
        ("Failing rows for this file", (
            f"SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHECK_NAME, SEVERITY, ROW_NO,\n"
            f"       KEY_COLUMN, KEY_VALUE, COLUMN_VALUE, VALUE_LENGTH\n"
            f"FROM {SCHEMA}.V_PROFILE_FAILED_ROWS\n"
            f"WHERE SOURCE_FILE = {f}\n"
            f"ORDER BY COLUMN_NAME, CHECK_NAME, ROW_NO;")),
        ("Column profile for this file", (
            f"SELECT COLUMN_POSITION, COLUMN_NAME, DATA_TYPE, LOADED_AS, IS_REQUIRED, MIN_LENGTH_RULE, MAX_LENGTH_RULE,\n"
            f"       ROW_COUNT, NULL_COUNT, NULL_PCT, DISTINCT_COUNT, MIN_LENGTH, MAX_LENGTH, TOP_VALUES\n"
            f"FROM {SCHEMA}.PROFILE_COLUMN\n"
            f"WHERE SOURCE_FILE = {f}\n"
            f"ORDER BY COLUMN_POSITION;")),
    ]


def save(conn, temp_table, meta, rules, results, checks):
    """
    meta: run_id, source_file, run_by, rules_by, run_at.
    checks: the list from build_checks() (index = check id), still pointing at temp_table.
    """
    run_id, source = meta["run_id"], meta["source_file"]
    table_name = Path(source).stem.upper()
    sensitive = {r["column"] for r in rules if r["type"] in ("SSN", "TAX_ID")}
    key = next((r["column"] for r in rules if r["unique"]), rules[0]["column"])
    by_id = {c["id"]: c for c in results["checks"]}
    common = [run_id, source, table_name]

    ensure_tables(conn)
    cur = conn.cursor()
    try:
        cur.execute("BEGIN")
        replaced = 0
        for t in TABLES:
            cur.execute(f"DELETE FROM {SCHEMA}.{t} WHERE SOURCE_FILE = %s", (source,))
            if t == "PROFILE_RUN":
                replaced = cur.rowcount or 0

        _insert(cur, "PROFILE_RUN",
                ["RUN_ID", "SOURCE_FILE", "TABLE_NAME", "ROW_COUNT", "COLUMN_COUNT", "CHECKS_RUN", "CHECKS_FAILED",
                 "ISSUES_FOUND", "COLUMNS_WITH_ISSUES", "RULES_PROPOSED_BY", "RUN_BY", "RUN_AT", "DURATION_SECONDS"],
                [common + [results["rows"], len(rules), results["checks_run"], results["checks_failed"], results["issues"],
                           results["columns_with_issues"], meta["rules_by"], meta["run_by"], meta["run_at"],
                           results["seconds"]]])

        profile = {p["column"]: p for p in results["profile"]}
        _insert(cur, "PROFILE_COLUMN",
                ["RUN_ID", "SOURCE_FILE", "TABLE_NAME", "COLUMN_POSITION", "COLUMN_NAME", "DATA_TYPE", "LOADED_AS",
                 "IS_REQUIRED", "IS_UNIQUE", "MIN_LENGTH_RULE", "MAX_LENGTH_RULE", "DATE_FORMAT", "ALLOWED_VALUES",
                 "RULE_SET_BY", "RULE_REASON", "ROW_COUNT", "NULL_COUNT", "NULL_PCT", "DISTINCT_COUNT",
                 "MIN_LENGTH", "MAX_LENGTH", "TOP_VALUES"],
                [common + [i + 1, r["column"], r["type"], "VARCHAR", r["required"], r["unique"], r["min_len"], r["max_len"],
                           r["date_format"], json.dumps(r["allowed_values"]), RULE_SET_BY.get(r["source"], r["source"]),
                           r["reason"][:1000], p["rows"], p["nulls"], p["null_pct"], p["distinct"], p["min_len"],
                           p["max_len"], json.dumps(p["top_values"])]
                 for i, r in enumerate(rules) for p in [profile[r["column"]]]],
                array_cols=("ALLOWED_VALUES", "TOP_VALUES"))

        _insert(cur, "PROFILE_CHECK_RESULT",
                ["RUN_ID", "SOURCE_FILE", "TABLE_NAME", "CHECK_ID", "COLUMN_NAME", "DATA_TYPE", "CHECK_NAME", "SEVERITY",
                 "STATUS", "FAILED_ROWS", "FAILED_PCT", "EXAMPLE_VALUES", "CHECK_CONDITION"],
                [common + [i, c["column"], c["type"], c["check"],
                           severity(c["check"]) if by_id[i]["failed"] else None,
                           "FAIL" if by_id[i]["failed"] else "PASS", by_id[i]["failed"], by_id[i]["pct"],
                           json.dumps(by_id[i]["examples"]), c["where"].replace(temp_table, "<SOURCE_TABLE>")[:4000]]
                 for i, c in enumerate(checks)],
                array_cols=("EXAMPLE_VALUES",))

        # failing rows: copied straight from the temp table inside Snowflake
        key_expr = masked(ident(key)) if key in sensitive else ident(key)
        parts = []
        for i, c in enumerate(checks):
            if not by_id[i]["failed"]:
                continue
            value = masked(ident(c["column"])) if c["column"] in sensitive else ident(c["column"])
            parts.append(
                f"SELECT * FROM (SELECT {lit(run_id)}, {lit(source)}, {lit(table_name)}, {i}, {lit(c['column'])}, "
                f"{lit(c['check'])}, {ident(ROW_NO)}, {lit(key)}, {key_expr}, {value}, LENGTH({ident(c['column'])}) "
                f"FROM {temp_table} WHERE {c['where']} ORDER BY {ident(ROW_NO)} LIMIT {MAX_ROWS})")
        failed_rows = 0
        if parts:
            cur.execute(f"INSERT INTO {SCHEMA}.PROFILE_FAILED_ROW (RUN_ID, SOURCE_FILE, TABLE_NAME, CHECK_ID, COLUMN_NAME, "
                        f"CHECK_NAME, ROW_NO, KEY_COLUMN, KEY_VALUE, COLUMN_VALUE, VALUE_LENGTH)\n"
                        + "\nUNION ALL\n".join(parts))
            failed_rows = cur.rowcount or 0
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
    finally:
        cur.close()
    return {"ok": True, "schema": SCHEMA, "source_file": source, "replaced_previous": bool(replaced),
            "rows_saved": {"PROFILE_RUN": 1, "PROFILE_COLUMN": len(rules), "PROFILE_CHECK_RESULT": len(checks),
                           "PROFILE_FAILED_ROW": failed_rows},
            "export_queries": export_queries(source)}
