import pytest


@pytest.fixture(autouse=True)
def _research_records_in_tmp(request, tmp_path):
    """Commands that write experiment records (run_backtest) must not write into the real
    research/experiments folder while tests run."""
    if "django_db" not in {m.name for m in request.node.iter_markers()} and not request.node.get_closest_marker("django_db"):
        yield
        return
    settings = request.getfixturevalue("settings")
    settings.BASE_DIR = tmp_path
    yield
