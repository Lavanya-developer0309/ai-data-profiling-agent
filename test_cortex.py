"""
Phase 1 - check that Snowflake Cortex AI works in this account.

    python test_cortex.py

Logs in, shows the account/region, then asks a few Cortex models to reply "OK".
Put the first model that passes in .env as CORTEX_MODEL.
"""
from sf import CORTEX_MODEL, complete, connect, query

MODELS = [CORTEX_MODEL, "claude-sonnet-4-5", "mistral-large2", "llama3.1-70b", "snowflake-arctic"]


def main():
    conn = connect()
    account, region, role, wh = query(conn, "SELECT CURRENT_ACCOUNT(), CURRENT_REGION(), CURRENT_ROLE(), CURRENT_WAREHOUSE()")[0]
    print(f"Connected: account={account} region={region} role={role} warehouse={wh}\n")

    working = []
    for model in dict.fromkeys(MODELS):
        try:
            reply = complete(conn, "Reply with the single word OK.", model).strip()
            print(f"  PASS  {model:<20} -> {reply[:40]!r}")
            working.append(model)
        except Exception as e:
            print(f"  FAIL  {model:<20} -> {str(e).splitlines()[0][:120]}")

    print()
    if working:
        print(f"Cortex works. Set CORTEX_MODEL={working[0]} in .env")
    else:
        print("No model worked.")
        print("Trial accounts can't use Cortex - add a payment method in Snowsight (Admin > Billing) or keep AI_PROVIDER=ollama.\n"
              "Otherwise, common fixes (run as ACCOUNTADMIN):\n"
              "  GRANT DATABASE ROLE SNOWFLAKE.CORTEX_USER TO ROLE MIG_DEVELOPER;\n"
              "  ALTER ACCOUNT SET CORTEX_ENABLED_CROSS_REGION = 'ANY_REGION';  -- if the model isn't in your region")
    conn.close()


if __name__ == "__main__":
    main()
