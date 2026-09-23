from pathlib import Path

from tumblr_scraper.media_manifest import (
    MediaManifest,
    logical_media_id,
    merge_media_observation,
    record_acquisition,
    record_optimizer_result,
)


def test_media_manifest_preserves_source_knowledge_and_monotonic_acquisition(tmp_path: Path):
    media_id = logical_media_id("https://media.example/image-500.jpg", source_id="post:7:photo:0")
    entry = merge_media_observation(None, media_id=media_id, variants=[
        {"url": "https://media.example/image-500.jpg", "width": 500},
        {"url": "https://media.example/image-1280.jpg", "width": 1280},
    ], best_known_quality="HIGH")
    entry = record_acquisition(entry, acquired_quality="HIGH", local_representation={"path": "media/high.jpg"})
    downgraded = record_acquisition(entry, acquired_quality="THUMBNAIL", local_representation={"path": "media/thumb.jpg"})
    assert downgraded["best_acquired_source_quality"] == "HIGH"
    assert downgraded["current_local_representation"]["path"] == "media/high.jpg"
    assert len(downgraded["known_source_variants"]) == 2


def test_failed_upgrade_does_not_replace_working_copy_and_optimizer_is_separate():
    entry = {"logical_media_id": "m", "best_acquired_source_quality": "COMPACT", "current_local_representation": {"path": "compact.jpg"}}
    failed = record_optimizer_result(entry, {"status": "failed", "error": "codec"})
    assert failed["current_local_representation"]["path"] == "compact.jpg"
    assert failed["optimizer_result"]["status"] == "failed"


def test_manifest_replays_latest_entry(tmp_path: Path):
    manifest = MediaManifest(tmp_path / "media-manifest.jsonl")
    manifest.append({"logical_media_id": "m", "best_acquired_source_quality": "COMPACT"})
    manifest.append({"logical_media_id": "m", "best_acquired_source_quality": "HIGH"})
    assert manifest.latest()["m"]["best_acquired_source_quality"] == "HIGH"
