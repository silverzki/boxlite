"""
The image catalog, end to end.

This is the only place the whole claim is exercised: an arbitrary public
image boots a box with no prior registration, and the pull that made it work
is what records the image. Reading the catalog and deleting from it are
covered by unit and integration tests; what needs a real runner is how a row
gets *written*.

Driven through `boxlite.cloud`, deliberately — the same run then covers the
client users are told to use. Boxes are still created with the existing SDK,
because creating a box is not what that client does.

The tests run in file order and share the catalog state they build up: a cold
pull is tens of seconds, so the suite pays for it once and walks the lifecycle
rather than re-booting per assertion. Each test names the step it is.

Environment:
  BOXLITE_E2E_CATALOG_IMAGE   Image to boot. Must be on a host the API's
                              allowlist admits. Defaults to a small public one
                              on quay.io, which the built-in allowlist accepts
                              — so this file works against a deployed stack
                              without a registry of our own.
  BOXLITE_E2E_ALLOW_MISSING_CATALOG=1
                              Skip instead of failing when the stack has no
                              catalog endpoints. For running against an
                              environment this change has not reached; without
                              it a 404 is the regression it looks like.

POL-544.

Not covered here: that a recorded tag does not follow an upstream move, and
that deleting the image is what re-resolves it. Proving it means moving a tag
upstream, which needs a repository under our control.
"""

from __future__ import annotations

import asyncio
import os
import re

import pytest

import boxlite
from boxlite.cloud import (
    CloudClient,
    ImageInUse,
    ImageNotFound,
    InvalidRequest,
    SyncCloudClient,
)
from e2e_auth import auth_context, request_json

# Small, public, and on a host the built-in allowlist admits
# (`quay.io` / `gcr.io` / `public.ecr.aws` — `docker.io` and `ghcr.io` are held
# back until every runner pulls tenant references anonymously).
DEFAULT_CATALOG_IMAGE = "quay.io/libpod/alpine:latest"

CATALOG_IMAGE = os.environ.get("BOXLITE_E2E_CATALOG_IMAGE", DEFAULT_CATALOG_IMAGE)

# `name` in the catalog is the repository without the tag: one row holds every
# version ever pulled under it.
CATALOG_NAME = CATALOG_IMAGE.split("@")[0].rsplit(":", 1)[0]

# A repository that cannot resolve, on the same allowlisted host so admission
# passes and the *pull* is what fails. A distinct repository, so a row written
# for it would show up as a name that was not there before — sharing
# CATALOG_NAME would make the assertion pass for the wrong reason.
UNPULLABLE_IMAGE = f"{CATALOG_NAME.rsplit('/', 1)[0]}/boxlite-e2e-does-not-exist:v1"
UNPULLABLE_NAME = UNPULLABLE_IMAGE.rsplit(":", 1)[0]


@pytest.fixture(scope="module")
def cloud() -> SyncCloudClient:
    """The catalog client.

    A 404 is failed, not skipped: the routes going missing is exactly the
    regression this file exists to catch, and a skip would report that as
    green. Running against a stack the change has not reached yet is a
    deliberate act, so it takes a deliberate opt-in.
    """
    status, _ = request_json("GET", "/images")
    if status == 404:
        if os.environ.get("BOXLITE_E2E_ALLOW_MISSING_CATALOG") == "1":
            pytest.skip("BOXLITE_E2E_ALLOW_MISSING_CATALOG=1 and this stack has no catalog")
        pytest.fail(
            "GET /images answered 404. If this stack predates the catalog, say so with "
            "BOXLITE_E2E_ALLOW_MISSING_CATALOG=1; otherwise the endpoints have regressed."
        )
    if status != 200:
        pytest.fail(f"GET /images answered {status}; the catalog is reachable or it is not")
    ctx = auth_context()
    return SyncCloudClient(api_key=ctx.token, base_url=ctx.url)


def _own_images(cloud: SyncCloudClient) -> list[dict]:
    """Rows this organization owns. Curated images are in the list too, and are nobody's."""
    return [row for row in cloud.images.list() if not row["curated"]]


def _find(cloud: SyncCloudClient, name: str) -> dict | None:
    return next((row for row in _own_images(cloud) if row["name"] == name), None)


@pytest.fixture(scope="module", autouse=True)
def clean_catalog(cloud: SyncCloudClient):
    """Clear our test rows before and after, so a re-run starts where the first did."""

    def drop() -> None:
        for name in (CATALOG_NAME, UNPULLABLE_NAME):
            try:
                cloud.images.delete(name)
            except (ImageNotFound, ImageInUse, InvalidRequest):
                # Absent, still held, or not ours — none of which this fixture
                # can or should resolve.
                pass

    drop()
    yield
    drop()


