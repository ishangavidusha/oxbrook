"""Write the site as Markdown for language models: llms.txt, llms-full.txt and
a `.md` beside every page.

A coding agent that has the documentation builds a correct Oxbrook app; one
that does not writes FastAPI with an Oxbrook import. These files put the whole
site in reach of one fetch. They are generated from the same `www/` sources and
the same docstrings as the HTML, at build time, so they cannot drift from it.

Each page's Markdown is the source file with what only MkDocs understands
rewritten: snippet includes are inlined, admonitions become blockquotes, and a
`::: oxbrook.Name` directive becomes the object's signatures and docstrings, so
the API reference is in the Markdown too. Relative links between pages already
point at `.md` files, and the copies sit at their source paths, so they resolve
there unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

import griffe
from mkdocs.exceptions import PluginError

HERE = Path(__file__).parent
INTRO = HERE / "llms-intro.md"

SNIPPET = re.compile(r'^--8<--\s+"([^"]+)"\s*$', re.M)
DIRECTIVE = re.compile(r"^::: ([\w.]+)\n((?:[ \t]+.*\n|\n(?=[ \t]))*)", re.M)
# What only MkDocs understands; none of it may survive into the Markdown.
LEFTOVER = re.compile(r"^(?:::: |!!! |\?\?\?\+? |=== |--8<--).*$", re.M)
ADMONITION = re.compile(r'^(!!!|\?\?\?\+?) (\w+)(?: "([^"]*)")?\n((?:\n|    .*\n)*)', re.M)


def on_post_build(config) -> None:
    docs = Path(config["docs_dir"])
    site = Path(config["site_dir"])
    root = Path(config["config_file_path"]).parent
    package = _load_package(config)

    pages = []  # (section, src path, title, summary, markdown)
    for section, src in _walk(config["nav"]):
        meta, text = _plain(docs, root, src, package)
        leftover = LEFTOVER.search(text)
        if leftover:
            raise PluginError(f"llms: {src} still has {leftover.group(0)!r} in its Markdown")
        title = _title(text, src)
        pages.append((section, src, title, meta.get("description") or _summary(text), text))
        out = site / src
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    base = config["site_url"] or ""
    intro = INTRO.read_text(encoding="utf-8").replace("{base}", base).rstrip() + "\n"
    index = [intro]
    full = [intro]
    current = None
    for section, src, title, summary, text in pages:
        if section != current:
            current = section
            index.append(f"\n## {section}\n")
        index.append(f"- [{title}]({base}{src})" + (f": {summary}" if summary else ""))
        full.append(f"\n\n<!-- {base}{src} -->\n\n{text.strip()}\n")
    (site / "llms.txt").write_text("\n".join(index) + "\n", encoding="utf-8")
    (site / "llms-full.txt").write_text("".join(full), encoding="utf-8")


def _walk(nav, section: str = "Docs"):
    """Pages in nav order, each with the top-level section it sits under."""
    for item in nav:
        if isinstance(item, str):
            yield section, item
        else:
            ((name, value),) = item.items()
            if isinstance(value, str):
                yield (section if section != "Docs" else name), value
            else:
                yield from _walk(value, name)


FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n+", re.S)


def _split_meta(text: str) -> tuple[dict[str, str], str]:
    """A page's `key: value` front matter, and the page without it."""
    match = FRONT_MATTER.match(text)
    if not match:
        return {}, text
    meta = {}
    for line in match.group(1).split("\n"):
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, text[match.end() :]


def _plain(docs: Path, root: Path, src: str, package) -> tuple[dict[str, str], str]:
    meta, text = _split_meta((docs / src).read_text(encoding="utf-8"))

    def include(match: re.Match) -> str:
        path = root / match.group(1)
        if not path.is_file():
            raise PluginError(f"llms: {src} includes {match.group(1)}, which does not exist")
        return path.read_text(encoding="utf-8")

    text = SNIPPET.sub(include, text)
    text = DIRECTIVE.sub(lambda m: _reference(package, m.group(1), src) + "\n", text)
    return meta, ADMONITION.sub(_quote, text)


def _quote(match: re.Match) -> str:
    kind, title, body = match.group(2), match.group(3), match.group(4)
    label = title or kind.capitalize()
    lines = body.rstrip("\n").split("\n")
    lines = [line[4:] if line.startswith("    ") else line for line in lines]
    while lines and not lines[0].strip():
        lines.pop(0)
    quoted = "\n".join(f"> {line}".rstrip() for line in lines)
    return f"> **{label}**\n>\n{quoted}\n\n"


def _title(text: str, src: str) -> str:
    match = re.search(r"^# (.+)$", text, re.M)
    if not match:
        raise PluginError(f"llms: {src} has no top-level heading")
    return match.group(1).strip()


