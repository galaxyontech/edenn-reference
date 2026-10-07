"""Reject a tool call that would crash or silently do the wrong thing.

The decision schema is advisory — it ships with ``strict: False``, so nothing
upstream guarantees the model's ``tool_args`` have the shapes the tools expect.
Each tool then coerces defensively on its own, and the coercions disagree about
what a mistake is. Three outcomes exist today, and only the first is acceptable:

* a clean refusal the model can repair (``_coerce_mix_param`` is the good
  example — non-numeric, NaN and Inf all raise, in range or not at all),
* an uncaught ``TypeError`` that becomes a 500 (``layers: 5`` iterates an int;
  ``count: {}`` int()s a dict),
* a SILENT wrong answer, which is the worst of the three because nothing
  anywhere reports it: ``start_s: "soon"`` puts an effect meant for 0:12 on the
  first frame, ``allow_overlap: "false"`` is truthy and walks straight through
  the narration-collision gate, ``force_music: "false"`` re-injects music into a
  plan that asked for none.

This module is the missing half of ``_coerce_mix_param``: the same standard,
applied to the arguments that never got it. It is deliberately narrow — it
covers the values that crash or misfire, not every field a tool reads.

Two rules constrain the implementation:

1. **It never mutates.** The spend guard fingerprints ``tool_args`` verbatim
   (``generate_sfx`` reads no arguments at all, yet a stray key still changes its
   fingerprint), so normalising here would quietly change which repeat calls
   count as duplicates.
2. **It never widens a refusal.** Unknown keys stay allowed, because tools ignore
   them today; documented silent fallbacks stay silent (``mode: "nonsense"``
   really is meant to become ``music_first``). Only genuine mistakes are named.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..models import ArgField

# Strings a model reaches for when it means a boolean. Python calls every one of
# them True, which is how "false" came to mean "yes".
_BOOLISH = frozenset({"true", "false", "yes", "no", "0", "1", "on", "off", "none", "null"})


class ToolArgsInvalid(ValueError):
    """A tool call whose arguments are wrong in a way worth naming.

    A ValueError so the deterministic ``/choices`` path keeps returning 400 — a
    malformed card submission IS a bad request. On the model's path the loop
    catches it and hands ``instruction`` back as a tool result, so the next step
    can fix the call instead of the turn dying.
    """

    def __init__(self, tool_name: str, field: str, problem: str, *, instruction: str = "") -> None:
        message = f"{tool_name}: {field} {problem}"
        super().__init__(message)
        self.tool_name = tool_name
        self.field = field
        self.instruction = instruction or f"Fix {field} and call {tool_name} again."


def _reject(tool: str, field: str, problem: str, instruction: str = "") -> None:
    raise ToolArgsInvalid(tool, field, problem, instruction=instruction)


def _check_number(
    tool: str,
    field: str,
    value: Any,
    *,
    allow_negative: bool = True,
    max_value: Optional[float] = None,
) -> None:
    """Accept what float() accepts today (numeric strings included); refuse the rest."""

    # Absent and empty mean the same thing to every consuming tool
    # (``float(args.get("speed") or 1.0)``), so refusing "" would newly reject a
    # call that has always meant "not supplied".
    if value is None or value == "":
        return
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        _reject(
            tool, field, f"must be a number, not {type(value).__name__}.",
            f"Pass {field} as a number, e.g. 12.5.",
        )
    try:
        num = float(value)
    except (TypeError, ValueError):
        _reject(
            tool, field, f"must be a number (got {value!r}).",
            f"Pass {field} as a number, e.g. 12.5.",
        )
        return
    if not math.isfinite(num):
        _reject(
            tool, field, "must be a finite number.",
            f"Pass a real value for {field} — infinity and NaN cannot be rendered.",
        )
    if not allow_negative and num < 0:
        _reject(
            tool, field, "cannot be negative.", f"Pass {field} as zero or more.",
        )
    # The one rule here that WIDENS a refusal, and deliberately: the fields that
    # carry a bound are the ones whose value reaches a paid render with nothing
    # downstream to clamp it. "30000" is not a request the product can mean.
    if max_value is not None and num > max_value:
        _reject(
            tool, field, f"cannot be more than {max_value:g} (got {num:g}).",
            f"Pass {field} as {max_value:g} or less.",
        )


def _check_bool(tool: str, field: str, value: Any) -> None:
    """A boolean must BE a boolean.

    Every bool-ish string is truthy in Python, so "false" here reads as yes — and
    these particular flags turn gates off. Better to be asked again.
    """

    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str) and value.strip().lower() in _BOOLISH:
        _reject(
            tool, field, f"must be true or false, not the text {value!r}.",
            f'Pass {field} as a JSON boolean (true / false), not a quoted string.',
        )
    if not isinstance(value, (int, float)):
        _reject(
            tool, field, f"must be true or false, not {type(value).__name__}.",
            f"Pass {field} as a JSON boolean (true / false).",
        )


def _check_list(tool: str, field: str, value: Any, *, of_dicts: bool = False) -> None:
    if not value:
        return
    if not isinstance(value, list):
        _reject(
            tool, field, f"must be a list, not {type(value).__name__}.",
            f"Pass {field} as a JSON array.",
        )
    if of_dicts:
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                _reject(
                    tool, f"{field}[{index}]",
                    f"must be an object, not {type(item).__name__}.",
                    f"Every entry in {field} is an object with its own fields.",
                )


def _check_enum(tool: str, field: str, value: Any, allowed: Iterable[str]) -> None:
    if value is None or value == "":
        return
    options = sorted(str(option) for option in allowed)
    if not isinstance(value, str) or value not in options:
        _reject(
            tool, field, f"must be one of {', '.join(options)} (got {value!r}).",
            f"Choose {field} from: {', '.join(options)}.",
        )


def _check_string(tool: str, field: str, value: Any) -> None:
    """A name must BE a name.

    Only ever applied to values a tool uses as an identifier — a take id, a
    layer name. Everything else it reads as text is left alone, because
    ``str(x)`` accepts anything and the tools that do that are not wrong to.
    """

    if value is None or isinstance(value, str):
        return
    _reject(
        tool, field, f"must be a name, not {type(value).__name__}.",
        f"Pass {field} as a string.",
    )


def _check_dict(tool: str, field: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        _reject(
            tool, field, f"must be an object, not {type(value).__name__}.",
            f"Pass {field} as a JSON object.",
        )


def _check_field(tool: str, field: str, value: Any, spec: "ArgField") -> None:
    """Apply one declared rule. Only ever refuses; never rewrites."""

    kind = spec.kind
    if kind in ("number", "int"):
        _check_number(
            tool, field, value,
            allow_negative=spec.allow_negative,
            max_value=spec.max_value,
        )
        if kind == "int" and value not in (None, "") and not isinstance(value, bool):
            try:
                # The tool coerces with int(), not float(), so "2.5" still dies
                # inside it — refuse here where the message can say why.
                if float(value) != int(float(value)):
                    _reject(
                        tool, field, f"must be a whole number (got {value!r}).",
                        f"Pass {field} as an integer, e.g. 2.",
                    )
            except (TypeError, ValueError):
                pass  # already reported by _check_number
    elif kind == "bool":
        _check_bool(tool, field, value)
    elif kind == "enum":
        _check_enum(tool, field, value, spec.enum)
    elif kind == "string":
        _check_string(tool, field, value)
    elif kind == "dict":
        _check_dict(tool, field, value)
    elif kind in ("list", "list_of_dicts"):
        _check_list(tool, field, value, of_dicts=(kind == "list_of_dicts"))
        for index, item in enumerate(value or []):
            for nested in spec.nested:
                if not nested.enforced:
                    continue
                if nested.name:
                    # A field inside each entry. The container's own type was
                    # just checked, so a non-dict entry is skipped rather than
                    # re-reported: tools keep the entries that parsed.
                    if isinstance(item, dict) and nested.name in _present_keys(item):
                        _check_field(
                            tool, f"{field}[{index}].{nested.name}",
                            item.get(nested.name), nested,
                        )
                else:
                    # The element itself is the value (a list of ids).
                    _check_field(tool, f"{field}[{index}]", item, nested)


def _present_keys(item: Mapping[str, Any]) -> set[str]:
    """Keys an entry actually carries, so an absent field is not invented."""

    return set(item.keys())


def validate_tool_args(tool_name: str, args: Optional[Mapping[str, Any]]) -> None:
    """Raise :class:`ToolArgsInvalid` when a call cannot mean what it says.

    The rules are DERIVED from the tool table rather than written out here, so a
    new tool cannot arrive without them: ``ToolSpec`` refuses to construct
    unless it has said what it reads. This function used to be a chain of
    hand-written branches covering six of fourteen tools, and the two most
    recently added were among the eight it missed.

    Runs before the spend guard records anything, so a refused call leaves no
    fingerprint behind to reject the corrected retry as a duplicate.
    """

    if not isinstance(args, Mapping) or not args:
        return

    from ..models import TOOL_SPECS_BY_NAME

    spec = TOOL_SPECS_BY_NAME.get(tool_name)
    if spec is None:
        # An unknown tool is the registry's refusal to make, not ours.
        return

    for field in spec.args:
        if not field.enforced:
            continue
        # Absent means absent. Many tools read `args.get(x) or default`, so a
        # missing key is the normal case and never a mistake.
        if field.name not in args:
            continue
        _check_field(tool_name, field.name, args.get(field.name), field)


__all__ = ["ToolArgsInvalid", "validate_tool_args"]
