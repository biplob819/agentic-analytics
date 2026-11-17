"""Results format v1 (SCH-06). Every schema in Tech Spec §3 is importable from here."""

from evalkit.schemas import Interval, ResultSet

from agentic_analytics.schemas._base import (
    ENGINE_OF,
    ENGINES,
    FIX_NAMES,
    MCP_BACKENDS,
    SCHEMA_VERSION,
    BackendName,
    Category,
    Engine,
    FixName,
)
from agentic_analytics.schemas.answers import AgentStep, AnalyticsAnswer, Execution
from agentic_analytics.schemas.config import ConfigError, RunConfig, load_run_config
from agentic_analytics.schemas.pack import DatasetPack
from agentic_analytics.schemas.questions import GoldResult, Probe, Question, Tolerance
from agentic_analytics.schemas.records import EQUIVALENCES, Equivalence, Record, Scores
from agentic_analytics.schemas.summary import (
    BackendSummary,
    CategoryAccuracy,
    FixUplift,
    RunSummary,
)

__all__ = [
    "ENGINES",
    "ENGINE_OF",
    "EQUIVALENCES",
    "FIX_NAMES",
    "MCP_BACKENDS",
    "SCHEMA_VERSION",
    "AgentStep",
    "AnalyticsAnswer",
    "BackendName",
    "BackendSummary",
    "Category",
    "CategoryAccuracy",
    "ConfigError",
    "DatasetPack",
    "Engine",
    "Equivalence",
    "Execution",
    "FixName",
    "FixUplift",
    "GoldResult",
    "Interval",
    "Probe",
    "Question",
    "Record",
    "ResultSet",
    "RunConfig",
    "RunSummary",
    "Scores",
    "Tolerance",
    "load_run_config",
]
