#!/usr/bin/env python3
"""github-core-clone-01: a GitHub-compatible forge (milestone 1: REST v3 + MCP).

Binds an ephemeral port, registers with the suite at $MAKEFUN_ENV_URL/register,
serves REST v3 under /api/v3 and MCP at /mcp, long-polls /done and exits 0.

Standard library only. Deterministic: no wall clock, no unseeded randomness.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import threading
import time
import zlib
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote

# --------------------------------------------------------------------------
# deterministic scenario logins (the suite's scenario RNG, replicated)
# --------------------------------------------------------------------------
M64 = (1 << 64) - 1
SCENARIO_SEED = 0x6A09E667F3BCC909

FIRST_NAMES = ["ada", "bram", "cora", "dmitri", "elena", "felix", "greta", "hugo", "iris",
               "jonas", "kira", "lars", "mira", "nico", "olga", "pavel", "quinn", "rosa",
               "sven", "tess", "ulla", "vito", "wren", "yara", "zane"]
LAST_NAMES = ["anders", "bauer", "costa", "dahl", "eriksen", "fischer", "garcia", "holm",
              "iversen", "jansen", "keller", "lind", "moreau", "novak", "olsen", "peters",
              "quist", "rossi", "sato", "turi", "ulrich", "vega", "weber", "xu", "yilmaz",
              "zimmer"]


def _fnv1a64(text: str) -> int:
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & M64
    return h


def _mix64(value: int) -> int:
    z = (value + 0x9E3779B97F4A7C15) & M64
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & M64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & M64
    return (z ^ (z >> 31)) & M64


class Rng:
    def __init__(self, seed: int):
        seed &= M64
        self.state = seed if seed else SCENARIO_SEED

    def next_u64(self) -> int:
        x = self.state
        x = (x ^ (x >> 12)) & M64
        x = (x ^ (x << 25)) & M64
        x = (x ^ (x >> 27)) & M64
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & M64

    def next_u32(self) -> int:
        return self.next_u64() >> 32

    def next_int(self, n: int) -> int:
        if n <= 1:
            return 0
        limit = 4294967296 - (4294967296 % n)
        while True:
            v = self.next_u32()
            if v < limit:
                return v % n

    def pick(self, items):
        return items[self.next_int(len(items))]


def scenario_logins():
    rng = Rng(_mix64(SCENARIO_SEED ^ _fnv1a64("users")))
    used = set()
    logins = []
    for _ in range(12):
        base = rng.pick(FIRST_NAMES) + rng.pick(LAST_NAMES)
        login = base
        suffix = 2
        while login in used:
            login = "%s-%d" % (base, suffix)
            suffix += 1
        used.add(login)
        logins.append(login)
    return logins


LOGINS = scenario_logins()
VIEWER = LOGINS[0]

BASE_TS = 1735689600  # the scenario epoch, seconds
_clock = [0]
_id_lock = threading.Lock()


def next_seq() -> int:
    with _id_lock:
        _clock[0] += 1
        return _clock[0]


def stamp() -> tuple[int, str]:
    n = next_seq()
    ts = BASE_TS + n
    return ts, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def sha1_hex(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def blob_oid(data: bytes) -> str:
    return sha1_hex(b"blob %d\0" % len(data) + data)


def tree_oid(files: dict) -> str:
    body = "\n".join("%s %s" % (path, oid) for path, oid in sorted(files.items()))
    return sha1_hex(b"tree %d\0" % len(body) + body.encode("utf-8"))


def commit_oid(tree: str, parents, message: str, when: int, who: str) -> tuple[str, bytes]:
    lines = ["tree %s" % tree]
    for p in parents:
        lines.append("parent %s" % p)
    ident = "%s <%s@users.noreply.github.com> %d +0000" % (who, who, when)
    lines.append("author %s" % ident)
    lines.append("committer %s" % ident)
    body = ("\n".join(lines) + "\n\n" + message + "\n").encode("utf-8")
    return sha1_hex(b"commit %d\0" % len(body) + body), body


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(text: str) -> bytes:
    try:
        return base64.b64decode(text or "", validate=False)
    except Exception:
        return b""


def b64decode_text(text: str) -> str:
    return b64decode(text).decode("utf-8", "replace")


def now_iso() -> str:
    return stamp()[1]


def page_params(query) -> tuple[int, int]:
    try:
        per_page = int(query.get("per_page", ["30"])[0])
    except ValueError:
        per_page = 30
    try:
        page = int(query.get("page", ["1"])[0])
    except ValueError:
        page = 1
    per_page = max(1, min(100, per_page))
    page = max(1, page)
    return per_page, page


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.users = {}
        self.repos = {}
        self.next_repo_id = 5000
        self.next_user_id = 1000
        self.next_issue_id = 100000
        self.next_comment_id = 200000
        self.next_pull_id = 300000
        self.next_label_id = 400000
        self.viewer = self.user(VIEWER)

    def user(self, login: str) -> dict:
        if login not in self.users:
            idx = len(self.users)
            self.users[login] = {
                "login": login,
                "id": 1000 + idx,
                "node_id": "U_%d" % (1000 + idx),
                "type": "User",
                "site_admin": False,
                "avatar_url": "https://avatars.githubusercontent.com/u/%d?v=4" % (1000 + idx),
                "html_url": "https://github.com/%s" % login,
                "url": "/api/v3/users/%s" % login,
            }
        return self.users[login]

    # -- repositories ------------------------------------------------------
    def own_repo(self, owner: str, name: str) -> dict:
        if owner != VIEWER:
            raise ApiError(404, "Not Found")
        repo = self.repos.get((VIEWER, name))
        if repo is None:
            raise ApiError(404, "Not Found")
        return repo

    def create_repo(self, name: str, private: bool = True, description: str = "") -> dict:
        if not isinstance(name, str) or name.strip() == "":
            raise ApiError(422, "Repository creation failed: name is required")
        name = name.strip()
        repo = self.repos.get((VIEWER, name))
        if repo is not None:
            if repo["description"] == (description or "") and repo["private"] == bool(private):
                return repo
            raise ApiError(422, "Repository creation failed: name already exists on this account")
        ts, iso = stamp()
        owner = self.user(VIEWER)
        repo_id = self.next_repo_id
        self.next_repo_id += 1
        repo = {
            "id": repo_id,
            "node_id": "R_%d" % repo_id,
            "name": name,
            "full_name": "%s/%s" % (VIEWER, name),
            "private": bool(private),
            "owner": {"login": owner["login"], "id": owner["id"], "type": "User",
                      "node_id": owner["node_id"],
                      "avatar_url": owner["avatar_url"]},
            "description": description or "",
            # GitHub-shaped scalars, grouped to keep the file compact
            "fork": False, "default_branch": "main",
            "created_at": iso, "updated_at": iso, "pushed_at": iso,
            "html_url": "https://github.com/%s/%s" % (VIEWER, name),
            "url": "/api/v3/repos/%s/%s" % (VIEWER, name),
            "clone_url": "http://localhost/%s/%s.git" % (VIEWER, name),
            "git_url": "git://localhost/%s/%s.git" % (VIEWER, name),
            "ssh_url": "git@localhost:%s/%s.git" % (VIEWER, name),
            "svn_url": "https://github.com/%s/%s" % (VIEWER, name),
            "homepage": None, "language": None, "size": 0, "topics": [], "license": None,
            "stargazers_count": 0, "watchers_count": 0, "forks_count": 0, "network_count": 0,
            "open_issues_count": 0, "open_issues": 0, "watchers": 0, "forks": 0,
            "subscribers_count": 0, "archived": False, "disabled": False,
            "has_issues": True, "has_projects": True, "has_wiki": True, "has_pages": False,
            "has_downloads": True, "has_discussions": False,
            "visibility": "private" if private else "public",
            "permissions": {"admin": True, "maintain": True, "push": True, "triage": True,
                            "pull": True},
            "allow_squash_merge": True, "allow_merge_commit": True, "allow_rebase_merge": True,
            "delete_branch_on_merge": False,
            # internal state (never serialized: repo_json skips it)
            "refs": {}, "commits": {}, "blobs": {}, "trees": {}, "branch_files": {},
            "labels": {}, "pulls": {}, "issues_by_number": {},
            "_git": GitStore(),
            "next_issue": 1, "next_pull": 1, "_ts": ts,
        }
        self.repos[(VIEWER, name)] = repo
        return repo

    def delete_repo(self, repo: dict) -> None:
        self.repos.pop((VIEWER, repo["name"]), None)

    # -- refs / commits / contents ----------------------------------------
    def tip(self, repo: dict, branch: str):
        ref = repo["refs"].get(branch)
        return ref["sha"] if ref else None

    def files_of(self, repo: dict, branch: str) -> dict:
        return repo["branch_files"].get(branch, {})

    def set_branch(self, repo: dict, branch: str, sha: str, files: dict) -> None:
        repo["refs"][branch] = {"ref": "refs/heads/%s" % branch, "sha": sha}
        repo["branch_files"][branch] = files

    def commit(self, repo: dict, branch: str, files: dict, message: str, parents) -> str:
        toid = tree_oid(files)
        repo["trees"][toid] = dict(files)
        when, _ = stamp()
        sha, body = commit_oid(toid, parents, message, when, VIEWER)
        repo["commits"][sha] = {
            "sha": sha,
            "tree": toid,
            "parents": list(parents),
            "message": message,
            "author": {"name": VIEWER, "email": "%s@users.noreply.github.com" % VIEWER,
                       "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))},
            "committer": {"name": VIEWER, "email": "%s@users.noreply.github.com" % VIEWER,
                          "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))},
            "_body": body,
        }
        repo["_body_%s" % sha] = body
        return sha

    def commit_json(self, repo: dict, sha: str) -> dict:
        c = repo["commits"][sha]
        return {
            "sha": sha,
            "node_id": "C_%s" % sha,
            "url": "/api/v3/repos/%s/%s/commits/%s" % (VIEWER, repo["name"], sha),
            "html_url": "https://github.com/%s/%s/commit/%s" % (VIEWER, repo["name"], sha),
            "commit": {"message": c["message"], "tree": {"sha": c["tree"]},
                       "author": dict(c["author"]), "committer": dict(c["committer"]),
                       "url": "/api/v3/repos/%s/%s/git/commits/%s" % (VIEWER, repo["name"], sha)},
            "author": {"login": VIEWER, "id": self.user(VIEWER)["id"]},
            "committer": {"login": VIEWER, "id": self.user(VIEWER)["id"]},
            "parents": [{"sha": p} for p in c["parents"]],
        }

    def walk(self, repo: dict, sha: str) -> list:
        out = []
        seen = set()
        stack = [sha]
        while stack:
            cur = stack.pop(0)
            if cur in seen or cur not in repo["commits"]:
                continue
            seen.add(cur)
            out.append(cur)
            stack.extend(repo["commits"][cur]["parents"])
        return out

    def touch(self, repo: dict) -> None:
        repo["updated_at"] = now_iso()
        repo["pushed_at"] = repo["updated_at"]

    # -- issues ------------------------------------------------------------
    def issue_json(self, repo: dict, issue: dict) -> dict:
        who = {"login": VIEWER, "id": self.user(VIEWER)["id"], "type": "User"}
        base = "/api/v3/repos/%s/%s" % (VIEWER, repo["name"])
        number = issue["number"]
        return {
            "id": issue["id"], "node_id": "I_%d" % issue["id"], "number": number,
            "title": issue["title"], "body": issue["body"], "state": issue["state"],
            "state_reason": issue.get("state_reason"), "locked": False, "draft": False,
            "comments": len(issue["comments"]), "user": who,
            "labels": [self.label_json(l) for l in issue["labels"]],
            "assignee": None, "assignees": [], "milestone": None,
            "created_at": issue["created_at"], "updated_at": issue["updated_at"],
            "closed_at": issue["closed_at"], "closed_by": None,
            "html_url": "https://github.com/%s/%s/issues/%d" % (VIEWER, repo["name"], number),
            "url": "%s/issues/%d" % (base, number), "repository_url": base,
            "comments_url": "%s/issues/%d/comments" % (base, number),
            "author_association": "OWNER",
        }

    def label_json(self, label: dict) -> dict:
        return {"id": label["id"], "node_id": "L_%d" % label["id"], "name": label["name"],
                "color": label.get("color", "ededed"), "default": False,
                "description": label.get("description")}

    def comment_json(self, repo: dict, issue: dict, comment: dict) -> dict:
        return {"id": comment["id"], "node_id": "IC_%d" % comment["id"], "body": comment["body"],
                "user": {"login": VIEWER, "id": self.user(VIEWER)["id"]},
                "created_at": comment["created_at"], "updated_at": comment["updated_at"],
                "url": "/api/v3/repos/%s/%s/issues/comments/%d" % (VIEWER, repo["name"], comment["id"]),
                "html_url": "https://github.com/%s/%s/issues/%d#issuecomment-%d" % (
                    VIEWER, repo["name"], issue["number"], comment["id"])}

    # -- pulls -------------------------------------------------------------
    def pull_json(self, repo: dict, pull: dict) -> dict:
        name = repo["name"]
        base = "/api/v3/repos/%s/%s" % (VIEWER, name)
        number = pull["number"]
        return {
            "id": pull["id"], "node_id": "PR_%d" % pull["id"], "number": number,
            "state": pull["state"], "locked": False, "draft": False, "title": pull["title"],
            "body": pull["body"], "user": {"login": VIEWER, "id": self.user(VIEWER)["id"],
                                           "type": "User"},
            "labels": [], "created_at": pull["created_at"], "updated_at": pull["updated_at"],
            "closed_at": pull["closed_at"], "merged_at": pull["merged_at"],
            "merged": pull["merged"], "merge_commit_sha": pull["merge_commit_sha"],
            "mergeable": pull["mergeable"], "mergeable_state": pull["mergeable_state"],
            "rebaseable": True, "head": pull["head"], "base": pull["base"],
            "html_url": "https://github.com/%s/%s/pull/%d" % (VIEWER, name, number),
            "url": "%s/pulls/%d" % (base, number),
            "diff_url": "https://github.com/%s/%s/pull/%d.diff" % (VIEWER, name, number),
            "patch_url": "https://github.com/%s/%s/pull/%d.patch" % (VIEWER, name, number),
            "comments_url": "%s/issues/%d/comments" % (base, number),
            "review_comments": 0, "comments": 0, "maintainer_can_modify": True,
            "author_association": "OWNER", "commits": len(pull.get("head_commits", [])),
            "additions": pull.get("additions", 0), "deletions": pull.get("deletions", 0),
            "changed_files": pull.get("changed_files", 0),
        }

    def changed_paths(self, repo: dict, base_sha: str, head_sha: str) -> list:
        base_files = repo["trees"].get(repo["commits"][base_sha]["tree"], {}) if base_sha in repo["commits"] else {}
        head_files = repo["trees"].get(repo["commits"][head_sha]["tree"], {}) if head_sha in repo["commits"] else {}
        out = []
        for path in sorted(set(base_files) | set(head_files)):
            if base_files.get(path) != head_files.get(path):
                out.append(path)
        return out

    def diff_files(self, repo: dict, paths: list, base_sha: str, head_sha: str) -> list:
        base_files = repo["trees"].get(repo["commits"][base_sha]["tree"], {}) if base_sha in repo["commits"] else {}
        head_files = repo["trees"].get(repo["commits"][head_sha]["tree"], {}) if head_sha in repo["commits"] else {}
        out = []
        for path in paths:
            before = repo["blobs"].get(base_files.get(path, ""), b"").decode("utf-8", "replace")
            after = repo["blobs"].get(head_files.get(path, ""), b"").decode("utf-8", "replace")
            before_lines = before.splitlines()
            after_lines = after.splitlines()
            before_set = set(before_lines)
            after_set = set(after_lines)
            additions = sum(1 for line in after_lines if line not in before_set)
            deletions = sum(1 for line in before_lines if line not in after_set)
            if path not in base_files:
                status = "added"
                additions = len(after_lines)
                deletions = 0
            elif path not in head_files:
                status = "removed"
                additions = 0
                deletions = len(before_lines)
            else:
                status = "modified"
                additions = max(additions, 1)
            out.append({
                "sha": head_files.get(path) or base_files.get(path, ""),
                "filename": path,
                "status": status,
                "additions": additions,
                "deletions": deletions,
                "changes": additions + deletions,
                "blob_url": "https://github.com/%s/%s/blob/%s/%s" % (VIEWER, repo["name"], head_sha, path),
                "raw_url": "https://github.com/%s/%s/raw/%s/%s" % (VIEWER, repo["name"], head_sha, path),
                "patch": "",
            })
        return out


STORE = Store()


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------

def tool_schemas() -> list:
    def tool(name, description, props, required):
        return {"name": name, "title": name, "description": description,
                "inputSchema": {"type": "object", "properties": props, "required": required,
                                "additionalProperties": False}}
    p_owner = {"type": "string", "description": "The repository owner's login."}
    p_repo = {"type": "string", "description": "The repository name."}
    p_full = {"type": "string", "description": "The repository as owner/repo."}
    p_title = {"type": "string", "description": "The issue title."}
    return [
        tool("create_repo", "Create a repository in the authenticated user's account.",
             {"name": {"type": "string", "description": "The repository name."},
              "owner": p_owner, "full_name": p_full,
              "description": {"type": "string", "description": "The repository description."},
              "private": {"type": "boolean", "description": "Whether the repository is private."}},
             ["name"]),
        tool("get_repo", "Get a repository by owner and name.",
             {"owner": p_owner, "repo": p_repo, "full_name": p_full}, ["owner", "repo"]),
        tool("create_issue", "Create an issue in a repository.",
             {"owner": p_owner, "repo": p_repo, "full_name": p_full, "title": p_title,
              "body": {"type": "string", "description": "The issue body."}},
             ["owner", "repo", "title"]),
        tool("list_issues", "List the issues of a repository.",
             {"owner": p_owner, "repo": p_repo, "full_name": p_full,
              "state": {"type": "string", "enum": ["open", "closed", "all"],
                        "description": "Which issues to list."},
              "per_page": {"type": "integer", "description": "How many issues to return."}},
             ["owner", "repo"]),
        tool("create_pull_request", "Open a pull request from a head branch into a base branch.",
             {"owner": p_owner, "repo": p_repo, "full_name": p_full, "title": p_title,
              "body": {"type": "string", "description": "The pull request body."},
              "head": {"type": "string", "description": "The head branch."},
              "base": {"type": "string", "description": "The base branch."}},
             ["owner", "repo", "title", "head", "base"]),
        tool("merge_pull_request", "Merge a pull request into its base branch.",
             {"owner": p_owner, "repo": p_repo, "full_name": p_full,
              "pull_number": {"type": "integer", "description": "The pull request number."},
              "merge_method": {"type": "string", "enum": ["merge", "squash", "rebase"],
                               "description": "The merge method."}},
             ["owner", "repo", "pull_number"]),
    ]


TOOLS = tool_schemas()
TOOLS_PAYLOAD = json.dumps({"tools": TOOLS}, sort_keys=True, separators=(",", ":"))
TOOL_INDEX = {t["name"]: t for t in TOOLS}


def normalize_name(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


NORMALIZED = {normalize_name(t["name"]): t for t in TOOLS}


def find_tool(name):
    key = normalize_name(name)
    if key in NORMALIZED:
        return NORMALIZED[key]
    for canon, tool in NORMALIZED.items():
        if key.endswith(canon):
            return tool
    return None


def arg(args, *keys, default=None):
    for key in keys:
        if key in args and args[key] is not None:
            return args[key]
    return default


def repo_ref_from_args(args):
    owner = arg(args, "owner", "org", "login")
    repo = arg(args, "repo", "repository", "name")
    full = arg(args, "full_name", "repo_full_name", "fullName")
    if (not owner or not repo) and isinstance(full, str) and "/" in full:
        owner, repo = full.split("/", 1)
    return owner, repo


def mcp_text_result(payload):
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": payload, "isError": False}


def mcp_error_result(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


def call_tool(name, args, store: Store):
    tool = find_tool(name)
    if tool is None:
        raise McpToolError(-32602, "Unknown tool: %s" % name)
    if not isinstance(args, dict):
        args = {}
    canon = tool["name"]
    with store.lock:
        if canon == "create_repo":
            repo_name = arg(args, "name", "repo", "repository")
            if not isinstance(repo_name, str) or repo_name.strip() == "":
                raise McpToolError(-32602, "create_repo requires a name")
            repo = store.create_repo(repo_name, bool(arg(args, "private", default=True)),
                                     arg(args, "description", "body", default="") or "")
            return mcp_text_result(repo_json(repo))
        owner, repo_name = repo_ref_from_args(args)
        if owner is None or repo_name is None:
            raise McpToolError(-32602, "%s requires owner and repo" % canon)
        repo = store.own_repo(owner, repo_name)
        if canon == "get_repo":
            return mcp_text_result(repo_json(repo))
        if canon == "create_issue":
            title = arg(args, "title", "issue_title", "subject")
            if not isinstance(title, str) or title == "":
                raise McpToolError(-32602, "create_issue requires a title")
            body = arg(args, "body", "issue_body", "text", default="") or ""
            issue, _ = create_issue(repo, title, body, arg(args, "labels", default=None))
            return mcp_text_result(store.issue_json(repo, issue))
        if canon == "list_issues":
            state = arg(args, "state", "status", default="open")
            issues = list_issues(repo, state)
            return mcp_text_result([store.issue_json(repo, i) for i in issues])
        if canon == "create_pull_request":
            title = arg(args, "title", "pr_title", "subject")
            head = arg(args, "head", "head_ref", "source", "branch", "from")
            base = arg(args, "base", "base_ref", "target", "into")
            if not isinstance(title, str) or not title:
                raise McpToolError(-32602, "create_pull_request requires a title")
            if not isinstance(head, str) or not head:
                raise McpToolError(-32602, "create_pull_request requires a head branch")
            if not isinstance(base, str) or not base:
                base = repo["default_branch"]
            pull = create_pull(repo, title, arg(args, "body", default="") or "", head, base)
            return mcp_text_result(store.pull_json(repo, pull))
        if canon == "merge_pull_request":
            number = arg(args, "pull_number", "number", "pull_request_number")
            if number is None:
                raise McpToolError(-32602, "merge_pull_request requires a pull_number")
            try:
                number = int(number)
            except (TypeError, ValueError):
                raise McpToolError(-32602, "merge_pull_request requires a numeric pull_number")
            pull = repo["pulls"].get(number)
            if pull is None:
                raise ApiError(404, "Not Found")
            result = merge_pull(repo, pull, arg(args, "merge_method", default="squash"))
            return mcp_text_result(result)
    raise McpToolError(-32602, "Unknown tool: %s" % name)


class McpToolError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# REST helpers
# --------------------------------------------------------------------------

def repo_json(repo: dict) -> dict:
    skip = {"refs", "commits", "blobs", "trees", "branch_files", "issues_by_number", "pulls",
            "labels", "next_issue", "next_pull"}
    out = {k: v for k, v in repo.items() if not k.startswith("_") and k not in skip}
    out["owner"] = dict(out["owner"])
    out["permissions"] = dict(out["permissions"])
    return out


def list_issues(repo: dict, state: str = "open", sort: str = "created", direction: str = "asc") -> list:
    issues = list(repo["issues_by_number"].values())
    if state == "open":
        issues = [i for i in issues if i["state"] == "open"]
    elif state == "closed":
        issues = [i for i in issues if i["state"] == "closed"]
    reverse = (direction == "desc")
    if sort == "created":
        issues.sort(key=lambda i: i["number"], reverse=reverse)
    elif sort == "updated":
        issues.sort(key=lambda i: i["number"], reverse=reverse)
    else:
        issues.sort(key=lambda i: i["number"], reverse=reverse)
    return issues


def list_pulls(repo: dict, state: str = "open") -> list:
    pulls = list(repo["pulls"].values())
    if state == "open":
        pulls = [p for p in pulls if p["state"] == "open"]
    elif state == "closed":
        pulls = [p for p in pulls if p["state"] == "closed"]
    pulls.sort(key=lambda p: p["number"])
    return pulls


def create_issue(repo: dict, title, body, labels=None):
    if not isinstance(title, str) or title == "":
        raise ApiError(422, "Validation Failed: title is required")
    wanted = []
    if isinstance(labels, list):
        for entry in labels:
            name = entry.get("name") if isinstance(entry, dict) else entry
            if name is None:
                continue
            if name not in repo["labels"]:
                raise ApiError(422, "Validation Failed: label %s does not exist" % name)
            wanted.append(repo["labels"][name])
    elif labels is None:
        wanted = []
    number = repo["next_issue"]
    repo["next_issue"] = number + 1
    ts, iso = stamp()
    issue = {"id": STORE.next_issue_id, "number": number, "title": title,
             "body": "" if body is None else str(body), "state": "open",
             "state_reason": None, "labels": wanted, "comments": [], "created_at": iso,
             "updated_at": iso, "closed_at": None, "_ts": ts}
    STORE.next_issue_id += 1
    repo["issues_by_number"][number] = issue
    STORE.touch(repo)
    return issue, wanted


def create_pull(repo: dict, title, body, head: str, base: str) -> dict:
    if head not in repo["refs"]:
        raise ApiError(422, "Validation Failed: head branch %s does not exist" % head)
    if base not in repo["refs"]:
        raise ApiError(422, "Validation Failed: base branch %s does not exist" % base)
    head_sha = repo["refs"][head]["sha"]
    base_sha = repo["refs"][base]["sha"]
    number = repo["next_pull"]
    repo["next_pull"] = number + 1
    ts, iso = stamp()
    paths = STORE.changed_paths(repo, base_sha, head_sha)
    files = STORE.diff_files(repo, paths, base_sha, head_sha)
    pull = {
        "id": STORE.next_pull_id, "number": number, "title": title,
        "body": "" if body is None else str(body), "state": "open", "merged": False,
        "merged_at": None, "merge_commit_sha": None, "created_at": iso, "updated_at": iso,
        "closed_at": None, "mergeable": True, "mergeable_state": "clean",
        "head": {"ref": head, "sha": head_sha, "label": "%s:%s" % (VIEWER, head),
                 "repo": None, "user": {"login": VIEWER}},
        "base": {"ref": base, "sha": base_sha, "label": "%s:%s" % (VIEWER, base),
                 "repo": None, "user": {"login": VIEWER}},
        "head_commits": [head_sha], "base_sha": base_sha, "head_sha": head_sha,
        "files": files, "additions": sum(f["additions"] for f in files),
        "deletions": sum(f["deletions"] for f in files), "changed_files": len(files), "_ts": ts,
    }
    STORE.next_pull_id += 1
    repo["pulls"][number] = pull
    STORE.touch(repo)
    return pull


def pull_conflict(repo: dict, pull: dict) -> bool:
    base = pull["base"]["ref"]
    current = repo["refs"][base]["sha"]
    if current == pull["base_sha"]:
        return False
    advanced = set(STORE.changed_paths(repo, pull["base_sha"], current))
    touched = {f["filename"] for f in pull["files"]}
    return bool(advanced & touched)


def merge_pull(repo: dict, pull: dict, method: str = "squash") -> dict:
    if pull["merged"]:
        raise ApiError(405, "Pull Request is not mergeable")
    if pull["state"] == "closed":
        raise ApiError(405, "Pull Request is not mergeable")
    if pull_conflict(repo, pull):
        pull["mergeable"] = False
        pull["mergeable_state"] = "dirty"
        raise ApiError(405, "Pull Request is not mergeable: merge conflict")
    base = pull["base"]["ref"]
    files = dict(STORE.files_of(repo, base))
    head_files = repo["trees"][repo["commits"][pull["head_sha"]]["tree"]]
    for path in STORE.changed_paths(repo, pull["base_sha"], pull["head_sha"]):
        if path in head_files:
            files[path] = head_files[path]
        else:
            files.pop(path, None)
    parent = repo["refs"][base]["sha"]
    message = "%s (#%d)" % (pull["title"], pull["number"])
    sha = STORE.commit(repo, base, files, message, [parent])
    STORE.set_branch(repo, base, sha, files)
    ts, iso = stamp()
    pull["state"] = "closed"
    pull["merged"] = True
    pull["merged_at"] = iso
    pull["closed_at"] = iso
    pull["merge_commit_sha"] = sha
    pull["updated_at"] = iso
    pull["merge_method"] = method
    pull["base"]["sha"] = parent
    STORE.touch(repo)
    return {"sha": sha, "merged": True, "message": "Pull Request successfully merged"}


# --------------------------------------------------------------------------
# git: object store, packfile codec and smart-HTTP protocol
# --------------------------------------------------------------------------

GIT_ZERO = "0" * 40
AGENT = "makefun-forge/1.0"
_TYPE_CODE = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}
_CODE_TYPE = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_MAX_SIDE_BAND = 65000


def git_oid(otype: str, data: bytes) -> str:
    return sha1_hex(("%s %d" % (otype, len(data))).encode("ascii") + b"\0" + data)


def pack_entry_header(otype: str, size: int) -> bytes:
    out = bytearray()
    first = (_TYPE_CODE[otype] << 4) | (size & 15)
    size >>= 4
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def pkt(payload) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return ("%04x" % (len(payload) + 4)).encode("ascii") + payload


PKT_FLUSH = b"0000"


def parse_pkt_lines(buf: bytes, start: int = 0):
    """Parse pkt-lines from `start`; stop after a flush packet or at raw bytes."""
    packets = []
    off = start
    n = len(buf)
    while off + 4 <= n:
        try:
            size = int(buf[off:off + 4], 16)
        except ValueError:
            break
        if size == 0:
            packets.append(("flush", b""))
            off += 4
            break
        if size in (1, 2):
            packets.append(("delim", b""))
            off += 4
            continue
        if size < 4 or off + size > n:
            raise ValueError("malformed pkt-line")
        packets.append(("data", buf[off + 4:off + size]))
        off += size
    return packets, off


def apply_delta(base: bytes, delta: bytes) -> bytes:
    """Rebuild an object from its delta against `base`."""
    pos = 0
    n = len(delta)

    def varint():
        nonlocal pos
        value = 0
        shift = 0
        while True:
            if pos >= n:
                raise ValueError("truncated delta")
            byte = delta[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value

    varint()
    want = varint()
    out = bytearray()
    while pos < n:
        op = delta[pos]
        pos += 1
        if op & 0x80:
            copy_off = 0
            copy_size = 0
            for shift, mask in ((0, 0x01), (8, 0x02), (16, 0x04), (24, 0x08)):
                if op & mask:
                    copy_off |= delta[pos] << shift
                    pos += 1
            for shift, mask in ((0, 0x10), (8, 0x20), (16, 0x40)):
                if op & mask:
                    copy_size |= delta[pos] << shift
                    pos += 1
            if copy_size == 0:
                copy_size = 0x10000
            out += base[copy_off:copy_off + copy_size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("bad delta opcode")
    if len(out) != want:
        raise ValueError("delta size mismatch")
    return bytes(out)


def read_pack(buf: bytes, base_lookup=None):
    """Decode a packfile (thin packs allowed) into a list of (type, data)."""
    if len(buf) < 32 or buf[:4] != b"PACK":
        raise ValueError("not a packfile")
    if struct.unpack(">I", buf[4:8])[0] not in (2, 3):
        raise ValueError("unsupported pack version")
    count = struct.unpack(">I", buf[8:12])[0]
    if count > 1000000:
        raise ValueError("pack declares too many objects")
    trailer = len(buf) - 20
    off = 12
    raw = []
    by_offset = {}
    for _ in range(count):
        start = off
        byte = buf[off]
        off += 1
        code = (byte >> 4) & 7
        size = byte & 15
        shift = 4
        while byte & 0x80:
            byte = buf[off]
            off += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        base_ref = None
        if code == 6:
            byte = buf[off]
            off += 1
            distance = byte & 0x7F
            while byte & 0x80:
                byte = buf[off]
                off += 1
                distance = ((distance + 1) << 7) | (byte & 0x7F)
            base_ref = start - distance
        elif code == 7:
            base_ref = buf[off:off + 20].hex()
            off += 20
        elif code not in (1, 2, 3, 4):
            raise ValueError("bad pack object type")
        dec = zlib.decompressobj()
        try:
            data = dec.decompress(buf[off:trailer])
        except zlib.error:
            raise ValueError("bad pack zlib stream")
        if not dec.eof:
            raise ValueError("truncated pack object")
        off = trailer - len(dec.unused_data)
        if len(data) != size:
            raise ValueError("pack object size mismatch")
        entry = {"offset": start, "code": code, "data": data, "base": base_ref}
        raw.append(entry)
        by_offset[start] = entry
    if off != trailer:
        raise ValueError("pack has trailing bytes")
    out = []
    by_oid = {}
    pending = []
    for entry in raw:
        if entry["code"] == 6 or entry["code"] == 7:
            pending.append(entry)
        else:
            otype = _CODE_TYPE[entry["code"]]
            entry["resolved"] = (otype, entry["data"])
            by_oid[git_oid(otype, entry["data"])] = entry["resolved"]
            out.append(entry)
    progress = True
    while pending and progress:
        progress = False
        for entry in list(pending):
            base = None
            ref = entry["base"]
            if entry["code"] == 6:
                holder = by_offset.get(ref)
                base = holder.get("resolved") if holder is not None else None
            else:
                base = by_oid.get(ref)
                if base is None and base_lookup is not None:
                    base = base_lookup(ref)
            if base is None:
                continue
            data = apply_delta(base[1], entry["data"])
            entry["resolved"] = (base[0], data)
            by_oid[git_oid(base[0], data)] = entry["resolved"]
            out.append(entry)
            pending.remove(entry)
            progress = True
    if pending:
        raise ValueError("a delta base is missing")
    return [entry["resolved"] for entry in out]


def commit_meta(data: bytes):
    """(tree oid, parent oids) from a commit object."""
    tree = ""
    parents = []
    for line in data.split(b"\n"):
        if line.startswith(b"tree "):
            tree = line[5:45].decode("ascii", "replace")
        elif line.startswith(b"parent "):
            parents.append(line[7:47].decode("ascii", "replace"))
        elif line == b"":
            break
    return tree, parents


def tree_entries(data: bytes):
    out = []
    off = 0
    n = len(data)
    while off < n:
        try:
            space = data.index(b" ", off)
            nul = data.index(b"\0", space + 1)
        except ValueError:
            raise ValueError("malformed tree object")
        mode = data[off:space].decode("ascii", "replace")
        name = data[space + 1:nul].decode("utf-8", "replace")
        oid = data[nul + 1:nul + 21].hex()
        out.append((mode, name, oid))
        off = nul + 21
    return out


def tag_target(data: bytes) -> str:
    for line in data.split(b"\n"):
        if line.startswith(b"object "):
            return line[7:47].decode("ascii", "replace")
        if line == b"":
            break
    return ""


def commit_identities(data: bytes):
    """(raw author line, raw committer line) of a commit object."""
    author = committer = b""
    for line in data.split(b"\n"):
        if line.startswith(b"author "):
            author = line[7:]
        elif line.startswith(b"committer "):
            committer = line[10:]
        elif line == b"":
            break
    return author, committer


def commit_message(data: bytes) -> str:
    idx = data.find(b"\n\n")
    if idx < 0:
        return ""
    return data[idx + 2:].decode("utf-8", "replace").rstrip("\n")


def ident_json(raw: bytes, fallback: str) -> dict:
    """Turn "Name <mail> seconds +hhmm" into the REST author shape."""
    text = raw.decode("utf-8", "replace").strip()
    name = text
    email = ""
    tail = []
    if " <" in text:
        name, rest = text.split(" <", 1)
        email = rest.split(">", 1)[0]
        tail = rest.split(">", 1)[1].strip().split(" ")
    when = 0
    zone = 0
    if tail and tail[0].lstrip("-").isdigit():
        when = int(tail[0])
        if len(tail) > 1 and len(tail[1]) == 5 and tail[1][0] in "+-":
            try:
                zone = int(tail[1][1:3]) * 3600 + int(tail[1][3:5]) * 60
                if tail[1][0] == "-":
                    zone = -zone
            except ValueError:
                zone = 0
    name = name.strip() or fallback
    date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when + zone)) if when else now_iso()
    return {"name": name, "email": email or "%s@users.noreply.github.com" % name,
            "date": date}


def git_tree_files(repo: dict, tree_oid: str) -> dict:
    """Flat path -> blob oid map of one git tree, registering its blobs."""
    gs = repo["_git"]
    out = {}
    stack = [(tree_oid, "")]
    guard = 0
    while stack:
        oid, prefix = stack.pop()
        guard += 1
        if guard > 200000:
            break
        obj = gs.objects.get(oid)
        if obj is None or obj[0] != "tree":
            continue
        for mode, name, child in tree_entries(obj[1]):
            path = prefix + name
            if mode in ("40000", "040000"):
                stack.append((child, path + "/"))
                continue
            if mode == "160000":
                continue
            child_obj = gs.objects.get(child)
            if child_obj is not None and child_obj[0] == "blob":
                repo["blobs"][child] = child_obj[1]
            out[path] = child
    return out


def import_git_ref(repo: dict, ref: str, sha: str) -> None:
    """Copy an accepted receive-pack ref update into the REST-facing record.

    A push lands in the git store only, while the REST layer (branches,
    contents, commits, pull requests) reads repo["refs"], repo["commits"],
    repo["trees"] and repo["blobs"].  Transcribe the pushed tip and its
    ancestors into those maps so the branch is visible over REST too.
    """
    if not ref.startswith("refs/heads/"):
        return
    branch = ref[len("refs/heads/"):]
    gs = repo["_git"]
    queue = [sha]
    seen = set()
    while queue:
        oid = queue.pop(0)
        if oid in seen:
            continue
        seen.add(oid)
        obj = gs.objects.get(oid)
        if obj is None:
            continue
        otype, data = obj
        if otype == "tag":
            target = tag_target(data)
            if target:
                queue.append(target)
            continue
        if otype != "commit":
            continue
        tree, parents = commit_meta(data)
        if oid not in repo["commits"]:
            if tree and tree not in repo["trees"]:
                repo["trees"][tree] = git_tree_files(repo, tree)
            author, committer = commit_identities(data)
            repo["commits"][oid] = {
                "sha": oid, "tree": tree, "parents": list(parents),
                "message": commit_message(data),
                "author": ident_json(author, VIEWER),
                "committer": ident_json(committer, VIEWER),
                "_body": data,
            }
            repo["_body_%s" % oid] = data
        queue.extend(parents)
    commit = repo["commits"].get(sha)
    if commit is None:
        return
    STORE.set_branch(repo, branch, sha, dict(repo["trees"].get(commit["tree"], {})))
    STORE.touch(repo)


class GitStore:
    """One repository's git objects and refs (content addressed, in memory)."""

    __slots__ = ("objects", "refs", "head", "zcache")

    def __init__(self):
        self.objects = {}
        self.refs = {}
        self.head = "refs/heads/main"
        self.zcache = {}

    def put(self, otype: str, data: bytes) -> str:
        oid = git_oid(otype, data)
        if oid not in self.objects:
            self.objects[oid] = (otype, data)
        return oid

    def add_pack(self, buf: bytes) -> int:
        added = 0
        for otype, data in read_pack(buf, self.objects.get):
            self.put(otype, data)
            added += 1
        return added

    def entry(self, oid: str) -> bytes:
        cached = self.zcache.get(oid)
        if cached is None:
            otype, data = self.objects[oid]
            cached = pack_entry_header(otype, len(data)) + zlib.compress(data, 1)
            self.zcache[oid] = cached
        return cached

    def write_pack(self, oids) -> bytes:
        parts = [b"PACK" + struct.pack(">II", 2, len(oids))]
        for oid in oids:
            parts.append(self.entry(oid))
        body = b"".join(parts)
        return body + hashlib.sha1(body).digest()


