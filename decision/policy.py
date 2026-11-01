"""policy.toml. A bad file stops the process and names the key."""

import tomllib
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_ROOT = Path(__file__).resolve().parents[1]
_REQUIRED_FIELD_NAMES = ("vendor", "due_date", "line_items", "total")


class PolicyError(RuntimeError):
    """policy.toml failed to load. The message names the key."""


class Policy(BaseModel):
    """The knobs in policy.toml. Checked when the file is loaded."""

    model_config = ConfigDict(extra="forbid")

    scrutiny_threshold: Decimal
    allowed_currencies: list[str] = Field(min_length=1)
    required_fields: list[str] = Field(min_length=1)
    money_tolerance: Decimal
    critique_cap: int = Field(ge=0)
    audit_send_back_cap: int = Field(ge=0)
    model_retry_attempts: int = Field(ge=1)
    near_threshold_ratio: Decimal
    price_spike_ratio: Decimal
    reference_prices: dict[str, Decimal]

    @field_validator(
        "scrutiny_threshold",
        "money_tolerance",
        "near_threshold_ratio",
        "price_spike_ratio",
        mode="before",
    )
    @classmethod
    def _as_decimal(cls, value: object) -> Decimal:
        try:
            amount = Decimal(str(value))
        except Exception as exc:
            raise ValueError("must be a decimal amount") from exc
        if not amount.is_finite():
            raise ValueError("must be a finite decimal amount")
        return amount

    @field_validator("scrutiny_threshold")
    @classmethod
    def _threshold(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @field_validator("money_tolerance", "price_spike_ratio")
    @classmethod
    def _non_negative(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("must not be negative")
        return value

    @field_validator("near_threshold_ratio")
    @classmethod
    def _ratio(cls, value: Decimal) -> Decimal:
        if value < 0 or value >= 1:
            raise ValueError("must be at least 0 and less than 1")
        return value

    @field_validator("reference_prices", mode="before")
    @classmethod
    def _prices(cls, value: object) -> dict[str, Decimal]:
        if not isinstance(value, dict) or not value:
            raise ValueError("must be a table of item prices")
        prices = {}
        for item, amount in value.items():
            try:
                price = Decimal(str(amount))
            except Exception as exc:
                raise ValueError("must be a table of item prices") from exc
            if not price.is_finite() or price <= 0:
                raise ValueError("must be a table of positive prices")
            prices[str(item)] = price
        return prices

    @field_validator("allowed_currencies")
    @classmethod
    def _currencies(cls, value: list[str]) -> list[str]:
        codes = []
        for item in value:
            code = str(item).strip().upper()
            if len(code) != 3 or not code.isalpha():
                raise ValueError("must be a list of 3-letter currency codes")
            codes.append(code)
        return codes

    @field_validator("required_fields")
    @classmethod
    def _fields(cls, value: list[str]) -> list[str]:
        names = []
        for item in value:
            name = str(item).strip()
            if name not in _REQUIRED_FIELD_NAMES:
                raise ValueError(f"unknown field {name}")
            if name not in names:
                names.append(name)
        return names


def load_policy(path: str | Path | None = None) -> Policy:
    """Read policy.toml. A bad file raises PolicyError and names the key."""

    policy_path = _ROOT / "policy.toml" if path is None else Path(path)
    if not policy_path.is_file():
        raise PolicyError(f"policy file not found: {policy_path}")
    try:
        raw = tomllib.loads(policy_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PolicyError(f"policy file: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError("policy file: the document must be a table of keys")
    try:
        return Policy.model_validate(raw)
    except ValidationError as exc:
        err = exc.errors()[0]
        key = ".".join(str(part) for part in err["loc"]) or "policy"
        raise PolicyError(f"{key}: {err['msg']}") from exc


POLICY = load_policy()
APPROVAL_THRESHOLD_USD = POLICY.scrutiny_threshold
CRITIQUE_CAP = POLICY.critique_cap
MAX_SEND_BACKS = POLICY.audit_send_back_cap
MONEY = POLICY.money_tolerance
