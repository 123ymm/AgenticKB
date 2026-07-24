from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_thaw_value(item) for item in value), key=str)
    return value


class SlotType(str, Enum):
    INPUT_SPEC = "INPUT_SPEC"
    RAW_FILE_BATCH = "RAW_FILE_BATCH"
    DOCUMENT_BATCH = "DOCUMENT_BATCH"
    FINALIZE_INPUT = "FINALIZE_INPUT"
    FINALIZE_RESULT = "FINALIZE_RESULT"


class ExecutionZone(str, Enum):
    INPUT = "input"
    DOCUMENT = "document"
    GLOBAL = "global"


class EditPolicy(str, Enum):
    FIXED = "fixed"
    PROTECTED = "protected"
    EDITABLE = "editable"


class ErrorPolicy(str, Enum):
    FAIL_FAST = "FAIL_FAST"
    SKIP_DOCUMENT = "SKIP_DOCUMENT"
    SKIP_WITH_EMPTY = "SKIP_WITH_EMPTY"
    FALLBACK = "FALLBACK"
    PAUSE_FOR_REVIEW = "PAUSE_FOR_REVIEW"


@dataclass(frozen=True)
class SlotDecl:
    name: str
    type: SlotType
    required: bool = True
    variadic: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type.value,
            "required": self.required,
            "variadic": self.variadic,
            "description": self.description,
        }


@dataclass(frozen=True)
class MiningOperatorDef:
    type: str
    version: str
    display_name: str
    description: str
    category: str
    zone: ExecutionZone
    edit_policy: EditPolicy
    input_slots: tuple[SlotDecl, ...]
    output_slots: tuple[SlotDecl, ...]
    requires: frozenset[str] = frozenset()
    provides: frozenset[str] = frozenset()
    param_schema_json: Mapping[str, Any] = field(default_factory=dict)
    error_policy: ErrorPolicy = ErrorPolicy.FAIL_FAST
    unique: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_slots", tuple(self.input_slots))
        object.__setattr__(self, "output_slots", tuple(self.output_slots))
        object.__setattr__(self, "requires", frozenset(self.requires))
        object.__setattr__(self, "provides", frozenset(self.provides))
        object.__setattr__(self, "param_schema_json", _freeze_value(self.param_schema_json))

    def input_slot(self, name: str) -> SlotDecl | None:
        return next((slot for slot in self.input_slots if slot.name == name), None)

    def output_slot(self, name: str) -> SlotDecl | None:
        return next((slot for slot in self.output_slots if slot.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "version": self.version,
            "displayName": self.display_name,
            "description": self.description,
            "category": self.category,
            "zone": self.zone.value,
            "editPolicy": self.edit_policy.value,
            "inputSlots": [slot.to_dict() for slot in self.input_slots],
            "outputSlots": [slot.to_dict() for slot in self.output_slots],
            "requires": sorted(self.requires),
            "provides": sorted(self.provides),
            "paramSchemaJson": _thaw_value(self.param_schema_json),
            "errorPolicy": self.error_policy.value,
            "unique": self.unique,
        }
