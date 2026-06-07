import json
from pathlib import Path

import pytest

from src.recording_library import RecordingDeletionError, RecordingLibrary


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
