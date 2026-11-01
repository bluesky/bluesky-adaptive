"""Errors the adaptive server reports to clients, with their HTTP status codes."""


class ServerError(Exception):
    """Base class for errors reported to the client with :attr:`status_code`."""

    status_code: int = 500


class NotFoundError(ServerError):
    """An agent, queueserver or catalog entry does not exist."""

    status_code = 404


class ConflictError(ServerError):
    """The request conflicts with the agent's or queueserver's current state."""

    status_code = 409


class InvalidRequestError(ServerError):
    """The request is well formed but cannot be satisfied: unknown names, failed builds, invalid points."""

    status_code = 422


class UnavailableError(ServerError):
    """A service the request needs did not answer."""

    status_code = 503
