"""Trinity API layer — REST + GraphQL + Dashboard."""
from trinity.api.graphql_schema import (
    schema,
    Query,
    Mutation,
    Subscription,
    self_test as graphql_self_test,
)

__all__ = [
    "schema",
    "Query",
    "Mutation",
    "Subscription",
    "graphql_self_test",
]
