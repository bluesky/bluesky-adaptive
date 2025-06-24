from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel


class MethodRegistration(BaseModel):
    """Metadata for method registration"""

    name: str
    description: str
    input_model: type[BaseModel] | None = None
    output_type: Any | None = None


@dataclass
class RegisteredMethod:
    """Internal storage of registered method"""

    callable: Callable
    registration: MethodRegistration
