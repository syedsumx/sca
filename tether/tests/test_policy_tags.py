"""Most-restrictive-policy rule (pure unit tests, no database)."""

from datetime import date

from tether.policy.tags import ConsentStatus, PolicyTags, most_restrictive, union_subjects


def test_consent_revoked_beats_pending_beats_granted() -> None:
    granted = PolicyTags(consent_status=ConsentStatus.GRANTED)
    pending = PolicyTags(consent_status=ConsentStatus.PENDING)
    revoked = PolicyTags(consent_status=ConsentStatus.REVOKED)
    assert most_restrictive([granted, pending]).consent_status == ConsentStatus.PENDING
    assert most_restrictive([pending, revoked, granted]).consent_status == ConsentStatus.REVOKED
    assert most_restrictive([granted, granted]).consent_status == ConsentStatus.GRANTED


def test_license_ranking_and_unknown_fails_closed() -> None:
    assert most_restrictive([{"license": "public"}, {"license": "internal"}]).license == "internal"
    assert (
        most_restrictive([{"license": "confidential"}, {"license": "internal"}]).license
        == "confidential"
    )
    # Unknown licenses rank as "restricted", above every known license.
    merged = most_restrictive([{"license": "restricted"}, {"license": "acme-custom-eula"}])
    assert merged.license in {"restricted", "acme-custom-eula"}
    assert most_restrictive(
        [{"license": "acme-custom-eula"}, {"license": "confidential"}]
    ).license == ("acme-custom-eula")
    # None means "no constraint" and never wins over a real license.
    assert most_restrictive([{"license": None}, {"license": "public"}]).license == "public"


def test_earliest_retention_wins() -> None:
    merged = most_restrictive(
        [
            {"retention_until": date(2030, 1, 1)},
            {"retention_until": date(2027, 6, 1)},
            {"retention_until": None},
        ]
    )
    assert merged.retention_until == date(2027, 6, 1)


def test_merge_is_order_independent_and_combines_fields() -> None:
    a = PolicyTags(
        consent_status="granted", license="confidential", retention_until=date(2031, 1, 1)
    )
    b = PolicyTags(consent_status="pending", license="public", retention_until=date(2028, 1, 1))
    assert most_restrictive([a, b]) == most_restrictive([b, a])
    merged = most_restrictive([a, b])
    assert merged == PolicyTags(
        consent_status="pending", license="confidential", retention_until=date(2028, 1, 1)
    )


def test_empty_merge_is_default() -> None:
    assert most_restrictive([]) == PolicyTags()


def test_union_subjects_sorted_and_deduplicated() -> None:
    assert union_subjects([["b", "a"], ["a", "c"], []]) == ["a", "b", "c"]


def test_allows_use() -> None:
    assert PolicyTags().allows_use()
    assert not PolicyTags(consent_status="revoked").allows_use()
    assert not PolicyTags(retention_until=date(2000, 1, 1)).allows_use()
