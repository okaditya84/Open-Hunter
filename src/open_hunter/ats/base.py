"""Base class and registry for ATS clients."""

from __future__ import annotations

import abc
from typing import Dict, List, Optional

from ..http_client import HttpClient
from ..models import Job


class ATSClient(abc.ABC):
    """One implementation per ATS platform.

    A client knows two things:
      * how to recognise its platform's URLs and pull out the board *token*
        (`token_from_url`), and
      * how to fetch every open job for a token (`fetch_jobs`).
    """

    name: str = "base"

    def __init__(self, http: HttpClient):
        self.http = http

    @abc.abstractmethod
    def token_from_url(self, url: str) -> Optional[str]:
        """Return the board token if `url` belongs to this ATS, else None."""

    @abc.abstractmethod
    def fetch_jobs(self, token: str, company_name: str) -> List[Job]:
        """Fetch all open jobs for the given board token."""

    def exists(self, token: str) -> bool:
        """Cheaply verify that `token` is a real, live board on this ATS.

        Used by guess-and-verify probing: we only ever accept a guessed token
        when the platform's own API confirms it exists, so a board we report
        is always real. Default: not probeable (return False).
        """
        return False


# Registry is populated at import time by _register_all().
ALL_CLIENTS: Dict[str, type] = {}


def _register_all() -> None:
    # Imported lazily to avoid circular imports.
    from . import (  # noqa: F401
        ashby,
        bamboohr,
        greenhouse,
        lever,
        recruitee,
        smartrecruiters,
        workable,
        workday,
    )

    for module in (
        greenhouse,
        lever,
        ashby,
        smartrecruiters,
        recruitee,
        workable,
        bamboohr,
        workday,
    ):
        client_cls = module.CLIENT
        ALL_CLIENTS[client_cls.name] = client_cls


def get_client(name: str, http: HttpClient) -> Optional[ATSClient]:
    if not ALL_CLIENTS:
        _register_all()
    cls = ALL_CLIENTS.get(name)
    return cls(http) if cls else None


def all_clients(http: HttpClient) -> List[ATSClient]:
    if not ALL_CLIENTS:
        _register_all()
    return [cls(http) for cls in ALL_CLIENTS.values()]
