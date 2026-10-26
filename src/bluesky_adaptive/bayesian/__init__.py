"""Custom models and kernels for Bayesian optimization."""

try:
    from .kernels import LatentKernel
    from .models import LatentGP
except ImportError as e:
    raise ImportError(
        "The bayesian module requires additional dependencies. Install them with: pip install bluesky-adaptive[ax]"
    ) from e

__all__ = [
    "LatentKernel",
    "LatentGP",
]
