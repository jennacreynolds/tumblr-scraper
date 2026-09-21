"""Transport adapters between the canonical application and its UI."""

from .local_http import LocalControlBridge, LocalControlRequestHandler

__all__ = ["LocalControlBridge", "LocalControlRequestHandler"]
