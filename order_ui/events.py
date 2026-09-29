"""The checked-in JSON Schema is also the consumer's runtime contract."""

from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_PATH = Path(__file__).parent / "schema" / "order-status.v1.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
STATUSES = SCHEMA["properties"]["status"]["enum"]


@dataclass(frozen=True)
class OrderEvent:
    payload: dict
    created_at: datetime
    updated_at: datetime


def parse_order_message(raw: bytes) -> OrderEvent:
    if len(raw) > 65536:
        raise ValueError("Order snapshot exceeds 64 KiB.")
    payload = json.loads(raw)
    error = next(VALIDATOR.iter_errors(payload), None)
    if error is not None:
        # Do not log arbitrary producer data (or jsonschema's full payload dump).
        field = ".".join(map(str, error.absolute_path)) or "snapshot"
        raise ValueError(f"Invalid {field}: schema rule {error.validator}.")
    for value in payload.values():
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Numeric values must be finite.")
    created = datetime.fromisoformat(payload["created_at"].upper().replace("Z", "+00:00"))
    updated = datetime.fromisoformat(payload["updated_at"].upper().replace("Z", "+00:00"))
    if created.utcoffset() is None or updated.utcoffset() is None or updated < created:
        raise ValueError("Timestamps require a UTC offset and updated_at >= created_at.")
    quantity, filled = payload["quantity"], payload["filled_quantity"]
    if quantity is not None and filled > quantity:
        raise ValueError("filled_quantity cannot exceed quantity.")
    if payload["status"] == "FILLED" and (filled <= 0 or (quantity is not None and filled != quantity)):
        raise ValueError("FILLED requires the entire quantity to be filled.")
    if payload["status"] == "PARTIALLY_FILLED" and (filled <= 0 or (quantity is not None and filled >= quantity)):
        raise ValueError("PARTIALLY_FILLED requires a positive, incomplete fill.")
    return OrderEvent(payload, created, updated)
