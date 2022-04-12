from abc import ABC, abstractmethod


class BaseAgent(ABC):
    """
    The most trivial Agent at least should be processing data in a stateful way.
    A useful Agent will either implement ask, report, or both.
    An optimized agent will likely have a vectorized approach to being told about many data.
    """

    @abstractmethod
    def tell(self, x, y):
        """
        Tell the agent about some new data
        Parameters
        ----------
        x :
            Independent variable for data observed
        y :
            Dependent variable for data observed

        Returns
        -------

        """
        ...

    def ask(self, batch_size: int):
        """
        Ask the agent for a new batch of points to measure

        Parameters
        ----------
        batch_size : int
            Number of new points to measure

        Returns
        -------
        Set of independent variables of length batch size

        """
        raise NotImplementedError

    def report(self):
        """
        Create a report given the data observed by the agent.
        """
        raise NotImplementedError

    def tell_many(self, xs, ys):
        """
        Tell the agent about some new data. It is likely that there is a more efficient approach to
        handling multiple observations for an agent. The default behavior is to iterate over all
        observations and call the ``tell`` method.

        Parameters
        ----------
        xs : list, array
            Array of independent variables for observations
        ys : list, array
            Array of dependent variables for observations

        Returns
        -------

        """
        for x, y in zip(xs, ys):
            self.tell(x, y)
