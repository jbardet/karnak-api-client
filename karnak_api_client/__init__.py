"""Python client for the Karnak management REST API (v2.0.0).

Transport client (``client``) plus idempotent desired-state apply
(``apply``). Endpoint reference: the v2.0.0 OpenAPI spec
(``api-collection/karnak-api.openapi.yaml`` in the Karnak fork repo) plus the
fork's auth-configs and pseudonym-bulk-import extensions.
"""

from .apply import (
    KarnakConfigError,
    apply_config,
    load_apply_config,
    profile_meta_from_yaml,
    write_json_atomic,
)
from .client import (
    ApiError,
    AuthenticationError,
    KarnakApiError,
    KarnakClient,
    KarnakClientConfig,
    KarnakError,
)

__version__ = "0.4.0"

__all__ = [
    "ApiError",
    "AuthenticationError",
    "KarnakApiError",
    "KarnakClient",
    "KarnakClientConfig",
    "KarnakConfigError",
    "KarnakError",
    "apply_config",
    "load_apply_config",
    "profile_meta_from_yaml",
    "write_json_atomic",
    "__version__",
]
