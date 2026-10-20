import warnings

from ._version import __version__ as __version__

warnings.warn(
    "bluesky-adaptive 0.x is discontinued. Starting with 1.0, bluesky-adaptive adopts the blop codebase "
    "(https://github.com/bluesky/blop) and continues its development under the bluesky-adaptive name "
    "at https://github.com/bluesky/bluesky-adaptive. The 1.0+ API differs from 0.x: the Kafka/Tiled "
    "Agent base, adjudicators, server, and lockstep recommenders are not carried forward. "
    "Pin `bluesky-adaptive<1` to keep this package.",
    FutureWarning,
    stacklevel=2,
)
