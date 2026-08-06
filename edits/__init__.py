"""Edit definitions for the env-driven fingerprint override patch.

Each module exposes ``edits(ctx)`` returning a list of :class:`Edit`.  ``ctx``
carries the API flavours detected in the target checkout (see
``detect_api_flavors``), because a few base/ APIs were renamed recently and the
generated code has to match whichever spelling the tree actually uses.
"""

import dataclasses
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Edit:
    """A single anchored replacement.

    ``anchor`` is a verbatim substring quoted from real Chromium source.  It
    must occur exactly once in the file; anything else is an error rather than
    a guess.  ``marker`` is a sentinel present in ``replacement`` that makes
    re-running the applier a no-op.
    """

    path: str
    anchor: str
    replacement: str
    marker: str
    why: str = ""


MARKER_PREFIX = "ENV_FP"


def detect_api_flavors(src: Path) -> dict:
    """Sniff the base/ APIs whose spelling changed recently.

    Older milestones use ``bool GetVar(std::string_view, std::string*)`` and
    ``base::Value::Dict``; current main uses ``std::optional<std::string>
    GetVar(...)`` and ``base::DictValue``.  Guessing wrong produces a build
    error, so read it out of the tree instead.

    ``WTF::String::FromUTF8`` is sniffed the same way: it was renamed to
    ``FromUtf8``, and on trees where the byte-span overload is the only one
    left a ``std::string`` argument no longer converts and has to be wrapped.
    """
    ctx = {}

    env_h = (src / "base" / "environment.h").read_text(errors="replace")
    ctx["optional_getvar"] = "std::optional<std::string> GetVar" in env_h

    values_h = (src / "base" / "values.h").read_text(errors="replace")
    # The rename introduced top-level base::DictValue / base::ListValue. Match
    # the class definition rather than a mention, and allow for the attribute
    # macros between the keyword and the name ("class BASE_EXPORT GSL_OWNER
    # DictValue {").
    ctx["renamed_value_types"] = bool(
        re.search(r"^class\s+[\w\s]*\bDictValue\s*\{", values_h, re.MULTILINE)
    )
    ctx["dict_type"] = "base::DictValue" if ctx["renamed_value_types"] else "base::Value::Dict"
    ctx["list_type"] = "base::ListValue" if ctx["renamed_value_types"] else "base::Value::List"

    json_h = (src / "base" / "json" / "json_reader.h").read_text(errors="replace")
    ctx["has_read_dict"] = "ReadDict(" in json_h

    wtf_string_h = (src / "third_party" / "blink" / "renderer" / "platform" /
                    "wtf" / "text" / "wtf_string.h").read_text(errors="replace")
    declarations = _FROM_UTF8_DECL.findall(wtf_string_h)
    if not declarations:
        raise RuntimeError(
            "no String::FromUtf8/FromUTF8 declaration found in "
            "third_party/blink/renderer/platform/wtf/text/wtf_string.h"
        )
    ctx["from_utf8_name"] = declarations[0][0]
    # An overload taking a string-like parameter accepts a std::string as-is.
    # Where the byte-span overload is the only one, the argument has to be
    # converted explicitly -- std::string does not convert to a span of
    # uint8_t, because char* does not convert to unsigned char*.
    ctx["from_utf8_needs_span"] = not any(
        "string_view" in parameters or "std::string" in parameters
        for _, parameters in declarations
    )

    span_h = (src / "base" / "containers" / "span.h").read_text(errors="replace")
    ctx["has_as_byte_span"] = "as_byte_span(" in span_h

    return ctx


# The edit modules are written against one canonical spelling; every call is
# rewritten below to whatever the target tree actually provides. Arguments are
# simple dereferences, so a paren-free match is enough.
_FROM_UTF8_DECL = re.compile(r"static\s+String\s+(FromUtf8|FromUTF8)\s*\(([^)]*)\)")
_FROM_UTF8_CALL = re.compile(r"String::FromUTF8\(([^()]*)\)")


def spell_from_utf8(text: str, ctx: dict) -> str:
    """Rewrite ``String::FromUTF8(x)`` into this tree's spelling of it."""
    name = ctx["from_utf8_name"]
    if name == "FromUTF8" and not ctx["from_utf8_needs_span"]:
        return text
    if ctx["from_utf8_needs_span"]:
        replacement = f"String::{name}(base::as_byte_span(\\1))"
    else:
        replacement = f"String::{name}(\\1)"
    return _FROM_UTF8_CALL.sub(replacement, text)


def collect_edits(ctx: dict) -> list:
    from . import shared, ua, navigator, webgl, webgpu, media, propagate

    out = []
    for module in (shared, ua, navigator, webgl, webgpu, media, propagate):
        for edit in module.edits(ctx):
            out.append(dataclasses.replace(
                edit,
                replacement=spell_from_utf8(edit.replacement, ctx),
                marker=spell_from_utf8(edit.marker, ctx),
            ))
    return out
