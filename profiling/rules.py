"""
Step 2 - propose profiling rules for each column.

The AI (local Ollama model or Snowflake Cortex) only sees column names and a few sample values and
picks a rule type + limits per column. It never counts anything: the counts
are done later by SQL on every row (step 3).

AI answers are checked by guardrails (see apply_guardrails) because small models
make predictable mistakes, e.g. copying sample values into the allowed list.
If AI is off or the call fails, built-in rules based on the column name and
sample values are used instead, and the page says so.
"""
import json
import re

import pandas as pd

RULE_TYPES = {
    "ID": "identifier or key (MEMBER_ID, CLAIM_ID, NPI, GROUP_NUMBER)",
    "NAME": "person first, last or middle name / initial",
    "DATE": "a date (service, effective, term, received date)",
    "BIRTH_DATE": "date of birth",
    "PHONE": "phone or fax number",
    "SSN": "social security number",
    "TAX_ID": "tax id / EIN",
    "ZIP": "US ZIP code",
    "STATE": "US state code",
    "CODE": "short code from a fixed list (gender, status, plan, relationship, type)",
    "AMOUNT": "money amount",
    "NUMBER": "number that is not money (units, line number, count)",
    "TEXT": "free text (address, city, organisation or group name)",
}
SAMPLE_SIZE = 20

PROMPT = """You are a data quality analyst on a healthcare payer data migration.
For each source column below, choose the column type and the profiling rules that apply.

Column types (use exactly one):
{types}

Guidelines:
- FIRST_NAME max_len 15, LAST_NAME max_len 35, middle name max_len 25, middle initial max_len 1 and not required.
- PHONE is 10 digits, SSN and TAX_ID 9 digits, ZIP 5 or 9 digits, NPI is an ID with min_len 10 and max_len 10.
- date_format is the Snowflake format the samples use, e.g. MM/DD/YYYY, YYYY-MM-DD or YYYYMMDD. null for non-dates.
- allowed_values: [] for almost every column. Only fill it for gender (M, F, U).
  The samples may contain BAD values, so never copy sample values into allowed_values.
- required: true when the target system needs a value. unique: true only for the primary key of the file.
- min_len / max_len: what the target system should accept, not what the samples happen to be. null if no limit.

Return ONLY a JSON array, one object per column, with exactly these keys:
column, type, required, min_len, max_len, date_format, allowed_values, unique, reason
reason is one short sentence explaining the choice.

Columns and sample values:
{columns}
"""

# (column-name pattern, type, extra settings) - first match wins
NAME_RULES = [
    (r"SSN", "SSN", {}),
    (r"TAX_?ID|(^|_)(TIN|EIN)($|_)", "TAX_ID", {}),
    (r"PHONE|FAX|(^|_)TEL", "PHONE", {}),
    (r"ZIP|POSTAL", "ZIP", {}),
    (r"(^|_)(STATE|ST)(_CD|_CODE)?$", "STATE", {}),
    (r"DOB|BIRTH", "BIRTH_DATE", {"required": True}),
    (r"DATE|(_|^)DTE?$", "DATE", {}),
    (r"MID(DLE)?_?(INIT|I$)|(^|_)MI$", "NAME", {"max_len": 1}),
    (r"FIRST_?N|FST_?N|FNAME", "NAME", {"max_len": 15, "required": True}),
    (r"LAST_?N|LST_?N|LNAME", "NAME", {"max_len": 35, "required": True}),
    (r"MID(DLE)?_?N", "NAME", {"max_len": 25}),
    (r"AMT|AMOUNT|CHARGE|PAID|COST|PRICE|BALANCE", "AMOUNT", {}),
    (r"GENDER|(^|_)SEX", "CODE", {"allowed_values": ["M", "F", "U"]}),
    (r"(^|_)NPI$", "ID", {"min_len": 10, "max_len": 10}),
    (r"(_|^)(ID|NUM|NUMBER|NO|NBR|KEY)$", "ID", {"required": True}),
    (r"(_|^)(CD|CODE|IND|FLAG|FLG|TYPE|STATUS)$", "CODE", {}),
    (r"UNITS?$|COUNT|QTY", "NUMBER", {}),
]

