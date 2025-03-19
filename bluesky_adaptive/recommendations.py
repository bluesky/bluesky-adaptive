"""Toy recommendation engines for testing / demo purposes."""

from bluesky.utils import RunEngineControlException


class NoRecommendation(Exception):
    """Exception to signal we have no recommended action."""

    ...


class RequestPause(RunEngineControlException):
    """Exception to signal that we recommend pausing the plan"""

    ...


class StepRecommender:
    """A very naive recommendation engine that takes a fixed step forward."""

    def __init__(self, step):
        """

        Parameters
        ----------
        step : array

        """
        self.step = step
        self.next_point = None

    def ingest(self, x, y):
        self.next_point = x + self.step

    def ingest_many(self, xs, ys):
        for x, y in zip(xs, ys):
            self.ingest(x, y)

    def suggest(self, n):
        if n != 1:
            raise NotImplementedError
        if self.next_point is None:
            raise NoRecommendation
        return self.next_point


class SequenceRecommender:
    """A very naive recommendation engine that takes a fixed step forward."""

    def __init__(self, seq):
        """

        Parameters
        ----------
        step : array

        """
        self.seq_iter = iter(seq)

    def ingest(self, x, y):
        # we don't care about input, just go through our sequence
        ...

    def ingest_many(self, xs, ys):
        # we don't care about input, just go through our sequence
        ...

    def suggest(self, n):
        if n != 1:
            raise NotImplementedError
        try:
            return next(self.seq_iter)
        except StopIteration:
            raise NoRecommendation
