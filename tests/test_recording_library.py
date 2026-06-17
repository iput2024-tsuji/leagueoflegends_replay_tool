import json
from pathlib import Path

import pytest

from src.recording_library import RecordingDeletionError, RecordingDeletionPlan, RecordingLibrary


def test_recording_library_deletes_owned_video_clips_and_json(tmp_path):
    recordings_dir = tmp_path / "recordings"
    json_dir = recordings_dir / "json"
    clips_dir = recordings_dir / "clips"
    json_dir.mkdir(parents=True)
    clips_dir.mkdir()

    video = recordings_dir / "game.mp4"
    clip = clips_dir / "game_clip_1000_2000.mp4"
    unrelated = recordings_dir / "unrelated.mp4"
    session = json_dir / "session.json"
    video.write_bytes(b"video")
    clip.write_bytes(b"clip")
    unrelated.write_bytes(b"keep")
    session.write_text(
        json.dumps({"obs_record_path": video.name}),
        encoding="utf-8",
    )

    library = RecordingLibrary(recordings_dir, json_dir)
    plan = library.plan_deletion(session)
    deleted = []

    def delete_file(path: Path) -> bool:
        deleted.append(path)
        path.unlink()
        return True

    result = library.delete(plan, delete_file)

    assert result.success is True
    assert deleted == [video.resolve(), clip.resolve(), session.resolve()]
    assert unrelated.exists()


def test_recording_library_never_deletes_video_outside_recordings_dir(tmp_path):
    recordings_dir = tmp_path / "recordings"
    json_dir = recordings_dir / "json"
    json_dir.mkdir(parents=True)
    external_video = tmp_path / "external.mp4"
    external_video.write_bytes(b"external")
    session = json_dir / "session.json"
    session.write_text(
        json.dumps({"obs_record_path": str(external_video)}),
        encoding="utf-8",
    )

    library = RecordingLibrary(recordings_dir, json_dir)
    plan = library.plan_deletion(session)

    assert plan.video_path is None
    assert plan.paths == (session.resolve(),)


def test_recording_library_rejects_json_outside_configured_directory(tmp_path):
    recordings_dir = tmp_path / "recordings"
    json_dir = recordings_dir / "json"
    json_dir.mkdir(parents=True)
    external_json = tmp_path / "session.json"
    external_json.write_text("{}", encoding="utf-8")

    library = RecordingLibrary(recordings_dir, json_dir)

    with pytest.raises(RecordingDeletionError, match="ディレクトリ外"):
        library.plan_deletion(external_json)


def test_recording_library_keeps_delete_plan_when_clip_listing_fails(monkeypatch, tmp_path):
    recordings_dir = tmp_path / "recordings"
    json_dir = recordings_dir / "json"
    clips_dir = recordings_dir / "clips"
    json_dir.mkdir(parents=True)
    clips_dir.mkdir()

    video = recordings_dir / "game.mp4"
    session = json_dir / "session.json"
    video.write_bytes(b"video")
    session.write_text(json.dumps({"obs_record_path": video.name}), encoding="utf-8")
    resolved_clips_dir = clips_dir.resolve()
    original_glob = Path.glob

    def fail_clip_glob(path: Path, pattern: str):
        if path.resolve() == resolved_clips_dir:
            raise OSError("access denied")
        return original_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", fail_clip_glob)

    library = RecordingLibrary(recordings_dir, json_dir)
    plan = library.plan_deletion(session)

    assert plan.video_path == video.resolve()
    assert plan.clip_paths == ()
    assert plan.paths == (video.resolve(), session.resolve())


def test_recording_deletion_plan_skips_paths_that_cannot_be_checked(monkeypatch, tmp_path):
    video = tmp_path / "game.mp4"
    session = tmp_path / "session.json"
    video.write_bytes(b"video")
    session.write_text("{}", encoding="utf-8")
    resolved_video = video.resolve()
    original_exists = Path.exists

    def fail_video_exists(path: Path):
        if path.resolve() == resolved_video:
            raise OSError("access denied")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", fail_video_exists)

    plan = RecordingDeletionPlan(json_path=session.resolve(), video_path=resolved_video)

    assert plan.paths == (session.resolve(),)
