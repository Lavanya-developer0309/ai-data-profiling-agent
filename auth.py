"""
Users and login, stored in a local SQLite file (instance/app.db).
Passwords are stored as salted hashes, never as plain text.
"""
import re
import sqlite3
import time

from flask import current_app, g
from werkzeug.security import check_password_hash, generate_password_hash

MAX_ATTEMPTS = 5          # failed logins before the account is locked
LOCK_SECONDS = 5 * 60     # how long the lock lasts
_failed = {}              # username -> (failed count, time of first failure)


def db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY,
                username      TEXT UNIQUE NOT NULL COLLATE NOCASE,
                full_name     TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                last_login    TEXT
            )""")


def get_user(user_id):
    return db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def validate_new_user(username, full_name, password, confirm):
    errors = {}
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,30}", username):
        errors["username"] = "3-30 characters: letters, numbers, dot, dash or underscore."
    elif db().execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        errors["username"] = "That username is already taken."
    if len(full_name.strip()) < 2:
        errors["full_name"] = "Please enter your name."
    if len(password) < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        errors["password"] = "At least 8 characters, with letters and numbers."
    elif password != confirm:
        errors["confirm"] = "Passwords do not match."
    return errors


def create_user(username, full_name, password):
    cur = db().execute(
        "INSERT INTO users (username, full_name, password_hash) VALUES (?, ?, ?)",
        (username, full_name.strip(), generate_password_hash(password)))
    db().commit()
    return get_user(cur.lastrowid)


def check_login(username, password):
    """Returns (user, None) on success or (None, error message)."""
    key = username.lower()
    now = time.time()
    count, since = _failed.get(key, (0, now))
    if now - since >= LOCK_SECONDS:
        count, since = 0, now
    if count >= MAX_ATTEMPTS:
        minutes = int((LOCK_SECONDS - (now - since)) // 60) + 1
        return None, f"Too many failed attempts. Try again in {minutes} min."

    user = db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if user and check_password_hash(user["password_hash"], password):
        _failed.pop(key, None)
        db().execute("UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?", (user["id"],))
        db().commit()
        return user, None

    _failed[key] = (count + 1, since)
    left = MAX_ATTEMPTS - count - 1
    hint = f" {left} attempt{'s' if left != 1 else ''} left." if 0 < left <= 2 else ""
    return None, "Incorrect username or password." + hint
