import logging

from fastapi import APIRouter, HTTPException, status

from .server_resources import SR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

items_dict = {}


def process_exceptions():
    """
    Must be called from within ``except`` block.
    """
    try:
        raise
    except Exception as ex:
        logger.exception(ex)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Failed to process the request: {ex}"
        )


@router.get("/")
async def root_handler():
    """
    This is the response for the root URL (e.g. http://localhost)
    """
    return {"message": "The HTTP server is alive!!!"}


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
    try:
        variables = await SR.worker_get_all_variable_descriptions()
        names = list(variables)
    except Exception:
        process_exceptions()

    return {"names": names}


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
    try:
        return await SR.worker_get_variable(name=name)
    except Exception:
        process_exceptions()


@router.post("/variable/{name}")
async def set_variable_handler(name: str, payload: dict = {}):
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
            status_code=status.HTTP_400_BAD_REQUEST, detail="Required parameter ('value') is missing."
        )

    try:
        value = payload["value"]
        return await SR.worker_set_variable(name=name, value=value)
    except Exception:
        process_exceptions()
