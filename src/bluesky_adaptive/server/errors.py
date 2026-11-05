"""Errors raised by the optimization manager, with the HTTP status the routers report."""

from typing import Any


class ServerError(Exception):
    """
    Base class for errors that the routers report to clients.

    Parameters
    ----------
    message : str
        Human-readable description of the error.
    detail : Any, optional
        JSON-compatible detail returned to the client instead of the message, for example
        Pydantic validation errors.
    """

    status_code: int = 500

    def __init__(self, message: str, detail: Any = None):
        super().__init__(message)
        self.detail = detail if detail is not None else message


class NotFoundError(ServerError):
    """A requested agent, optimization, trial, or catalog entry does not exist."""

    status_code = 404


class ConflictError(ServerError):
    """The operation is not allowed in the current state, for example another optimization is active."""

    status_code = 409


class InvalidRequestError(ServerError):
    """The request is well-formed JSON but its content is invalid."""

    status_code = 422


class UnavailableError(ServerError):
    """The server is not configured for the operation, for example it has no queueserver."""

    status_code = 503
