import mimetypes
import os
import re
from pathlib import Path
from typing import Optional
from typing import Union
from zlib import adler32

from flask import abort
from flask import Blueprint
from flask import current_app
from flask import g
from flask import render_template
from flask import request
from flask import Response
from flask import send_file
from flask import url_for
from werkzeug.exceptions import NotFound
from werkzeug.security import safe_join
from werkzeug.utils import append_slash_redirect

from lektor.assets import Asset
from lektor.assets import Directory
from lektor.constants import PRIMARY_ALT
from lektor.db import Record

bp = Blueprint("serve", __name__)


Filename = Union[str, os.PathLike]


def _rewrite_html_for_editing(html: bytes, edit_url: str) -> bytes:
    """Adds an "edit pencil" button to the text of an HTML page.

    The pencil will link to ``edit_url``.
    """
    button_script = render_template("edit-button.html", edit_url=edit_url)

    def button(m):
        return button_script.encode("utf-8") + m.group(0)

    return re.sub(rb"(?i)</\s*head\s*>|\Z", button, html, count=1)


def _send_html_for_editing(
    filename: Filename, edit_url: str, mimetype: str = "text/html"
) -> Response:
    """Serve an HTML file, after mangling it to add an "edit pencil" button."""
    try:
        with open(filename, "rb") as fp:
            html = fp.read()
            st = os.stat(fp.fileno())
    except OSError:
        abort(404)
    html = _rewrite_html_for_editing(html, edit_url)
    check = adler32(f"{filename}\0{edit_url}".encode("utf-8")) & 0xFFFFFFFF
    resp = Response(html, mimetype=mimetype)
    resp.set_etag(f"{st.st_mtime}-{st.st_size}-{check}")
    return resp


def _deduce_mimetype(filename: Filename) -> str:
    mimetype = mimetypes.guess_type(filename)[0]
    if mimetype is None:
        mimetype = "application/octet-stream"
    return mimetype


def _safe_send_file(filename: Filename, mimetype: Optional[str] = None) -> Response:
    try:
        resp = send_file(filename, mimetype=mimetype)
    except OSError:  # FileNotFoundError, PermissionError
        abort(404)
    return resp


def _get_index_html(directory: Directory) -> Asset:
    """Find an index.html (or equivalent) asset for a Directory asset."""
    for name in "index.html", "index.htm":
        index = directory.get_child(name, from_url=True)
        if index is not None:
            break
    else:
        abort(404)
    return index


class ArtifactServer:
    """Resolve url_path to a Lektor source object, build it, serve the result.

    Redirects to slash-appended path if appropriate.

    Raises NotFound if source object can not be resolved, or if it does not
    produce an artifact.

    """

    def __init__(self, lektor_context):
        self.lektor_ctx = lektor_context

    def resolve_url_path(self, url_path):
        pad = self.lektor_ctx.pad
        source = pad.resolve_url_path(url_path)
        if source is None:
            # if not found, try stripping trailing "index.html"
            url_head, sep, url_tail = url_path.rpartition("/")
            if url_tail == "index.html":
                source = pad.resolve_url_path(url_head + sep)
            if not isinstance(source, Record):
                # For asset Directories, we implicity add an
                # index.html or index.htm back on, whichever exists.  If
                # we implicitly strip an index.html here, we might end
                # up adding an index.htm later, and thus would serve
                # the index.htm with the index.html was explicitly
                # requested.
                source = None
        if source is None:
            abort(404)
        return source

    def build_primary_artifact(self, source):
        with self.lektor_ctx.cli_reporter():
            prog, _ = self.lektor_ctx.builder.build(source)
        artifact = prog.primary_artifact
        if artifact is None:
            abort(404)
        return artifact

    def lookup_build_failure(self, artifact):
        return self.lektor_ctx.failure_controller.lookup_failure(artifact.artifact_name)

    @staticmethod
    def handle_build_failure(failure, edit_url=None):
        html = render_template("build-failure.html", **failure.data).encode("utf-8")
        if edit_url is not None:
            html = _rewrite_html_for_editing(html, edit_url)
        return Response(html, mimetype="text/html")

    def get_edit_url(self, source):
        primary_alternative = self.lektor_ctx.config.primary_alternative
        if not isinstance(source, Record):
            # Asset or VirtualSourceObject — not editable
            return None
        record = source.record
        alt = (
            record.alt if record.alt not in (PRIMARY_ALT, primary_alternative) else None
        )
        return url_for("url.edit", path=record.path, alt=alt)

    def serve_artifact(self, url_path):
        source = self.resolve_url_path(url_path)

        # If the request path does not end with a slash but we
        # requested a URL that actually wants a trailing slash, we
        # append it.  This is consistent with what apache and nginx do
        # and it ensures our relative urls work.
        if (
            not url_path.endswith("/")
            and source.url_path.endswith("/")
            and source.url_path != "/"
        ):
            return append_slash_redirect(request.environ)

        if isinstance(source, Directory):
            # Special case for asset directories: resolve to index.html if possible
            source = _get_index_html(source)

        artifact = self.build_primary_artifact(source)
        edit_url = self.get_edit_url(source)

        # If there was a build failure for the given artifact, we want
        # to render this instead of sending the (most likely missing or
        # corrupted) file.
        failure = self.lookup_build_failure(artifact)
        if failure is not None:
            return self.handle_build_failure(failure, edit_url)

        mimetype = _deduce_mimetype(artifact.dst_filename)
        if mimetype == "text/html" and edit_url is not None:
            return _send_html_for_editing(artifact.dst_filename, edit_url, mimetype)
        return _safe_send_file(artifact.dst_filename, mimetype=mimetype)


def serve_artifact(path):
    if not hasattr(g, "artifact_server"):
        # pylint: disable=assigning-non-slot
        lektor_ctx = current_app.lektor_info.make_lektor_context()
        g.artifact_server = ArtifactServer(lektor_ctx)
    return g.artifact_server.serve_artifact(path)


def serve_file(path):
    """Serve file directly from Lektor's output directory."""
    output_path = current_app.lektor_info.output_path

    safe_path = safe_join("", *(path.strip("/").split("/")))
    if safe_path is None:
        abort(404)

    filename = Path(output_path, safe_path)  # coverts safe_path to native path seps
    if filename.is_dir():
        if not path.endswith("/"):
            return append_slash_redirect(request.environ)
        for index in filename / "index.html", filename / "index.htm":
            if index.is_file():
                return _safe_send_file(index, mimetype="text/html")
        abort(404)

    return _safe_send_file(filename, mimetype=_deduce_mimetype(filename.name))


@bp.route("/", defaults={"path": ""})
@bp.route("/<path:path>")
def serve_artifact_or_file(path):
    try:
        return serve_artifact(path)
    except NotFound:
        return serve_file(path)


@bp.errorhandler(404)
def serve_error_page(error):
    try:
        return serve_artifact("404.html"), 404
    except NotFound as e:
        return e