DATE_FORMATS = [
    (r"\d{1,2}/\d{1,2}/\d{4}", "MM/DD/YYYY"),
    (r"\d{4}-\d{2}-\d{2}", "YYYY-MM-DD"),
    (r"\d{8}", "YYYYMMDD"),
    (r"\d{1,2}-\d{1,2}-\d{4}", "MM-DD-YYYY"),
]


# ---------------------------------------------------------------- file + samples

def read_source(path):
    """Read a delimited file as text columns. Empty cells stay as ''."""
    with open(path, "rb") as f:
        raw = f.read(65536)
    encoding = "utf-8"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        encoding = "latin-1"
    header = raw.decode(encoding, "replace").splitlines()[0] if raw else ""
    sep = max("|,\t;", key=header.count)
    df = pd.read_csv(path, sep=sep, dtype=str, keep_default_na=False, encoding=encoding)
    df.columns = [str(c).strip() for c in df.columns]
    return df, sep


def samples(series, n=SAMPLE_SIZE):
    """Up to n distinct non-empty values, most common first."""
    s = series[series.str.strip() != ""]
    return [v[:60] for v in s.value_counts().index[:n]]


def _masked(column, values):
    """Never send real SSN / tax id digits to the AI - the shape is enough."""
    if re.search(r"SSN|TAX_?ID|(^|_)(TIN|EIN)($|_)", column.upper()):
        return list(dict.fromkeys(re.sub(r"\d", "9", v) for v in values))  # distinct shapes only
    return values


# ---------------------------------------------------------------- rules

def clean_rule(column, r, source):
    """Coerce one rule (from AI or built-in) to a safe, complete shape."""
    def as_int(v):
        try:
            return int(v) if v not in (None, "") and int(v) >= 0 else None
        except (TypeError, ValueError):
            return None

    rtype = str(r.get("type", "TEXT")).upper().strip()
    allowed = r.get("allowed_values") or []
    if isinstance(allowed, str):
        allowed = allowed.split(",")
    return {
        "column": column,
        "type": rtype if rtype in RULE_TYPES else "TEXT",
        "required": bool(r.get("required", False)),
        "min_len": as_int(r.get("min_len")),
        "max_len": as_int(r.get("max_len")),
        "date_format": (str(r.get("date_format") or "").strip() or None) if rtype in ("DATE", "BIRTH_DATE") else None,
        "allowed_values": [str(a).strip() for a in allowed if str(a).strip()],
        "unique": bool(r.get("unique", False)),
        "reason": str(r.get("reason") or "").strip()[:200],
        "source": source,
    }


def _date_format(values):
    """The format most sample values use, or None if they don't look like dates."""
    for pattern, fmt in DATE_FORMATS:
        if values and sum(bool(re.fullmatch(pattern, v)) for v in values) >= len(values) * 0.8:
            return fmt
    return None


def apply_guardrails(rule, values):
    """Fix the mistakes small AI models make. Returns a list of notes for the page."""
    notes = []
    name = rule["column"].upper()
    if re.search(r"DOB|BIRTH", name) and rule["type"] != "BIRTH_DATE":
        rule["type"] = "BIRTH_DATE"
        notes.append("type set to date of birth")
    if rule["type"] in ("DATE", "BIRTH_DATE"):
        rule["date_format"] = rule["date_format"] or _date_format(values) or "MM/DD/YYYY"
        rule["min_len"] = rule["max_len"] = None          # the date-format check covers length
    else:
        rule["date_format"] = None

    allowed = rule["allowed_values"]
    if re.search(r"GENDER|(^|_)SEX", name):
        if allowed != ["M", "F", "U"]:
            notes.append(f"allowed values set to M, F, U (AI said {', '.join(allowed) or 'none'})")
        rule["allowed_values"] = ["M", "F", "U"]
    elif rule["type"] != "CODE":
        rule["allowed_values"] = []                       # STATE uses the built-in US list
    elif allowed and set(allowed) <= set(values):
        rule["allowed_values"] = []
        notes.append("AI copied the sample values into the allowed list, so it was removed - add the real list if you have one")

    if rule["min_len"] is not None and rule["min_len"] <= 1:
        rule["min_len"] = None                            # same as the "missing values" check
    for key, broken in (("min_len", lambda v, n: len(v) < n), ("max_len", lambda v, n: len(v) > n)):
        n = rule[key]
        if n is not None and values and sum(broken(v, n) for v in values) > len(values) / 2:
            rule[key] = None
            notes.append(f"{key.replace('_len', '')} length {n} removed - most sample values break it")
    return notes


