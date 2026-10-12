"""Minimal KEY=VALUE config files (a dotenv subset), so machine-specific settings live outside Git.

Supported: blank lines, `# comments`, optional `export ` prefix, values in single or double quotes.
Values never override variables already set in the environment, so the shell always wins.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, MutableMapping, Optional

KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


def load_env_file(path, environ: Optional[MutableMapping[str, str]] = None) -> List[str]:
    """Load `path` into `environ` (default os.environ). Returns the keys that were set."""
    environ = os.environ if environ is None else environ
    path = Path(path).expanduser()
    if not path.is_file():
        return []
    loaded = []
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not KEY.match(key):
            raise ValueError(f"{path}:{number}: expected KEY=VALUE")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key not in environ:
            environ[key] = value
            loaded.append(key)
    return loaded


def default_agent_env_path() -> Path:
    if os.environ.get("SHAREDCOMPUTE_AGENT_ENV"):
        return Path(os.environ["SHAREDCOMPUTE_AGENT_ENV"]).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "sharedcompute" / "agent.env"
