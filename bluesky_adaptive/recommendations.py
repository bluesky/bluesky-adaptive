"""Toy recommendation engines for testing / demo purposes."""


class NoRecommendation(Exception):
    "Exception to signal we have no recommended action."
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

    def tell(self, x, y):
        self.next_point = x + self.step

    def ask(self, n, tell_pending=True):
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

    def tell(self, x, y):
        ...

    def ask(self, n, tell_pending=True):
        if n != 1:
            raise NotImplementedError
        try:
            return next(self.seq_iter)
        except StopIteration:
            raise NoRecommendation
