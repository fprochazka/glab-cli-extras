from __future__ import annotations

import datetime as dt
import io
import zipfile

import pytest

from glab_cli_extras.pipeline.extras import _safe_extract_zip
from glab_cli_extras.pipeline.models import parse_job, parse_pipeline
from glab_cli_extras.pipeline.summary import (
    artifact_is_live,
    decide_extras,
)

NOW = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.UTC)


def _archive_job(*, job_id: int = 1, expire_at: str | None, file_type: str = "archive"):
    return parse_job(
        {
            "id": job_id,
            "name": "build:assets",
            "stage": "build",
            "status": "success",
            "web_url": "https://gitlab.example.com/x/y/-/jobs/1",
            "artifacts_expire_at": expire_at,
            "artifacts": [{"file_type": file_type, "size": 1, "filename": "a.zip", "file_format": "zip"}],
        }
    )


class TestArtifactIsLive:
    def test_non_archive_excluded(self):
        assert artifact_is_live(_archive_job(expire_at=None, file_type="junit"), NOW) is False

    def test_null_expiry_included(self):
        assert artifact_is_live(_archive_job(expire_at=None), NOW) is True

    def test_future_expiry_included(self):
        assert artifact_is_live(_archive_job(expire_at="2026-12-31T00:00:00.000Z"), NOW) is True

    def test_past_expiry_excluded(self):
        assert artifact_is_live(_archive_job(expire_at="2026-01-01T00:00:00.000Z"), NOW) is False

    def test_unparseable_expiry_treated_as_live(self):
        assert artifact_is_live(_archive_job(expire_at="not-a-date"), NOW) is True

    def test_no_archive_at_all_excluded(self, sample_job_success):
        assert artifact_is_live(parse_job(sample_job_success), NOW) is False


class TestArtefactSelection:
    def test_force_off_selects_nothing(self, sample_pipeline_success, sample_job_with_archive):
        p = parse_pipeline(sample_pipeline_success)
        jobs = [parse_job(sample_job_with_archive)]
        plan = decide_extras(p, jobs, [], now=NOW)
        assert plan.need_artefacts is False
        assert plan.artefact_jobs == ()

    def test_force_selects_live_archive_only(
        self, sample_pipeline_success, sample_job_with_archive, sample_job_with_only_junit, sample_job_success
    ):
        p = parse_pipeline(sample_pipeline_success)
        jobs = [
            parse_job(sample_job_with_archive),  # live archive → selected
            parse_job(sample_job_with_only_junit),  # junit only → excluded
            parse_job(sample_job_success),  # no artifacts → excluded
        ]
        plan = decide_extras(p, jobs, [], force_artefacts=True, now=NOW)
        assert plan.need_artefacts is True
        assert plan.artefact_jobs == (1005,)

    def test_force_excludes_expired_archive(self, sample_pipeline_success, sample_job_with_archive):
        p = parse_pipeline(sample_pipeline_success)
        raw = {**sample_job_with_archive, "artifacts_expire_at": "2026-01-01T00:00:00.000Z"}
        plan = decide_extras(p, [parse_job(raw)], [], force_artefacts=True, now=NOW)
        assert plan.need_artefacts is True
        assert plan.artefact_jobs == ()

    def test_force_includes_null_expiry_archive(self, sample_pipeline_success, sample_job_with_archive):
        p = parse_pipeline(sample_pipeline_success)
        raw = {**sample_job_with_archive, "artifacts_expire_at": None}
        plan = decide_extras(p, [parse_job(raw)], [], force_artefacts=True, now=NOW)
        assert plan.artefact_jobs == (1005,)


def _make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


class TestSafeExtractZip:
    def test_extracts_normal_zip(self, tmp_path):
        zip_path = tmp_path / "a.zip"
        zip_path.write_bytes(_make_zip({"hello.txt": b"hi there", "sub/dir/file.bin": b"\x00\x01\x02\x03"}))
        dest = tmp_path / "out"
        dest.mkdir()
        file_count, total_bytes = _safe_extract_zip(zip_path, dest)
        assert file_count == 2
        assert total_bytes == len(b"hi there") + len(b"\x00\x01\x02\x03")
        assert (dest / "hello.txt").read_bytes() == b"hi there"
        assert (dest / "sub" / "dir" / "file.bin").read_bytes() == b"\x00\x01\x02\x03"

    def test_rejects_zip_slip_member(self, tmp_path):
        zip_path = tmp_path / "evil.zip"
        zip_path.write_bytes(_make_zip({"../evil.txt": b"pwned"}))
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(ValueError, match="escapes destination"):
            _safe_extract_zip(zip_path, dest)
        assert not (tmp_path / "evil.txt").exists()

    def test_corrupt_zip_raises_badzipfile(self, tmp_path):
        zip_path = tmp_path / "broken.zip"
        zip_path.write_bytes(b"this is not a zip")
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(zipfile.BadZipFile):
            _safe_extract_zip(zip_path, dest)
