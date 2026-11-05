"""Write JSON reports shared by ingestion and evaluation."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json(path: Path, payload: Any) -> None:
    """
    Write a UTF-8 JSON file, creating its parent directories if needed.

    Parameters
    ----------
    path : pathlib.Path
        Local output file to create or replace.
    payload : Any
        JSON-serializable content. Non-finite numbers are rejected.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def write_json_atomic(path: Path, payload: Any) -> None:
    """
    Replace a JSON file only after its complete contents have been written.

    Parameters
    ----------
    path : pathlib.Path
        Local output file. Its parent directories are created if needed.
    payload : Any
        JSON-serializable content. Non-finite numbers are rejected.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temporary_path = Path(output.name)
            json.dump(
                payload, output, ensure_ascii=False, indent=2, allow_nan=False
            )
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