def git_peel(gs: GitStore, oid: str) -> str:
    for _ in range(16):
        obj = gs.objects.get(oid)
        if obj is None or obj[0] != "tag":
            return oid
        target = tag_target(obj[1])
        if not target:
            return oid
        oid = target
    return oid


def walk_objects(gs: GitStore, roots, stop=frozenset()):
    """Every object reachable from `roots`, in a deterministic DFS order."""
    out = []
    seen = set()
    stack = [oid for oid in reversed(list(roots)) if oid in gs.objects]
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        seen.add(oid)
        obj = gs.objects.get(oid)
        if obj is None:
            continue
        out.append(oid)
        otype, data = obj
        if otype == "commit":
            tree, parents = commit_meta(data)
            if oid not in stop:
                for parent in reversed(parents):
                    if parent not in seen:
                        stack.append(parent)
            if tree:
                stack.append(tree)
        elif otype == "tree":
            for _mode, _name, child in reversed(tree_entries(data)):
                if child not in seen:
                    stack.append(child)
        elif otype == "tag":
            target = tag_target(data)
            if target:
                stack.append(target)
    return out


def walk_commits(gs: GitStore, wants, depth: int, stop=frozenset()):
    """Commits within `depth` generations of `wants` -> (ordered list, depth map)."""
    order = []
    depths = {}
    queue = [(git_peel(gs, oid), 1) for oid in wants]
    index = 0
    while index < len(queue):
        oid, level = queue[index]
        index += 1
        if oid in depths:
            continue
        obj = gs.objects.get(oid)
        if obj is None or obj[0] != "commit":
            continue
        depths[oid] = level
        order.append(oid)
        if level >= depth or oid in stop:
            continue
        _tree, parents = commit_meta(obj[1])
        for parent in parents:
            if parent not in depths:
                queue.append((parent, level + 1))
    return order, depths


