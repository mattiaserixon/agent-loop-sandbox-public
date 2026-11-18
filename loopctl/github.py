"""loopctl's only way to read GitHub, and it can only read (invariant I11, decision record 0028, proof test T-INV-11 a).

The controller reads its target through the access the platform gives the session for attached repositories: HTTP
`GET`/`HEAD` to api.github.com (the platform authenticates the request; loopctl sends no credential) and
`git fetch`/`git ls-remote`. That credential can write as the owner, so read-only is enforced here, before any I/O:
any other HTTP method or git subcommand raises `ReadOnlyViolation`. The public surface is exactly `READ_SURFACE`; a
test fails if anything else becomes reachable. Writes to the state repo are not GitHub reads and live in the store.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

READ_METHODS = frozenset({"GET", "HEAD"})
GIT_READ_SUBCOMMANDS = frozenset({"fetch", "ls-remote"})
API = "https://api.github.com"
# Everything a caller may touch. tests/test_github_readonly.py fails if the module exposes more.
READ_SURFACE = frozenset({"ReadOnlyViolation", "Response", "GitHubReader", "GitHubReader.get", "GitHubReader.head",
                          "git_read"})  # fmt: skip


class ReadOnlyViolation(PermissionError):
    """Something tried to write to GitHub through loopctl's read-only client."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    data: Any


def _require_read_method(method: str) -> str:
    if method not in READ_METHODS:
        raise ReadOnlyViolation(f"loopctl's GitHub client only reads (GET, HEAD); refused {method!r}")
    return method


def _require_git_read(args: Sequence[str]) -> list[str]:
    argv = list(args)
    if not argv or argv[0] not in GIT_READ_SUBCOMMANDS:
        raise ReadOnlyViolation(f"loopctl's git reads are fetch and ls-remote only; refused {argv[:1]!r}")
    if any(a in ("--upload-pack", "-u", "--exec") or a.startswith(("--upload-pack=", "--exec=")) for a in argv):
        raise ReadOnlyViolation("loopctl's git reads don't run custom remote programs")
    return argv


def _require_repository_path(path: str) -> None:
    """A path is `/segment/segment…` under `/repos/<repo>`: no scheme, query, fragment, backslash, control character,
    empty segment or `.`/`..` segment, also after percent-decoding (any number of times), so `%2e%2e` can't climb."""
    decoded, previous = path, None
    while decoded != previous:  # decode until stable: %252e -> %2e -> .
        previous, decoded = decoded, urllib.parse.unquote(decoded)
    bad = (not path.startswith("/") or any(c in decoded for c in "?#\\") or any(ord(c) < 32 or ord(c) == 127 for c in decoded)
           or any(segment in ("", ".", "..") for segment in decoded[1:].split("/")) or "://" in decoded)
    if bad:
        raise ValueError(f"path must be relative to the repository, got {path!r}")


class GitHubReader:
    """`GitHubReader("owner/name").get("/issues", {"state": "open"})`. The opener is injectable for tests."""

    def __init__(self, repo: str, opener: Callable[..., Any] = urllib.request.urlopen, api: str = API,
                 timeout: float = 20.0) -> None:  # fmt: skip
        self._repo = repo
        self._opener = opener
        self._api = api
        self._timeout = timeout

    def get(self, path: str, params: Mapping[str, str | int] | None = None) -> Response:
        return self._request("GET", path, params)

    def head(self, path: str) -> Response:
        return self._request("HEAD", path, None)

    def _request(self, method: str, path: str, params: Mapping[str, str | int] | None) -> Response:
        _require_read_method(method)  # before anything is built or sent
        _require_repository_path(path)
        url = f"{self._api}/repos/{self._repo}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, method=method)  # no body, no credential: the platform authenticates
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "loopctl")
        try:
            with self._opener(request, timeout=self._timeout) as reply:
                status, headers, body = reply.status, dict(reply.headers), reply.read()
        except urllib.error.HTTPError as exc:
            status, headers, body = exc.code, dict(exc.headers or {}), exc.read()
        data = json.loads(body) if body and method == "GET" else None
        return Response(status, headers, data)


def git_read(args: Sequence[str], cwd: str | None = None,
             runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> subprocess.CompletedProcess[str]:  # fmt: skip  # noqa: E501
    """Run `git fetch …` or `git ls-remote …`; anything else raises before git starts."""
    argv = _require_git_read(args)
    return runner(["git", *argv], cwd=cwd, capture_output=True, text=True, timeout=60, check=False)

