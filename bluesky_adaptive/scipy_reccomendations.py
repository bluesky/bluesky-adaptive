from scipy.optimize import minimize
from threading import Thread, Event
from queue import Empty, Queue

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

    def tell(self, x, y):
        print(f"tell {x=}, {y=}")
        self._internal_from_exp.put(y * self._scale)
        if self._thread is None:

            def minimize_worker(init):
                first_time = True

                def inner_worker(x):
                    nonlocal first_time
                    if first_time:
                        first_time = False
                    else:
                        self._internal_to_exp.put(x)
                    return self._internal_from_exp.get()

                self.result = minimize(inner_worker, init)
                self._minimizer_done.set()

            self._thread = Thread(target=minimize_worker, args=(x,))
            self._thread.start()

    def tell_many(self, xs, ys):
        for x, y in zip(xs, ys):
            self.tell(x, y)

    def ask(self, n, tell_pending=True):
        print(f"top of ask, {self._minimizer_done.is_set()=}")
        if self._minimizer_done.is_set():
            raise NoRecommendation
        try:
            ret = self._internal_to_exp.get(timeout=1)
        except Empty:
            raise NoRecommendation
        else:
            print(f"ask {ret=}")
            return ret
