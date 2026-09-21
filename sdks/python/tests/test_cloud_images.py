"""
The four catalog methods, on both faces.

Driven through a stub transport so each test is about the call the method
makes — its verb, its path and what it hands back — rather than about HTTP,
which `test_cloud_transport.py` covers.
"""

import pytest

from boxlite.cloud import (
    CloudClient,
    Images,
    SyncCloudClient,
    SyncImages,
)

DIGEST = "sha256:" + "a" * 64

CURATED_ROW = {"name": "python", "id": None, "curated": True, "tags": []}
OWN_ROW = {
    "name": "quay.io/acme/app",
    "id": "8c6a2f0e-0000-4000-8000-000000000001",
    "curated": False,
    "tags": ["v1"],
    "versionCount": 1,
    "sizeBytes": 4096,
    "lastUsedAt": "2026-01-01T00:00:00.000Z",
}


class StubTransport:
    """Records what was asked for and replays what was queued."""

    def __init__(self, answers=None):
        self.calls = []
        self._answers = answers or {}

    def request(self, method, path):
        self.calls.append((method, path))
        return self._answers.get((method, path))

    # The real one refuses a non-list; that refusal is the transport's and
    # is tested against the real transport, so this only has to route.
    def request_list(self, method, path):
        return self.request(method, path)


def _sync(answers=None):
    transport = StubTransport(answers)
    return SyncImages(transport), transport


def _async(answers=None):
    transport = StubTransport(answers)
    return Images(transport), transport


class TestSyncFace:
    def test_lists(self):
        images, transport = _sync({("GET", "images"): [CURATED_ROW, OWN_ROW]})

        rows = images.list()

        assert transport.calls == [("GET", "images")]
        assert [row["name"] for row in rows] == ["python", "quay.io/acme/app"]

    def test_gets_by_reference(self):
        images, transport = _sync(
            {("GET", "images/quay.io%2Facme%2Fapp"): {**OWN_ROW, "versions": []}}
        )

        detail = images.get("quay.io/acme/app")

        assert transport.calls == [("GET", "images/quay.io%2Facme%2Fapp")]
        assert detail["name"] == "quay.io/acme/app"

    def test_gets_by_catalog_id(self):
        images, transport = _sync()

        images.get(OWN_ROW["id"])

        assert transport.calls == [("GET", f"images/{OWN_ROW['id']}")]

    def test_deletes(self):
        images, transport = _sync()

        assert images.delete("quay.io/acme/app") is None
        assert transport.calls == [("DELETE", "images/quay.io%2Facme%2Fapp")]

    def test_reads_usage(self):
        images, transport = _sync(
            {("GET", "images/usage"): {"count": 2, "limit": 20, "knownBytes": 5120}}
        )

        usage = images.usage()

        assert transport.calls == [("GET", "images/usage")]
        assert usage == {"count": 2, "limit": 20, "knownBytes": 5120}

    def test_refuses_a_traversal_without_asking_the_server(self):
        images, transport = _sync()

        with pytest.raises(ValueError):
            images.get("../../etc/passwd")

        assert transport.calls == []


class TestAsyncFace:
    """The same four calls, and they must reach the same places."""

    @pytest.mark.asyncio
    async def test_lists(self):
        images, transport = _async({("GET", "images"): [OWN_ROW]})

        rows = await images.list()

        assert transport.calls == [("GET", "images")]
        assert rows[0]["name"] == "quay.io/acme/app"

    @pytest.mark.asyncio
    async def test_gets(self):
        images, transport = _async(
            {
                (
                    "GET",
                    f"images/quay.io%2Facme%2Fapp%40{DIGEST.replace(':', '%3A')}",
                ): OWN_ROW
            }
        )

        await images.get(f"quay.io/acme/app@{DIGEST}")

        assert transport.calls == [
            ("GET", f"images/quay.io%2Facme%2Fapp%40{DIGEST.replace(':', '%3A')}")
        ]

    @pytest.mark.asyncio
    async def test_deletes(self):
        images, transport = _async()

        assert await images.delete("quay.io/acme/app") is None
        assert transport.calls == [("DELETE", "images/quay.io%2Facme%2Fapp")]

    @pytest.mark.asyncio
    async def test_reads_usage(self):
        images, transport = _async(
            {("GET", "images/usage"): {"count": 0, "limit": 20, "knownBytes": 0}}
        )

        usage = await images.usage()

        assert transport.calls == [("GET", "images/usage")]
        assert usage["limit"] == 20

    @pytest.mark.asyncio
    async def test_refuses_a_traversal_without_asking_the_server(self):
        images, transport = _async()

        with pytest.raises(ValueError):
            await images.get("../../etc/passwd")

        assert transport.calls == []


class TestFacesStayAligned:
    """
    C12. The two faces are one API written twice, and the import path is a
    cross-release promise: when the internals are replaced with a generated
    client, code written against these names must not change. A method that
    exists on one face and not the other breaks that quietly.
    """

    @staticmethod
    def _public(obj):
        return {name for name in dir(obj) if not name.startswith("_")}

    def test_the_catalog_offers_the_same_methods_on_both_faces(self):
        assert self._public(SyncImages) == self._public(Images)

    def test_the_clients_offer_the_same_surface(self):
        assert self._public(SyncCloudClient) == self._public(CloudClient)

    def test_the_catalog_is_exactly_the_four_methods(self):
        assert self._public(SyncImages) == {"list", "get", "delete", "usage"}

    def test_a_client_exposes_the_catalog_under_images(self):
        assert self._public(SyncCloudClient) == {"images"}