def select_fetch_objects(gs: GitStore, wants, haves, depth, client_shallow):
    """(oids to send, shallow lines, unshallow lines) for one fetch."""
    stop = frozenset(client_shallow)
    shallow_lines = []
    unshallow_lines = []
    if depth is not None:
        # The client's shallow lines say where its own history stops; a deepen
        # request counts the new depth from the wants, so they never cut this walk.
        order, depths = walk_commits(gs, wants, depth)
        oids = []
        included = set()
        for oid in order:
            if oid in included:
                continue
            included.add(oid)
            oids.append(oid)
            tree = commit_meta(gs.objects[oid][1])[0]
            if tree:
                for sub in walk_objects(gs, [tree]):
                    if sub not in included:
                        included.add(sub)
                        oids.append(sub)
        boundaries = []
        for oid in order:
            _tree, parents = commit_meta(gs.objects[oid][1])
            if any(parent not in depths for parent in parents):
                boundaries.append(oid)
        shallow_lines = [oid for oid in boundaries if oid not in stop]
        unshallow_lines = [oid for oid in client_shallow
                           if oid in depths and oid not in boundaries]
    else:
        oids = walk_objects(gs, wants, stop)
    excluded = set()
    if haves:
        excluded = set(walk_objects(gs, haves, stop))
    return [oid for oid in oids if oid not in excluded], shallow_lines, unshallow_lines


