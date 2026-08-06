"""Python client for the Karnak management REST API.

Transport client (``client``) plus idempotent desired-state apply
(``apply``). Endpoint reference:
https://github.com/nroduit/karnak/blob/master/API.md
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

__version__ = "0.1.0"

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
