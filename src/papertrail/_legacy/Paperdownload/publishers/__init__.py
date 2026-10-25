"""Publisher PDF adapters.

The router and CLI own route planning, registry reloads, and public metadata
clients. Adapters only execute an already-approved plan through the supplied
transport and the existing download governor/ledger/job stores.
"""

from __future__ import annotations

from typing import Any, Mapping

from .base import (
    DailyRequestBudgetError,
    PublisherAdapter,
    UrllibTransport,
    make_daily_budget_reserver,
    reserve_daily_request,
)
from .elsevier_adapter import ElsevierAdapter
from .oa_adapter import OAAdapter
from .springer_adapter import SpringerNatureAdapter
from .wiley_adapter import WileyAdapter


ADAPTERS = {
    "oa": OAAdapter,
    "elsevier": ElsevierAdapter,
    "springer_nature": SpringerNatureAdapter,
    "springer": SpringerNatureAdapter,
    "wiley": WileyAdapter,
}


def create_adapter(
    name: str,
    registry_entry: Mapping[str, Any],
    *,
    transport: Any = None,
    credential_reader: Any = None,
    elsevier_insttoken: bool = False,
    wiley_redirect_doi: str | None = None,
) -> PublisherAdapter:
    """Create one adapter from a freshly loaded registry entry.

    For OA, pass an entry-shaped snapshot containing the registry's
    ``oa_pdf_domains`` list. Subscription adapters require the full current
    publisher entry. The caller must reload the registry before each execution
    and must not reuse a plan's older registry snapshot as authorization.
    """

    adapter_type = ADAPTERS.get(name)
    if adapter_type is None:
        raise ValueError("Unknown publisher adapter")
    if type(elsevier_insttoken) is not bool or (elsevier_insttoken and name != "elsevier"):
        raise ValueError("ELSEVIER_AUTH_MODE_INVALID")
    if wiley_redirect_doi is not None and name != 'wiley':
        raise ValueError('WILEY_SINGLE_ARTICLE_SCOPE_INVALID')
    if name == "elsevier":
        return adapter_type(registry_entry, transport=transport,
                            credential_reader=credential_reader,
                            use_institution_token=elsevier_insttoken)
    if name == 'wiley' and wiley_redirect_doi is not None:
        return adapter_type(registry_entry, transport=transport,
                            credential_reader=credential_reader,
                            api_directed_binary_doi=wiley_redirect_doi)
    return adapter_type(
        registry_entry,
        transport=transport,
        credential_reader=credential_reader,
    )


__all__ = [
    "ADAPTERS",
    "DailyRequestBudgetError",
    "ElsevierAdapter",
    "OAAdapter",
    "PublisherAdapter",
    "SpringerNatureAdapter",
    "UrllibTransport",
    "WileyAdapter",
    "create_adapter",
    "make_daily_budget_reserver",
    "reserve_daily_request",
]
