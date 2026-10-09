"""Typed settings read from the environment.

    class Config(Settings, prefix="APP_"):
        database_url: SecretStr
        debug: bool = False
        allowed_origins: list[str] = []

    config = Config()          # APP_DATABASE_URL, APP_DEBUG, APP_ALLOWED_ORIGINS

A pydantic model, so the types, defaults and validation are pydantic's own;
what this adds is where the values come from and how a mistake is reported.
Every problem is collected and reported at once, each by the variable to set,
and a value is never repeated in an error: the variable might hold a secret,
and errors end up in logs.
"""

import json
import os
import types
import typing
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, SecretBytes, SecretStr, ValidationError

#: Every Settings subclass, in definition order, so `oxbrook settings` can list
#: the ones a target defines even when creating one failed during the import.
_DEFINED: list[type["Settings"]] = []


class SettingsError(ValueError):
    """Settings could not be read. `problems` maps each variable to what is wrong."""

    def __init__(self, cls: type, problems: dict[str, str]) -> None:
        self.settings = cls
        self.problems = problems
        lines = "\n".join(f"  {name}: {problem}" for name, problem in problems.items())
        super().__init__(f"{cls.__name__} cannot be read from the environment:\n{lines}")


class Settings(BaseModel):
    """Settings for an app, each field read from an environment variable.

        class Config(Settings, prefix="APP_"):
            database_url: SecretStr
            workers: int = 4

        config = Config()

    A field `database_url` is read from `DATABASE_URL`, after `prefix`.
    `Field(alias="NAME")` names the variable exactly, prefix and all. A value
    passed to the constructor wins over the environment, which is how a test
    sets one.

    Values are converted by pydantic, as a request body would be: `"8000"` to
    an `int`, `"true"`, `"1"` or `"yes"` to `True`. A list, tuple or set
    is written as JSON (`["a", "b"]`) or as plain comma-separated text
    (`a,b`); a dict or a nested model as JSON.

    `secrets_dir` is a directory of files, one per variable, named as the
    variable or its lowercase: where Docker and Kubernetes mount secrets. An
    environment variable wins over a file.

    Settings are frozen once read. They are shared by every worker loop and
    thread, and configuration that changed underneath a running server would
    be configuration no one could see.

    A missing or invalid value raises `SettingsError`, listing every variable
    that needs attention. Values are never included in the message. Type a
    secret as `SecretStr` so it is not printed by `repr` either.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    _prefix: ClassVar[str] = ""
    _secrets_dir: ClassVar[Path | None] = None

    def __init_subclass__(cls, *, prefix: str | None = None,
                          secrets_dir: str | os.PathLike[str] | None = None,
                          **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Inherited unless restated, so a subclass of a prefixed class keeps it.
        if prefix is not None:
            if not isinstance(prefix, str):
                raise TypeError(f"prefix must be a string, not {type(prefix).__name__}")
            cls._prefix = prefix
        if secrets_dir is not None:
            cls._secrets_dir = Path(secrets_dir)
        _DEFINED.append(cls)

    def __init__(self, **values: Any) -> None:
        cls = type(self)
        data, problems = _gather(cls, values)
        try:
            super().__init__(**data)
        except ValidationError as exc:
            # Errors are located by the key validated under, the alias if any.
            names = {(f.alias or n): n for n, f in cls.model_fields.items()}
            for error in exc.errors(include_input=False, include_url=False):
                key, *inner = error["loc"]
                field = names.get(str(key), str(key))
                where = ".".join(str(part) for part in inner)
                message = "required, and not set" if error["type"] == "missing" and not inner \
                    else (f"{where}: " if where else "") + error["msg"]
                problems.setdefault(_variable(cls, field), message)
        if problems:
            raise SettingsError(cls, problems)

    @classmethod
    def variables(cls) -> list[dict[str, Any]]:
        """Every variable this class reads, for `oxbrook settings`: name, field,
        type, whether it is required or a secret, its default, and where a value
        would come from now. Never the value itself."""
        rows = []
        for name, field in cls.model_fields.items():
            variable = _variable(cls, name)
            source = "environment" if variable in os.environ \
                else "secrets file" if _secret_file(cls, variable) is not None \
                else None
            secret = _is_secret(field.annotation)
            rows.append({
                "variable": variable,
                "field": name,
                "type": _type_name(field.annotation),
                "required": field.is_required(),
                "secret": secret,
                "default": None if field.is_required() or secret
                else _shown(field.get_default(call_default_factory=True)),
                "source": source or ("default" if not field.is_required() else None),
            })
        return rows


def _variable(cls: type[Settings], name: str) -> str:
    field = cls.model_fields.get(name)
    if field is not None and field.alias:
        return field.alias
    return f"{cls._prefix}{name}".upper()


def _secret_file(cls: type[Settings], variable: str) -> Path | None:
    directory = cls._secrets_dir
    if directory is None:
        return None
    for candidate in (variable, variable.lower()):
        path = directory / candidate
        if path.is_file():
            return path
    return None


def _gather(cls: type[Settings], values: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Raw values by the key pydantic validates under, and the problems found
    before validation: text that should have been JSON and was not."""
    data: dict[str, Any] = {}
    problems: dict[str, str] = {}
    unknown = set(values) - set(cls.model_fields)
    if unknown:
        raise TypeError(f"{cls.__name__} has no setting {', '.join(sorted(unknown))}")
    for name, field in cls.model_fields.items():
        key = field.alias or name
        if name in values:
            data[key] = values[name]
            continue
        variable = _variable(cls, name)
        raw = os.environ.get(variable)
        if raw is None:
            path = _secret_file(cls, variable)
            if path is None:
                continue
            # A trailing newline is how most editors and `echo` end a file,
            # never part of a secret.
            raw = path.read_text().removesuffix("\n").removesuffix("\r")
        try:
            data[key] = _parse(field.annotation, raw)
        except ValueError:
            problems[variable] = "not valid JSON"
    return data, problems


def _unwrap(annotation: Any) -> Any:
    """The type under `Optional[...]` and `Annotated[...]`."""
    while True:
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
        elif origin in (typing.Union, types.UnionType):
            args = [a for a in typing.get_args(annotation) if a is not type(None)]
            if len(args) != 1:
                return annotation
            annotation = args[0]
        else:
            return annotation


def _parse(annotation: Any, raw: str) -> Any:
    """Environment text as the field's type expects it. Scalars are left to
    pydantic's own conversion; only containers need reading here."""
    kind = _unwrap(annotation)
    origin = typing.get_origin(kind) or kind
    if origin in (list, tuple, set, frozenset):
        text = raw.strip()
        if text.startswith("["):
            return json.loads(text)
        return [item.strip() for item in text.split(",") if item.strip()]
    if origin is dict or (isinstance(origin, type) and issubclass(origin, BaseModel)):
        return json.loads(raw)
    return raw


def _is_secret(annotation: Any) -> bool:
    kind = _unwrap(annotation)
    return isinstance(kind, type) and issubclass(kind, SecretStr | SecretBytes)


def _type_name(annotation: Any) -> str:
    kind = _unwrap(annotation)
    optional = kind is not annotation and typing.get_origin(annotation) in (
        typing.Union, types.UnionType)
    name = getattr(kind, "__name__", None) if typing.get_origin(kind) is None else None
    text = name or str(kind).replace("typing.", "")
    return f"{text} | None" if optional else text


def _shown(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)