def git_is_ancestor(gs: GitStore, ancestor: str, descendant: str) -> bool:
    if ancestor == descendant:
        return True
    stack = [descendant]
    seen = set()
    steps = 0
    while stack:
        oid = stack.pop()
        if oid == ancestor:
            return True
        if oid in seen:
            continue
        seen.add(oid)
        steps += 1
        if steps > 500000:
            return False
        obj = gs.objects.get(oid)
        if obj is None or obj[0] != "commit":
            continue
        _tree, parents = commit_meta(obj[1])
        stack.extend(parents)
    return False


def sideband_pack(pack: bytes) -> bytes:
    parts = []
    for start in range(0, len(pack), _MAX_SIDE_BAND):
        parts.append(pkt(b"\x01" + pack[start:start + _MAX_SIDE_BAND]))
    return b"".join(parts)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
SERVER_NAME = "makefun-forge"
SERVER_VERSION = "1.0.0"


class ResponseCache:
    """Byte-exact replay cache for repeated identical reads.

    The store is single-writer, so any mutating request (or any request that
    carries a body) throws every entry away.  Purely in-memory: it is never
    part of the exported state.  Insertion order is the eviction order, so the
    oldest entry goes first once the bound is reached.
    """

    __slots__ = ("entries", "limit", "lock")

    def __init__(self, limit: int = 256):
        self.entries = {}
        self.limit = limit
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            return self.entries.get(key)

    def put(self, key, blob: bytes) -> None:
        with self.lock:
            if key in self.entries:
                return
            self.entries[key] = blob
            while len(self.entries) > self.limit:
                oldest = next(iter(self.entries), None)
                if oldest is None:
                    break
                del self.entries[oldest]

    def clear(self) -> None:
        with self.lock:
            self.entries.clear()


