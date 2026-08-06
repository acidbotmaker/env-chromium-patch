"""Edit definitions for the env-driven fingerprint override patch.

Each module exposes ``edits(ctx)`` returning a list of :class:`Edit`.  ``ctx``
carries the API flavours detected in the target checkout (see
``detect_api_flavors``), because a few base/ APIs were renamed recently and the
generated code has to match whichever spelling the tree actually uses.
"""

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

    return ctx


def collect_edits(ctx: dict) -> list:
    from . import shared, ua, navigator, webgl, webgpu, media, propagate

    out = []
    for module in (shared, ua, navigator, webgl, webgpu, media, propagate):
        out.extend(module.edits(ctx))
    return out
