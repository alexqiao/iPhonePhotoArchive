from __future__ import annotations

from photoarchive.domain import DiscoveryRequest
from photoarchive.fixture import FixturePhotosClient


def test_fixture_excludes_future_and_missing_dates(app_config, demo_fixture) -> None:  # type: ignore[no-untyped-def]
    client = FixturePhotosClient(demo_fixture)
    assets = list(
        client.discover(
            DiscoveryRequest(
                cutoff_at_utc=app_config.cutoff_at_utc(),
                media_types=frozenset(app_config.archive_policy.media_types),
                batch_size=2,
            )
        )
    )
    assert {asset.photos_local_id for asset in assets} == {
        "basic-image",
        "live-complete",
        "live-incomplete",
        "raw-pair",
        "duplicate-content",
        "same-name-resources",
    }
    same_name = next(asset for asset in assets if asset.photos_local_id == "same-name-resources")
    assert {resource.original_name for resource in same_name.resources} == {"DUPLICATE.BIN"}
    assert len({resource.source_path.read_bytes() for resource in same_name.resources}) == 2
