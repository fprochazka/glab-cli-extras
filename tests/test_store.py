from __future__ import annotations

import threading
from pathlib import Path

from glab_cli_extras.store.atomic import atomic_write_bytes, atomic_write_json


def test_concurrent_writers_of_one_file_never_fail_or_mix_their_bytes(tmp_path: Path) -> None:
    target = tmp_path / "dir" / "state.json"
    payloads = [bytes([65 + n]) * 200_000 for n in range(8)]
    errors: list[BaseException] = []

    def writer(data: bytes) -> None:
        try:
            for _ in range(20):
                atomic_write_bytes(target, data)
        except BaseException as exc:  # pragma: no cover - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(p,)) for p in payloads]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert target.read_bytes() in payloads
    assert [p.name for p in target.parent.iterdir()] == ["state.json"]


def test_json_is_written_as_utf8(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    atomic_write_json(target, {"name": "Žluťoučký kůň"})
    assert "Žluťoučký kůň" in target.read_text(encoding="utf-8")
