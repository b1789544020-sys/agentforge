"""Shared types for the versioned quality evidence contracts."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints

QualityLayer = Literal["output", "tool", "retrieval", "system"]
QUALITY_LAYERS: tuple[QualityLayer, ...] = ("output", "tool", "retrieval", "system")
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("quality timestamps must be UTC RFC 3339 datetimes")
    return value


UtcDatetime = Annotated[datetime, AfterValidator(_utc_datetime)]


class QualityModel(BaseModel):
    """Typed projection: unknown v1 keys are ignored, never persisted by this model."""

    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)


class QualityError(QualityModel):
    category: Literal[
        "provider",
        "timeout",
        "permission",
        "budget",
        "cancelled",
        "tool",
        "retrieval",
        "internal",
    ]
    message: str
    retryable: bool
    step_id: NonEmptyStr | None = None


def ensure_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")


__all__ = [
    "QUALITY_LAYERS",
    "NonEmptyStr",
    "QualityError",
    "QualityLayer",
    "QualityModel",
    "Sha256",
    "UtcDatetime",
    "ensure_unique",
]
