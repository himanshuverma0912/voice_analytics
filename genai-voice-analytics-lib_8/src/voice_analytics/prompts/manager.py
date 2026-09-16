"""Loading and rendering the bundled prompt catalogue.

Prompts are Jinja templates in a YAML file that ships *inside the wheel*. The
original service opened ``domains-prompt.yaml`` by relative path, which meant it
only worked when the process happened to start in the repository root. Reading
it through ``importlib.resources`` makes the library work from any directory,
inside any container.

Callers who need their own catalogue can point ``PromptManager`` at a file on
disk instead.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Template

from voice_analytics.exceptions import PromptNotFoundError

_PACKAGE_DATA = "voice_analytics.prompts.data"
_CATALOGUE_FILENAME = "domains-prompt.yaml"


class PromptManager:
    """Renders prompts from a domain/task catalogue."""

    def __init__(self, catalogue: dict[str, Any]):
        self._catalogue = catalogue

    @classmethod
    def from_package(cls) -> "PromptManager":
        """Load the catalogue bundled with this library."""
        source = (
            resources.files(_PACKAGE_DATA)
            .joinpath(_CATALOGUE_FILENAME)
            .read_text(encoding="utf-8")
        )
        return cls(yaml.safe_load(source) or {})

    @classmethod
    def from_file(cls, path: str | Path) -> "PromptManager":
        """Load a catalogue from disk, for callers overriding the defaults."""
        file_path = Path(path)
        if not file_path.is_file():
            raise PromptNotFoundError(f"Prompt catalogue not found: {file_path}")
        # safe_load, never load: the latter can construct arbitrary Python objects.
        return cls(yaml.safe_load(file_path.read_text(encoding="utf-8")) or {})

    def get(self, domain: str, task: str, **variables: Any) -> str:
        """Render the prompt for ``domain``/``task`` with the given variables.

        Raises ``PromptNotFoundError`` when the combination is not in the
        catalogue -- a silent empty prompt would produce plausible nonsense.
        """
        template_str = (self._catalogue.get(domain) or {}).get(task)
        if not template_str:
            raise PromptNotFoundError(f"No prompt configured for {domain} -> {task}")
        return Template(template_str).render(**variables).strip()

    def domains(self) -> list[str]:
        """Domains present in the catalogue."""
        return sorted(self._catalogue)

    def tasks(self, domain: str) -> list[str]:
        """Tasks available within a domain."""
        return sorted(self._catalogue.get(domain) or {})


@lru_cache(maxsize=1)
def default_prompt_manager() -> PromptManager:
    """The bundled catalogue, parsed once per process."""
    return PromptManager.from_package()
