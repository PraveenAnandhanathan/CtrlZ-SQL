"""Loading a rulebook from YAML.

Validation here is strict on purpose. An unknown key in a safety rule is an
error, never a shrug: the failure mode of a lenient loader is a rule that
silently does nothing, which is the worst possible outcome for a file whose
entire job is to stop mistakes.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Iterable, Optional

from ..errors import ConfigError
from .model import (
    ACTIONS,
    CONDITION_FIELDS,
    Condition,
    Policy,
    Rule,
    Scope,
    coerce_action,
)

#: File names searched for, in order, walking up from the working directory.
POLICY_FILENAMES = ("ctrlz.policy.yaml", "ctrlz.policy.yml", ".ctrlz.policy.yaml")

DEFAULTS_PATH = pathlib.Path(__file__).with_name("defaults.yaml")

SUPPORTED_VERSION = 1

_TOP_LEVEL_KEYS = {"version", "defaults", "rules"}
_DEFAULTS_KEYS = {"risk_threshold", "block_on_risk"}
_RULE_KEYS = {"name", "when", "action", "risk", "message", "scope"}
_SCOPE_KEYS = {"environments", "actors"}


def find_policy_file(start: Optional[str] = None) -> Optional[pathlib.Path]:
    """Locate the nearest policy file, or None to use the built-in defaults.

    ``$CTRLZ_POLICY`` wins if set; otherwise we walk up from ``start`` so a
    repository's rulebook applies to everything inside it.
    """
    override = os.environ.get("CTRLZ_POLICY")
    if override:
        path = pathlib.Path(override).expanduser()
        if not path.is_file():
            raise ConfigError(f"CTRLZ_POLICY points at {path}, which does not exist")
        return path

    current = pathlib.Path(start or os.getcwd()).resolve()
    for directory in (current, *current.parents):
        for name in POLICY_FILENAMES:
            candidate = directory / name
            if candidate.is_file():
                return candidate
    return None


def load_policy(path: Optional[str | pathlib.Path] = None) -> Policy:
    """Load a policy, falling back to the shipped defaults."""
    if path is None:
        found = find_policy_file()
        return load_file(found) if found else load_defaults()
    return load_file(pathlib.Path(path))


def load_defaults() -> Policy:
    policy = load_file(DEFAULTS_PATH)
    policy.source = "<built-in defaults>"
    return policy


def load_file(path: pathlib.Path) -> Policy:
    import yaml

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML -- {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc

    policy = parse(raw, source=str(path))
    return policy


def parse(raw: Any, source: str = "<memory>") -> Policy:
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: expected a mapping at the top level")

    _reject_unknown(raw, _TOP_LEVEL_KEYS, source, "top level")

    version = raw.get("version", SUPPORTED_VERSION)
    if version != SUPPORTED_VERSION:
        raise ConfigError(
            f"{source}: policy version {version!r} is not supported by this "
            f"version of ctrlz (expected {SUPPORTED_VERSION})"
        )

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError(f"{source}: 'defaults' must be a mapping")
    _reject_unknown(defaults, _DEFAULTS_KEYS, source, "defaults")

    rules_raw = raw.get("rules") or []
    if not isinstance(rules_raw, list):
        raise ConfigError(f"{source}: 'rules' must be a list")

    rules = [_parse_rule(entry, source, index) for index, entry in enumerate(rules_raw)]
    _reject_duplicates(rules, source)

    return Policy(
        rules=rules,
        risk_threshold=_int(defaults.get("risk_threshold", 70), "risk_threshold", source),
        block_on_risk=bool(defaults.get("block_on_risk", False)),
        version=version,
        source=source,
    )


# -- rule parsing ----------------------------------------------------------


def _parse_rule(entry: Any, source: str, index: int) -> Rule:
    where = f"{source}: rules[{index}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{where} must be a mapping")

    name = entry.get("name")
    if not name or not isinstance(name, str):
        raise ConfigError(f"{where} needs a 'name'")
    where = f"{source}: rule '{name}'"

    _reject_unknown(entry, _RULE_KEYS, where, "rule")

    try:
        action = coerce_action(entry.get("action"))
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from exc

    risk = _int(entry.get("risk", 0), "risk", where)
    if not 0 <= risk <= 100:
        raise ConfigError(f"{where}: risk must be between 0 and 100, not {risk}")

    return Rule(
        name=name,
        when=_parse_condition(entry.get("when") or {}, where),
        action=action,
        risk=risk,
        message=(entry.get("message") or name).strip(),
        scope=_parse_scope(entry.get("scope") or {}, where),
    )


def _parse_condition(raw: Any, where: str) -> Condition:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: 'when' must be a mapping")
    _reject_unknown(raw, CONDITION_FIELDS, where, "condition")

    return Condition(
        statement=_upper_tuple(raw.get("statement")),
        kind=_lower_tuple(raw.get("kind")),
        has_filter=_optional_bool(raw.get("has_filter"), "has_filter", where),
        filter_is_tautology=_optional_bool(
            raw.get("filter_is_tautology"), "filter_is_tautology", where
        ),
        unfiltered=_optional_bool(raw.get("unfiltered"), "unfiltered", where),
        tables=_tuple(raw.get("tables")),
        writes_untracked=_optional_bool(
            raw.get("writes_untracked"), "writes_untracked", where
        ),
        max_confidence=_optional_float(raw.get("max_confidence"), "max_confidence", where),
        min_confidence=_optional_float(raw.get("min_confidence"), "min_confidence", where),
    )


def _parse_scope(raw: Any, where: str) -> Scope:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: 'scope' must be a mapping")
    _reject_unknown(raw, _SCOPE_KEYS, where, "scope")
    return Scope(
        environments=_tuple(raw.get("environments")),
        actors=_tuple(raw.get("actors")),
    )


# -- validation helpers ----------------------------------------------------


def _reject_unknown(raw: dict, allowed: Iterable[str], where: str, what: str) -> None:
    allowed = set(allowed)
    unknown = sorted(set(raw) - allowed)
    if unknown:
        suggestion = _closest(unknown[0], allowed)
        hint = f" (did you mean '{suggestion}'?)" if suggestion else ""
        raise ConfigError(
            f"{where}: unknown {what} field{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(repr(u) for u in unknown)}{hint}. "
            f"Known fields: {', '.join(sorted(allowed))}"
        )


def _reject_duplicates(rules: list[Rule], source: str) -> None:
    seen: set[str] = set()
    for rule in rules:
        if rule.name in seen:
            raise ConfigError(
                f"{source}: rule '{rule.name}' is defined more than once; "
                f"the later one would silently shadow the earlier"
            )
        seen.add(rule.name)


def _closest(word: str, options: Iterable[str]) -> Optional[str]:
    import difflib

    matches = difflib.get_close_matches(word, list(options), n=1, cutoff=0.7)
    return matches[0] if matches else None


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def _upper_tuple(value: Any) -> tuple[str, ...]:
    return tuple(v.upper() for v in _tuple(value))


def _lower_tuple(value: Any) -> tuple[str, ...]:
    return tuple(v.lower() for v in _tuple(value))


def _optional_bool(value: Any, field: str, where: str) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{where}: '{field}' must be true or false, not {value!r}")


def _optional_float(value: Any, field: str, where: str) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: '{field}' must be a number, not {value!r}") from exc


def _int(value: Any, field: str, where: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}: '{field}' must be a whole number, not {value!r}") from exc
