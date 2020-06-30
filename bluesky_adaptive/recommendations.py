"""Toy recommendation engines for testing / demo purposes."""


class NoRecommendation(Exception):
    """Exception to signal we have no recommended action."""

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

    def tell_many(self, xs, ys):
        for x, y in zip(xs, ys):
            self.tell(x, y)

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