# The admission gate lets an organization start three cold pulls per 60-second
# window, and this file needs more creates than that. Waiting the window out is
# not a workaround for a flaky server: the refusal is the documented contract,
# it carries how long is left, and a suite that treated it as a failure would
# be reporting the gate working as a bug.
_WINDOW_REMAINING = re.compile(r"(\d+)s left in the current one")


async def _create_box(rt, image: str, *, attempts: int = 3):
    """Create a box, waiting out the cold-pull budget if the gate asks us to."""
    for attempt in range(attempts):
        try:
            return await rt.create(boxlite.BoxOptions(image=image, auto_remove=True))
        except Exception as failure:
            remaining = _WINDOW_REMAINING.search(str(failure))
            if remaining is None or attempt == attempts - 1:
                raise
            await asyncio.sleep(int(remaining.group(1)) + 1)
    raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_an_arbitrary_public_image_boots_a_box(rt, cloud):
    """Step 1. No registration call and no catalog entry beforehand — just a reference."""
    assert _find(cloud, CATALOG_NAME) is None, (
        f"{CATALOG_NAME} was in the catalog before the first box booted"
    )

    box = await _create_box(rt, CATALOG_IMAGE)
    try:
        assert box.id
    finally:
        await rt.remove(box.id, force=True)


@pytest.mark.asyncio
async def test_using_it_is_what_records_it(rt, cloud):
    """
    Step 2. The row appears because a box booted, not because anything
    registered it — which is the whole shape of this release.
    """
    row = _find(cloud, CATALOG_NAME)
    assert row is not None, f"{CATALOG_NAME} never entered the catalog after a box booted from it"

    detail = cloud.images.get(row["id"])
    assert detail["versions"], "the image is recorded but carries no version"
    version = detail["versions"][0]
    # The digest and the size come from the runner, the only thing that knows
    # them. Missing either would mean the report never arrived.
    assert version["digest"].startswith("sha256:")
    assert version["sizeBytes"] > 0
    assert version["sourceRef"] == CATALOG_IMAGE

    usage = cloud.images.usage()
    assert usage["count"] >= 1
    assert usage["knownBytes"] >= version["sizeBytes"]


@pytest.mark.asyncio
async def test_a_second_box_adds_no_second_version(rt, cloud):
    """
    Step 3. Booting the same reference again resolves through the catalog, so
    it records nothing new.

    What this cannot observe from outside is the reference the runner was
    handed. That it is digest-pinned is held by the resolver's own exit
    assertion (`assertPinnedOnCatalogHit`) and its unit tests; reading the job
    payload needs database access this suite does not have against a deployed
    stack.
    """
    before = cloud.images.get(CATALOG_NAME)

    box = await _create_box(rt, CATALOG_IMAGE)
    try:
        after = cloud.images.get(CATALOG_NAME)
        assert len(after["versions"]) == len(before["versions"]), (
            "a second box on the same reference recorded another version; it "
            "resolved through the registry instead of through the catalog"
        )
        assert after["tags"] == before["tags"]
    finally:
        await rt.remove(box.id, force=True)


@pytest.mark.asyncio
async def test_a_box_still_holding_it_blocks_removal(rt, cloud):
    """
    Step 4. A box that has not been destroyed can still boot from the image, so
    the entry cannot go. The refusal names the box, which is the only way the
    caller can act on it.
    """
    box = await _create_box(rt, CATALOG_IMAGE)
    try:
        with pytest.raises(ImageInUse) as caught:
            cloud.images.delete(CATALOG_NAME)
        assert caught.value.status == 409
        assert box.id in caught.value.message
    finally:
        await rt.remove(box.id, force=True)


@pytest.mark.asyncio
async def test_removal_frees_the_name_and_the_quota(rt, cloud):
    """Step 5. With nothing holding it, the entry goes and the count falls."""
    before = cloud.images.usage()["count"]

    cloud.images.delete(CATALOG_NAME)

    assert _find(cloud, CATALOG_NAME) is None
    assert cloud.images.usage()["count"] == before - 1

    # Idempotent for the caller: the row is still there with `deletedAt` set,
    # and every read skips it.
    with pytest.raises(ImageNotFound) as caught:
        cloud.images.delete(CATALOG_NAME)
    assert caught.value.status == 404

    # The name is free again, and using the reference brings it back as a new
    # entry — which is what makes deletion the escape hatch for a pinned tag.
    box = await _create_box(rt, CATALOG_IMAGE)
    try:
        assert _find(cloud, CATALOG_NAME) is not None
    finally:
        await rt.remove(box.id, force=True)


