"""Policy tags and the *most restrictive* merge rule.

An artifact derived from several inputs carries the union of their subject ids and the
most restrictive combination of their policies:

* consent: ``revoked`` > ``pending`` > ``granted``
* license: ``restricted`` > ``confidential`` > ``internal`` > ``public``. An unknown license
  counts as ``restricted`` (fail closed). ``None`` means "no license constraint".
* retention: the earliest ``retention_until`` wins.
"""

from collections.abc import Iterable, Mapping
from datetime import date
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class ConsentStatus(StrEnum):
    GRANTED = "granted"
    PENDING = "pending"
    REVOKED = "revoked"


CONSENT_RANK: dict[ConsentStatus, int] = {
    ConsentStatus.GRANTED: 0,
    ConsentStatus.PENDING: 1,
    ConsentStatus.REVOKED: 2,
}

LICENSE_RANK: dict[str, int] = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}
UNKNOWN_LICENSE_RANK = LICENSE_RANK["restricted"]


def license_rank(license_: str | None) -> int:
    if license_ is None:
        return -1
    return LICENSE_RANK.get(license_.lower(), UNKNOWN_LICENSE_RANK)


class PolicyTags(BaseModel):
    """Policy attached to a subject's data and carried along by every derived artifact."""

    model_config = ConfigDict(frozen=True)

    consent_status: ConsentStatus = ConsentStatus.GRANTED
    license: str | None = None
    retention_until: date | None = None

    @classmethod
    def coerce(cls, value: "PolicyTags | Mapping[str, Any] | None") -> "PolicyTags":
        if value is None:
            return cls()
        if isinstance(value, PolicyTags):
            return value
        return cls.model_validate(dict(value))

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def is_expired(self, today: date | None = None) -> bool:
        return self.retention_until is not None and self.retention_until < (today or date.today())

    def allows_use(self, today: date | None = None) -> bool:
        """True when the data may be used for retrieval right now."""
        return self.consent_status != ConsentStatus.REVOKED and not self.is_expired(today)


def most_restrictive(policies: Iterable[PolicyTags | Mapping[str, Any] | None]) -> PolicyTags:
    """Combine policies so the result is at least as restrictive as each input."""
    items = [PolicyTags.coerce(p) for p in policies]
    if not items:
        return PolicyTags()
    consent = max((p.consent_status for p in items), key=CONSENT_RANK.__getitem__)
    licenses = [p.license for p in items if p.license is not None]
    lic: str | None = max(licenses, key=license_rank) if licenses else None
    retention_dates = [p.retention_until for p in items if p.retention_until is not None]
    retention = min(retention_dates) if retention_dates else None
    return PolicyTags(consent_status=consent, license=lic, retention_until=retention)


def union_subjects(groups: Iterable[Iterable[str]]) -> list[str]:
    """Sorted, de-duplicated union of subject ids."""
    out: set[str] = set()
    for g in groups:
        out.update(g)
    return sorted(out)
