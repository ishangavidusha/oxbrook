"""Static files.

    app.static("/assets", "frontend/dist/assets", cache_control="max-age=31536000, immutable")
    app.static("/", "frontend/dist", fallback="index.html")

Served in Rust. A file request never wakes a Python worker, never counts against
`max_concurrency`, and is streamed from disk rather than read into memory.
Middleware does not run for it: a file that needs authorisation belongs behind a
route, not in a static directory.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StaticMount:
    prefix: str
    directory: str
    index: str | None
    fallback: str | None
    dotfiles: bool
    cache_control: str | None

    def as_spec(self) -> tuple:
        return (self.prefix, self.directory, self.index, self.fallback, self.dotfiles,
                self.cache_control)

    def shapes(self) -> list[str]:
        """Route shapes this mount occupies, for conflict checks against routes."""
        if self.prefix == "/":
            return ["/", "/{}"]
        return [self.prefix, f"{self.prefix}/{{}}"]


def _plain_name(value: str, what: str) -> str:
    if not value or value in (".", "..") or "/" in value or "\\" in value:
        raise ValueError(f"{what} {value!r} must be a file name, not a path")
    return value


def build_mount(
    prefix: str,
    directory: str | Path,
    *,
    index: str | None,
    fallback: str | None,
    dotfiles: bool,
    cache_control: str | None,
) -> StaticMount:
    if prefix != "/" and (not prefix.startswith("/") or prefix.endswith("/")):
        raise ValueError(
            f"static prefix {prefix!r} must start with '/' and not end with one, "
            f"like '/assets', or be '/'"
        )
    if "{" in prefix or "}" in prefix:
        raise ValueError(f"static prefix {prefix!r} cannot contain path parameters")
    root = Path(directory).resolve()
    if not root.is_dir():
        raise ValueError(
            f"static directory {str(directory)!r} does not exist (resolved to {root}); "
            f"relative paths resolve from the working directory, so "
            f"Path(__file__).parent / 'static' is safer"
        )
    if index is not None:
        _plain_name(index, "index")
    if fallback is not None:
        candidate = (root / fallback).resolve()
        if root not in candidate.parents or not candidate.is_file():
            raise ValueError(f"fallback {fallback!r} is not a file inside {root}")
    if cache_control is not None and ("\r" in cache_control or "\n" in cache_control):
        raise ValueError("cache_control cannot contain a line break")
    return StaticMount(prefix, str(root), index, fallback, dotfiles, cache_control)
