from __future__ import annotations

from abc import ABC
from collections.abc import Mapping, Sequence
from copy import deepcopy
from logging import getLogger
from pathlib import Path
from typing import Any, Union

import numpy as np
import yaml
from numpy.typing import ArrayLike

from bluesky_adaptive.agents.base import Agent

logger = getLogger("bluesky_adaptive.agents")


class XoptGeneratorAgentBase(Agent, ABC):
    """Agent base class powered by an Xopt Generator instance.

    This class intentionally stores only a Generator object and never stores an Xopt object.
    The class supports two construction paths:
    - Passing a Generator object directly.
    - Loading a generator-only YAML configuration via ``from_generator_yaml``.
    """

    name = "xopt-generator"

    def __init__(
        self,
        *,
        generator,
        dependent_keys: Sequence[str] | None = None,
        **kwargs,
    ):
        self.generator = self._validate_generator(generator)
        self.variable_names = self._get_variable_names()
        self.output_names = self._get_output_names(dependent_keys)
        super().__init__(**kwargs)

    @staticmethod
    def _validate_generator(generator):
        try:
            from xopt.generator import Generator
        except ImportError as exc:
            raise ImportError("Xopt is required to use XoptGeneratorAgentBase.") from exc

        if not isinstance(generator, Generator):
            raise TypeError("'generator' must be an instance of xopt.generator.Generator")
        return generator

    def _get_variable_names(self) -> list[str]:
        vocs = self.generator.vocs
        names = getattr(vocs, "variable_names", None)
        if names is None:
            names = list(vocs.variables.keys())
        if not names:
            raise ValueError("Generator VOCS must define at least one variable.")
        return list(names)

    def _get_output_names(self, dependent_keys: Sequence[str] | None) -> list[str]:
        if dependent_keys is not None:
            return list(dependent_keys)

        vocs = self.generator.vocs
        names = getattr(vocs, "output_names", None)
        if names is None:
            names = []
        return list(names)

    @classmethod
    def from_generator_yaml(cls, yaml_file: Union[str, Path], **kwargs):
        """Build the agent from generator-only YAML.

        Accepted formats:
        - Direct generator config (e.g. keys like ``name`` and ``vocs``).
        - A single wrapped top-level ``generator`` key.

        The YAML must not contain other top-level Xopt fields such as
        ``evaluator`` or ``stopping_condition``.
        """
        yaml_path = Path(yaml_file)
        if not yaml_path.exists():
            raise OSError(f"Generator YAML file not found: {yaml_path}")

        with yaml_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle)

        generator_config = cls._parse_generator_yaml_config(config)

        try:
            from xopt.generators import get_generator
        except ImportError as exc:
            raise ImportError("Xopt is required to load generator YAML.") from exc

        if "name" not in generator_config:
            raise ValueError("Generator YAML must define a 'name' field.")

        generator_name = generator_config["name"]
        generator_kwargs = deepcopy(generator_config)
        generator_kwargs.pop("name")

        generator_cls = get_generator(generator_name)
        generator = generator_cls.model_validate(generator_kwargs)
        return cls(generator=generator, **kwargs)

    @staticmethod
    def _parse_generator_yaml_config(config: Any) -> dict[str, Any]:
        if not isinstance(config, dict):
            raise TypeError("Generator YAML must parse to a dictionary.")

        if "generator" in config:
            unexpected = set(config.keys()) - {"generator"}
            if unexpected:
                raise ValueError(
                    "Generator YAML must contain only the 'generator' key when wrapped. "
                    f"Unexpected keys: {sorted(unexpected)}"
                )
            config = config["generator"]

        if not isinstance(config, dict):
            raise TypeError("'generator' YAML entry must be a dictionary.")

        forbidden = {
            "evaluator",
            "strict",
            "dump_file",
            "serialize_torch",
            "serialize_inline",
            "stopping_condition",
        }
        present_forbidden = sorted(forbidden.intersection(config.keys()))
        if present_forbidden:
            raise ValueError(
                "Only generator YAML is supported. "
                f"Disallowed keys found: {present_forbidden}"
            )

        return config

    def _coerce_named_values(
        self,
        values: Union[ArrayLike, Mapping[str, Any], None],
        names: Sequence[str],
        *,
        label: str,
    ) -> dict[str, Any]:
        if values is None:
            return {}

        if isinstance(values, Mapping):
            return dict(values)

        arr = np.atleast_1d(np.asarray(values, dtype=object))
        if arr.size != len(names):
            raise ValueError(f"{label} has {arr.size} values but expected {len(names)} based on VOCS.")

        return {name: arr[idx] for idx, name in enumerate(names)}

    def ingest(self, independent_variable, dependent_variable=None) -> dict[str, ArrayLike]:
        variable_payload = self._coerce_named_values(
            independent_variable,
            self.variable_names,
            label="independent_variable",
        )
        output_payload = self._coerce_named_values(
            dependent_variable,
            self.output_names,
            label="dependent_variable",
        )

        payload = {**variable_payload, **output_payload}
        self.generator.ingest([payload])

        cache_len = 0 if self.generator.data is None else len(self.generator.data)
        doc = {"cache_len": cache_len}
        doc.update({f"observed_{k}": v for k, v in payload.items()})
        return doc

    def suggest(self, batch_size=1):
        if batch_size > 1 and not self.generator.supports_batch_generation:
            logger.warning(f"Batch size {batch_size} is not supported by {self.generator.name}. Reducing to 1.")
            batch_size = 1

        candidates = self.generator.generate(batch_size)

        docs = []
        points = []
        cache_len = 0 if self.generator.data is None else len(self.generator.data)

        for candidate in candidates:
            point = np.atleast_1d(np.asarray([candidate[name] for name in self.variable_names], dtype=object))
            points.append(point)
            docs.append(
                {
                    "cache_len": cache_len,
                    **{f"candidate_{name}": value for name, value in candidate.items()},
                }
            )

        return docs, points

    def report(self, **kwargs) -> dict[str, ArrayLike]:
        cache_len = 0 if self.generator.data is None else len(self.generator.data)
        return {
            "cache_len": cache_len,
            "generator_name": self.generator.name,
            "generator_class": str(type(self.generator)),
        }

    def start(self, *args, **kwargs):
        self.metadata.update(
            {
                "xopt_generator_name": self.generator.name,
                "xopt_generator_class": str(type(self.generator)),
            }
        )
        super().start(*args, **kwargs)
