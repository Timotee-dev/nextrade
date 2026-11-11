"""Settings must survive a .env copied straight from .env.example (empty values)."""

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROBE = ("import django, os; os.environ['DJANGO_SETTINGS_MODULE']='config.settings'; django.setup(); "
         "from django.conf import settings as s; print(s.DATABASES['default']['ENGINE']); "
         "print(s.NEXTRADE['BINANCE_BASE_URL']); print(len(s.NEXTRADE['DEFAULT_SYMBOLS']))")


def run_with_env_file(tmp_path, env_text):
    project = tmp_path / "proj"
    shutil.copytree(ROOT, project, ignore=shutil.ignore_patterns(".venv", "data", "*.sqlite3", "__pycache__",
                                                                 ".pytest_cache", "tests"))
    (project / ".env").write_text(env_text)
    env = {k: v for k, v in __import__("os").environ.items()
           if k not in ("DATABASE_URL", "BINANCE_BASE_URL", "NEXTRADE_SYMBOLS")}
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=project, env=env, capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0, out.stderr
    return out.stdout.split()


def test_env_example_copy_works(tmp_path):
    engine, url, n = run_with_env_file(tmp_path, (ROOT / ".env.example").read_text())
    assert engine.endswith("sqlite3") and url == "https://api.binance.com" and int(n) == 10


def test_empty_values_fall_back_to_defaults(tmp_path):
    engine, url, n = run_with_env_file(tmp_path, "DATABASE_URL=\nBINANCE_BASE_URL=\nNEXTRADE_SYMBOLS=\n")
    assert engine.endswith("sqlite3") and url == "https://api.binance.com" and int(n) == 10
