from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_LOCALE = "uk"
LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"


def t(key: str, **kwargs: Any) -> str:
    value = _lookup(key)
    if not isinstance(value, str):
        raise KeyError(f"Translation key does not point to a string: {key}")

    return value.format(**kwargs) if kwargs else value


def t_optional(key: str, default: str) -> str:
    try:
        value = _lookup(key)
    except KeyError:
        return default

    return value if isinstance(value, str) else default


def _lookup(key: str) -> Any:
    current: Any = _load_locale(DEFAULT_LOCALE)
    for part in key.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(f"Missing translation key: {key}")
        current = current[part]

    return current


@lru_cache(maxsize=8)
def _load_locale(locale: str) -> dict[str, Any]:
    path = LOCALES_DIR / f"{locale}.json"
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(f"Locale file must contain a JSON object: {path}")

    return data