def guess_rule(column, values, position=None):
    """Built-in rule from the column name, falling back to the sample values."""
    name = column.upper()
    for pattern, rtype, extra in NAME_RULES:
        if re.search(pattern, name):
            rule = {"type": rtype, **extra, "reason": f"Column name looks like {RULE_TYPES[rtype].split(' (')[0]}."}
            break
    else:
        if values and all(re.fullmatch(r"-?\d+(\.\d+)?", v) for v in values):
            rule = {"type": "NUMBER", "reason": "All sample values are numbers."}
        elif _date_format(values):
            rule = {"type": "DATE", "reason": "Sample values look like dates."}
        else:
            rule = {"type": "TEXT", "reason": "Free text column."}
    if rule["type"] in ("DATE", "BIRTH_DATE"):
        rule["date_format"] = _date_format(values) or "MM/DD/YYYY"
    if rule["type"] == "ID" and position == 0:
        rule["unique"] = True
        rule["reason"] = "First ID column - probably the file's primary key (untick unique if not)."
    return clean_rule(column, rule, "rules")


def guess_rules(df):
    return [guess_rule(c, samples(df[c]), i) for i, c in enumerate(df.columns)]


def parse_json_array(text):
    """Pull the JSON array out of an AI reply (it may add ```json fences or chatter)."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        raise ValueError("AI reply did not contain a JSON array")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("AI reply was not a list")
    return [d for d in data if isinstance(d, dict)]


def build_prompt(df):
    types = "\n".join(f"- {k}: {v}" for k, v in RULE_TYPES.items())
    columns = "\n".join(f"{c}: {json.dumps(_masked(c, samples(df[c])))}" for c in df.columns)
    return PROMPT.format(types=types, columns=columns)


def propose_rules(df, ask_ai=None):
    """
    Returns (rules, info). ask_ai(prompt) -> reply text; None means Snowflake isn't set up.
    Columns the AI skipped fall back to the built-in rule, so every column always has one.
    """
    if ask_ai is None:
        return guess_rules(df), {"source": "rules", "message": "AI is switched off, so built-in rules were used."}
    try:
        by_column = {str(r.get("column", "")).strip().upper(): r for r in parse_json_array(ask_ai(build_prompt(df)))}
    except Exception as e:  # connection, Cortex or JSON problem - still give the user something to work with
        return guess_rules(df), {"source": "rules", "message": f"AI call failed, built-in rules were used instead ({str(e).splitlines()[0][:160]})."}

    rules, missed, fixed, key_column = [], [], 0, None
    for i, c in enumerate(df.columns):
        values = samples(df[c])
        if c.upper() not in by_column:
            rules.append(guess_rule(c, values, i))
            missed.append(c)
            continue
        rule = clean_rule(c, by_column[c.upper()], "ai")
        notes = apply_guardrails(rule, values)
        if rule["unique"]:
            if key_column:
                rule["unique"] = False
                notes.append(f"unique removed - {key_column} is already the key")
            else:
                key_column = c
        if notes:
            fixed += 1
            rule["reason"] = (rule["reason"] + " Adjusted: " + "; ".join(notes) + ".").strip()
        rules.append(rule)

    message = f"AI proposed the checks for {len(rules) - len(missed)} of {len(rules)} columns."
    if fixed:
        message += f" Guardrails corrected {fixed} of them (see 'Adjusted' notes)."
    if missed:
        message += f" Built-in rules used for: {', '.join(missed)}."
    return rules, {"source": "ai", "message": message}


def validate_rules(rules, columns):
    """Check rules sent back from the page before saving them. Returns (clean rules, errors)."""
    errors = []
    by_column = {r.get("column"): r for r in rules if isinstance(r, dict)}
    missing = [c for c in columns if c not in by_column]
    if missing:
        errors.append(f"No rule for: {', '.join(missing)}")
    clean = [clean_rule(c, by_column[c], by_column[c].get("source", "user")) for c in columns if c in by_column]
    for r in clean:
        if r["min_len"] is not None and r["max_len"] is not None and r["min_len"] > r["max_len"]:
            errors.append(f"{r['column']}: min length is bigger than max length")
    return clean, errors
