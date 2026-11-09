from __future__ import annotations

from argparse import Namespace
from unittest.mock import patch

from glab_cli_extras.discussion.delete import run
from glab_cli_extras.discussion.ids import NoteRef
from glab_cli_extras.discussion.models import MrContext


class TestDelete:
    def test_delete_note(self, capsys, mr_context: MrContext) -> None:
        with (
            patch("glab_cli_extras.discussion.delete.resolve_mr_context", return_value=mr_context),
            patch("glab_cli_extras.discussion.delete.glab_api") as mock_api,
        ):
            args = Namespace(note_ref=NoteRef("note", 99999))
            run(args)

            mock_api.assert_called_once_with(
                "projects/42/merge_requests/7/notes/99999",
                method="DELETE",
                hostname="gitlab.example.com",
            )

        captured = capsys.readouterr()
        assert "Deleted note:99999" in captured.out

    def test_delete_draft(self, capsys, mr_context: MrContext) -> None:
        with (
            patch("glab_cli_extras.discussion.delete.resolve_mr_context", return_value=mr_context),
            patch("glab_cli_extras.discussion.delete.glab_api") as mock_api,
        ):
            args = Namespace(note_ref=NoteRef("draft", 15))
            run(args)

            mock_api.assert_called_once_with(
                "projects/42/merge_requests/7/draft_notes/15",
                method="DELETE",
                hostname="gitlab.example.com",
            )

        assert "Deleted draft:15" in capsys.readouterr().out
