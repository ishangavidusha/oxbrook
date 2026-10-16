"""Form bodies: HTML forms and file uploads.

    @app.post("/profile")
    async def profile(request: Request):
        form = request.form()
        name = form["name"]                # first value of a text field
        tags = form.getlist("tag")          # every value of a repeated field
        avatar = form.get("avatar")         # an UploadFile, or None
        ...

Or bind the fields to a pydantic model, validated into a `422` like a JSON body:

    class Signup(BaseModel):
        email: str
        interests: list[str] = []
        avatar: UploadFile | None = None

    @app.post("/signup")
    async def signup(_: Request, data: Signup = Form()):
        ...

Both `application/x-www-form-urlencoded` and `multipart/form-data` are parsed
in Rust, on the worker thread, only when a handler asks. A file is held in
memory: the whole body is already bounded by `max_body`. For uploads larger
than that, read the raw body incrementally with `BodyStream`.
"""

from collections.abc import Iterator, Mapping
from typing import Any


class UploadFile:
    """One file from a multipart form."""

    __slots__ = ("content_type", "data", "filename", "name")

    def __init__(self, name: str, filename: str, content_type: str | None, data: bytes) -> None:
        self.name = name
        #: As the client sent it. Never use it as a filesystem path unchecked:
        #: it is client input, and may be empty or contain `..` and slashes.
        self.filename = filename
        self.content_type = content_type
        self.data = data

    @property
    def size(self) -> int:
        """Length of the file in bytes."""
        return len(self.data)

    def read(self) -> bytes:
        """The file's contents. Held in memory already: this does not touch the network."""
        return self.data

    def text(self, encoding: str = "utf-8") -> str:
        """The contents decoded as text, UTF-8 unless told otherwise."""
        return self.data.decode(encoding)

    def __repr__(self) -> str:
        return (
            f"UploadFile(name={self.name!r}, filename={self.filename!r}, "
            f"content_type={self.content_type!r}, size={self.size})"
        )

    @classmethod
    def __get_pydantic_core_schema__(cls, _source: Any, _handler: Any) -> Any:
        from pydantic_core import core_schema

        return core_schema.is_instance_schema(cls)

    @classmethod
    def __get_pydantic_json_schema__(cls, _schema: Any, _handler: Any) -> dict:
        return {"type": "string", "format": "binary"}


class FormData(Mapping):
    """Parsed form fields, in the order they arrived.

    Indexing returns the first value for a name, as most forms have one; use
    `getlist` for a field that repeats. Text fields are `str`, files are
    `UploadFile`.
    """

    __slots__ = ("_items",)

    def __init__(self, items: list[tuple[str, Any]]) -> None:
        self._items = items

    @classmethod
    def from_parts(cls, parts: list[tuple]) -> "FormData":
        """Build one from the server's parsed parts: `(name, value)` for a field,
        `(name, filename, content_type, data)` for a file. Called by the
        server; handlers receive the result."""
        items: list[tuple[str, Any]] = []
        for part in parts:
            if len(part) == 2:
                items.append((part[0], part[1]))
            else:
                name, filename, content_type, data = part
                items.append((name, UploadFile(name, filename, content_type, data)))
        return cls(items)

    def __getitem__(self, name: str) -> Any:
        for key, value in self._items:
            if key == name:
                return value
        raise KeyError(name)

    def __iter__(self) -> Iterator[str]:
        seen: dict[str, None] = {}
        for key, _ in self._items:
            seen.setdefault(key, None)
        return iter(seen)

    def __len__(self) -> int:
        return len({key for key, _ in self._items})

    def getlist(self, name: str) -> list[Any]:
        """Every value sent under `name`, in order; an empty list when there is none."""
        return [value for key, value in self._items if key == name]

    def multi_items(self) -> list[tuple[str, Any]]:
        """Every (name, value) pair, repeats included."""
        return list(self._items)

    @property
    def files(self) -> list[UploadFile]:
        """Every uploaded file, whatever field it came in."""
        return [value for _, value in self._items if isinstance(value, UploadFile)]

    def __repr__(self) -> str:
        return f"FormData({self._items!r})"


class Form:
    """Marks a pydantic-model argument as bound from a form body.

        async def signup(_: Request, data: Signup = Form()): ...

    `max_parts` bounds how many fields and files one request may send.
    """

    __slots__ = ("max_parts",)

    def __init__(self, *, max_parts: int = 1000) -> None:
        if max_parts < 1:
            raise ValueError("max_parts must be at least 1")
        self.max_parts = max_parts

    def __repr__(self) -> str:
        return f"Form(max_parts={self.max_parts})"


def has_files(model: Any) -> bool:
    """Whether a model declares an `UploadFile` field, which makes it multipart."""
    import typing

    for info in model.model_fields.values():
        annotation = info.annotation
        candidates = [annotation, *typing.get_args(annotation)]
        if any(c is UploadFile for c in candidates):
            return True
    return False
