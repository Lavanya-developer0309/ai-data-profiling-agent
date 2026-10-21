"""
Which AI proposes the profiling rules.

    AI_PROVIDER=ollama   (default) local model on this laptop - free, data never leaves the machine
    AI_PROVIDER=cortex   Snowflake Cortex - needs a paid Snowflake account (not available on trials)
    AI_PROVIDER=none     built-in rules only
"""
import json
import os
import urllib.error
import urllib.request

from profiling.rules import RULE_TYPES

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")

# Ollama "structured output": the model must answer with exactly this shape
RULES_SCHEMA = {
    "type": "object",
    "properties": {"columns": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "column": {"type": "string"},
            "type": {"type": "string", "enum": list(RULE_TYPES)},
            "required": {"type": "boolean"},
            "min_len": {"type": ["integer", "null"]},
            "max_len": {"type": ["integer", "null"]},
            "date_format": {"type": ["string", "null"]},
            "allowed_values": {"type": "array", "items": {"type": "string"}},
            "unique": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": ["column", "type", "required", "min_len", "max_len", "date_format", "allowed_values", "unique", "reason"],
    }}},
    "required": ["columns"],
}


def provider():
    return os.getenv("AI_PROVIDER", "ollama").lower()


def model():
    return os.getenv("CORTEX_MODEL", "mistral-large2") if provider() == "cortex" else os.getenv("OLLAMA_MODEL", "qwen3:4b-instruct")


def _ollama_up():
    try:
        urllib.request.urlopen(f"{OLLAMA_URL}/api/version", timeout=2)
        return True
    except (urllib.error.URLError, OSError):
        return False


def status():
    """What the page shows in the sidebar."""
    p = provider()
    if p == "ollama":
        return {"provider": p, "model": model(), "ready": _ollama_up(),
                "problem": None if _ollama_up() else "Ollama is not running - start it with: ollama serve"}
    if p == "cortex":
        ok = bool(os.getenv("SNOWFLAKE_ACCOUNT") and os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH"))
        return {"provider": p, "model": model(), "ready": ok, "problem": None if ok else "Snowflake is not set up in .env"}
    return {"provider": "none", "model": None, "ready": False, "problem": "AI is switched off (AI_PROVIDER=none)"}


SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "overview": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "integer"}, "explanation": {"type": "string"}, "recommendation": {"type": "string"}},
            "required": ["id", "explanation", "recommendation"]}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "overview", "findings", "next_steps"],
}


def ollama_json(prompt, schema):
    body = json.dumps({
        "model": model(), "stream": False, "think": False, "format": schema,
        "options": {"temperature": 0}, "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", body, {"Content-Type": "application/json"})
    try:
        reply = json.loads(urllib.request.urlopen(req, timeout=600).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Ollama: {e.read().decode(errors='replace')[:200]}") from None
    except (urllib.error.URLError, OSError):
        raise RuntimeError("Ollama is not running - start it with: ollama serve") from None
    return json.loads(reply["message"]["content"])


def ask_ollama(prompt):
    return json.dumps(ollama_json(prompt, RULES_SCHEMA)["columns"])


def ask_cortex(prompt):
    import sf
    conn = sf.connect()
    try:
        return sf.complete(conn, prompt)
    finally:
        conn.close()


def get_ask_ai():
    """Rules (step 2): function prompt -> reply text, or None when AI is off."""
    return {"ollama": ask_ollama, "cortex": ask_cortex}.get(provider())


def get_ask_summary():
    """Report (step 4): function prompt -> dict, or None when AI is off."""
    if provider() == "ollama":
        return lambda prompt: ollama_json(prompt, SUMMARY_SCHEMA)
    if provider() == "cortex":
        def ask(prompt):
            text = ask_cortex(prompt)
            return json.loads(text[text.find("{"):text.rfind("}") + 1])
        return ask
    return None
