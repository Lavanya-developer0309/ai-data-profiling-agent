"""
Step 4 - client report: plain-English summary + Excel workbook.

The AI only writes words. Severity and every number come from the Snowflake
results, and verify_numbers() rejects any AI text that contains a number that
isn't in those results (a made-up or miscalculated count). In that case the
standard written summary below is used instead and the page says so.
"""
import io
import json
import re
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

MAX_FINDINGS = 15
SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}

PROMPT = """You are a data migration consultant writing for a healthcare payer client.
Below are the EXACT data profiling results computed by Snowflake for one source file.
Write a short, plain-English summary the client can act on.

Strict rules:
- Use ONLY numbers that appear in the results below, written exactly as they appear there.
- Never calculate new numbers (no totals, differences or percentages of your own).
- Do not repeat the example values for columns that contain SSN or tax id.
- No markdown, no bullet characters.

Return JSON with:
- headline: one sentence, at most 15 words
- overview: 2-3 sentences on the overall data quality and what matters most for the migration
- findings: one object per finding id: {{"id": <id>, "explanation": "1-2 sentences: what is wrong and why it matters",
  "recommendation": "1 sentence: what the client should do"}}
- next_steps: 3-5 short actions

Results:
{facts}
"""

RECOMMENDATIONS = [  # (check name starts with, recommendation) - used when the AI is off or rejected
    ("Missing values", "Confirm whether these records can load without a value or must be corrected at source."),
    ("Duplicates", "Agree which copy of each duplicated record to keep before the load."),
    ("Not in:", "Provide a crosswalk for these codes or confirm the records should be rejected."),
    ("Invalid date", "Correct these dates at source or confirm they should be rejected."),
    ("Future date", "Check these dates at source - they are later than today."),
    ("Older than", "Check these birth dates at source - the age is not realistic."),
    ("Before 1900", "Check these dates at source - they are earlier than 1900."),
    ("Leading / trailing spaces", "These will be trimmed during the load; no client action unless spaces are meaningful."),
    ("Special characters", "Confirm whether the target accepts these characters or agree a cleansing rule."),
    ("Unusual special characters", "Confirm whether the target accepts these characters or agree a cleansing rule."),
    ("Contains digits", "Correct these names at source."),
    ("Longer than", "Agree whether to truncate these values or correct them at source."),
    ("Shorter than", "Correct these values at source - they are shorter than the target allows."),
    ("Dashes or other characters", "These will be cleansed to digits only; confirm values like 'UNKNOWN' should be blank."),
]


def severity(check):
    if check == "Leading / trailing spaces":
        return "Low"
    if check.startswith(("Missing values", "Duplicates", "Not in:", "Invalid", "Not a valid", "Not 9", "Not 10",
                         "Not 5", "Not a number", "Future date", "Older than")):
        return "High"
    return "Medium"


def build_facts(run):
    """The only information the AI gets - all taken from the Snowflake results."""
    res = run["results"]
    failing = [c for c in res["checks"] if c["failed"]]
    failing.sort(key=lambda c: (SEVERITY_ORDER[severity(c["check"])], -c["failed"]))
    sensitive = {r["column"] for r in run["rules"] if r["type"] in ("SSN", "TAX_ID")}
    findings = [{
        "id": i + 1, "column": c["column"], "check": c["check"], "severity": severity(c["check"]),
        "failed_rows": c["failed"], "percent_of_rows": c["pct"],
        "examples": [] if c["column"] in sensitive else c["examples"][:3],
    } for i, c in enumerate(failing[:MAX_FINDINGS])]
    return {
        "file": run["filename"], "rows_checked": res["rows"], "checks_run": res["checks_run"],
        "checks_with_issues": res["checks_failed"], "issues_found": res["issues"],
        "columns_with_issues": res["columns_with_issues"], "columns_total": len(res["profile"]),
        "findings_not_listed": max(0, len(failing) - MAX_FINDINGS), "findings": findings,
    }