RESPONSE_CACHE = ResponseCache(256)


def cacheable_status(blob: bytes) -> bool:
    """Only finished successes (and plain 404s) are worth replaying."""
    if not blob.startswith(b"HTTP/1.1 "):
        return False
    code = blob[9:12]
    return code[:1] == b"2" or code == b"404"


class BufferedWriter:
    """Collect one finished response: status line, headers and body bytes."""

    __slots__ = ("chunks", "real")

    def __init__(self, real):
        self.chunks = []
        self.real = real

    def write(self, data) -> int:
        if isinstance(data, (bytes, bytearray, memoryview)):
            self.chunks.append(bytes(data))
        else:
            self.chunks.append(str(data).encode("latin-1"))
        return len(data)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        pass

    def finish(self) -> bytes:
        return b"".join(self.chunks)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "makefun-forge/1.0"
    sys_version = ""
    # Nagle plus the peer's delayed ACK costs ~45 ms on every keep-alive
    # round trip; the suite measures that latency, so turn it off.
    disable_nagle_algorithm = True

    # ---- plumbing --------------------------------------------------------
    def log_message(self, fmt, *args):  # silence
        pass

    def handle_one_request(self):
        # one handler instance serves every request on a connection: the body
        # cache must not survive from the previous one.
        self._body_cache = None
        super().handle_one_request()

    def send_error(self, code, message=None, explain=None):
        """Never answer HTML: a protocol-level error is JSON too."""
        try:
            self.send_json(code, {"message": message or explain or "Error",
                                  "documentation_url": "https://docs.github.com/rest"})
        except Exception:
            pass

    def _request_id(self) -> str:
        return "%016x" % (next_seq() * 2654435761 & 0xFFFFFFFFFFFFFFFF)

    def send_json(self, status: int, payload, extra: dict | None = None, raw: bytes | None = None):
        if payload is None and raw is None:
            raw = b""
        if raw is None:
            raw = json.dumps(payload).encode("utf-8")
        body = raw
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-GitHub-Request-Id", self._request_id())
        self.send_header("X-OAuth-Scopes", "repo, read:org")
        self.send_header("X-Accepted-OAuth-Scopes", "repo")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def send_error_json(self, status: int, message: str, extra: dict | None = None):
        self._drain()
        self.send_json(status, {"message": message, "documentation_url":
                                "https://docs.github.com/rest"}, extra)

    def _drain(self):
        try:
            self.read_body()
        except Exception:
            pass

    def read_body(self) -> bytes:
        cached = getattr(self, "_body_cache", None)
        if cached is not None:
            return cached
        length = self.headers.get("Content-Length")
        if length is None:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                self._body_cache = self._read_chunked()
            else:
                self._body_cache = b""
            return self._body_cache
        try:
            n = int(length)
        except ValueError:
            n = 0
        self._body_cache = self.rfile.read(n) if n > 0 else b""
        return self._body_cache

    def _read_chunked(self) -> bytes:
        data = b""
        while True:
            line = self.rfile.readline().strip()
            if not line:
                break
            try:
                size = int(line.split(b";")[0], 16)
            except ValueError:
                break
            if size == 0:
                self.rfile.readline()
                break
            data += self.rfile.read(size)
            self.rfile.readline()
        return data

    def json_body(self, required: bool = False) -> dict:
        raw = self.read_body()
        if raw.strip() == b"":
            if required:
                raise ApiError(400, "Problems parsing JSON")
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, "Problems parsing JSON")
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            return {"_list": value}
        return {}

    def authed(self) -> bool:
        value = self.headers.get("Authorization") or self.headers.get("authorization") or ""
        if not value.startswith("Bearer ") and not value.startswith("bearer "):
            return False
        return value.split(" ", 1)[1].strip() != ""

    def require_auth(self) -> bool:
        if not self.authed():
            self.send_error_json(401, "Requires authentication")
            return False
        return True

    # ---- git smart HTTP --------------------------------------------------
    def send_git(self, status: int, ctype: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, max-age=0, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-GitHub-Request-Id", self._request_id())
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_git(self, method: str, path: str, query: dict) -> bool:
        """Serve /{owner}/{repo}.git/...; True when the path was ours."""
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2 or not parts[1].endswith(".git"):
            return False
        owner = parts[0]
        name = parts[1][:-4]
        rest = parts[2:]
        service = (query.get("service") or [""])[0]
        if owner != VIEWER or (VIEWER, name) not in STORE.repos:
            self.send_error_json(404, "Not Found")
            return True
        repo = STORE.repos[(VIEWER, name)]
        if method in ("GET", "HEAD") and rest == ["info", "refs"]:
            if service in ("git-upload-pack", "git-receive-pack"):
                self.git_advertise(repo, service)
            else:
                self.send_error_json(404, "Not Found")
            return True
        if method == "POST" and rest == ["git-upload-pack"]:
            self.git_upload_pack(repo)
            return True
        if method == "POST" and rest == ["git-receive-pack"]:
            self.git_receive_pack(repo)
            return True
        self.send_error_json(404, "Not Found")
        return True

    def git_advertise(self, repo, service):
        gs = repo["_git"]
        refs = sorted(gs.refs.items())
        head_oid = gs.refs.get(gs.head)
        if service == "git-upload-pack":
            caps = ("side-band-64k thin-pack ofs-delta shallow no-progress multi_ack_detailed"
                    " symref=HEAD:%s agent=%s" % (gs.head, AGENT))
        else:
            caps = "report-status delete-refs side-band-64k ofs-delta agent=%s" % AGENT
        body = pkt("# service=%s\n" % service) + PKT_FLUSH
        if service == "git-upload-pack" and "version=2" in (self.headers.get("Git-Protocol") or ""):
            body += (pkt("version 2\n") + pkt("agent=%s\n" % AGENT) + pkt("ls-refs=unborn\n")
                     + pkt("fetch=shallow\n") + pkt("object-format=sha1\n") + PKT_FLUSH)
        else:
            lines = []
            if refs:
                lines.append("%s HEAD\0%s\n" % (head_oid or GIT_ZERO, caps))
                for ref, oid in refs:
                    lines.append("%s %s\n" % (oid, ref))
                    peeled = git_peel(gs, oid) if ref.startswith("refs/tags/") else oid
                    if peeled != oid:
                        lines.append("%s %s^{}\n" % (peeled, ref))
            else:
                lines.append("%s capabilities^{}\0%s\n" % (GIT_ZERO, caps))
            for line in lines:
                body += pkt(line)
            body += PKT_FLUSH
        self.send_git(200, "application/x-%s-advertisement" % service, body)

    def git_upload_pack(self, repo):
        raw = self.read_body()
        try:
            if "version=2" in (self.headers.get("Git-Protocol") or ""):
                body = self.git_v2(repo, raw)
            else:
                body = self.git_v0_fetch(repo, raw)
        except ValueError as err:
            self.send_error_json(400, "malformed git request: %s" % err)
            return
        self.send_git(200, "application/x-git-upload-pack-result", body)

    def git_v2(self, repo, raw):
        packets, _off = parse_pkt_lines(raw)
        data = [payload for kind, payload in packets if kind == "data"]
        if not data:
            raise ValueError("empty request")
        first = data[0].decode("utf-8", "replace").strip()
        command = first[len("command="):] if first.startswith("command=") else first
        args = []
        after_delim = False
        for kind, payload in packets:
            if kind == "delim":
                after_delim = True
                continue
            if after_delim and kind == "data":
                args.append(payload.decode("utf-8", "replace").rstrip("\n"))
        if command == "ls-refs":
            return self.git_ls_refs(repo, args)
        if command == "fetch":
            return self.git_fetch(repo, args)
        raise ValueError("unknown command %s" % command)

    def git_ls_refs(self, repo, args):
        gs = repo["_git"]
        peel = "peel" in args
        symrefs = "symrefs" in args
        unborn = "unborn" in args
        prefixes = [a[len("ref-prefix "):] for a in args if a.startswith("ref-prefix ")]

        def keep(name):
            if not prefixes:
                return True
            return any(name.startswith(prefix) for prefix in prefixes)

        lines = []
        head_oid = gs.refs.get(gs.head)
        if keep("HEAD"):
            if head_oid:
                line = "%s HEAD" % head_oid
                if symrefs:
                    line += " symref-target:%s" % gs.head
                lines.append(line)
            elif unborn:
                line = "unborn HEAD"
                if symrefs:
                    line += " symref-target:%s" % gs.head
                lines.append(line)
        for ref, oid in sorted(gs.refs.items()):
            if not keep(ref):
                continue
            line = "%s %s" % (oid, ref)
            if peel:
                peeled = git_peel(gs, oid)
                if peeled != oid:
                    line += " peeled:%s" % peeled
            lines.append(line)
        body = b"".join(pkt(line + "\n") for line in lines) + PKT_FLUSH
        return body

    def git_fetch(self, repo, args):
        gs = repo["_git"]
        wants = []
        haves = []
        client_shallow = []
        depth = None
        for arg in args:
            if arg.startswith("want "):
                wants.append(arg[5:].strip())
            elif arg.startswith("have "):
                haves.append(arg[5:].strip())
            elif arg.startswith("shallow "):
                client_shallow.append(arg[8:].strip())
            elif arg.startswith("deepen "):
                try:
                    depth = max(0, int(arg[7:].strip()))
                except ValueError:
                    depth = None
        oids, shallow_lines, unshallow_lines = select_fetch_objects(
            gs, wants, haves, depth, client_shallow)
        body = b""
        if shallow_lines or unshallow_lines:
            body += pkt("shallow-info\n")
            for oid in shallow_lines:
                body += pkt("shallow %s\n" % oid)
            for oid in unshallow_lines:
                body += pkt("unshallow %s\n" % oid)
            body += PKT_FLUSH
        body += pkt("packfile\n")
        body += sideband_pack(gs.write_pack(oids))
        body += PKT_FLUSH
        return body

    def git_v0_fetch(self, repo, raw):
        gs = repo["_git"]
        packets, off = parse_pkt_lines(raw)
        wants = []
        client_shallow = []
        depth = None
        for kind, payload in packets:
            if kind != "data":
                continue
            line = payload.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("want "):
                wants.append(line[5:].split(" ")[0])
            elif line.startswith("shallow "):
                client_shallow.append(line[8:].strip())
            elif line.startswith("deepen "):
                try:
                    depth = max(0, int(line[7:].strip()))
                except ValueError:
                    depth = None
        haves = []
        for kind, payload in parse_pkt_lines(raw, off)[0]:
            if kind != "data":
                continue
            line = payload.decode("utf-8", "replace").rstrip("\n")
            if line.startswith("have "):
                haves.append(line[5:].strip())
        oids, _shallow, _unshallow = select_fetch_objects(
            gs, wants, haves, depth, client_shallow)
        return pkt("NAK\n") + sideband_pack(gs.write_pack(oids)) + PKT_FLUSH

    def git_receive_pack(self, repo):
        raw = self.read_body()
        gs = repo["_git"]
        commands = []
        caps = set()
        try:
            packets, off = parse_pkt_lines(raw)
            first = True
            for kind, payload in packets:
                if kind != "data":
                    continue
                text = payload.decode("utf-8", "replace").rstrip("\n")
                if first:
                    nul = text.find("\0")
                    if nul >= 0:
                        caps = set(text[nul + 1:].split(" "))
                        text = text[:nul]
                    first = False
                parts = text.split(" ")
                if len(parts) < 3:
                    raise ValueError("bad push command line")
                commands.append((parts[0], parts[1], parts[2]))
        except ValueError as err:
            self.send_error_json(400, "malformed git request: %s" % err)
            return
        sideband = "side-band-64k" in caps
        pack = raw[off:]
        unpack = "ok"
        if pack:
            try:
                gs.add_pack(pack)
            except ValueError:
                unpack = "error"
        problems = {}
        with STORE.lock:
            for old, new, ref in commands:
                if unpack != "ok":
                    problems[ref] = "unpacker error"
                    continue
                if new == GIT_ZERO:
                    gs.refs.pop(ref, None)
                    if ref.startswith("refs/heads/"):
                        branch = ref[len("refs/heads/"):]
                        repo["refs"].pop(branch, None)
                        repo["branch_files"].pop(branch, None)
                        STORE.touch(repo)
                    continue
                if new not in gs.objects:
                    problems[ref] = "missing necessary objects"
                    continue
                if old != GIT_ZERO and not git_is_ancestor(gs, old, new):
                    problems[ref] = "non-fast-forward"
                    continue
                gs.refs[ref] = new
                # The update lives in the git store; mirror it into the record
                # the REST layer reads, so a pushed branch shows up in
                # /branches, /contents and pull-request creation.
                import_git_ref(repo, ref, new)
        lines = ["unpack %s" % unpack]
        for _old, _new, ref in commands:
            if ref in problems:
                lines.append("ng %s %s" % (ref, problems[ref]))
            else:
                lines.append("ok %s" % ref)
        if sideband:
            body = b"".join(pkt(b"\x01" + pkt(line + "\n")) for line in lines) + PKT_FLUSH
        else:
            body = b"".join(pkt(line + "\n") for line in lines) + PKT_FLUSH
        self.send_git(200, "application/x-git-receive-pack-result", body)

    # ---- verbs -----------------------------------------------------------
    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def do_OPTIONS(self):
        self.send_json(200, {"ok": True}, {"Allow": "GET, POST, PATCH, PUT, DELETE"})

    def _replay(self, blob: bytes) -> None:
        try:
            self.wfile.write(blob)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def dispatch(self, method: str):
        # Reads repeat a lot here (the suite hammers one issue URL and shallow
        # clones back to back), so identical GET/HEAD requests that carry no
        # body are answered by replaying the exact bytes we sent last time.
        verb = self.command
        key = None
        if verb in ("GET", "HEAD"):
            key = (verb, self.path, self.headers.get("Authorization") or "",
                   self.headers.get("Git-Protocol") or "")
        capture = None
        try:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            # Always consume the body first: a request whose body is left in the
            # socket desynchronizes the next request on the same keep-alive connection.
            if self.read_body():
                key = None
            if key is None:
                # The store is single-writer: any mutation, and any read that
                # carries a body, invalidates every response we are holding.
                RESPONSE_CACHE.clear()
            else:
                cached = RESPONSE_CACHE.get(key)
                if cached is not None:
                    self._replay(cached)
                    return
                capture = BufferedWriter(self.wfile)
                self.wfile = capture
            if path == "/healthz":
                self.send_json(200, {"ok": True, "status": "ok"})
                return
            if path == "/mcp" or path.startswith("/mcp/"):
                self.handle_mcp(method)
                return
            if ".git" in path and self.handle_git(method, path, query):
                return
            if path.startswith("/api/v3"):
                self.handle_rest(method, path[len("/api/v3"):] or "/", query)
                return
            if path.startswith("/api/graphql"):
                self.send_json(200, {"data": None, "errors": [
                    {"message": "GraphQL is not implemented in this milestone"}]})
                return
            self.send_error_json(404, "Not Found")
        except ApiError as err:
            self.send_error_json(err.status, err.message)
        except Exception as err:  # never leak HTML
            self.send_error_json(500, "Internal error: %s" % err)
        finally:
            # Hand the socket back its writer, send the finished bytes and keep
            # them for the next identical read.
            if capture is not None:
                self.wfile = capture.real
                blob = capture.finish()
                self._replay(blob)
                if key is not None and cacheable_status(blob):
                    RESPONSE_CACHE.put(key, blob)

    # ---- REST ------------------------------------------------------------
    def handle_rest(self, method: str, path: str, query: dict):
        store = STORE
        if not self.require_auth():
            return
        if method == "POST" and path.startswith("/graphql"):
            self.send_json(200, {"data": None, "errors": [
                {"message": "GraphQL is not implemented in this milestone"}]})
            return
        segments = [s for s in path.split("/") if s != ""]
        with store.lock:
            if segments == []:
                self.rest_root()
                return
            head = segments[0]
            if head == "meta":
                self.send_json(200, {"installed_version": SERVER_VERSION,
                                     "verifiable_password_authentication": False,
                                     "ssh_key_fingerprints": {}, "hooks": [],
                                     "web": [], "api": [], "git": [], "pages": [],
                                     "importer": [], "actions": [], "dependabot": []})
                return
            if head == "user":
                if len(segments) == 1 and method == "GET":
                    self.send_json(200, dict(store.viewer))
                    return
                if len(segments) == 2 and segments[1] == "repos":
                    if method == "GET":
                        repos = [repo_json(r) for (o, n), r in
                                 sorted(store.repos.items(), key=lambda kv: kv[1]["id"])]
                        self.send_paged(repos, query)
                        return
                    if method == "POST":
                        body = self.json_body(required=True)
                        repo = store.create_repo(body.get("name"),
                                                 body.get("private", False),
                                                 body.get("description") or "")
                        self.send_json(201, repo_json(repo))
                        return
            if head == "repos" and len(segments) >= 3:
                self.handle_repo(method, segments[1], segments[2], segments[3:], query)
                return
            if head == "rate_limit":
                self.send_json(200, {"resources": {}, "rate": {"limit": 5000, "remaining": 4999,
                                                               "reset": BASE_TS, "used": 1}})
                return
        self.send_error_json(404, "Not Found")

    def rest_root(self):
        self.send_json(200, {
            "current_user_url": "/api/v3/user",
            "current_user_repositories_url": "/api/v3/user/repos",
            "repository_url": "/api/v3/repos/{owner}/{repo}",
            "user_url": "/api/v3/users/{user}",
            "issues_url": "/api/v3/issues",
            "emojis_url": "/api/v3/emojis",
            "rate_limit_url": "/api/v3/rate_limit",
            "installed_version": SERVER_VERSION,
        })

    def send_paged(self, items: list, query: dict):
        per_page, page = page_params(query)
        start = (page - 1) * per_page
        window = items[start:start + per_page]
        headers = {}
        links = []
        host = self.headers.get("Host") or "localhost"
        parsed = urlparse(self.path)
        def link(page_number, rel):
            qs = dict((k, v[0]) for k, v in query.items())
            qs["page"] = str(page_number)
            qs["per_page"] = str(per_page)
            url = "http://%s%s?%s" % (host, parsed.path, "&".join(
                "%s=%s" % (quote(k), quote(v)) for k, v in sorted(qs.items())))
            return '<%s>; rel="%s"' % (url, rel)
        last_page = max(1, (len(items) + per_page - 1) // per_page)
        if page < last_page:
            links.append(link(page + 1, "next"))
            links.append(link(last_page, "last"))
        if page > 1:
            links.append(link(page - 1, "prev"))
            links.append(link(1, "first"))
        if links:
            headers["Link"] = ", ".join(links)
        self.send_json(200, window, headers)

    def handle_repo(self, method: str, owner: str, name: str, rest: list, query: dict):
        store = STORE
        if owner != VIEWER or (VIEWER, name) not in store.repos:
            self.send_error_json(404, "Not Found")
            return
        repo = store.repos[(VIEWER, name)]
        if rest == []:
            if method == "GET":
                self.send_json(200, repo_json(repo))
                return
            if method in ("PATCH", "POST"):
                body = self.json_body(required=True)
                if "description" in body:
                    repo["description"] = body["description"] if body["description"] is not None else ""
                if "name" in body and isinstance(body["name"], str) and body["name"]:
                    repo["name"] = body["name"]
                if "private" in body:
                    repo["private"] = bool(body["private"])
                if "default_branch" in body and body["default_branch"]:
                    repo["default_branch"] = body["default_branch"]
                if "has_issues" in body:
                    repo["has_issues"] = bool(body["has_issues"])
                store.touch(repo)
                self.send_json(200, repo_json(repo))
                return
            if method == "DELETE":
                store.delete_repo(repo)
                self.send_json(204, None, raw=b"")
                return
        if rest[0] == "contents":
            self.handle_contents(method, repo, rest[1:], query)
            return
        if rest[0] == "branches":
            if len(rest) == 1 and method == "GET":
                branches = [{"name": b, "commit": {"sha": ref["sha"],
                                                   "url": "/api/v3/repos/%s/%s/commits/%s" % (VIEWER, name, ref["sha"])},
                             "protected": False}
                            for b, ref in sorted(repo["refs"].items())]
                self.send_json(200, branches)
                return
            if len(rest) == 2 and method == "GET":
                ref = repo["refs"].get(rest[1])
                if ref is None:
                    self.send_error_json(404, "Branch not found")
                    return
                self.send_json(200, {"name": rest[1], "commit": {"sha": ref["sha"]},
                                     "protected": False})
                return
        if rest[0] == "commits":
            if len(rest) == 1 and method == "GET":
                ref = query.get("sha", [None])[0]
                branch = ref or repo["default_branch"]
                if branch not in repo["refs"]:
                    if ref in repo["commits"]:
                        tip_sha = ref
                    else:
                        self.send_json(200, [])
                        return
                else:
                    tip_sha = repo["refs"][branch]["sha"]
                shas = store.walk(repo, tip_sha)
                per_page, page = page_params(query)
                start = (page - 1) * per_page
                self.send_json(200, [store.commit_json(repo, s) for s in shas[start:start + per_page]])
                return
            if len(rest) == 2 and method == "GET":
                if rest[1] not in repo["commits"]:
                    self.send_error_json(404, "Not Found")
                    return
                self.send_json(200, store.commit_json(repo, rest[1]))
                return
        if rest[0] == "git" and len(rest) >= 2:
            if rest[1] == "refs" and len(rest) >= 3:
                if rest[2] == "heads" and len(rest) == 4:
                    ref = repo["refs"].get(rest[3])
                    if ref is None:
                        self.send_error_json(404, "Not Found")
                        return
                    self.send_json(200, {"ref": "refs/heads/%s" % rest[3],
                                         "node_id": "MDM6UmVm", "url": "/api/v3/repos/%s/%s/git/refs/heads/%s" % (VIEWER, name, rest[3]),
                                         "object": {"sha": ref["sha"], "type": "commit",
                                                    "url": "/api/v3/repos/%s/%s/git/commits/%s" % (VIEWER, name, ref["sha"])}})
                    return
                if method == "GET":
                    refs = [{"ref": ref["ref"], "node_id": "MDM6UmVm",
                             "object": {"sha": ref["sha"], "type": "commit"}}
                            for ref in sorted(repo["refs"].values(), key=lambda r: r["ref"])]
                    self.send_json(200, refs)
                    return
            if rest[1] == "refs" and method == "POST":
                body = self.json_body(required=True)
                ref = body.get("ref") or ""
                sha = body.get("sha") or ""
                if not ref.startswith("refs/heads/"):
                    self.send_error_json(422, "Validation Failed: ref must be refs/heads/<branch>")
                    return
                branch = ref[len("refs/heads/"):]
                if branch in repo["refs"]:
                    self.send_error_json(422, "Reference already exists")
                    return
                if sha not in repo["commits"]:
                    self.send_error_json(422, "Validation Failed: sha is not a commit")
                    return
                store.set_branch(repo, branch, sha, dict(repo["trees"][repo["commits"][sha]["tree"]]))
                store.touch(repo)
                self.send_json(201, {"ref": ref, "node_id": "MDM6UmVm",
                                     "url": "/api/v3/repos/%s/%s/git/refs/heads/%s" % (VIEWER, name, branch),
                                     "object": {"sha": sha, "type": "commit"}})
                return
            if rest[1] == "commits" and len(rest) == 3 and method == "GET":
                if rest[2] not in repo["commits"]:
                    self.send_error_json(404, "Not Found")
                    return
                self.send_json(200, {"sha": rest[2], "tree": {"sha": repo["commits"][rest[2]]["tree"]},
                                     "message": repo["commits"][rest[2]]["message"],
                                     "parents": [{"sha": p} for p in repo["commits"][rest[2]]["parents"]]})
                return
        if rest[0] == "labels":
            if method == "GET":
                self.send_json(200, [store.label_json(l) for l in repo["labels"].values()])
                return
            if method == "POST":
                body = self.json_body(required=True)
                label_name = body.get("name")
                if not isinstance(label_name, str) or label_name == "":
                    self.send_error_json(422, "Validation Failed: name is required")
                    return
                label = repo["labels"].get(label_name)
                if label is None:
                    label = {"id": store.next_label_id, "name": label_name,
                             "color": body.get("color", "ededed"),
                             "description": body.get("description")}
                    store.next_label_id += 1
                    repo["labels"][label_name] = label
                self.send_json(201, store.label_json(label))
                return
        if rest[0] == "issues":
            self.handle_issues(method, repo, rest[1:], query)
            return
        if rest[0] == "pulls":
            self.handle_pulls(method, repo, rest[1:], query)
            return
        self.send_error_json(404, "Not Found")

    def handle_contents(self, method: str, repo: dict, rest: list, query: dict):
        store = STORE
        branch = query.get("ref", [None])[0] or repo["default_branch"]
        files = store.files_of(repo, branch)
        if method == "GET":
            if not rest:
                entries = [self.content_entry(repo, path, files[path], branch) for path in sorted(files)]
                self.send_json(200, entries)
                return
            path = "/".join(rest)
            if path in files:
                data = repo["blobs"].get(files[path], b"")
                self.send_json(200, {
                    "type": "file", "encoding": "base64", "size": len(data),
                    "name": path.split("/")[-1], "path": path, "content": b64encode(data),
                    "sha": files[path], "url": "/api/v3/repos/%s/%s/contents/%s" % (VIEWER, repo["name"], path),
                    "git_url": "/api/v3/repos/%s/%s/git/blobs/%s" % (VIEWER, repo["name"], files[path]),
                    "html_url": "https://github.com/%s/%s/blob/%s/%s" % (VIEWER, repo["name"], branch, path),
                    "download_url": None, "_links": {"self": "/api/v3/repos/%s/%s/contents/%s" % (VIEWER, repo["name"], path)}})
                return
            prefix = path.rstrip("/") + "/"
            entries = [self.content_entry(repo, p, files[p], branch) for p in sorted(files)
                       if p.startswith(prefix)]
            if entries:
                self.send_json(200, entries)
                return
            self.send_error_json(404, "Not Found")
            return
        body = self.json_body(required=True)
        message = body.get("message") or "Update"
        if method == "PUT":
            path = "/".join(rest)
            if not path:
                self.send_error_json(422, "Validation Failed: path is required")
                return
            target_branch = body.get("branch") or repo["default_branch"]
            target_files = dict(store.files_of(repo, target_branch))
            if target_branch not in repo["refs"]:
                source_branch = repo["default_branch"]
                if source_branch in repo["refs"]:
                    base_sha = store.tip(repo, source_branch)
                    target_files = dict(store.files_of(repo, source_branch))
                else:
                    base_sha = None
                    target_files = {}
            else:
                base_sha = store.tip(repo, target_branch)
            existing = target_files.get(path)
            provided_sha = body.get("sha")
            created = existing is None
            if not created and provided_sha is not None and provided_sha != existing:
                self.send_error_json(409, "Conflict: sha does not match the current content")
                return
            data = b64decode(body.get("content") or "")
            oid = blob_oid(data)
            repo["blobs"][oid] = data
            target_files[path] = oid
            parents = [base_sha] if base_sha else []
            sha = store.commit(repo, target_branch, target_files, message, parents)
            store.set_branch(repo, target_branch, sha, target_files)
            store.touch(repo)
            self.send_json(201 if created else 200, {
                "content": {"name": path.split("/")[-1], "path": path, "sha": oid,
                            "size": len(data), "type": "file", "encoding": "base64",
                            "content": b64encode(data),
                            "url": "/api/v3/repos/%s/%s/contents/%s" % (VIEWER, repo["name"], path)},
                "commit": {"sha": sha, "node_id": "C_%s" % sha, "message": message,
                           "url": "/api/v3/repos/%s/%s/git/commits/%s" % (VIEWER, repo["name"], sha),
                           "html_url": "https://github.com/%s/%s/commit/%s" % (VIEWER, repo["name"], sha),
                           "author": {"name": VIEWER, "email": "%s@users.noreply.github.com" % VIEWER},
                           "committer": {"name": VIEWER, "email": "%s@users.noreply.github.com" % VIEWER},
                           "tree": {"sha": repo["commits"][sha]["tree"]},
                           "parents": [{"sha": p} for p in repo["commits"][sha]["parents"]]},
            })
            return
        if method == "DELETE":
            path = "/".join(rest)
            target_branch = body.get("branch") or repo["default_branch"]
            target_files = dict(store.files_of(repo, target_branch))
            if path not in target_files:
                self.send_error_json(404, "Not Found")
                return
            provided_sha = body.get("sha")
            if provided_sha is not None and provided_sha != target_files[path]:
                self.send_error_json(409, "Conflict: sha does not match the current content")
                return
            del target_files[path]
            base_sha = store.tip(repo, target_branch)
            sha = store.commit(repo, target_branch, target_files, message, [base_sha] if base_sha else [])
            store.set_branch(repo, target_branch, sha, target_files)
            store.touch(repo)
            self.send_json(200, {"commit": {"sha": sha, "message": message,
                                            "url": "/api/v3/repos/%s/%s/git/commits/%s" % (VIEWER, repo["name"], sha)},
                                 "content": None})
            return
        self.send_error_json(405, "Method Not Allowed")

    def content_entry(self, repo: dict, path: str, oid: str, branch: str) -> dict:
        depth = path.count("/")
        if depth == 0:
            name = path
        else:
            name = path.split("/")[0]
        return {"type": "file", "size": len(repo["blobs"].get(oid, b"")), "name": path.split("/")[-1],
                "path": path, "sha": oid,
                "url": "/api/v3/repos/%s/%s/contents/%s" % (VIEWER, repo["name"], path),
                "git_url": "/api/v3/repos/%s/%s/git/blobs/%s" % (VIEWER, repo["name"], oid),
                "html_url": "https://github.com/%s/%s/blob/%s/%s" % (VIEWER, repo["name"], branch, path),
                "download_url": None}

    def handle_issues(self, method: str, repo: dict, rest: list, query: dict):
        store = STORE
        if not rest:
            if method == "GET":
                state = (query.get("state", ["open"])[0] or "open").lower()
                sort = (query.get("sort", ["created"])[0] or "created").lower()
                direction = (query.get("direction", ["asc"])[0] or "asc").lower()
                if direction not in ("asc", "desc"):
                    direction = "asc"
                issues = list_issues(repo, state, sort, direction)
                per_page, page = page_params(query)
                start = (page - 1) * per_page
                self.send_json(200, [store.issue_json(repo, i) for i in issues[start:start + per_page]])
                return
            if method == "POST":
                body = self.json_body(required=True)
                issue, _ = create_issue(repo, body.get("title", ""), body.get("body", ""),
                                        body.get("labels"))
                self.send_json(201, store.issue_json(repo, issue))
                return
        if len(rest) == 1 and method in ("GET", "PATCH"):
            try:
                number = int(rest[0])
            except ValueError:
                self.send_error_json(404, "Not Found")
                return
            issue = repo["issues_by_number"].get(number)
            if issue is None:
                self.send_error_json(404, "Not Found")
                return
            if method == "GET":
                self.send_json(200, store.issue_json(repo, issue))
                return
            body = self.json_body(required=True)
            if "state" in body and body["state"] in ("open", "closed"):
                issue["state"] = body["state"]
                issue["closed_at"] = now_iso() if body["state"] == "closed" else None
            if "title" in body and body["title"]:
                issue["title"] = body["title"]
            if "body" in body and body["body"] is not None:
                issue["body"] = body["body"]
            issue["updated_at"] = now_iso()
            store.touch(repo)
            self.send_json(200, store.issue_json(repo, issue))
            return
        if len(rest) == 2 and rest[1] == "comments":
            try:
                number = int(rest[0])
            except ValueError:
                self.send_error_json(404, "Not Found")
                return
            issue = repo["issues_by_number"].get(number)
            if issue is None:
                self.send_error_json(404, "Not Found")
                return
            if method == "GET":
                self.send_json(200, [store.comment_json(repo, issue, c) for c in issue["comments"]])
                return
            if method == "POST":
                body = self.json_body(required=True)
                ts, iso = stamp()
                comment = {"id": store.next_comment_id, "body": body.get("body") or "",
                           "created_at": iso, "updated_at": iso, "_ts": ts}
                store.next_comment_id += 1
                issue["comments"].append(comment)
                self.send_json(201, store.comment_json(repo, issue, comment))
                return
        self.send_error_json(404, "Not Found")

    def handle_pulls(self, method: str, repo: dict, rest: list, query: dict):
        store = STORE
        if not rest:
            if method == "GET":
                state = (query.get("state", ["open"])[0] or "open").lower()
                pulls = list_pulls(repo, state)
                per_page, page = page_params(query)
                start = (page - 1) * per_page
                self.send_json(200, [store.pull_json(repo, p) for p in pulls[start:start + per_page]])
                return
            if method == "POST":
                body = self.json_body(required=True)
                head = body.get("head")
                base = body.get("base") or repo["default_branch"]
                if isinstance(head, str) and ":" in head:
                    head = head.split(":", 1)[1]
                pull = create_pull(repo, body.get("title", ""), body.get("body", ""), head, base)
                self.send_json(201, store.pull_json(repo, pull))
                return
        try:
            number = int(rest[0])
        except (IndexError, ValueError):
            self.send_error_json(404, "Not Found")
            return
        pull = repo["pulls"].get(number)
        if pull is None:
            self.send_error_json(404, "Not Found")
            return
        if len(rest) == 1 and method == "GET":
            self.send_json(200, store.pull_json(repo, pull))
            return
        if len(rest) == 1 and method == "PATCH":
            body = self.json_body(required=True)
            if "title" in body and body["title"]:
                pull["title"] = body["title"]
            if "body" in body and body["body"] is not None:
                pull["body"] = body["body"]
            if "state" in body and body["state"] in ("open", "closed"):
                pull["state"] = body["state"]
            pull["updated_at"] = now_iso()
            self.send_json(200, store.pull_json(repo, pull))
            return
        if len(rest) == 2 and rest[1] == "files" and method == "GET":
            paths = store.changed_paths(repo, pull["base_sha"], pull["head_sha"])
            self.send_json(200, pull["files"])
            return
        if len(rest) == 2 and rest[1] == "merge" and method == "PUT":
            body = self.json_body()
            result = merge_pull(repo, pull, body.get("merge_method") or "squash")
            self.send_json(200, result)
            return
        if len(rest) == 2 and rest[1] == "commits" and method == "GET":
            self.send_json(200, [store.commit_json(repo, pull["head_sha"])])
            return
        self.send_error_json(404, "Not Found")

    # ---- MCP -------------------------------------------------------------
    def handle_mcp(self, method: str):
        if not self.require_auth():
            return
        if method == "DELETE":
            self.send_json(200, {"ok": True})
            return
        if method == "GET":
            self.send_json(405, {"message": "Method Not Allowed: POST JSON-RPC to /mcp"})
            return
        if method != "POST":
            self.send_json(405, {"message": "Method Not Allowed"})
            return
        raw = self.read_body()
        try:
            message = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_mcp({"jsonrpc": "2.0", "id": None,
                           "error": {"code": -32700, "message": "Parse error"}}, status=400)
            return
        if isinstance(message, list):
            responses = [r for r in (self.mcp_one(m) for m in message) if r is not None]
            if not responses:
                self.send_json(202, None, raw=b"")
                return
            self.send_mcp(responses)
            return
        response = self.mcp_one(message)
        if response is None:
            self.send_json(202, None, raw=b"")
            return
        self.send_mcp(response)

    def send_mcp(self, payload, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-GitHub-Request-Id", self._request_id())
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def mcp_one(self, message):
        if not isinstance(message, dict):
            return {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "Invalid Request"}}
        rid = message.get("id")
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method is None or not isinstance(method, str):
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32600, "message": "Invalid Request"}}
        if rid is None:
            # notification: nothing to answer
            return None
        try:
            result = self.mcp_dispatch(method, params)
        except McpToolError as err:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": err.code, "message": err.message}}
        except ApiError as err:
            return {"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32602, "message": "%s (HTTP %d)" % (err.message, err.status)}}
        except Exception as err:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32603, "message": "Internal error: %s" % err}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def mcp_dispatch(self, method: str, params: dict):
        if method == "server/discover":
            return {
                "supportedVersions": ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26"],
                "protocolVersion": "2026-07-28",
                "capabilities": {"tools": {"listChanged": False}, "logging": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION,
                               "title": "makefun forge"},
                "instructions": "A GitHub-compatible forge: repositories, issues, pull "
                                "requests and contents over REST v3 and these tools.",
                "_meta": {"io.modelcontextprotocol/serverInfo": {
                    "name": SERVER_NAME, "version": SERVER_VERSION}},
            }
        if method == "initialize":
            requested = params.get("protocolVersion")
            if not isinstance(requested, str) or len(requested) != 10:
                requested = "2025-11-25"
            return {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION,
                               "title": "makefun forge"},
                "instructions": "A GitHub-compatible forge over REST v3 and MCP tools.",
            }
        if method in ("notifications/initialized", "notifications/cancelled"):
            return {}
        if method == "ping":
            return {}
        if method == "tools/list":
            return json.loads(TOOLS_PAYLOAD)
        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments")
            if not isinstance(args, dict):
                args = {}
            if not isinstance(name, str) or name == "":
                raise McpToolError(-32602, "tools/call requires a tool name")
            with STORE.lock:
                return call_tool(name, args, STORE)
        if method in ("resources/list", "prompts/list", "resources/templates/list"):
            key = {"resources/list": "resources", "prompts/list": "prompts",
                   "resources/templates/list": "resourceTemplates"}[method]
            return {key: []}
        raise McpToolError(-32601, "Method not found: %s" % method)


