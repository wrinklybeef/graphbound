"""A small, bounded, host-pinned transport for Microsoft Graph."""

from graphbound.client import (
    DEFAULT_TIMEOUT_SECONDS,
    GRAPH_BASE_URL,
    MAX_BATCH_REQUESTS,
    GraphClient,
)
from graphbound.errors import (
    GraphError,
    GraphErrorDetails,
    GraphRetryExhausted,
    InvalidGraphObjectId,
    UnsafeGraphUrl,
)
from graphbound.events import RequestFailure, RetryScheduled
from graphbound.identifiers import graph_object_id, graph_object_segment
from graphbound.retry import (
    TRANSIENT_READ_STATUSES,
    BudgetHttpSession,
    ReadBudget,
    ReadRetryPolicy,
    current_read_budget,
    read_budget,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "GRAPH_BASE_URL",
    "MAX_BATCH_REQUESTS",
    "TRANSIENT_READ_STATUSES",
    "BudgetHttpSession",
    "GraphClient",
    "GraphError",
    "GraphErrorDetails",
    "GraphRetryExhausted",
    "InvalidGraphObjectId",
    "ReadBudget",
    "ReadRetryPolicy",
    "RequestFailure",
    "RetryScheduled",
    "UnsafeGraphUrl",
    "__version__",
    "current_read_budget",
    "graph_object_id",
    "graph_object_segment",
    "read_budget",
]
