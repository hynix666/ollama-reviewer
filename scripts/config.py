"""Configuration loading: defaults, config file, environment overrides.

Engine policy rather than a front end's concern: any caller (cli.py,
mcp_server.py, a test) loads the same cfg dict through the same precedence
rules - config file over built-in defaults, environment over both.
"""

import json
import os

import ollama_client as oc

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
)

DEFAULTS = {
    "base_url": "http://127.0.0.1:11434",
    "model": "qwen3-coder:30b",
    "fallback_models": [],
    "consensus_models": [],
    "temperature": 0.1,
    "timeout_s": 180,
    "connect_timeout_s": 5,
    "max_retries": 3,
    "backoff_base_s": 1.5,
    "max_file_chars": 60000,
    "max_total_chars": 180000,
    "max_files": 25,
    "allow_cloud_models": False,
}


def load_config():
    """Config file over defaults, environment over both. Never fails hard."""
    cfg = dict(DEFAULTS)
    notes = []
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        notes.append("No config.json found; using built-in defaults.")
    except json.JSONDecodeError as e:
        notes.append("config.json is malformed (%s); using built-in defaults." % e)

    env_url = os.environ.get("OLLAMA_HOST")
    if env_url:
        cfg["base_url"] = env_url
        notes.append("Endpoint overridden by OLLAMA_HOST (%s)." % env_url)

    normalized = oc.normalize_base_url(cfg["base_url"])
    if normalized != cfg["base_url"].rstrip("/"):
        notes.append(
            "Rewrote bind address %s to %s for connecting." % (cfg["base_url"], normalized)
        )
    cfg["base_url"] = normalized

    if os.environ.get("OLLAMA_REVIEW_MODEL"):
        cfg["model"] = os.environ["OLLAMA_REVIEW_MODEL"]
        notes.append("Model overridden by OLLAMA_REVIEW_MODEL.")
    if os.environ.get("OLLAMA_REVIEW_TIMEOUT"):
        try:
            cfg["timeout_s"] = int(os.environ["OLLAMA_REVIEW_TIMEOUT"])
        except ValueError:
            notes.append("OLLAMA_REVIEW_TIMEOUT is not an integer; ignored.")
    return cfg, notes
