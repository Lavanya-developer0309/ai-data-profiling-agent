"""
Snowflake connection + Cortex call, shared by the scripts and the Streamlit app.

Uses the same key-pair login as the dbt project. Settings come from .env
(see .env.example).
"""
import os

import snowflake.connector
from dotenv import load_dotenv

load_dotenv()

CORTEX_MODEL = os.getenv("CORTEX_MODEL", "mistral-large2")


def connect():
    params = dict(
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        user=os.getenv("SNOWFLAKE_USER", "MIG_DBT_SVC"),
        authenticator="SNOWFLAKE_JWT",
        private_key_file=os.path.expanduser(os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"]),
        role=os.getenv("SNOWFLAKE_ROLE", "MIG_DEVELOPER"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE", "MIG_WH"),
        database=os.getenv("SNOWFLAKE_DATABASE", "HC_MIGRATION_DB"),
        schema=os.getenv("SNOWFLAKE_SCHEMA", "STAGING"),
    )
    if os.getenv("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"):
        params["private_key_file_pwd"] = os.environ["SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"]
    return snowflake.connector.connect(**params)


def query(conn, sql, binds=None):
    cur = conn.cursor()
    try:
        cur.execute(sql, binds)
        return cur.fetchall()
    finally:
        cur.close()


def complete(conn, prompt, model=None):
    """Ask Snowflake Cortex. The data never leaves the Snowflake account."""
    return query(conn, "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s)", (model or CORTEX_MODEL, prompt))[0][0]
