"""Helper functions an classes that will be shared across per-event and per-run."""

import itertools

import numpy as np


def chain_zip(motors, next_point):
    """
    Interlace motors and next_point values.

    This converts two lists ::

       m = (motor1, motor2, ..)
       p = (pos1, pos2, ..)

    into a single list ::

       args = (motor1, pos1, motor2, pos2, ...)

    which is what `bluesky.plan_stubs.mv` expects.

    Parameters
    ----------
    motors : iterable
        The list of ophyd objects that should be moved,
        must be same length and next_point

    next_point : iterable
        The list values, must be same length as motors.

    Returns
    -------
    list
       A list alternating (motor, value, motor, value, ...)

    """
    return list(itertools.chain(*zip(motors, next_point)))


def extract_event(independent_keys, dependent_keys, payload):
    """
    Extract the independent and dependent data from Event['data'].

    Parameters
    ----------
    independent_keys : List[str]
        The names of the independent keys in the events

    dependent_keys : List[str]
        The names of the dependent keys in the events
    payload : dict[str, Any]
        The ev['data'] dict from an Event Model Event document.

    Returns
    -------
    independent : np.array
        A numpy array where the first axis maps to the independent variables

    measurements : np.array
        A numpy array where the first axis maps to the dependent variables

    """
    # This is your "motor positions"
    independent = np.asarray([payload[k] for k in independent_keys])
    # This is the extracted measurements
    measurement = np.asarray([payload[k] for k in dependent_keys])
    return independent, measurement


def extract_event_page(*key_lists, payload):
    """
    Extract sets of  data from EventPage['data'].

    This assumes that all values can be safely cast to the same type.

    Parameters
    ----------
    key_list : varargs
        The names of the keys in the events to extract

    payload : dict[str, List[Any]]
        The ev['data'] dict from an Event Model Event document.

    Returns
    -------
    independent : np.array
        A numpy array where the first axis maps to the independent variables

    measurements : np.array
        A numpy array where the first axis maps to the dependent variables

    """
    # the data comes out of the EventPag "column major"
    # we transpose to get "row major"
    return tuple(np.atleast_2d(np.asarray([payload[k] for k in key_list])).T for key_list in key_lists)
