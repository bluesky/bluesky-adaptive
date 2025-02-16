from queue import Empty, Queue
from threading import Event, Thread

from scipy.optimize import minimize

from bluesky_adaptive.recommendations import NoRecommendation


class MinimizerReccomender:
    """A very naive recommendation engine that uses scipy.optomize.minimize"""

    def __init__(self, scale=1):
        """

        Parameters
        ----------
        step : array

        """
        self._internal_from_exp = Queue()
        self._internal_to_exp = Queue()
        self._minimizer_done = Event()
        self._scale = scale
        self.result = None
        self._thread = None

    def ingest(self, x, y):
        self._internal_from_exp.put(y * self._scale)
        if self._thread is None:

            def minimize_worker(init):
                def inner_gen():
                    # this is the yield to absorb the pump
                    yield None
                    # the initial point is where the first measurement
                    # already is.  The x will be the next value
                    x = yield self._internal_from_exp.get()
                    while True:
                        # which we that put on the out queue which will be
                        # picked up by ``suggest``
                        self._internal_to_exp.put(x)
                        # we then block (the background thread) on getting the
                        # next measurement which will be put in place by the
                        # next call to ingest
                        x = yield self._internal_from_exp.get()

                gen = inner_gen()
                # prime the generator so we can get start with real values
                # straight away
                gen.send(None)

                self.result = minimize(gen.send, init)
                self._minimizer_done.set()
                gen.close()

            self._thread = Thread(target=minimize_worker, args=(x,))
            self._thread.start()

    def ingest_many(self, xs, ys):
        for x, y in zip(xs, ys):
            self.ingest(x, y)

    def suggest(self, n):
        if self._minimizer_done.is_set():
            raise NoRecommendation
        try:
            ret = self._internal_to_exp.get(timeout=1)
        except Empty:
            raise NoRecommendation
        else:
            return ret