def _numbers(text):
    return {n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def verify_numbers(summary, facts):
    """Numbers in the AI's text that do not appear in the facts (should be empty)."""
    allowed = _numbers(json.dumps(facts, ensure_ascii=False))
    allowed |= {str(int(float(n))) for n in allowed if re.fullmatch(r"\d+\.0+", n)}
    return sorted(_numbers(json.dumps(summary, ensure_ascii=False)) - allowed, key=lambda n: float(n))


def fallback_summary(facts):
    """Standard written summary - no AI."""
    high = [f for f in facts["findings"] if f["severity"] == "High"]
    findings = []
    for f in facts["findings"]:
        examples = f" Examples: {', '.join(map(str, f['examples']))}." if f["examples"] else ""
        rec = next((r for prefix, r in RECOMMENDATIONS if f["check"].startswith(prefix)),
                   "Review the examples with the client and agree a cleansing rule.")
        findings.append({"id": f["id"],
                         "explanation": f"{f['failed_rows']:,} rows in {f['column']} failed the check '{f['check']}'.{examples}",
                         "recommendation": rec})
    if not facts["findings"]:
        headline = f"No data issues found in {facts['file']}."
        overview = f"All {facts['checks_run']} checks passed on {facts['rows_checked']:,} rows."
    else:
        headline = f"{facts['issues_found']:,} data issues found in {facts['columns_with_issues']} of {facts['columns_total']} columns."
        overview = (f"{facts['checks_run']} checks were run on all {facts['rows_checked']:,} rows of {facts['file']}; "
                    f"{facts['checks_with_issues']} found issues. "
                    + (f"The high-severity findings must be resolved or accepted before the migration load." if high else
                       "None of the findings block the load, but they should be reviewed."))
    steps = ["Review the high-severity findings with the client data owner." if high else "Review the findings with the client.",
             "Agree a cleansing or rejection rule for each finding.",
             "Send a corrected file, then re-run this profile to confirm."]
    return {"headline": headline, "overview": overview, "findings": findings, "next_steps": steps}


def _clean(summary, facts):
    """Keep only the expected fields; fill any finding the AI skipped from the standard text."""
    base = fallback_summary(facts)
    by_id = {}
    for f in summary.get("findings") or []:
        try:
            by_id[int(f.get("id"))] = f
        except (TypeError, ValueError, AttributeError):
            pass
    findings = []
    for f in base["findings"]:
        ai = by_id.get(f["id"], {})
        findings.append({"id": f["id"],
                         "explanation": str(ai.get("explanation") or f["explanation"]).strip(),
                         "recommendation": str(ai.get("recommendation") or f["recommendation"]).strip()})
    steps = [str(s).strip() for s in (summary.get("next_steps") or []) if str(s).strip()][:5]
    return {"headline": str(summary.get("headline") or base["headline"]).strip(),
            "overview": str(summary.get("overview") or base["overview"]).strip(),
            "findings": findings, "next_steps": steps or base["next_steps"]}


def write_summary(run, ask_json=None):
    """Returns (summary, info). ask_json(prompt) -> dict; None means AI is off."""
    facts = build_facts(run)
    if ask_json is None:
        return fallback_summary(facts), {"source": "standard", "message": "AI is switched off - standard summary used."}
    if not facts["findings"]:
        return fallback_summary(facts), {"source": "standard", "message": "No issues found - standard summary used."}
    prompt = PROMPT.format(facts=json.dumps(facts, indent=1, ensure_ascii=False))
    problem = None
    for attempt in range(2):  # one retry, telling the AI what it got wrong
        try:
            summary = _clean(ask_json(prompt), facts)
        except Exception as e:
            return fallback_summary(facts), {"source": "standard", "message": f"AI call failed - standard summary used ({str(e).splitlines()[0][:150]})."}
        bad = verify_numbers(summary, facts)
        if not bad:
            note = " (second attempt)" if attempt else ""
            return summary, {"source": "ai", "message": f"Summary written by AI{note}. Every number in it was checked against the Snowflake results ✓"}
        problem = ", ".join(bad[:8])
        prompt += f"\nYour previous answer used numbers that are not in the results: {problem}. Use only numbers from the results."
    return fallback_summary(facts), {"source": "standard",
                                     "message": f"AI summary rejected - it used numbers not in the results ({problem}). Standard summary used instead."}


# ---------------------------------------------------------------- Excel

INK = "1F2937"
FILLS = {"High": "FDE2E4", "Medium": "FEF3C7", "Low": "E5E7EB", "head": "312E81", "band": "F3F4F6"}
THIN = Side(style="thin", color="E5E7EB")


def _header(ws, row, titles):
    for col, title in enumerate(titles, 1):
        cell = ws.cell(row=row, column=col, value=title)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=FILLS["head"])
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 22


