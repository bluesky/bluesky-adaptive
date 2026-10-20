import warnings

from ._version import __version__ as __version__

warnings.warn(
    "bluesky-adaptive 0.x is discontinued. bluesky-adaptive>=1.0 is a different codebase "
    "(the former blop, https://github.com/bluesky/blop) with a different API; the Kafka/Tiled Agent base, "
    "adjudicators, server, and lockstep recommenders are not carried forward. "
    "Pin `bluesky-adaptive<1` to keep this package. "
    "See https://github.com/bluesky/bluesky-adaptive for the migration summary.",
    FutureWarning,
    stacklevel=2,
)