@pytest.mark.asyncio
async def test_a_pull_that_fails_records_nothing(rt, cloud):
    """
    Step 6. The catalog is written when a box reaches STARTED, so an image that
    never pulls must leave no trace — otherwise a typo would spend one of the
    organization's slots forever.
    """
    assert _find(cloud, UNPULLABLE_NAME) is None

    box = None
    try:
        box = await _create_box(rt, UNPULLABLE_IMAGE)
        # The premise is that this reference cannot resolve. If it booted, the
        # pull succeeded and this test is no longer testing what it says.
        pytest.fail(
            f"{UNPULLABLE_IMAGE} booted a box, so it is pullable and this test "
            f"can no longer show what a failed pull does"
        )
    except pytest.fail.Exception:
        raise
    except Exception as failure:
        message = str(failure)
        # A stack with nothing to schedule onto never reaches the pull, so the
        # assertion below would hold for a reason that has nothing to do with
        # the catalog. Say so rather than report a pass.
        if "No available runners" in message:
            pytest.skip("no runner is attached to this stack, so no pull was attempted")
        # The gate's own refusal reads "Too many image pulls started recently",
        # so the word "pull" cannot tell it apart from a registry failure.
        # Match the window it reports instead — a structural marker the prose
        # around it can change without breaking.
        if _WINDOW_REMAINING.search(message):
            pytest.fail(
                f"the cold-pull budget ran out before the registry was reached, so an "
                f"empty catalog proves nothing here: {message}"
            )
        # Admission refusing the reference is a different test. This one needs
        # the gate to let it through and the *registry* to be what fails.
        assert "not allowed" not in message, (
            f"admission refused {UNPULLABLE_IMAGE} instead of letting the pull fail: {message}"
        )
        # What is left must show the pull was actually attempted. Auth, a
        # dropped connection or a scheduling error all stop short of the
        # registry, and the assertion below would then hold for a reason this
        # test never established. Failing loudly on an unfamiliar message is
        # the safe direction: a false red gets read, a false green does not.
        assert UNPULLABLE_NAME in message or "failed to pull" in message.lower(), (
            f"create failed before the pull was attempted, so an empty catalog "
            f"proves nothing here: {message}"
        )
    finally:
        if box is not None:
            await rt.remove(box.id, force=True)

    assert _find(cloud, UNPULLABLE_NAME) is None, (
        f"{UNPULLABLE_NAME} entered the catalog even though it never pulled"
    )


@pytest.mark.asyncio
async def test_curated_boxes_never_touch_the_catalog(rt, cloud, image):
    """
    Step 7. The path every existing caller takes is unchanged: a curated image
    is the operator's, so it neither enters an organization's catalog nor
    spends any of its limit.
    """
    before = cloud.images.usage()
    names_before = {row["name"] for row in _own_images(cloud)}

    box = await _create_box(rt, image)
    try:
        assert {row["name"] for row in _own_images(cloud)} == names_before
        assert cloud.images.usage() == before
    finally:
        await rt.remove(box.id, force=True)


class TestAdmission:
    """
    Step 8. What the gate refuses, it refuses synchronously and with a reason.
    A 500 here would mean the runner was handed something it should never have
    seen; a 2xx would mean the gate is not there at all.
    """

    def test_refuses_a_host_outside_the_allowlist(self, cloud):
        status, body = request_json(
            "POST", auth_context().v1("boxes"), {"image": "example.invalid/acme/app:v1"}
        )
        assert status == 400, f"expected a refusal, got {status}: {body}"
        assert "example.invalid" in str(body)

    def test_refuses_the_metadata_address(self, cloud):
        status, body = request_json(
            "POST", auth_context().v1("boxes"), {"image": "169.254.169.254/acme/app:v1"}
        )
        assert status == 400, f"expected a refusal, got {status}: {body}"
        # Without this the test passes on any 400 the create path happens to
        # raise, which would not show the gate saw the address at all.
        assert "169.254.169.254" in str(body), f"the refusal never named the address: {body}"

    def test_refuses_a_traversal_before_it_is_sent(self, cloud):
        # The client's own boundary: this never becomes a request.
        with pytest.raises(ValueError):
            cloud.images.get("../../etc/passwd")


@pytest.mark.asyncio
async def test_the_async_face_answers_the_same(cloud):
    """
    Step 9. The two faces are one API written twice; a run that exercised only
    the blocking one would leave the other unproven against a real server.
    """
    ctx = auth_context()
    asynchronous = CloudClient(api_key=ctx.token, base_url=ctx.url)

    assert [row["name"] for row in await asynchronous.images.list()] == [
        row["name"] for row in cloud.images.list()
    ]
    assert await asynchronous.images.usage() == cloud.images.usage()

    row = _find(cloud, CATALOG_NAME)
    if row is not None:
        assert (await asynchronous.images.get(row["id"]))["id"] == row["id"]
