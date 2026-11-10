"""glab-search-code: the search, the downloads and the files of a run, on a fake glab."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from fakeglab import FakeGlab, Route, render
from glab_cli_extras.cli import search_code
from glab_cli_extras.gitlab.client import GitLabClient
from glab_cli_extras.gitlab.glab import GlabRunner, ProcessResult
from glab_cli_extras.gitlab.refs import RefError, Repo
from glab_cli_extras.gitlab.retry import RetryPolicy
from glab_cli_extras.search.files import output_path, shorten
from glab_cli_extras.search.run import SearchOptions, run_search

HOST = "gitlab.example.com"


def hit(project_id: int, path: str, startline: int = 1, ref: str = "main", data: str = "@GeneratedValue\n") -> Any:
    return {
        "basename": path.rsplit(".", 1)[0],
        "data": data,
        "path": path,
        "filename": path,
        "id": None,
        "ref": ref,
        "startline": startline,
        "project_id": project_id,
    }


def projects_route(*projects: tuple[int, str]) -> Route:
    nodes = [
        {"id": f"gid://gitlab/Project/{pid}", "fullPath": path, "webUrl": f"https://{HOST}/{path}"}
        for pid, path in projects
    ]
    return Route("projects(ids", {"data": {"projects": {"nodes": nodes}}})


async def no_sleep(seconds: float) -> None:
    return None


def client(runner: GlabRunner, attempts: int = 5) -> GitLabClient:
    return GitLabClient(HOST, runner=runner, max_concurrency=4, retry_reads=RetryPolicy(attempts, sleep=no_sleep))


def search(runner: GlabRunner, directory: Path, **kwargs: Any) -> Any:
    options = SearchOptions(host=HOST, query=kwargs.pop("query", "GeneratedValue"), directory=directory, **kwargs)
    return asyncio.run(run_search(options, client(runner)))


def lines(path: Path) -> list[str]:
    return [line.split("] ", 1)[1] for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_search_downloads_each_matching_file_once_into_the_project_tree(tmp_path: Path) -> None:
    fake = FakeGlab(
        [
            Route("search?", [hit(1, "src/App.java", 3), hit(1, "src/App.java", 40), hit(2, "lib/Entity.java")]),
            projects_route((1, "group/app"), (2, "group/sub/lib")),
            Route("projects/1/repository/files/src%2FApp.java/raw", "class App {}\n"),
            Route("projects/2/repository/files/lib%2FEntity.java/raw", "class Entity {}\n"),
        ]
    )
    summary = search(fake, tmp_path / "run")

    assert (summary.hits, summary.files, summary.projects, summary.downloaded) == (3, 2, 2, 2)
    assert (tmp_path / "run/files/group/app/src/App.java").read_text() == "class App {}\n"
    assert (tmp_path / "run/files/group/sub/lib/lib/Entity.java").read_text() == "class Entity {}\n"
    assert sum("/repository/files/" in e for e in fake.endpoints()) == 2
    assert sum(e == "graphql" for e in fake.endpoints()) == 1
    metadata = json.loads((tmp_path / "run/metadata.json").read_text())
    assert metadata[0] == {
        "project_id": 1,
        "project_path": "group/app",
        "file_path": "src/App.java",
        "ref": "main",
        "output_file": "files/group/app/src/App.java",
        "url": f"https://{HOST}/group/app/-/blob/main/src/App.java",
        "hits": [3, 40],
    }
    hits = json.loads((tmp_path / "run/hits.json").read_text())
    assert [(h["project_path"], h["path"], h["startline"]) for h in hits] == [
        ("group/app", "src/App.java", 3),
        ("group/app", "src/App.java", 40),
        ("group/sub/lib", "lib/Entity.java", 1),
    ]
    assert hits[0]["data"] == "@GeneratedValue\n" and hits[0]["ref"] == "main"
    assert (tmp_path / "run/README.txt").is_file()
    assert lines(tmp_path / "run/download.log")[-1] == "Done: OK 2, SKIP 0, FAIL 0"


def test_the_query_and_the_ref_are_url_encoded(tmp_path: Path) -> None:
    query = "a&b #c+d %e"
    fake = FakeGlab(
        [
            Route("search?", [hit(1, "docs/read me.md", ref="feature/x&y")]),
            projects_route((1, "group/project")),
            Route("/repository/files/", "text"),
        ]
    )
    search(fake, tmp_path / "run", query=query)
    search_call, _, file_call = fake.endpoints()
    assert "search=a%26b+%23c%2Bd+%25e" in search_call
    assert file_call == "projects/1/repository/files/docs%2Fread%20me.md/raw?ref=feature%2Fx%26y"


def test_pages_are_followed_by_x_next_page(tmp_path: Path) -> None:
    fake = FakeGlab(
        [
            Route("&page=2", [hit(1, "b.py")]),
            Route("search?", [hit(1, "a.py")], headers={"X-Next-Page": "2"}),
            projects_route((1, "group/project")),
            Route("/repository/files/", "x"),
        ]
    )
    summary = search(fake, tmp_path / "run")
    assert summary.files == 2 and not summary.truncated


def test_a_search_that_reaches_the_page_cap_is_reported_as_truncated(tmp_path: Path) -> None:
    page = [hit(1, f"f{i}.py") for i in range(100)]
    fake = FakeGlab([Route("search?", page, headers={"X-Next-Page": "2"})])

    class OnlySearch:
        async def __call__(self, args: Sequence[str], timeout: float, stdin: bytes | None = None) -> ProcessResult:
            if args[-1] != "graphql" and "search?" not in args[-1]:
                return ProcessResult(1, render(404, {"message": "404 Not Found"}), b"")
            return await fake(args, timeout, stdin)

    summary = search(OnlySearch(), tmp_path / "run")
    assert summary.hits == 10_000 and summary.truncated
    assert search_code.format_summary("q", summary).count("Search stopped after 10000 hits") == 1


def test_a_binary_file_is_written_byte_for_byte(tmp_path: Path) -> None:
    payload = bytes(range(256)) * 4
    fake = FakeGlab(
        [Route("search?", [hit(1, "img/logo.png")]), projects_route((1, "group/p")), Route("/files/", payload)]
    )
    search(fake, tmp_path / "run")
    assert (tmp_path / "run/files/group/p/img/logo.png").read_bytes() == payload


def test_a_project_the_lookup_misses_goes_under_its_id(tmp_path: Path) -> None:
    fake = FakeGlab([Route("search?", [hit(7, "a.py")]), projects_route(), Route("/files/", "x")])
    search(fake, tmp_path / "run")
    assert (tmp_path / "run/files/project-7/a.py").is_file()
    assert "FAIL: no path for project 7; its files go under project-7" in lines(tmp_path / "run/download.log")


def test_projects_are_looked_up_100_per_query(tmp_path: Path) -> None:
    fake = FakeGlab([Route("search?", [hit(i, "a.py") for i in range(1, 151)]), projects_route(), Route("/f", "x")])
    search(fake, tmp_path / "run")
    batches = [len(c.variables["ids"]) for c in fake.calls if c.endpoint == "graphql"]
    assert batches == [100, 50]


class Flaky:
    """Answers the first `failures` file downloads with `status`, then as the fake does."""

    def __init__(self, fake: FakeGlab, status: int, failures: int, headers: dict[str, str] | None = None) -> None:
        self.fake, self.status, self.failures, self.headers = fake, status, failures, headers or {}
        self.file_calls = 0

    async def __call__(self, args: Sequence[str], timeout: float, stdin: bytes | None = None) -> ProcessResult:
        if "/repository/files/" in args[-1]:
            self.file_calls += 1
            if self.file_calls <= self.failures:
                return ProcessResult(1, render(self.status, {"message": "nope"}, self.headers), b"")
        return await self.fake(args, timeout, stdin)


def test_a_rate_limited_download_is_retried(tmp_path: Path) -> None:
    fake = FakeGlab([Route("search?", [hit(1, "a.py")]), projects_route((1, "g/p")), Route("/files/", "x")])
    runner = Flaky(fake, 429, 2, {"Retry-After": "1"})
    summary = search(runner, tmp_path / "run")
    assert (summary.downloaded, summary.failed, runner.file_calls) == (1, 0, 3)


def test_a_forbidden_download_is_not_retried_and_is_logged(tmp_path: Path) -> None:
    fake = FakeGlab(
        [Route("search?", [hit(1, "a.py"), hit(1, "b.py")]), projects_route((1, "g/p")), Route("/files/", "x")]
    )
    runner = Flaky(fake, 403, 1)
    summary = search(runner, tmp_path / "run")
    assert (summary.downloaded, summary.failed, runner.file_calls) == (1, 1, 2)
    failures = [line for line in lines(tmp_path / "run/download.log") if line.startswith("FAIL:")]
    assert len(failures) == 1 and "nope (HTTP 403)" in failures[0]
    assert [entry["file_path"] for entry in json.loads((tmp_path / "run/metadata.json").read_text())] == ["b.py"]


def test_two_files_that_map_to_one_output_file_download_once(tmp_path: Path) -> None:
    fake = FakeGlab(
        [Route("search?", [hit(1, "a:b.py"), hit(1, "a?b.py")]), projects_route((1, "g/p")), Route("/files/", "x")]
    )
    summary = search(fake, tmp_path / "run")
    assert (summary.downloaded, summary.skipped) == (1, 1)
    assert any(line.startswith("SKIP: g/p/a?b.py @ main: maps to") for line in lines(tmp_path / "run/download.log"))


def test_a_long_component_is_shortened_to_255_bytes_with_a_hash() -> None:
    name = "é" * 200 + ".java"
    short = shorten(name)
    assert len(short.encode()) <= 255 and short.endswith(".java")
    assert short != shorten("é" * 201 + ".java")
    assert shorten("App.java") == "App.java"


def test_a_path_never_leaves_the_run_dir() -> None:
    assert output_path("group/p", "/../a/./b\\c\x00.py").as_posix() == "files/group/p/__/a/_/b_c_.py"


def test_no_hits_still_writes_the_index_files(tmp_path: Path) -> None:
    summary = search(FakeGlab([Route("search?", [])]), tmp_path / "run")
    assert summary.hits == 0
    assert json.loads((tmp_path / "run/metadata.json").read_text()) == []
    assert json.loads((tmp_path / "run/hits.json").read_text()) == []


def test_the_scope_flags_pick_the_endpoint(tmp_path: Path) -> None:
    fake = FakeGlab([Route("search?", [])])
    search(fake, tmp_path / "a", project="group/sub/project")
    search(fake, tmp_path / "b", group="group/sub")
    assert [e.split("?")[0] for e in fake.endpoints()] == [
        "projects/group%2Fsub%2Fproject/search",
        "groups/group%2Fsub/search",
    ]


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> FakeGlab:
    fake = FakeGlab()
    monkeypatch.setattr(search_code, "make_client", lambda host, workers, attempts, verbose: client(fake, attempts))
    monkeypatch.setattr(search_code, "resolve_repo", lambda spec: Repo(HOST, "group/project"))
    return fake


def test_the_cli_prints_the_summary_and_ends_with_the_paths(
    cli: FakeGlab, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.routes += [Route("search?", [hit(1, "a.py")]), projects_route((1, "group/project")), Route("/files/", "x")]
    assert search_code.main(["GeneratedValue"]) == 0
    captured = capsys.readouterr()
    out = captured.out.splitlines()
    directory = Path(out[-2].removeprefix("Output directory: "))
    assert directory.parent == tmp_path / "base" / HOST / "-" / "search"
    assert "-GeneratedValue-" in directory.name
    assert out[-1] == f"Metadata file: {directory / 'metadata.json'}"
    assert out[-3] == f"Hits file: {directory / 'hits.json'}"
    assert out[:3] == [
        "Search query: GeneratedValue",
        "Found 1 hits in 1 files of 1 projects",
        "Total files downloaded: 1",
    ]
    assert "Progress: 1/1 (100%)" in captured.err


def test_the_cli_output_dir_names_the_run_dir(cli: FakeGlab, tmp_path: Path) -> None:
    cli.routes.append(Route("search?", []))
    assert search_code.main(["q", "--hostname", HOST, "--output-dir", str(tmp_path / "mine")]) == 0
    assert (tmp_path / "mine" / "metadata.json").is_file()


def test_no_hits_exit_0(cli: FakeGlab, capsys: pytest.CaptureFixture[str]) -> None:
    cli.routes.append(Route("search?", []))
    assert search_code.main(["nothing-matches-this"]) == 0
    assert "No results found" in capsys.readouterr().out


def test_a_400_on_an_instance_search_names_advanced_search(cli: FakeGlab, capsys: pytest.CaptureFixture[str]) -> None:
    cli.routes.append(Route("search?", {"error": "Scope supported only with advanced search"}, status=400))
    assert search_code.main(["q"]) == 1
    err = capsys.readouterr().err
    assert "glab-search-code: error: Scope supported only with advanced search" in err
    assert "--project" in err


def test_without_hostname_outside_a_repo_is_a_usage_error(
    cli: FakeGlab, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(spec: str | None) -> Repo:
        raise RefError("no git remotes found")

    monkeypatch.setattr(search_code, "resolve_repo", fail)
    with pytest.raises(SystemExit) as raised:
        search_code.main(["q"])
    assert raised.value.code == 2
    assert "pass --hostname" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["q", "--workers", "0"],
        ["q", "--workers", "51"],
        ["q", "--max-retries", "0"],
        ["q", "--project", "g/p", "--group", "g"],
        [" "],
    ],
)
def test_bad_options_are_usage_errors(cli: FakeGlab, args: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        search_code.main(args)
    assert raised.value.code == 2
    assert cli.calls == []
