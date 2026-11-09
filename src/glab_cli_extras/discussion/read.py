"""glab-discussion read: one file per thread, kept up to date incrementally in the MR's dir.

The MR snapshot (one GraphQL request per 100 threads) gives every thread's note versions. A thread is fetched again
only when its notes changed since its file was written (`sig` in `.meta.json`), or when the user's draft replies in it
changed (`drafts_sig`), or its file is missing. The drafts list is one more request. When nothing changed, that is the
whole cost of a run. The dir is the same one `glab-watch mr` writes; a watch renders published notes only, and a read
puts the user's draft replies back.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from glab_cli_extras.discussion.context import resolve_mr_context
from glab_cli_extras.discussion.formatter import format_draft_thread
from glab_cli_extras.discussion.models import DraftNote, MrContext, UserInfo, parse_draft_note
from glab_cli_extras.discussion.terminal import is_interactive_terminal
from glab_cli_extras.events import utc_now_iso
from glab_cli_extras.gitlab.client import GitLabClient, encode_project
from glab_cli_extras.gitlab.refs import ResourceRef
from glab_cli_extras.gitlab.retry import standalone_client
from glab_cli_extras.paths import base_dir
from glab_cli_extras.serde import stable_hash, to_jsonable
from glab_cli_extras.store.layout import THREADS_DIR, ensure_readme, mr_dir
from glab_cli_extras.store.state import load_state, locked, put_snapshot, save_state
from glab_cli_extras.store.threads import ThreadStore, render_thread
from glab_cli_extras.sync.mr import MrSnapshot, fetch_discussions, fetch_snapshot, snapshot_users, thread_sig

CURRENT_USER_QUERY = "query { currentUser { username name bot } }"


def drafts_sig(drafts: Sequence[DraftNote]) -> str:
    """The version of the user's draft replies in a thread; empty when there are none."""
    if not drafts:
        return ""
    return stable_hash([(d.id, d.body, d.resolve_discussion) for d in drafts])


async def _fetch(client: GitLabClient, ref: ResourceRef) -> tuple[MrSnapshot, list[DraftNote]]:
    path = f"projects/{encode_project(ref.project)}/merge_requests/{ref.id}/draft_notes"
    snapshot, raw_drafts = await asyncio.gather(
        fetch_snapshot(client, ref, with_threads=True, with_pipeline=True),
        client.get_pages(path, kind="draft_notes"),
    )
    drafts = sorted((parse_draft_note(d) for d in raw_drafts if isinstance(d, dict)), key=lambda d: d.id)
    return snapshot, drafts


async def _current_user(client: GitLabClient) -> UserInfo | None:
    """Drafts carry only an author id, and only the caller's own drafts are listed, so the author is the caller."""
    result = await client.graphql(CURRENT_USER_QUERY, {}, kind="current_user")
    user = (result.data or {}).get("currentUser") if isinstance(result.data, dict) else None
    if not isinstance(user, dict) or not user.get("username"):
        return None
    return UserInfo(0, str(user["username"]), str(user.get("name") or user["username"]), bool(user.get("bot")))


def _ordered_files(store: ThreadStore) -> list[str]:
    """Thread files in creation order, then the files of drafts that start a thread, by draft id."""
    threads = sorted(e["filename"] for k, e in store.meta.items() if not k.startswith("draft-"))
    drafts = [
        e["filename"]
        for k, e in sorted(store.meta.items(), key=lambda i: _draft_key_id(i[0]))
        if k.startswith("draft-")
    ]
    return threads + drafts


def _created(discussion: dict[str, Any]) -> str:
    notes = discussion.get("notes") or [{}]
    return str(notes[0].get("created_at", ""))


def _draft_key_id(key: str) -> int:
    rest = key.removeprefix("draft-")
    return int(rest) if rest.isdigit() else 0


