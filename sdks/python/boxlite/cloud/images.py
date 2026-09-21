"""
The cloud image catalog.

Not to be confused with `rt.images`, which is the *local* image cache on an
embedded runtime and raises `Unsupported` on a REST backend. This one is the
organization's catalog on the server: what it has pulled, how much of its limit
that uses, and what it may remove.

Rows come back as plain dictionaries in the shape the API publishes. That is
deliberate: the wire shape is the contract this module promises to keep, and
wrapping it in classes here would mean two shapes to keep in step with one API.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ._transport import Transport, quote_segment

__all__ = ["Images", "SyncImages"]

_IMAGES = "images"


class SyncImages:
    """The catalog, blocking."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    def list(self) -> list[dict[str, Any]]:
        """
        Every image this organization can boot from.

        Includes the operator's curated set, which every organization shares:
        those rows carry `curated: true`, no `id`, and cannot be deleted.
        """
        # `request_list` rather than `request`: an answer that is not a list
        # must not read as an empty catalog, because "you have no images" is
        # the one wrong answer here that a caller cannot tell from a right one.
        return self._transport.request_list("GET", _IMAGES)

    def get(self, id_or_ref: str) -> dict[str, Any]:
        """
        One image, with its versions.

        Takes a catalog id, or any reference that names the image — the server
        resolves it, because deciding what `acme/app` means is the server's
        job. This client never builds a registry address of its own.
        """
        return self._transport.request("GET", f"{_IMAGES}/{quote_segment(id_or_ref)}")

    def delete(self, id_or_ref: str) -> None:
        """
        Remove an image from the catalog.

        The entry goes, not the bytes: runners keep what they cached, and using
        the same reference again brings it back as a new entry. That is also
        how to pick up a tag that has moved upstream.

        Raises:
            ImageNotFound: No such image — including a second delete of one.
            ImageInUse: A box that has not been destroyed can still boot from it.
        """
        self._transport.request("DELETE", f"{_IMAGES}/{quote_segment(id_or_ref)}")

    def usage(self) -> dict[str, Any]:
        """
        `{count, limit, knownBytes}` for this organization.

        The limit is on the number of images, not on bytes. `knownBytes` is
        what the pulled manifests declared.
        """
        return self._transport.request("GET", f"{_IMAGES}/usage")


class Images:
    """The catalog, async. Every method is its blocking twin off the event loop."""

    def __init__(self, transport: Transport) -> None:
        self._blocking = SyncImages(transport)

    async def list(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._blocking.list)

    async def get(self, id_or_ref: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._blocking.get, id_or_ref)

    async def delete(self, id_or_ref: str) -> None:
        await asyncio.to_thread(self._blocking.delete, id_or_ref)

    async def usage(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._blocking.usage)
