"""Compatibility exports for historical callers; implementations are layered."""

from .api.evaluation import install_evaluation
from .evaluation_schemas import Case, DatasetInput, RunInput, Judgment
from .evaluation_metrics import fingerprint, case_metrics, aggregate_metrics, METRIC_KEYS
from .evaluation_codec import export_evaluation, restore_evaluation, validate_evaluation_backup
from .repositories.evaluation import dataset as _dataset

__all__ = [
    'install_evaluation',
    'Case',
    'DatasetInput',
    'RunInput',
    'Judgment',
    'fingerprint',
    'case_metrics',
    'aggregate_metrics',
    'METRIC_KEYS',
    'export_evaluation',
    'restore_evaluation',
    'validate_evaluation_backup',
    '_dataset',
]
