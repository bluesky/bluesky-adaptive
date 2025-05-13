import pickle
import time
import warnings
from abc import ABC
from typing import Sequence, Dict, Tuple
from logging import getLogger

import numpy as np
import zmq
from numpy._typing import ArrayLike

from .base import Agent

logger = getLogger("bluesky_adaptive.agents")

SLEEP_FOR_AGENT_TIME = .1
SLEEP_FOR_TSUCHINOKO_TIME = .1
FORCE_KICKSTART_TIME = 5


class TsuchinokoBase(ABC):
    def __init__(self, *args, host: str = '127.0.0.1', port: int = 5557, **kwargs):
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
        self.send_payload({'send_targets': True})  # kickstart to recover from shutdowns
        self.last_targets_received = time.time()  # forgive lack of response until now

    def setup_socket(self):
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PAIR)

        # Attempt to connect, retry every second if fails
        while True:
            try:
                self.socket.connect(f"tcp://{self.host}:{self.port}")
            except zmq.ZMQError:
                logger.info(f'Unable to connect to tcp://{self.host}:{self.port}. Retrying in 1 second...')
                time.sleep(1)
            else:
                logger.info(f'Connected to tcp://{self.host}:{self.port}.')
                break

    def tell(self, x, y, v):
        """
        Send measurement to BlueskyAdaptiveEngine
        """
        yv = (y, v)
        payload = {'target_measured': (x, yv)}
        self.send_payload(payload)

    def ask(self, batch_size: int) -> Sequence[ArrayLike]:
        """
        Wait until at least one target is received, also exhaust the queue of received targets, overwriting old ones
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
        assert 'targets' in payload
        self.last_targets_received = time.time()
        return payload['targets']

    def send_payload(self, payload: dict):
        logger.info(f'message: {payload}')
        self.socket.send(pickle.dumps(payload))

    def recv_payload(self, flags=0) -> dict:
        payload_response = pickle.loads(self.socket.recv(flags=flags))
        logger.info(f'response: {payload_response}')
        return payload_response


class TsuchinokoAgent(TsuchinokoBase, Agent, ABC):
    """
    A Bluesky-Adaptive 'Agent'. This Agent communicates with Tsuchinoko over zmq to request new targets and report back
    measurements. This is an abstract class that must be subclassed.

    A `tsuchinoko.execution.bluesky_adaptive.BlueskyAdaptiveEngine` is required for the Tsuchinoko server to complement
    one of these `TsuchinokoAgent`.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._targets_shape = None

    def tell(self, x, y, v) -> Dict[str, ArrayLike]:
        super().tell(x, y, v)
        return self.get_tell_document(x, y, v)

    def ask(self, batch_size: int) -> Tuple[Sequence[Dict[str, ArrayLike]], Sequence[ArrayLike]]:
        targets = super().ask(batch_size)
        return self.get_ask_documents(targets), targets

    def get_tell_document(self, x, y, v) -> Dict[str, ArrayLike]:
        """
        Return any single document corresponding to 'tell'-ing Tsuchinoko about the newly measured `x`, `y` data

        Parameters
        ----------
        x :
            Independent variable for data observed
        y :
            Dependent variable for data observed
        v :
            Variance for measurement of y

        Returns
        -------
        dict
            Dictionary to be unpacked or added to a document

        """

        return dict(independent=np.asarray(x),
                    observable=np.asarray(y),
                    variance=np.asarray(v))

    def get_ask_documents(self, targets: Sequence[ArrayLike]) -> Sequence[Dict[str, ArrayLike]]:
        """
        Ask the agent for a new batch of points to measure.

        Parameters
        ----------
        targets : List[Tuple]
            The new target positions to be measured received during this `ask`.

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
            warnings.warn('The length of the target queue has changed. A new databroker run will be generated')
            self.close_and_restart()

        return dict(targets=np.asarray(targets))


class GPCamTsuchinokoAgent(TsuchinokoAgent, ABC):
    def __init__(self, gp_optimizer, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gp_optimizer = gp_optimizer

    def get_ask_documents(self, targets: Sequence[ArrayLike]) -> Sequence[Dict[str, ArrayLike]]:
        blacklist = ["x_data",
                     "y_data",
                     "noise_variances"]  # keys with ragged state
        gpcam_state = self.gp_optimizer.__getstate__()
        sanitized_gpcam_state = dict(**{key if key not in blacklist else f"STATEDICT-{key}": np.asarray(val)
                                        for key, val in gpcam_state.items()
                                        if key in blacklist})
        target_state = super().get_ask_documents(targets)

        return target_state | sanitized_gpcam_state
