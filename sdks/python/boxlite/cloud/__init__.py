"""
The BoxLite cloud management API.

    from boxlite.cloud import SyncCloudClient

    c = SyncCloudClient(api_key="blk_live_...")   # or BOXLITE_API_KEY
    for row in c.images.list():
        print(row["name"], row["tags"], row["lastUsedAt"])

Two things are called "images", and they are not the same thing:

===========================  ==========================================  ==================================
Namespace                    What it is                                  Where it works
===========================  ==========================================  ==================================
``rt.images``                The **local** image cache on this machine   Embedded backends only. On a REST
                             (``pull`` / ``list``)                       backend it raises ``Unsupported``
``c.images``                 The **cloud** catalog your organization     REST. This module
                             has pulled (``list`` / ``get`` /
                             ``delete`` / ``usage``)
===========================  ==========================================  ==================================

Boxes are still created through the existing SDK — ``Boxlite.rest(...)``. This
client does not create anything: an image enters the catalog by being used.

Naming follows the rest of the package: the bare name is async, the ``Sync``
prefix blocks. Nothing here needs a third-party package, and nothing here
builds a registry address of its own — the server resolves what a reference
means.
"""

from __future__ import annotations

from ._transport import DEFAULT_TIMEOUT_SECONDS, Transport
from .errors import (
    AuthenticationError,
    CloudConnectionError,
    CloudError,
    ImageInUse,
    ImageLimitExceeded,
    ImageNotFound,
    InvalidRequest,
    PermissionDenied,
    RateLimited,
    ServerError,
)
from .images import Images, SyncImages

__all__ = [
    "AuthenticationError",
    "CloudClient",
    "CloudConnectionError",
    "CloudError",
    "ImageInUse",
    "ImageLimitExceeded",
    "ImageNotFound",
    "Images",
    "InvalidRequest",
    "PermissionDenied",
    "RateLimited",
    "ServerError",
    "SyncCloudClient",
    "SyncImages",
]


class SyncCloudClient:
    """
    Blocking access to the cloud management API.

    Args:
        api_key: Defaults to ``BOXLITE_API_KEY``. The key carries the
            organization, so nothing selects one separately.
        base_url: Defaults to ``BOXLITE_REST_URL`` — the same address
            ``BoxliteRestOptions.from_env()`` reads, because it is the same
            server.
        timeout: Seconds per request. Catalog calls are short.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = Transport(api_key=api_key, base_url=base_url, timeout=timeout)
        self._images = SyncImages(self._transport)

    @property
    def images(self) -> SyncImages:
        """The organization's image catalog. Not the local cache — see the module docs."""
        return self._images


class CloudClient:
    """Async access to the cloud management API. Same surface as :class:`SyncCloudClient`."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = Transport(api_key=api_key, base_url=base_url, timeout=timeout)
        self._images = Images(self._transport)

    @property
    def images(self) -> Images:
        """The organization's image catalog. Not the local cache — see the module docs."""
        return self._images
