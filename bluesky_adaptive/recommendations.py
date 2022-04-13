"""Toy recommendation engines for testing / demo purposes."""
from .agents import BaseAgent
import pandas as pd


class NoRecommendation(Exception):
    """Exception to signal we have no recommended action."""

    ...


class StepRecommender(BaseAgent):
    """A very naive recommendation engine that takes a fixed step forward."""

    def __init__(self, step):
        """

        Parameters
        ----------
        step : array

        """
        self.step = step
        self.next_point = None

    def tell(self, x, y):
        self.next_point = x + self.step

    def tell_many(self, xs, ys):
        for x, y in zip(xs, ys):
            self.tell(x, y)

    def ask(self, n, tell_pending=True):
        if n != 1:
            raise NotImplementedError
        if self.next_point is None:
            raise NoRecommendation
        return self.next_point


class SequenceRecommender(BaseAgent):
    """A very naive recommendation engine that takes a fixed step forward."""

    def __init__(self, seq):
        """

        Parameters
        ----------
        step : array

        """
        self.seq_iter = iter(seq)

    def tell(self, x, y):
        # we don't care about input, just go through our sequence
        ...

    def tell_many(self, xs, ys):
        # we don't care about input, just go through our sequence
        ...

    def ask(self, n, tell_pending=True):
        if n != 1:
            raise NotImplementedError
        try:
            return next(self.seq_iter)
        except StopIteration:
            raise NoRecommendation


class SequentialSummaryAgent(BaseAgent):
    """Simple agent reports a statistical summary of data and is a sequence recommender"""

    def __init__(self, seq, verbose=True):
        self.seq_iter = iter(seq)
        self.independents = []
        self.dependents = []
        self.last_summary = None
        self.verbose = verbose
        self.n_reports = 0  # Number of reports is mainly just tracked for testing.

    def tell(self, x, y):
        self.independents.append(x)
        self.dependents.append(y)
        self.last_summary = pd.Series(self.dependents).describe()

    def ask(self, batch_size):
        if batch_size != 1:
            raise NotImplementedError
        try:
            return next(self.seq_iter)
        except StopIteration:
            raise NoRecommendation

    def report(self):
        self.n_reports += 1
        if self.verbose:
            print(self.last_summary)
        return self.last_summary
