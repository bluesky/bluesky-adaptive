import pickle
import time
import warnings
from abc import ABC
from collections.abc import Sequence
from logging import getLogger

import numpy as np
import zmq
from numpy._typing import ArrayLike

from .base import Agent

logger = getLogger("bluesky_adaptive.agents")

SLEEP_FOR_AGENT_TIME = 0.1
SLEEP_FOR_TSUCHINOKO_TIME = 0.1
FORCE_KICKSTART_TIME = 5


class TsuchinokoBase:
    def __init__(self, *args, host: str = "127.0.0.1", port: int = 5557, **kwargs):
        """

        Parameters
        ----------
        args
            args passed through to `bluesky_adaptive.agents.base.Agent.__init__()`
        host
            A host address target for the zmq socket.
        port
            The port used for the zmq socket.
        kwargs
            kwargs passed through to `bluesky_adaptive.agents.base.Agent.__init__()`
        """

        super().__init__(*args, **kwargs)
        self.host = host
        self.port = port
        self.outbound_measurements = []
        self.context = None
        self.socket = None
        self.setup_socket()
        self.last_targets_received = time.time()
        self.kickstart()

    def kickstart(self):
        self.send_payload({"send_targets": True})  # kickstart to recover from shutdowns
        self.last_targets_received = time.time()  # forgive lack of response until now

    def setup_socket(self):
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PAIR)

        # Attempt to connect, retry every second if fails
        while True:
            try:
                self.socket.connect(f"tcp://{self.host}:{self.port}")
            except zmq.ZMQError:
                logger.info(f"Unable to connect to tcp://{self.host}:{self.port}. Retrying in 1 second...")
                time.sleep(1)
            else:
                logger.info(f"Connected to tcp://{self.host}:{self.port}.")
                break

    def ingest(self, x, yv):
        """
        Send measurement to BlueskyAdaptiveEngine
        """
        payload = {"target_measured": (x, yv)}
        self.send_payload(payload)

    def suggest(self, batch_size: int = 1) -> Sequence[ArrayLike]:
        """
        Wait until at least one target is received, also exhaust the queue of
        received targets, overwriting old ones
        """
        payload = None
        while True:
            try:
                payload = self.recv_payload(flags=zmq.NOBLOCK)
            except zmq.ZMQError:
                if payload is not None:
                    break
                else:
                    time.sleep(SLEEP_FOR_TSUCHINOKO_TIME)
                    if time.time() > self.last_targets_received + FORCE_KICKSTART_TIME:
                        self.kickstart()
        assert "candidate" in payload
        self.last_targets_received = time.time()
        return payload

    def send_payload(self, payload: dict):
        logger.info(f"message: {payload}")
        self.socket.send(pickle.dumps(payload))

    def recv_payload(self, flags=0) -> dict:
        payload_response = pickle.loads(self.socket.recv(flags=flags))
        logger.info(f"response: {payload_response}")
        return payload_response


class TsuchinokoAgent(TsuchinokoBase, Agent, ABC):
    """
    A Bluesky-Adaptive 'Agent'. This Agent communicates with Tsuchinoko over zmq
    to request new targets and report back measurements. This is an abstract
    class that must be subclassed.

    A `tsuchinoko.execution.bluesky_adaptive.BlueskyAdaptiveEngine` is required
    for the Tsuchinoko server to complement one of these `TsuchinokoAgent`.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._targets_shape = None

    def ingest(self, x, yv) -> dict[str, ArrayLike]:
        super().ingest(x, yv)
        return self.get_ingest_document(x, yv)

    def suggest(self, batch_size: int = 1) -> tuple[Sequence[dict[str, ArrayLike]], Sequence[ArrayLike]]:
        targets = super().suggest(batch_size)
        optimizer_state = targets.pop("optimizer")
        return self.get_suggest_documents(targets, optimizer_state), targets

    def get_ingest_document(self, x, yv) -> dict[str, ArrayLike]:
        """
        Return any single document corresponding to 'tell'-ing Tsuchinoko about the newly measured `x`, `y` data

        Parameters
        ----------
        x :
            Independent variable for data observed
        yv :
            Dependent variable for data observed, concatenated with variance
        Returns
        -------
        dict
            Dictionary to be unpacked or added to a document

        """
        y, v = yv
        return {"independent": np.asarray(x), "observable": np.asarray(y), "variance": np.asarray(v)}

    def get_suggest_documents(
        self, targets: Sequence[ArrayLike], optimizer_state: dict
    ) -> Sequence[dict[str, ArrayLike]]:
        """
        Ask the agent for a new batch of points to measure.

        Parameters
        ----------
        targets : List[Tuple]
            The new target positions to be measured received during this `ask`.
        optimizer_state: Dict
            The serialized state of a GPOptimizer instance

        Returns
        -------
        docs : Sequence[dict]
            Documents of key metadata from the ask approach for each point in next_points.
            Must be length of batch size.

        """

        # check if targets length changes
        if not self._targets_shape:
            self._targets_shape = len(targets)
        if self._targets_shape != len(targets):
            warnings.warn(
                "The length of the target queue has changed. A new databroker run will be generated", stacklevel=2
            )
            self.close_and_restart()

        return [targets | optimizer_state]
