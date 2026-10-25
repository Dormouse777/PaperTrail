"""Build a bounded, offline institution-access observation record.

This module has no browser, network, filesystem, or authentication integration.
It stores only explicitly supplied, allowlisted observations and fixed scope
limits; it cannot verify authentication or infer a durable session.
"""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import date, datetime
from typing import Literal


Browser = Literal["Edge", "TraeCN-Chromium"]
ResourceReachability = Literal["not_observed", "reachable", "not_reachable"]
ReentryObservation = Literal[
    "not_observed",
    "same_resource_visible_after_reentry",
    "resource_not_visible_after_reentry",
]
LimitedUserConfirmation = Literal["not_confirmed", "visible_observations_only"]
AUTHORIZED_INSTITUTION_VISIBLE_IDENTIFIERS = frozenset({"授权机构", "authorized institution"})

_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
_CREDENTIAL_LIKE_HOST = re.compile(
    r"(?i)(?:^|[.-])(?:password|passwd|credential|secret|token|cookie|session|ticket|"
    r"bearer|api[_-]?key)(?:$|[.-])"
)


def _record_date(value: str | date) -> str:
    if isinstance(value, datetime):
        raise ValueError("record_date accepts a calendar date only, not a timestamp")
    if isinstance(value, date):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("record_date must be a valid YYYY-MM-DD date") from error
    else:
        raise ValueError("record_date must be a YYYY-MM-DD date")
    if parsed > date.today():
        raise ValueError("record_date cannot be in the future")
    return parsed.isoformat()


def _public_dns_host(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be a DNS host only")
    if any(character in value for character in "/\\?#@:") or "://" in value:
        raise ValueError(f"{field_name} must be a DNS host only, without URL data")
    try:
        host = value.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValueError(f"{field_name} must be a valid DNS host") from error

    # This validates syntax and excludes obvious local/credential-like names;
    # it does not resolve the hostname or establish that it is institutionally
    # official or publicly reachable.
    labels = host.split(".")
    if (
        len(host) > 253
        or len(labels) < 2
        or any(not _DNS_LABEL.fullmatch(label) for label in labels)
        or host.endswith((".localhost", ".local", ".internal", ".lan"))
        or _CREDENTIAL_LIKE_HOST.search(host)
    ):
        raise ValueError(
            f"{field_name} must be a non-local DNS hostname without credential-like labels"
        )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(f"{field_name} must be a DNS hostname, not an IP address")
    return host


def _institution_label(value: str) -> str:
    if not isinstance(value, str) or value not in AUTHORIZED_INSTITUTION_VISIBLE_IDENTIFIERS:
        raise ValueError("institution_visible_identifier must match an approved authorized-institution label")
    return value


def create_acceptance_record(
    *,
    record_date: str | date,
    browser: Browser,
    official_entry_domain: str,
    institution_visible_identifier: str,
    selected_database_public_host: str,
    resource_reachability: ResourceReachability,
    reentry_observation: ReentryObservation,
    limited_user_confirmation: LimitedUserConfirmation,
) -> dict[str, object]:
    """Return a JSON-safe record containing only bounded visible observations.

    Domain fields accept DNS hostnames only: schemes, paths, query strings,
    fragments, credentials, ports, and IP literals are rejected. Confirmation
    can cover visible observations only; authentication is never represented as
    verified and a subsequent view never proves a persistent session.
    """
    if browser not in ("Edge", "TraeCN-Chromium"):
        raise ValueError("browser must be a supported browser label")
    if not isinstance(resource_reachability, str) or resource_reachability not in (
        "not_observed",
        "reachable",
        "not_reachable",
    ):
        raise ValueError("resource_reachability must be a supported observation state")
    if not isinstance(reentry_observation, str) or reentry_observation not in (
        "not_observed",
        "same_resource_visible_after_reentry",
        "resource_not_visible_after_reentry",
    ):
        raise ValueError("reentry_observation must be a supported observation state")
    if not isinstance(limited_user_confirmation, str) or limited_user_confirmation not in (
        "not_confirmed",
        "visible_observations_only",
    ):
        raise ValueError("limited_user_confirmation must be a supported limited choice")
    if resource_reachability == "not_observed" and reentry_observation != "not_observed":
        raise ValueError("Re-entry cannot be recorded when resource reachability was not observed")

    return {
        "record_date": _record_date(record_date),
        "browser": browser,
        "official_entry_domain": _public_dns_host(official_entry_domain, "official_entry_domain"),
        "institution_visible_identifier": _institution_label(institution_visible_identifier),
        "selected_database_public_host": _public_dns_host(
            selected_database_public_host, "selected_database_public_host"
        ),
        "resource_reachability": resource_reachability,
        "resource_observation_source": "user_supplied_visible_observation",
        "reentry_observation": reentry_observation,
        "limited_user_confirmation": limited_user_confirmation,
        "entry_domain_status": "user_supplied_not_independently_verified",
        "database_host_status": "user_supplied_not_network_checked",
        "authentication_claim": "not_assessed",
        "persistent_session_claim": "not_inferred",
    }


def serialize_acceptance_record(record: dict[str, object]) -> str:
    """Serialize a record created by this module without performing file I/O."""
    if set(record) != {
        "record_date",
        "browser",
        "official_entry_domain",
        "institution_visible_identifier",
        "selected_database_public_host",
        "resource_reachability",
        "resource_observation_source",
        "reentry_observation",
        "limited_user_confirmation",
        "entry_domain_status",
        "database_host_status",
        "authentication_claim",
        "persistent_session_claim",
    }:
        raise ValueError("Only the allowlisted acceptance-record fields can be serialized")
    if (
        record["resource_observation_source"] != "user_supplied_visible_observation"
        or record["entry_domain_status"] != "user_supplied_not_independently_verified"
        or record["database_host_status"] != "user_supplied_not_network_checked"
        or record["authentication_claim"] != "not_assessed"
        or record["persistent_session_claim"] != "not_inferred"
    ):
        raise ValueError("Verification boundaries cannot be changed")
    validated = create_acceptance_record(
        record_date=record["record_date"],
        browser=record["browser"],
        official_entry_domain=record["official_entry_domain"],
        institution_visible_identifier=record["institution_visible_identifier"],
        selected_database_public_host=record["selected_database_public_host"],
        resource_reachability=record["resource_reachability"],
        reentry_observation=record["reentry_observation"],
        limited_user_confirmation=record["limited_user_confirmation"],
    )
    return json.dumps(validated, ensure_ascii=False, sort_keys=True)
