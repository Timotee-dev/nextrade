"""Model registry lookups used by the trader (filled in by Phase 13)."""


def production_ensemble(strategy_code: str):
    """(ensemble, version) of the promoted production model for a strategy, or None."""
    try:
        from .models import ModelVersion
    except ImportError:
        return None
    return ModelVersion.production_ensemble(strategy_code) if hasattr(ModelVersion, "production_ensemble") else None
