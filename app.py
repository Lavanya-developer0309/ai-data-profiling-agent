"""
AI Data Profiling Agent - web app
==================================
    .venv/bin/python app.py      ->  http://127.0.0.1:5050

Step 1: login page + main page (upload and preview a source file).
Step 2: upload -> AI (local Ollama / Snowflake Cortex, or built-in rules) proposes checks per column -> user edits and approves.
Step 3: Snowflake runs the approved checks on every row -> exact counts + example bad values.
Step 4: AI writes the client summary from those numbers (numbers verified) -> Excel report.
Pipeline page: the hc-pipeline agent's runs and decisions (Snowflake) + human approvals (hc-pipeline/state/approvals.db).
"""
import json
import os
import secrets
import sqlite3
import uuid
from datetime import datetime
from datetime import timedelta
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
import io

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.utils import secure_filename

import ai
import auth
from profiling import checks as profiling_checks
from profiling import report as profiling_report
from profiling import rules as profiling_rules

load_dotenv()
BASE = Path(__file__).parent


def _secret_key(instance_path):
    """Keep one random key in instance/ so logins survive a restart."""
    path = Path(instance_path) / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip()


def create_app(test_config=None):
    app = Flask(__name__, instance_path=str(BASE / "instance"))
    os.makedirs(app.instance_path, exist_ok=True)
    app.config.update(
        DATABASE=os.path.join(app.instance_path, "app.db"),
        SECRET_KEY=os.getenv("APP_SECRET_KEY") or _secret_key(app.instance_path),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        MAX_CONTENT_LENGTH=200 * 1024 * 1024,
    )
    app.config["UPLOADS"] = os.path.join(app.instance_path, "uploads")
    app.config["ASK_AI"] = None          # tests plug in a fake AI function here, or "off"
    app.config["ASK_SUMMARY_AI"] = None  # same, for the step 4 summary
    if test_config:
        app.config.update(test_config)
    auth.init_db(app.config["DATABASE"])
    app.teardown_appcontext(auth.close_db)

    @app.before_request
    def load_user_and_check_csrf():
        g.user = auth.get_user(session["user_id"]) if "user_id" in session else None
        if "user_id" in session and g.user is None:   # user was deleted
            session.clear()
        session.setdefault("csrf", secrets.token_hex(16))
        if request.method == "POST" and request.form.get("csrf") != session["csrf"] \
                and request.headers.get("X-CSRF-Token") != session["csrf"]:
            abort(400, "Form expired - please reload the page and try again.")

    @app.context_processor
    def template_globals():
        return {"csrf": session.get("csrf"), "user": g.get("user")}

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    def start_session(user, remember):
        next_url = request.args.get("next", "")
        session.clear()
        session["user_id"] = user["id"]
        session["csrf"] = secrets.token_hex(16)
        session.permanent = remember
        return redirect(next_url if next_url.startswith("/") and not next_url.startswith("//") else url_for("main"))

    @app.route("/")
    def index():
        return redirect(url_for("main" if g.user else "login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if g.user:
            return redirect(url_for("main"))
        mode, errors, form = "signin", {}, {}
        if request.method == "POST":
            form = request.form
            mode = form.get("mode", "signin")
            username = form.get("username", "").strip()
            password = form.get("password", "")
            remember = form.get("remember") == "on"
            if mode == "register":
                errors = auth.validate_new_user(username, form.get("full_name", ""), password, form.get("confirm", ""))
                if not errors:
                    user = auth.create_user(username, form["full_name"], password)
                    flash(f"Welcome, {user['full_name']}! Your account is ready.")
                    return start_session(user, remember)
            else:
                user, error = auth.check_login(username, password)
                if user:
                    return start_session(user, remember)
                errors["signin"] = error
        return render_template("login.html", mode=mode, errors=errors, form=form), 400 if errors else 200

    @app.post("/logout")
    def logout():
        session.clear()
        flash("You have been signed out.")
        return redirect(url_for("login"))

    @app.route("/app")
    @login_required
    def main():
        return render_template("main.html")

    # ---------- pipeline page: agent runs + approvals ----------
    pipeline_dir = Path(os.path.expanduser(os.getenv("PIPELINE_DIR", "~/Desktop/Lavanya AI/hc-pipeline")))
    app.config.setdefault("APPROVALS_DB", str(pipeline_dir / "state" / "approvals.db"))

    def approvals_db():
        path = Path(app.config["APPROVALS_DB"])
        if not path.exists():
            return None
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    def snowflake_rows(sql, binds=None):
        import sf
        conn = sf.connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, binds)
            cols = [d[0].lower() for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()

    @app.route("/pipeline")
    @login_required
    def pipeline():
        return render_template("pipeline.html")

    @app.get("/api/pipeline/approvals")
    @login_required
    def pipeline_approvals():
        conn = approvals_db()
        if conn is None:
            return jsonify(pending=[], recent=[])
        with conn:
            pending = [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE status = 'PENDING' ORDER BY id")]
            recent = [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE status != 'PENDING' ORDER BY id DESC LIMIT 10")]
        return jsonify(pending=pending, recent=recent)

    @app.post("/api/pipeline/approvals/<int:approval_id>")
    @login_required
    def answer_approval(approval_id):
        body = request.get_json(silent=True) or {}
        if "approve" not in body:
            return jsonify(error="Say approve: true or false."), 400
        conn = approvals_db()
        if conn is None:
            abort(404)
        note = (body.get("note") or "").strip()[:500]
        note = f"{note} (by {g.user['username']})".strip() if note else f"by {g.user['username']}"
        with conn:
            changed = conn.execute("""UPDATE approvals SET status = ?, note = ?, answered_at = ?
                                      WHERE id = ? AND status = 'PENDING'""",
                                   ("APPROVED" if body["approve"] else "REJECTED", note,
                                    datetime.now().isoformat(timespec="seconds"), approval_id)).rowcount
        if not changed:
            return jsonify(error="This request was already answered or timed out."), 409
        return jsonify(ok=True)

    @app.get("/api/pipeline/runs")
    @login_required
    def pipeline_runs():
        try:
            runs = snowflake_rows("""SELECT RUN_ID, BATCH_ID, APPROVAL_MODE, MODEL, STARTED_AT, SECONDS, FINAL_STATUS, STEPS, SUMMARY, PATH
                                     FROM HC_MIGRATION_DB.AUDIT.V_AGENT_RUNS ORDER BY STARTED_AT DESC LIMIT 25""")
        except Exception as e:
            return jsonify(error=f"Snowflake: {str(e).splitlines()[0][:200]}"), 502
        return jsonify(runs=runs)

    @app.get("/api/pipeline/runs/<run_id>")
    @login_required
    def pipeline_run(run_id):
        try:
            steps = snowflake_rows("""SELECT STEP_NO, TOOL, REASON, VERDICT, APPROVAL, RESULT_OK, RESULT, LOGGED_AT
                                      FROM HC_MIGRATION_DB.AUDIT.AGENT_DECISION_LOG WHERE RUN_ID = %s ORDER BY STEP_NO""", (run_id,))
        except Exception as e:
            return jsonify(error=f"Snowflake: {str(e).splitlines()[0][:200]}"), 502
        for st in steps:
            st["result"] = json.loads(st["result"]) if isinstance(st["result"], str) else st["result"]
        return jsonify(steps=steps)

    def snowflake_configured():
        return bool(os.getenv("SNOWFLAKE_ACCOUNT") and os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH"))

    def ask_ai():
        """Function that sends a prompt to the AI, or None when AI is off."""
        if app.config["ASK_AI"] == "off":
            return None
        return app.config["ASK_AI"] or ai.get_ask_ai()

    @app.get("/api/status")
    @login_required
    def status():
        return jsonify(snowflake_configured=snowflake_configured(), ai=ai.status())

    # ---------- profiling runs: each upload is a run stored in instance/uploads/<run_id>/ ----------
    def run_dir(run_id):
        if not run_id or not all(ch in "0123456789abcdef" for ch in run_id):
            abort(404)
        return Path(app.config["UPLOADS"]) / run_id

    def load_run(run_id):
        path = run_dir(run_id) / "run.json"
        if not path.exists():
            abort(404)
        run = json.loads(path.read_text())
        if run["user_id"] != g.user["id"]:
            abort(404)
        return run

    def save_run(run):
        (run_dir(run["run_id"]) / "run.json").write_text(json.dumps(run, indent=2))

    @app.post("/api/runs")
    @login_required
    def create_run():
        """Upload a file and get proposed rules for every column."""
        file = request.files.get("file")
        if not file or not file.filename:
            return jsonify(error="Please choose a file."), 400
        name = secure_filename(file.filename) or "source.txt"
        if not name.lower().endswith((".csv", ".txt", ".tsv", ".psv")):
            return jsonify(error="Please upload a CSV, TXT or TSV file."), 400

        run_id = uuid.uuid4().hex
        folder = run_dir(run_id)
        folder.mkdir(parents=True)
        file.save(folder / name)
        try:
            df, sep = profiling_rules.read_source(folder / name)
        except Exception as e:
            return jsonify(error=f"Could not read the file: {str(e).splitlines()[0][:200]}"), 400
        if df.empty or len(df.columns) < 1:
            return jsonify(error="The file needs a header row and at least one data row."), 400

        rules, info = profiling_rules.propose_rules(df, ask_ai())
        sample_values = {c: profiling_rules.samples(df[c], 5) for c in df.columns}
        run = dict(run_id=run_id, user_id=g.user["id"], filename=name, sep=sep, rows=len(df),
                   columns=list(df.columns), rules=rules, rules_info=info, status="rules_proposed",
                   created_at=datetime.now().isoformat(timespec="seconds"))
        save_run(run)
        return jsonify(run_id=run_id, filename=name, rows=len(df), columns=run["columns"],
                       rules=rules, info=info, samples=sample_values, rule_types=profiling_rules.RULE_TYPES)

    @app.put("/api/runs/<run_id>/rules")
    @login_required
    def approve_rules(run_id):
        """Save the rules after the user reviewed / edited them."""
        run = load_run(run_id)
        rules, errors = profiling_rules.validate_rules((request.get_json(silent=True) or {}).get("rules") or [], run["columns"])
        if errors:
            return jsonify(error="; ".join(errors)), 400
        run.update(rules=rules, status="rules_approved", approved_at=datetime.now().isoformat(timespec="seconds"))
        run.pop("results", None)  # rules changed - old results no longer match
        save_run(run)
        return jsonify(ok=True, rules=rules)

    @app.post("/api/runs/<run_id>/execute")
    @login_required
    def execute_run(run_id):
        """Load the file into Snowflake and run every approved check on all rows."""
        run = load_run(run_id)
        if run["status"] not in ("rules_approved", "checked"):
            return jsonify(error="Approve the checks first."), 400
        if not snowflake_configured():
            return jsonify(error="Snowflake is not set up - add SNOWFLAKE_ACCOUNT and SNOWFLAKE_PRIVATE_KEY_PATH to .env."), 400
        import sf
        df, _ = profiling_rules.read_source(run_dir(run_id) / run["filename"])
        try:
            conn = sf.connect()
        except Exception as e:
            return jsonify(error=f"Could not connect to Snowflake: {str(e).splitlines()[0][:200]}"), 502
        try:
            save_meta = {"run_id": run_id, "source_file": run["filename"], "run_by": g.user["full_name"],
                         "rules_by": ai.model() if run["rules_info"]["source"] == "ai" else "built-in rules",
                         "run_at": datetime.now().isoformat(sep=" ", timespec="seconds")}
            results, failures = profiling_checks.run_checks(conn, df, run["rules"], run_id, save_meta)
        except Exception as e:
            return jsonify(error=f"Snowflake error: {str(e).splitlines()[0][:300]}"), 502
        finally:
            conn.close()
        (run_dir(run_id) / "failures.json").write_text(json.dumps(failures))
        run.update(results=results, status="checked", checked_at=datetime.now().isoformat(timespec="seconds"))
        run.pop("summary", None)
        save_run(run)
        return jsonify(results)

    def checked_run(run_id):
        run = load_run(run_id)
        if "results" not in run:
            abort(400, "Run the checks in Snowflake first.")
        return run

    @app.get("/api/runs/<run_id>/checks/<int:check_id>/rows")
    @login_required
    def check_rows(run_id, check_id):
        """The exact rows behind one count: read from the uploaded file by row number."""
        run = checked_run(run_id)
        check = next((c for c in run["results"]["checks"] if c.get("id") == check_id), None)
        failures_path = run_dir(run_id) / "failures.json"
        if check is None:
            abort(404)
        if not failures_path.exists():
            return jsonify(error="Run the checks in Snowflake again to see the rows."), 409
        row_nos = json.loads(failures_path.read_text()).get(str(check_id), [])

        df, _ = profiling_rules.read_source(run_dir(run_id) / run["filename"])
        rule = next(r for r in run["rules"] if r["column"] == check["column"])
        profile = next(p for p in run["results"]["profile"] if p["column"] == check["column"])
        key = next((r["column"] for r in run["rules"] if r["unique"]), df.columns[0])
        sensitive = {r["column"] for r in run["rules"] if r["type"] in ("SSN", "TAX_ID")}
        rows = []
        for n in row_nos:
            record = df.iloc[n - 1]
            full = {c: profiling_checks.mask_value(record[c]) if c in sensitive else record[c] for c in df.columns}
            rows.append({"row": n, "key": full[key], "value": full[check["column"]],
                         "length": len(record[check["column"]]), "record": full})
        return jsonify(
            table=Path(run["filename"]).stem.upper(), column=check["column"], check=check["check"],
            type=rule["type"], loaded_as="VARCHAR (text)", failed=check["failed"], pct=check["pct"],
            rule={k: rule[k] for k in ("required", "unique", "min_len", "max_len", "date_format", "allowed_values")},
            observed={k: profile[k] for k in ("min_len", "max_len", "nulls", "distinct")},
            key_column=key, columns=list(df.columns), rows=rows, limit=profiling_checks.MAX_ROWS)

    @app.post("/api/runs/<run_id>/summary")
    @login_required
    def write_summary(run_id):
        """AI writes the plain-English summary; every number in it is verified against the results."""
        run = checked_run(run_id)
        if app.config["ASK_SUMMARY_AI"] == "off":
            ask = None
        else:
            ask = app.config["ASK_SUMMARY_AI"] or ai.get_ask_summary()
        summary, info = profiling_report.write_summary(run, ask)
        run.update(summary=summary, summary_info=info)
        save_run(run)
        facts = profiling_report.build_facts(run)
        return jsonify(summary=summary, info=info, findings=facts["findings"])

    @app.get("/api/runs/<run_id>/report.xlsx")
    @login_required
    def download_report(run_id):
        run = checked_run(run_id)
        summary = run.get("summary") or profiling_report.fallback_summary(profiling_report.build_facts(run))
        data = profiling_report.build_excel(run, summary, g.user["full_name"])
        return send_file(io.BytesIO(data), as_attachment=True, download_name=profiling_report.report_filename(run),
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.errorhandler(400)
    def bad_request(e):
        if request.path.startswith("/api/"):
            return jsonify(error=e.description), 400
        return render_template("error.html", message=e.description), 400

    @app.errorhandler(413)
    def too_large(_e):
        return jsonify(error="File is larger than 200 MB."), 413

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.getenv("APP_PORT", 5050)), debug=True)
