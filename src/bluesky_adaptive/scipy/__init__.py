"""Scipy Backend for Pertubative gradient and in house global optimizers."""

try:
    from .configs import SCP, Objective, RangeDOF, ScipyCFG
    from .inverter import InteractiveOptimizer
    from .normalizers import SHGO, DualAnnealing, Minimize
    from .scipy import Scipy
except ImportError as e:
    raise ImportError(
        "The scipy integration requires additional dependencies. Install them with: pip install bluesky-adaptive[scipy]"
    ) from e

__all__ = [
    "SCP",
    "ScipyCFG",
    "Scipy",
    "DualAnnealing",
    "Minimize",
    "SHGO",
    "InteractiveOptimizer",
    "Objective",
    "RangeDOF",
]
