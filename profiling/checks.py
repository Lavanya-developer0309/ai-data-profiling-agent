"""
Step 3 - run the approved checks in Snowflake on every row.

The file is loaded into a TEMPORARY table (gone when the connection closes),
then two queries run:
  1. column profile: rows, nulls, distinct values, min/max length, top values
  2. one UNION ALL branch per check: failed row count + up to 5 example bad values

  3. the row numbers of the failing rows (up to MAX_ROWS per check), so the page
     can show the exact rows behind every count

Every number shown to the user comes from these queries - never from the AI.
Check names match the "Checks that will run" chips in static/app.js.
"""
import json
import time

from snowflake.connector.pandas_tools import write_pandas

US_STATES = ("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
             "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR VI GU AS MP").split()
EXAMPLES = 5
TOP_VALUES = 5
ROW_NO = "_PROFILER_ROW_NO"   # extra column added on load: 1 = first data row of the file
MAX_ROWS = 1000               # failing row numbers kept per check for the drill-down


def ident(name):
    return '"' + name.replace('"', '""') + '"'


def lit(value):
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def mask_value(value):
    """Python version of masked(): 123-45-6789 -> XXX-XX-6789."""
    if value is None or len(value) <= 4:
        return value
    return "".join("X" if ch.isdigit() else ch for ch in value[:-4]) + value[-4:]


def masked(v):
    """SSN / tax id examples: keep the shape and last 4 digits, X out the rest."""
    return f"IFF(LENGTH({v}) > 4, REGEXP_REPLACE(LEFT({v}, LENGTH({v}) - 4), '[0-9]', 'X') || RIGHT({v}, 4), {v})"


def build_checks(table, rules):
    """List of checks: {column, type, check, where, example}. `where` selects the failing rows."""
    checks = []
    for r in rules:
        raw = ident(r["column"])
        v = f"NULLIF(TRIM({raw}), '')"
        digits = f"REGEXP_REPLACE({v}, '[^0-9]', '')"
        example = masked(v) if r["type"] in ("SSN", "TAX_ID") else v

        def add(name, where, ex=None):
            checks.append({"column": r["column"], "type": r["type"], "check": name, "where": where,
                           "example": ex if ex is not None else example})

        if r["required"]:
            add("Missing values", f"{v} IS NULL", "NULL")
        if r["min_len"] is not None:
            add(f"Shorter than {r['min_len']}", f"LENGTH({v}) < {int(r['min_len'])}")
        if r["max_len"] is not None:
            add(f"Longer than {r['max_len']}", f"LENGTH({v}) > {int(r['max_len'])}")
        if r["unique"]:  # every copy after the first one
            add("Duplicates", f"{v} IS NOT NULL AND {ident(ROW_NO)} NOT IN "
                              f"(SELECT MIN({ident(ROW_NO)}) FROM {table} GROUP BY {v})")

        t = r["type"]
        if t == "ID":
            add("Special characters", f"REGEXP_LIKE({v}, '.*[^A-Za-z0-9_-].*')")
        elif t == "NAME":
            add("Contains digits", f"REGEXP_LIKE({v}, '.*[0-9].*')")
            add("Special characters", f"REGEXP_LIKE({v}, '.*[^A-Za-z0-9 .''-].*')")
        elif t in ("DATE", "BIRTH_DATE"):
            fmt = r["date_format"] or "MM/DD/YYYY"
            d = f"TRY_TO_DATE({v}, {lit(fmt)})"
            add(f"Invalid date / not {fmt}", f"{v} IS NOT NULL AND {d} IS NULL")
            if t == "DATE":
                add("Future date (except 12/31/9999)", f"{d} > CURRENT_DATE() AND YEAR({d}) <> 9999")
                add("Before 1900", f"{d} < '1900-01-01'::DATE")
            else:
                add("Future date", f"{d} > CURRENT_DATE()")
                add("Older than 120 years", f"{d} < DATEADD(YEAR, -120, CURRENT_DATE())")
        elif t == "PHONE":
            add("Not 10 digits", f"LENGTH({digits}) <> 10")
            add("Special characters / formatting", f"REGEXP_LIKE({v}, '.*[^0-9].*')")
            add("All same digit (0000000000)", f"LENGTH({v}) = 10 AND REPLACE({v}, LEFT({v}, 1), '') = ''")
        elif t in ("SSN", "TAX_ID"):
            add("Not 9 digits", f"LENGTH({digits}) <> 9")
            add("Dashes or other characters", f"REGEXP_LIKE({v}, '.*[^0-9].*')")
            if t == "SSN":  # SSA rules - tax ids (EIN) may start with 9
                add("Invalid number (000, 666, 9xx…)",
                    f"LENGTH({digits}) = 9 AND (LEFT({digits}, 3) IN ('000', '666') OR LEFT({digits}, 1) = '9' "
                    f"OR SUBSTR({digits}, 4, 2) = '00' OR RIGHT({digits}, 4) = '0000')")
        elif t == "ZIP":
            add("Not 5 or 9 digits", f"NOT REGEXP_LIKE({v}, '[0-9]{{5}}(-?[0-9]{{4}})?')")
        elif t == "STATE":
            add("Not a valid US state code", f"{v} NOT IN ({', '.join(map(lit, US_STATES))})")
        elif t == "CODE" and r["allowed_values"]:
            add(f"Not in: {', '.join(r['allowed_values'])}", f"{v} NOT IN ({', '.join(map(lit, r['allowed_values']))})")
        elif t == "AMOUNT":
            add("Not a valid amount", f"{v} IS NOT NULL AND TRY_TO_DECIMAL({v}, 18, 2) IS NULL")
            add("Negative amount", f"TRY_TO_DECIMAL({v}, 18, 2) < 0")
        elif t == "NUMBER":
            add("Not a number", f"{v} IS NOT NULL AND TRY_TO_NUMBER({v}) IS NULL")
        elif t == "TEXT":
            add("Unusual special characters", f"REGEXP_LIKE({v}, '.*[^A-Za-z0-9 .,''#/&()-].*')")

        add("Leading / trailing spaces", f"{v} IS NOT NULL AND {raw} <> TRIM({raw})", f"'\"' || {raw} || '\"'")
    return checks