def _summary(text: str) -> str:
    """The first sentence of the page's first paragraph of prose.

    A page whose opening does not summarise it sets `description:` in its front
    matter instead, which the theme also uses for the HTML meta description.
    """
    in_code = False
    paragraph: list[str] = []
    for line in text.split("\n"):
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        stripped = line.strip()
        if not stripped:
            if paragraph:
                break
            continue
        if line[0].isspace() or stripped[0] in "#>|-*!<:" or stripped[0].isdigit():
            if paragraph:
                break
            continue
        paragraph.append(stripped)
    prose = " ".join(paragraph)
    sentence = re.split(r"(?<=[.:])\s", prose, maxsplit=1)[0]
    return sentence.rstrip(":").rstrip()


# --- the API reference, from docstrings ---------------------------------------


def _load_package(config):
    handler = config["plugins"]["mkdocstrings"].config["handlers"]["python"]
    paths = [str(Path(config["config_file_path"]).parent / p) for p in handler.get("paths", [])]
    loader = griffe.GriffeLoader(
        search_paths=paths, docstring_parser=griffe.Parser.google, allow_inspection=True
    )
    # The Rust half is documented by inspection, which needs it imported
    # before its alias from `oxbrook` can resolve.
    import oxbrook._core  # noqa: F401

    package = loader.load("oxbrook")
    loader.resolve_aliases(external=False, implicit=False)
    return package


def _reference(package, path: str, src: str) -> str:
    name = path.removeprefix("oxbrook.")
    try:
        obj = package[name]
        if obj.is_alias:
            obj = obj.final_target
    except (KeyError, griffe.AliasResolutionError, griffe.CyclicAliasError) as exc:
        raise PluginError(f"llms: {src} documents {path}, which cannot be resolved: {exc}") from exc
    return _render(obj, f"## `{name}`", owner=None)


def _render(obj, heading: str, owner) -> str:
    parts = [heading, ""]
    signature = _signature(obj, owner)
    if signature:
        parts += ["```python", signature, "```", ""]
    if obj.docstring:
        parts += [obj.docstring.value.strip(), ""]
    init = obj.members.get("__init__") if obj.is_class else None
    if init is not None and not init.is_alias and init.docstring:
        parts += [init.docstring.value.strip(), ""]  # as merge_init_into_class does
    if obj.is_class:
        for member in _members(obj):
            parts.append(_render(member, f"### `{obj.name}.{member.name}`", owner=obj))
    if obj.is_module:
        # A module is documented by what it exports, each as its own section.
        names = obj.exports or [n for n in obj.members if not n.startswith("_")]
        for name in names:
            member = obj.members[str(name)]
            if member.is_alias:
                member = member.final_target
            parts.append(_render(member, f"## `{obj.name}.{name}`", owner=None))
    return "\n".join(parts)


def _members(cls):
    for name, member in cls.members.items():
        if name.startswith("_") or member.is_alias:
            continue
        # An attribute is listed only when it is documented: the rest are the
        # constructor's arguments stored on self, already in the signature.
        if member.is_attribute and not member.docstring:
            continue
        if member.is_function or member.is_attribute or member.is_class:
            yield member


def _signature(obj, owner) -> str:
    if obj.is_class:
        init = obj.members.get("__init__")
        params = _params(init, drop_first=True) if init is not None and init.is_function else None
        return f"class {obj.name}({params})" if params else f"class {obj.name}"
    if obj.is_function:
        method = owner is not None and "staticmethod" not in obj.labels
        params = _params(obj, drop_first=method)
        returns = f" -> {obj.returns}" if obj.returns else ""
        prefix = "async def" if "async" in obj.labels else "def"
        if "property" in obj.labels:
            return f"{obj.name}: {obj.returns}" if obj.returns else ""
        return f"{prefix} {obj.name}({params}){returns}"
    if obj.is_attribute:
        annotation = f": {obj.annotation}" if obj.annotation else ""
        value = f" = {obj.value}" if obj.value is not None and len(str(obj.value)) < 60 else ""
        return f"{obj.name}{annotation}{value}" if annotation or value else ""
    return ""


def _params(function, drop_first: bool) -> str:
    kinds = griffe.ParameterKind
    out: list[str] = []
    seen_star = False
    previous = None
    params = list(function.parameters)
    if drop_first and params and params[0].name in ("self", "cls"):
        params = params[1:]
    for p in params:
        if previous is kinds.positional_only and p.kind is not kinds.positional_only:
            out.append("/")
        if p.kind is kinds.keyword_only and not seen_star:
            out.append("*")
            seen_star = True
        text = p.name
        if p.kind is kinds.var_positional:
            text, seen_star = f"*{p.name}", True
        elif p.kind is kinds.var_keyword:
            text = f"**{p.name}"
        if p.annotation is not None:
            text += f": {p.annotation}"
        if p.default is not None and p.kind not in (kinds.var_positional, kinds.var_keyword):
            text += f" = {p.default}" if p.annotation is not None else f"={p.default}"
        out.append(text)
        previous = p.kind
    if previous is kinds.positional_only:
        out.append("/")
    return ", ".join(out)
