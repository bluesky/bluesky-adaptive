from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from .shared_ns import shared_ns
import typing

import logging
logger = logging.getLogger("uvicorn")

router = APIRouter(prefix="/api")

items_dict = {}


@router.get("/")
async def root_handler():
    """
    This is the response for the root URL (e.g. http://localhost)
    """
    return {"message": "The HTTP server is alive!!!"}


@router.get("/parameters/names")
async def get_parameter_names():
    """
    Get the list of parameter names.

    Returns
    -------
    dict
        Dictionary with the following keys: ``names`` - list of names of the
        available parameters.
    """
    return {"names": list(shared_ns["server_parameters"])}


@router.get("/variables/names")
async def get_variable_names():
    """
    Get the list of variable names.

    Returns
    -------
    dict
        Dictionary with the following keys: ``names`` - list of names of the
        available variables.
    """
    return {"names": list(shared_ns["server_variables"])}


@router.get("/parameter/{name}")
async def get_parameter_handler(name: str):
    """
    Returns value of a server parameter. The API call returns HTTP Error 404 if
    the parameter does not exist.

    Parameters
    ----------
    name: str
        Parameter name

    Returns
    -------
    dict
       A dictionary that contains one item. The dictionary maps parameter name to its value.
    """
    if name not in shared_ns["server_parameters"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Parameter {name!r} is not available."
        )
    return {name: shared_ns["server_parameters"][name]}


@router.get("/variable/{name}")
async def get_variable_handler(name: str):
    """
    Returns value of a server variable. The API call returns HTTP Error 404 if
    the variable does not exist.

    Parameters
    ----------
    name: str
        Variable name

    Returns
    -------
    dict
       A dictionary that contains one item. The dictionary maps variable name to its value.
    """
    if name not in shared_ns["server_variables"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Variable {name!r} is not available."
        )
    return {name: shared_ns["server_variables"][name]}



@router.post("/parameter/{name}")
async def set_parameter_handler(name: str, payload: dict={}):
    """
    Sets the value of a server parameter item. The item name (``name``) is specified as part
    of the URL. The API call is successful as long as ``payload`` dictionary contains an element
    named ``value``.

    Parameters
    ----------
    name: str
        Item name
    payload: dict
        A dictionary ``{"value": <value>}``.

    Returns
    -------
    dict
       A dictionary that contains a single item. The dictionary maps item name to item value.
    """
    if "value" not in payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Required parameter ('value') is missing."
        )

    if name not in shared_ns["server_parameters"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Parameter {name!r} is not available."
        )

    value = payload["value"]
    shared_ns["server_parameters"][name] = value
    return {name: value}


@router.post("/variable/{name}")
async def set_variable_handler(name: str, payload: dict={}):
    """
    Sets the value of a server variable. The item name (``name``) is specified as part
    of the URL. The API call is successful as long as ``payload`` dictionary contains an element
    named ``value``.

    Parameters
    ----------
    name: str
        Item name
    payload: dict
        A dictionary ``{"value": <value>}``.

    Returns
    -------
    dict
       A dictionary that contains a single item. The dictionary maps item name to item value.
    """
    if "value" not in payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Required parameter ('value') is missing."
        )

    if name not in shared_ns["server_variables"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Variable {name!r} is not available."
        )

    value = payload["value"]
    shared_ns["server_variables"][name] = value
    return {name: value}