# --------------------------------------------------------------------------
# boot
# --------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # 50 concurrent clone connections at once overflow the default backlog of 5,
    # so their SYNs are dropped and retried on a 1-2-4 s timer: accept them all.
    request_queue_size = 1024


def post_json(url: str, payload: dict, timeout: float = 5.0) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            try:
                return response.status, json.loads(body.decode("utf-8") or "{}")
            except Exception:
                return response.status, {}
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read().decode("utf-8") or "{}")
        except Exception:
            return err.code, {}
    except Exception as err:
        return 0, {"error": str(err)}


def get_json(url: str, timeout: float = 5.0):
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            try:
                return response.status, json.loads(body.decode("utf-8") or "{}")
            except Exception:
                return response.status, {}
    except Exception:
        return 0, {}


def register(port: int) -> bool:
    base = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not base:
        print("no MAKEFUN_ENV_URL: serving without registration", flush=True)
        return False
    for attempt in range(60):
        status, body = post_json(base + "/register", {"port": port}, timeout=3.0)
        if status == 200:
            print("registered on port %d" % port, flush=True)
            return True
        if status == 409:
            print("registration rejected: already registered", flush=True)
            return True
        time.sleep(min(0.1 * (attempt + 1), 1.0))
    print("registration failed", flush=True)
    return False


def poll_done(stop_after_seconds: float = 0.0) -> None:
    base = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not base:
        return
    while True:
        status, body = get_json(base + "/done", timeout=5.0)
        if status == 200 and isinstance(body, dict) and body.get("done") is True:
            break
        time.sleep(0.05)
    time.sleep(0.25)
    sys.stdout.flush()
    os._exit(0)


def main() -> int:
    server = Server(("0.0.0.0", 0), Handler)
    port = server.server_address[1]
    print("listening on 0.0.0.0:%d (pid %d)" % (port, os.getpid()), flush=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2},
                              daemon=True)
    thread.start()
    register(port)
    if os.environ.get("MAKEFUN_ENV_URL"):
        threading.Thread(target=poll_done, daemon=True).start()
    while True:
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