def _table(ws, start_row, titles, rows, widths, severity_col=None, number_cols=(), pct_cols=()):
    _header(ws, start_row, titles)
    for r, values in enumerate(rows, start_row + 1):
        for c, value in enumerate(values, 1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = Border(bottom=THIN)
            if c in number_cols:
                cell.number_format = "#,##0"
            if c in pct_cols:
                cell.number_format = "0.00%"
        if severity_col:
            sev = values[severity_col - 1]
            if sev in FILLS:
                ws.cell(row=r, column=severity_col).fill = PatternFill("solid", fgColor=FILLS[sev])
                ws.cell(row=r, column=severity_col).font = Font(bold=True)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = ws.cell(row=start_row + 1, column=1)
    if rows:
        ws.auto_filter.ref = f"A{start_row}:{get_column_letter(len(titles))}{start_row + len(rows)}"


def build_excel(run, summary, user_name):
    res, facts = run["results"], build_facts(run)
    wb = Workbook()

    ws = wb.active
    ws.title = "Summary"
    ws.sheet_view.showGridLines = False
    ws["A1"] = "Data Profiling Report"
    ws["A1"].font = Font(size=18, bold=True, color=INK)
    ws["A2"] = f"{run['filename']}  ·  profiled {run.get('checked_at', '')[:16].replace('T', ' ')}  ·  prepared by {user_name}"
    ws["A2"].font = Font(color="6B7280")
    tiles = [("Rows checked", res["rows"]), ("Checks run", res["checks_run"]), ("Checks with issues", res["checks_failed"]),
             ("Issues found", res["issues"]), ("Columns with issues", f"{res['columns_with_issues']} of {len(res['profile'])}")]
    spans = [(1, 2), (3, 3), (4, 4), (5, 5), (6, 6)]   # columns A+B are narrow, so the first tile uses both
    for i, ((label, value), (first, last)) in enumerate(zip(tiles, spans)):
        for r in (4, 5):
            for col in range(first, last + 1):
                ws.cell(row=r, column=col).fill = PatternFill("solid", fgColor=FILLS["band"])
            if last > first:
                ws.merge_cells(start_row=r, start_column=first, end_row=r, end_column=last)
        ws.cell(row=4, column=first, value=label).font = Font(size=9, color="6B7280")
        cell = ws.cell(row=5, column=first, value=value)
        cell.font = Font(size=16, bold=True, color="B91C1C" if i >= 2 and res["issues"] else INK)
        cell.number_format = "#,##0"
        cell.alignment = Alignment(horizontal="left")

    ws["A7"] = summary["headline"]
    ws["A7"].font = Font(size=13, bold=True, color=INK)
    ws.merge_cells("A8:G8")
    ws["A8"] = summary["overview"]
    ws["A8"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[8].height = 64

    by_id = {f["id"]: f for f in facts["findings"]}
    rows = [[f["id"], by_id[f["id"]]["severity"], by_id[f["id"]]["column"], by_id[f["id"]]["check"],
             by_id[f["id"]]["failed_rows"], f["explanation"], f["recommendation"]] for f in summary["findings"]]
    ws["A10"] = "Findings"
    ws["A10"].font = Font(size=13, bold=True, color=INK)
    _table(ws, 11, ["#", "Severity", "Column", "Check", "Failed rows", "What is wrong", "Recommendation"], rows,
           [6, 11, 20, 28, 12, 55, 50], severity_col=2, number_cols=(5,))
    ws.freeze_panes = None
    ws.auto_filter.ref = None
    r = 12 + len(rows) + 1
    ws.cell(row=r, column=1, value="Next steps").font = Font(size=13, bold=True, color=INK)
    for i, step in enumerate(summary["next_steps"], 1):
        ws.cell(row=r + i, column=1, value=i)
        ws.cell(row=r + i, column=2, value=step)
    note = r + len(summary["next_steps"]) + 2
    ws.cell(row=note, column=1, value="All counts were computed by Snowflake SQL on every row. "
                                      "AI chose the checks and wrote the text only. SSN and tax id values are masked.").font = Font(italic=True, color="6B7280", size=9)

    ws = wb.create_sheet("All checks")
    rows = [[severity(c["check"]) if c["failed"] else "Passed", c["column"], c["check"], c["failed"], c["pct"] / 100,
             ", ".join(map(str, c["examples"]))] for c in res["checks"]]
    _table(ws, 1, ["Severity", "Column", "Check", "Failed rows", "% of rows", "Example values"], rows,
           [11, 20, 34, 12, 11, 60], severity_col=1, number_cols=(4,), pct_cols=(5,))

    ws = wb.create_sheet("Column profile")
    rows = [[p["column"], p["type"], p["nulls"], p["null_pct"] / 100, p["distinct"], p["min_len"], p["max_len"],
             ", ".join(f"{t['value'] if t['value'] is not None else '(empty)'} ({t['count']:,})" for t in p["top_values"])]
            for p in res["profile"]]
    _table(ws, 1, ["Column", "Type", "Nulls", "Null %", "Distinct", "Min length", "Max length", "Most common values"], rows,
           [22, 12, 10, 9, 10, 11, 11, 70], number_cols=(3, 5), pct_cols=(4,))

    ws = wb.create_sheet("Rules used")
    rows = [[r["column"], r["type"], "Yes" if r["required"] else "", "Yes" if r["unique"] else "", r["min_len"], r["max_len"],
             r["date_format"] or "", ", ".join(r["allowed_values"]), {"ai": "AI", "rules": "Built-in", "user": "Edited"}.get(r["source"], r["source"]),
             r["reason"]] for r in run["rules"]]
    _table(ws, 1, ["Column", "Type", "Required", "Unique", "Min len", "Max len", "Date format", "Allowed values", "Set by", "Why"], rows,
           [22, 12, 9, 8, 8, 8, 13, 20, 10, 60])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def report_filename(run):
    return re.sub(r"\.[^.]+$", "", run["filename"]) + f"_profiling_report_{datetime.now():%Y%m%d}.xlsx"