def checks_sql(table, checks):
    return "\nUNION ALL\n".join(
        f"SELECT {i} AS N, COUNT(*) AS FAILED, ARRAY_SLICE(ARRAY_UNIQUE_AGG(EX), 0, {EXAMPLES}) AS EXAMPLES "
        f"FROM (SELECT {c['example']} AS EX FROM {table} WHERE {c['where']})"
        for i, c in enumerate(checks))


def failures_sql(table, checks):
    row_no = ident(ROW_NO)
    return "\nUNION ALL\n".join(
        f"SELECT * FROM (SELECT {i} AS N, {row_no} AS ROW_NO FROM {table} WHERE {c['where']} "
        f"ORDER BY {row_no} LIMIT {MAX_ROWS})"
        for i, c in enumerate(checks))


def profile_sql(table, rules):
    stats, top = [], []
    for i, r in enumerate(rules):
        v = f"NULLIF(TRIM({ident(r['column'])}), '')"
        shown = masked(v) if r["type"] in ("SSN", "TAX_ID") else v
        stats.append(f"SELECT {i} AS N, COUNT(*), COUNT_IF({v} IS NULL), COUNT(DISTINCT {v}), "
                     f"MIN(LENGTH({v})), MAX(LENGTH({v})) FROM {table}")
        top.append(f"SELECT * FROM (SELECT {i} AS N, {shown} AS VAL, COUNT(*) AS CNT FROM {table} "
                   f"GROUP BY 2 QUALIFY ROW_NUMBER() OVER (ORDER BY CNT DESC, VAL) <= {TOP_VALUES})")
    return "\nUNION ALL\n".join(stats), "\nUNION ALL\n".join(top)


def run_checks(conn, df, rules, run_id, save_meta=None):
    """Load df into a temp table and run every check.
    With save_meta, the results are also stored in the permanent PROFILING tables (see store.py).
    Returns (results saved with the run, {check id: [failing row numbers]}).
    The temp table is dropped at the end (and would vanish with the connection anyway)."""
    started = time.time()
    table = ident(f"PROFILE_{run_id[:12].upper()}")
    cur = conn.cursor()
    try:
        cols = ", ".join(ident(c) + " VARCHAR" for c in df.columns)
        cur.execute(f"CREATE OR REPLACE TEMPORARY TABLE {table} ({cols}, {ident(ROW_NO)} NUMBER)")
        ok, _chunks, loaded, _ = write_pandas(conn, df.assign(**{ROW_NO: range(1, len(df) + 1)}),
                                              table.strip('"'), quote_identifiers=True)
        if not ok or loaded != len(df):
            raise RuntimeError(f"Loaded {loaded} of {len(df)} rows into Snowflake")
        load_seconds = time.time() - started

        stats_q, top_q = profile_sql(table, rules)
        cur.execute(stats_q)
        stats = {row[0]: row[1:] for row in cur.fetchall()}
        cur.execute(top_q)
        top = {}
        for n, val, cnt in sorted(cur.fetchall(), key=lambda x: (x[0], -x[2])):
            top.setdefault(n, []).append({"value": val, "count": cnt})

        checks = build_checks(table, rules)
        cur.execute(checks_sql(table, checks))
        counts = {row[0]: (row[1], json.loads(row[2]) if row[2] else []) for row in cur.fetchall()}
        failures = {}
        if any(n for n, _ in counts.values()):
            cur.execute(failures_sql(table, [c for c in checks]))
            for n, row_no in cur.fetchall():
                failures.setdefault(n, []).append(int(row_no))
        failures = {n: sorted(rows) for n, rows in failures.items()}
    finally:
        cur.close()

    total = len(df)
    profile = []
    for i, r in enumerate(rules):
        rows, nulls, distinct, min_len, max_len = stats[i]
        profile.append({"column": r["column"], "type": r["type"], "rows": rows, "nulls": nulls,
                        "null_pct": round(100 * nulls / total, 2) if total else 0, "distinct": distinct,
                        "min_len": min_len, "max_len": max_len, "top_values": top.get(i, [])})
    results = []
    for i, c in enumerate(checks):
        failed, examples = counts[i]
        results.append({"id": i, "column": c["column"], "type": c["type"], "check": c["check"], "failed": failed,
                        "pct": round(100 * failed / total, 2) if total else 0, "examples": examples})
    failing = [r for r in results if r["failed"]]
    out = {
        "rows": total,
        "checks_run": len(results),
        "checks_failed": len(failing),
        "issues": sum(r["failed"] for r in failing),
        "columns_with_issues": len({r["column"] for r in failing}),
        "load_seconds": round(load_seconds, 1),
        "seconds": round(time.time() - started, 1),
        "profile": profile,
        "checks": sorted(results, key=lambda r: -r["failed"]),
    }
    try:
        if save_meta:
            from profiling import store
            try:
                out["saved"] = store.save(conn, table, save_meta, rules, out, checks)
            except Exception as e:  # results are still shown; the page says the save failed
                out["saved"] = {"ok": False, "error": str(e).splitlines()[0][:300]}
    finally:
        conn.cursor().execute(f"DROP TABLE IF EXISTS {table}")
    return out, failures
