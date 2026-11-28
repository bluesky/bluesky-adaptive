"""Xopt backend for optimization."""

try:
    from .optimizer import XoptOptimizer
except ImportError as e:
    raise ImportError(
        "The xopt integration requires additional dependencies. Install them with: pip install bluesky-adaptive[xopt]"
    ) from e

__all__ = ["XoptOptimizer"]
