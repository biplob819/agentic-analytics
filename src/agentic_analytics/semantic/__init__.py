"""The semantic layer (N1.4, D-8): one `semantic.yaml` per pack generates the Snowflake semantic
view (SEM-02) and the descriptions prompt block (SEM-03), hashed as `semantic_hash`, with a
parity check between the two generated texts (SEM-04)."""

from __future__ import annotations

from agentic_analytics.semantic.model import (
    SEMANTIC_FILE,
    Dimension,
    Fact,
    Metric,
    SemanticError,
    SemanticModel,
    SemanticTable,
    check_columns,
    comment_text,
    load_semantic,
    pack_table_columns,
    parse_semantic,
)
from agentic_analytics.semantic.parity import (
    SemanticParityError,
    require_semantic_parity,
    semantic_parity,
)
from agentic_analytics.semantic.prompt import (
    descriptions_block,
    parse_descriptions_block,
    semantic_hash,
)
from agentic_analytics.semantic.snowflake import (
    default_names,
    parse_semantic_view_ddl,
    semantic_view_ddl,
    semantic_view_grant_sql,
)

__all__ = [
    "SEMANTIC_FILE",
    "Dimension",
    "Fact",
    "Metric",
    "SemanticError",
    "SemanticModel",
    "SemanticParityError",
    "SemanticTable",
    "check_columns",
    "comment_text",
    "default_names",
    "descriptions_block",
    "load_semantic",
    "pack_table_columns",
    "parse_descriptions_block",
    "parse_semantic",
    "parse_semantic_view_ddl",
    "require_semantic_parity",
    "semantic_hash",
    "semantic_parity",
    "semantic_view_ddl",
    "semantic_view_grant_sql",
]
