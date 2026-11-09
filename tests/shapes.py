"""Builders for GitLab response bodies, in the shapes recorded from a GitLab 19.5 instance. All names are invented."""

from __future__ import annotations

from typing import Any

HOST = "gitlab.example.com"
PROJECT = "group/project"


def gql_job(
    job_id: int,
    name: str,
    status: str,
    *,
    stage: str = "test",
    allow_failure: bool = False,
    trace_size: int | None = None,
) -> dict[str, Any]:
    artifacts = [{"fileType": "TRACE", "size": trace_size}] if trace_size is not None else []
    return {
        "id": f"gid://gitlab/Ci::Build/{job_id}",
        "name": name,
        "status": status.upper(),
        "stage": {"name": stage},
        "allowFailure": allow_failure,
        "startedAt": None if status in ("created", "pending") else "2026-11-09T10:00:00Z",
        "finishedAt": "2026-11-09T10:05:00Z" if status in ("success", "failed", "canceled") else None,
        "artifacts": {"nodes": artifacts},
    }


def gql_pipeline(
    pipeline_id: int,
    status: str,
    jobs: list[dict[str, Any]],
    downstream: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "project": {
            "pipeline": {
                "id": f"gid://gitlab/Ci::Pipeline/{pipeline_id}",
                "iid": "77",
                "status": status.upper(),
                "ref": "feature/login-form",
                "sha": "a" * 40,
                "path": f"/{PROJECT}/-/pipelines/{pipeline_id}",
                "jobs": {"nodes": jobs},
                "downstream": {"nodes": downstream or []},
            }
        }
    }


def gql_downstream(pipeline_id: int, status: str, jobs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": f"gid://gitlab/Ci::Pipeline/{pipeline_id}",
        "iid": "12",
        "status": status.upper(),
        "path": f"/group/deploy-tools/-/pipelines/{pipeline_id}",
        "project": {"fullPath": "group/deploy-tools"},
        "jobs": {"nodes": jobs},
    }


def gql_thread(
    discussion_sha: str,
    notes: list[tuple[int, str, str]],
    *,
    resolvable: bool = True,
    resolved: bool = False,
    system: bool = False,
) -> dict[str, Any]:
    return {
        "id": f"gid://gitlab/Discussion/{discussion_sha}",
        "resolvable": resolvable,
        "resolved": resolved,
        "notes": {
            "nodes": [
                {
                    "id": f"gid://gitlab/DiscussionNote/{note_id}",
                    "updatedAt": updated_at,
                    "system": system,
                    "author": {"username": author},
                }
                for note_id, updated_at, author in notes
            ]
        },
    }


def gql_mr(
    *,
    iid: int = 42,
    state: str = "opened",
    title: str = "Add the login form",
    description: str = "Adds a login form.",
    draft: bool = False,
    merge_status: str = "MERGEABLE",
    head_sha: str = "b" * 40,
    approved_by: list[str] | None = None,
    approvals_left: int = 1,
    reviewers: list[str] | None = None,
    pipeline_id: int | None = 5001,
    pipeline_status: str = "RUNNING",
    pipeline_sha: str | None = None,
    failed_jobs: list[str] | None = None,
    threads: list[dict[str, Any]] | None = None,
    has_next_page: bool = False,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "iid": str(iid),
        "state": state,
        "title": title,
        "description": description,
        "draft": draft,
        "detailedMergeStatus": merge_status,
        "diffHeadSha": head_sha,
        "sourceBranch": "feature/login-form",
        "targetBranch": "main",
        "webUrl": f"https://{HOST}/{PROJECT}/-/merge_requests/{iid}",
        "approved": not approvals_left,
        "approvalsLeft": approvals_left,
        "approvedBy": {"nodes": [{"username": u} for u in approved_by or []]},
        "reviewers": {"nodes": [{"username": u} for u in reviewers or ["reviewer-one"]]},
        "headPipeline": (
            {
                "id": f"gid://gitlab/Ci::Pipeline/{pipeline_id}",
                "status": pipeline_status,
                "sha": pipeline_sha or head_sha,
                "path": f"/{PROJECT}/-/pipelines/{pipeline_id}",
                "project": {"fullPath": PROJECT},
                "jobs": {"nodes": [{"name": n, "allowFailure": False} for n in failed_jobs or []]},
            }
            if pipeline_id
            else None
        ),
    }
    if threads is not None:
        node["discussions"] = {
            "pageInfo": {"hasNextPage": has_next_page, "endCursor": "cursor-1"},
            "nodes": threads,
        }
    return {"project": {"mergeRequest": node}}


def rest_discussion(
    discussion_sha: str, notes: list[tuple[int, str, str]], *, resolved: bool = False, diff: bool = False
) -> dict[str, Any]:
    rendered = []
    for note_id, author, body in notes:
        note: dict[str, Any] = {
            "id": note_id,
            "type": "DiffNote" if diff else "DiscussionNote",
            "body": body,
            "author": {"id": note_id, "username": author},
            "created_at": "2026-11-09T09:30:00.000Z",
            "updated_at": "2026-11-09T09:31:00.000Z",
            "system": False,
            "resolvable": True,
            "resolved": resolved,
        }
        if diff:
            note["position"] = {
                "base_sha": "c" * 40,
                "start_sha": "c" * 40,
                "head_sha": "b" * 40,
                "old_path": "src/login.py",
                "new_path": "src/login.py",
                "position_type": "text",
                "old_line": None,
                "new_line": 17,
            }
        rendered.append(note)
    return {"id": discussion_sha, "individual_note": False, "notes": rendered}


def rest_mr(
    iid: int,
    state: str,
    updated_at: str,
    *,
    created_at: str = "2026-11-01T08:00:00.000Z",
    title: str = "Refactor the session store",
) -> dict[str, Any]:
    return {
        "id": 900000 + iid,
        "iid": iid,
        "title": title,
        "state": state,
        "created_at": created_at,
        "updated_at": updated_at,
        "author": {"username": "author-one"},
        "source_branch": f"feature/change-{iid}",
        "target_branch": "main",
        "draft": False,
        "web_url": f"https://{HOST}/{PROJECT}/-/merge_requests/{iid}",
    }


def rest_push_event(
    event_id: int,
    ref: str | None,
    action: str,
    commit_from: str | None,
    commit_to: str | None,
    *,
    title: str = "Fix the date parser",
    ref_count: int | None = None,
) -> dict[str, Any]:
    return {
        "id": event_id,
        "project_id": 321,
        "action_name": "pushed to",
        "author_username": "author-one",
        "push_data": {
            "commit_count": 0 if ref is None else 1,
            "action": action,
            "ref_type": "branch" if ref is not None else None,
            "commit_from": commit_from,
            "commit_to": commit_to,
            "ref": ref,
            "commit_title": title,
            "ref_count": ref_count,
        },
    }


def gql_branch_pipelines(entries: list[tuple[int, str]], project_id: int = 321) -> dict[str, Any]:
    return {
        "project": {
            "id": f"gid://gitlab/Project/{project_id}",
            "pipelines": {
                "nodes": [
                    {
                        "id": f"gid://gitlab/Ci::Pipeline/{pid}",
                        "iid": str(pid - 5000),
                        "status": status.upper(),
                        "sha": "d" * 40,
                        "path": f"/{PROJECT}/-/pipelines/{pid}",
                        "createdAt": "2026-11-09T10:00:00Z",
                    }
                    for pid, status in entries
                ]
            },
        }
    }