async def sync(
    ctx: MrContext, client: GitLabClient, *, full: bool, base: Path | None = None
) -> tuple[ThreadStore, dict[str, list[str]], bool]:
    """Bring the MR's thread files up to date. Return the store, the files by change, and whether it was a first run."""
    ref = ResourceRef("mr", ctx.hostname, ctx.project_path, ctx.mr_iid)
    directory = mr_dir(base or base_dir(), ctx.hostname, ctx.project_path, ctx.mr_iid)
    threads_dir = directory / THREADS_DIR
    snapshot, drafts = await _fetch(client, ref)
    users = snapshot_users(snapshot)
    if drafts:
        me = await _current_user(client)
        if me is not None:
            users.setdefault(me.username, me)
            for draft in drafts:
                draft.author_username, draft.is_bot = me.username, me.is_bot
    with locked(directory):
        ensure_readme(directory, "mr")
        if full and threads_dir.exists():
            shutil.rmtree(threads_dir)
        store = ThreadStore(threads_dir)
        first_run = not store.meta
        threads = {t.id: t for t in snapshot.threads or ()}
        replies: dict[str, list[DraftNote]] = {}
        standalone: list[DraftNote] = []
        for draft in drafts:
            if draft.discussion_id and draft.discussion_id in threads:
                replies.setdefault(draft.discussion_id, []).append(draft)
            else:
                standalone.append(draft)
        wanted: dict[str, tuple[str, str]] = {}
        for tid, thread in threads.items():
            sig, dsig = thread_sig(thread), drafts_sig(replies.get(tid, ()))
            if store.sig(tid) != sig or store.drafts_sig(tid) != dsig or store.path_for(tid) is None:
                wanted[tid] = (sig, dsig)
        discussions = await fetch_discussions(client, ref, set(wanted)) if wanted else {}
        changes: dict[str, list[str]] = {"new": [], "updated": [], "deleted": []}
        # GitLab's own URL of the MR, as a watch uses it, so both render the same file whatever URL form was passed.
        mr_url = snapshot.web_url or ctx.mr_url
        for tid in sorted(discussions, key=lambda i: _created(discussions[i])):
            sig, dsig = wanted[tid]
            filename, content = render_thread(discussions[tid], mr_url, users, replies.get(tid, ()))
            result = store.write(tid, filename, content, sig=sig, drafts_sig=dsig)
            if result.status != "unchanged":
                changes[result.status].append(filename)
            if result.replaced:
                changes["deleted"].append(result.replaced)
        for draft in standalone:
            filename = f"draft-{draft.id}.txt"
            result = store.write(f"draft-{draft.id}", filename, format_draft_thread(draft))
            if result.status != "unchanged":
                changes[result.status].append(filename)
        keep = set(threads) | {f"draft-{d.id}" for d in standalone}
        for key in sorted(set(store.meta) - keep):
            removed = store.remove(key)
            if removed:
                changes["deleted"].append(removed)
        store.save()
        state = load_state(directory, ref.key)
        put_snapshot(state, to_jsonable(snapshot), utc_now_iso(), threads_observed_at=utc_now_iso())
        save_state(directory, state)
    return store, changes, first_run


def run(args: argparse.Namespace) -> None:
    ctx = resolve_mr_context(args)
    run_with_context(args, ctx, standalone_client(ctx.hostname))


def run_with_context(args: argparse.Namespace, ctx: MrContext, client: GitLabClient) -> None:
    if args.dump:
        dump_mode = True
    elif args.no_dump:
        dump_mode = False
    else:
        dump_mode = not is_interactive_terminal()
    store, changes, first_run = asyncio.run(sync(ctx, client, full=bool(args.full)))
    if not dump_mode:
        blocks = [(store.directory / name).read_text(encoding="utf-8") for name in _ordered_files(store)]
        print("\n\n".join(blocks))
        return
    print("\n".join(dump_lines(store, changes, first_run)))


def dump_lines(store: ThreadStore, changes: dict[str, list[str]], first_run: bool) -> list[str]:
    """What `read --dump` prints: the threads dir, then the files that changed. glab-mr-status prints them too."""
    total = len(store.meta)
    lines = [f"Discussions: {store.directory}/"]
    if first_run:
        lines.append(f"  synced {len(changes['new'])} discussion threads")
    elif changes["new"] or changes["updated"] or changes["deleted"]:
        lines += [f"  updated: {name}" for name in changes["updated"]]
        lines += [f"  new: {name}" for name in changes["new"]]
        lines += [f"  deleted: {name}" for name in changes["deleted"]]
        unchanged = total - len(changes["new"]) - len(changes["updated"])
        if unchanged > 0:
            lines.append(f"  ({unchanged} discussions up to date)")
    else:
        lines.append(f"  ({total} discussions up to date)")
    return lines
