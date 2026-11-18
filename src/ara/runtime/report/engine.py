"""Hand-rolled mini template engine — SSTI-safe.

Grammar (deliberately tiny):

- ``{{ name }}`` or ``{{ a.b.c }}`` — substitute a dotted-path lookup
  in the rendering context. Missing keys render as empty string.
- ``{% for x in items %}...{% endfor %}`` — iterate. ``items`` is a
  dotted-path reference that must resolve to a list. Inside the body,
  ``x`` (single identifier) is bound to the loop variable.
- ``{% if name %}...{% endif %}`` — render the body iff the dotted
  path resolves to a truthy value. No ``elif``/``else`` in v1.
- ``{# comment #}`` — stripped at render time.

Explicitly NOT supported:
- Method calls, attribute calls, function calls of any kind.
- Arbitrary expressions; the only operator is ``in`` inside ``for``.
- Filters, pipes, format specs.
- File includes / extends.
- Macros.
- Anything that touches ``__class__``, ``__globals__``, or any
  Python builtin via the context.

Values flow exclusively through :func:`_resolve_path`, which walks a
dotted path on dict-like or attribute-like objects but never invokes
callables. This is the SSTI defence: there is no path from a malicious
context value into Python execution.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class TemplateError(Exception):
    pass


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOTTED_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _resolve_path(path: str, ctx: dict) -> Any:
    """Walk a dotted path on a context dict / nested dicts. Never
    invokes callables. Missing keys → returns None."""
    if not _DOTTED_RE.match(path):
        raise TemplateError(f"invalid identifier path: {path!r}")
    parts = path.split(".")
    cur: Any = ctx
    for p in parts:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            try:
                cur = getattr(cur, p)
            except AttributeError:
                return None
        # SSTI defence: refuse to walk into or render callables.
        # Applies whether the value came from dict.get or getattr.
        if callable(cur):
            raise TemplateError(
                f"path {path!r} resolves to a callable; refused"
            )
    return cur


# ---------- tokenizer ----------

@dataclass(frozen=True)
class _Token:
    kind: str   # "TEXT" | "EXPR" | "TAG"
    body: str


_TOKEN_RE = re.compile(
    r"(?P<expr>\{\{.*?\}\})|(?P<tag>\{%.*?%\})|(?P<comment>\{#.*?#\})",
    re.DOTALL,
)


def _tokenize(src: str) -> list[_Token]:
    out: list[_Token] = []
    pos = 0
    for m in _TOKEN_RE.finditer(src):
        if m.start() > pos:
            out.append(_Token("TEXT", src[pos:m.start()]))
        if m.group("expr"):
            out.append(_Token("EXPR", m.group("expr")[2:-2].strip()))
        elif m.group("tag"):
            out.append(_Token("TAG", m.group("tag")[2:-2].strip()))
        # comment: drop entirely
        pos = m.end()
    if pos < len(src):
        out.append(_Token("TEXT", src[pos:]))
    return out


# ---------- AST ----------

@dataclass(frozen=True)
class TextNode:
    text: str


@dataclass(frozen=True)
class ExprNode:
    path: str


@dataclass(frozen=True)
class ForNode:
    var: str
    iter_path: str
    body: list[Any]


@dataclass(frozen=True)
class IfNode:
    cond_path: str
    body: list[Any]


# ---------- parser ----------

_FOR_RE = re.compile(r"^for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+(.+)$")
_END_RE = re.compile(r"^end(for|if)$")
_IF_RE = re.compile(r"^if\s+(.+)$")


def _parse(tokens: list[_Token]) -> list[Any]:
    pos = [0]

    def parse_block(end_tag: str | None) -> list[Any]:
        nodes: list[Any] = []
        while pos[0] < len(tokens):
            tok = tokens[pos[0]]
            if tok.kind == "TEXT":
                nodes.append(TextNode(tok.body))
                pos[0] += 1
                continue
            if tok.kind == "EXPR":
                nodes.append(ExprNode(tok.body))
                pos[0] += 1
                continue
            # TAG
            body = tok.body
            m = _END_RE.match(body)
            if m:
                if end_tag is None or m.group(1) != end_tag:
                    raise TemplateError(
                        f"unexpected {{% {body} %}} (expected end{end_tag})"
                        if end_tag else f"unexpected {{% {body} %}}"
                    )
                pos[0] += 1
                return nodes
            m = _FOR_RE.match(body)
            if m:
                var, iter_path = m.group(1), m.group(2).strip()
                if not _DOTTED_RE.match(iter_path):
                    raise TemplateError(
                        f"for-iter must be a dotted identifier, got {iter_path!r}"
                    )
                pos[0] += 1
                inner = parse_block("for")
                nodes.append(ForNode(var=var, iter_path=iter_path, body=inner))
                continue
            m = _IF_RE.match(body)
            if m:
                cond = m.group(1).strip()
                if not _DOTTED_RE.match(cond):
                    raise TemplateError(
                        f"if-cond must be a dotted identifier, got {cond!r}"
                    )
                pos[0] += 1
                inner = parse_block("if")
                nodes.append(IfNode(cond_path=cond, body=inner))
                continue
            raise TemplateError(f"unknown tag: {{% {body} %}}")
        if end_tag is not None:
            raise TemplateError(f"unterminated block (missing {{% end{end_tag} %}})")
        return nodes

    nodes = parse_block(None)
    return nodes


# ---------- renderer ----------

def _format_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return ", ".join(_format_value(x) for x in v)
    return str(v)


def _render_nodes(nodes: list[Any], ctx: dict) -> str:
    out: list[str] = []
    for n in nodes:
        if isinstance(n, TextNode):
            out.append(n.text)
        elif isinstance(n, ExprNode):
            out.append(_format_value(_resolve_path(n.path, ctx)))
        elif isinstance(n, ForNode):
            iterable = _resolve_path(n.iter_path, ctx)
            if iterable is None:
                continue
            if not isinstance(iterable, list):
                raise TemplateError(
                    f"for-loop expects a list at {n.iter_path!r}, "
                    f"got {type(iterable).__name__}"
                )
            for item in iterable:
                inner_ctx = dict(ctx)
                inner_ctx[n.var] = item
                out.append(_render_nodes(n.body, inner_ctx))
        elif isinstance(n, IfNode):
            if _resolve_path(n.cond_path, ctx):
                out.append(_render_nodes(n.body, ctx))
        else:
            raise TemplateError(f"unknown node type: {type(n).__name__}")
    return "".join(out)


# ---------- public ----------

def render(template_src: str, ctx: dict) -> str:
    """Render a template source against a context dict. Pure function.

    Raises :class:`TemplateError` on syntax errors, unknown tags,
    invalid identifier paths, or non-list for-iter values.
    """
    tokens = _tokenize(template_src)
    nodes = _parse(tokens)
    return _render_nodes(nodes, ctx)
