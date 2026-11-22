"""Per-task AI policy and mock responses. Feature code registers its task once (e.g. at import of its service module):

    from internest_ai.tasks import register_task, register_mock_response

    register_task("cv_polish", schema={"name": str, "skills": [str]}, private=True)
    register_mock_response("cv_polish", {"name": "Mona", "skills": ["SQL"]})     # or a callable(messages) -> dict

    with override_mock("cv_polish", {...}):                                         # tests: a temporary mock answer

Unregistered tasks get the default policy (not private, any provider, user-scoped cache) and an empty mock reply.
"""
import contextlib
import copy
from dataclasses import dataclass, field, replace

from . import prompts


@dataclass(frozen=True)
class TaskSpec:
    name: str
    schema: object = None           # prompts shape; replies that do not match fail over to the next provider
    private: bool = False           # external providers only receive redacted data (local ones get the original)
    local_only: bool = False        # never sent to an external provider at all
    long_context: bool = False      # only providers flagged long_context
    user_scoped_cache: bool = True  # include the user id in the cache key whenever a user is given
    mock: object = field(default=None, compare=False)  # dict, or callable(messages) -> dict, for the mock provider
    description: str = ""


_TASKS: dict[str, TaskSpec] = {}


def _check_mock(spec: TaskSpec):
    if spec.mock is not None and not callable(spec.mock):
        if not isinstance(spec.mock, dict):
            raise TypeError(f"mock response for {spec.name} must be a dict or a callable")
        prompts.validate_shape(spec.mock, spec.schema)


def register_task(name: str, **options) -> TaskSpec:
    """Create or update a task's policy (re-registering keeps the options not passed again)."""
    spec = replace(_TASKS[name], **options) if name in _TASKS else TaskSpec(name=name, **options)
    _check_mock(spec)
    _TASKS[name] = spec
    return spec


def get_task(name: str) -> TaskSpec:
    return _TASKS.get(name) or TaskSpec(name=name)


def register_mock_response(task: str, response) -> TaskSpec:
    """The mock provider's answer for `task`: a schema-valid dict, or callable(messages) -> dict."""
    return register_task(task, mock=response)


@contextlib.contextmanager
def override_mock(task: str, response):
    previous = _TASKS.get(task)
    register_mock_response(task, response)
    try:
        yield
    finally:
        if previous is None:
            _TASKS.pop(task, None)
        else:
            _TASKS[task] = previous


def mock_response(task: str, messages) -> dict:
    """Deterministic, schema-valid answer for the mock provider."""
    spec = get_task(task)
    if spec.mock is not None:
        value = spec.mock(messages) if callable(spec.mock) else copy.deepcopy(spec.mock)
    elif spec.schema is not None:
        value = prompts.example_for(spec.schema)
    else:
        value = {}
    if spec.schema is not None:
        prompts.validate_shape(value, spec.schema)
    return value
