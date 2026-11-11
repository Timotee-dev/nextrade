"""Model artifacts on disk, with an integrity hash checked on every load."""

from __future__ import annotations

import hashlib
from pathlib import Path

import joblib


class ArtifactIntegrityError(Exception):
    pass


def save_artifact(ensemble: list, directory: Path, version: str) -> tuple[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{version}.joblib"
    joblib.dump(ensemble, path)
    return str(path), hashlib.sha256(path.read_bytes()).hexdigest()


def load_artifact(path: str, sha256: str) -> list:
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ArtifactIntegrityError(f"{path} does not match its recorded hash; refusing to load it")
    return joblib.load(path)
