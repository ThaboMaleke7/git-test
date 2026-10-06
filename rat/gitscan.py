"""Single-pass git history scanner.

Parses ``git log --no-merges -M50% --numstat -z`` into a compact, cacheable
structure that the metrics engine aggregates:

    {
        "sha": "<resolved ref commit>",
        "commits": [
            {"h": hash, "t": committer timestamp (unix), "n": author name,
             "e": author email, "p": parent hashes, "s": subject,
             "f": [(path, added, removed), ...]},
            ... newest first ...
        ],
    }

Semantics (per the COMS3011A brief):
* only non-merge commits reachable from the requested reference are scanned;
* rename detection is enabled at 50% (``-M50%``); a pure rename contributes
  nothing, a rename with edits is attributed to the *new* path;
* deletions are recorded as removed lines on the deleted path;
* binary files (numstat ``-``) are not measured at all;
* ``%aN``/``%aE`` make git apply the repository's ``.mailmap`` so merged
  identity is available out of the box;
* ``%ct`` is the committer date, used for every time based filter.
"""

from __future__ import annotations

import os
import pickle
import re
import subprocess
import threading
import time

CACHE_VERSION = 3

# start of a numstat record token: "12\t3\t" or "-\t-\t" (path may follow inline)
_RECORD_RE = re.compile(rb"^(\d+|-)\t(\d+|-)\t")


class ScanError(RuntimeError):
    """Raised when git cannot scan the repository."""


def _git_env() -> dict:
    env = dict(os.environ)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",  # never hang on credential prompts
            "GIT_PAGER": "cat",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C.UTF-8",
        }
    )
    return env


def run_git(args, cwd=None, timeout=None, check=False) -> subprocess.CompletedProcess:
    """Run a git command, returning the CompletedProcess (text mode)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_git_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        text=True,
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise ScanError((proc.stderr or proc.stdout or "git failed").strip())
    return proc


def resolve_commit(repo: str, ref: str) -> str:
    """Resolve *ref* (branch, tag, sha, HEAD...) to a commit sha."""
    ref = (ref or "HEAD").strip() or "HEAD"
    proc = run_git(["-C", repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ScanError(f"reference '{ref}' does not resolve to a commit")
    return proc.stdout.strip()


def count_commits(repo: str, sha: str) -> int:
    proc = run_git(["-C", repo, "rev-list", "--count", "--no-merges", sha], check=True)
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return 0


def list_refs(repo: str) -> dict:
    """Return {"head": sha, "branches": [...], "tags": [...]}."""
    out = {"head": None, "branches": [], "tags": []}
    proc = run_git(["-C", repo, "rev-parse", "--verify", "--quiet", "HEAD"])
    if proc.returncode == 0 and proc.stdout.strip():
        out["head"] = proc.stdout.strip()

    fmt = "%(objectname)\t%(refname)\t%(objecttype)\t%(*objecttype)"
    proc = run_git(["-C", repo, "for-each-ref", f"--format={fmt}", "refs/heads", "refs/tags"])
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        sha, refname, otype = parts[0], parts[1], parts[2]
        peeled = parts[3] if len(parts) > 3 else ""
        if refname.startswith("refs/heads/"):
            out["branches"].append({"name": refname[len("refs/heads/"):], "sha": sha})
        elif refname.startswith("refs/tags/"):
            # annotated tags carry the commit in *objecttype; peel if needed
            name = refname[len("refs/tags/"):]
            commit = sha
            if otype == "tag" or peeled == "commit":
                p = run_git(["-C", repo, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"])
                commit = p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else sha
            out["tags"].append({"name": name, "sha": commit})
    return out


def has_mailmap(repo: str) -> bool:
    return os.path.isfile(os.path.join(repo, ".mailmap"))


def is_shallow(repo: str) -> bool:
    git_dir_proc = run_git(["-C", repo, "rev-parse", "--git-dir"])
    git_dir = git_dir_proc.stdout.strip() or ".git"
    if not os.path.isabs(git_dir):
        git_dir = os.path.join(repo, git_dir)
    return os.path.isfile(os.path.join(git_dir, "shallow"))


def tree_files(repo: str, sha: str) -> list:
    """All file paths in the tree at *sha* (used for KPIs / object pickers)."""
    proc = run_git(["-C", repo, "ls-tree", "-r", "-z", "--name-only", sha], check=True)
    return [p for p in proc.stdout.split("\x00") if p]


def parse_history(repo: str, ref: str, progress=None, total_hint: int = 0) -> dict:
    """Stream the full history of *ref* through one git process.

    *progress(done, total)* is invoked periodically so long scans of large
    repositories can be surfaced in the UI.
    """
    sha = resolve_commit(repo, ref)
    total = total_hint or count_commits(repo, sha)

    fmt = "--format=@@%H\x1f%ct\x1f%aN\x1f%aE\x1f%P\x1f%s"
    cmd = [
        "git", "-C", repo, "log", sha,
        "--no-merges",
        "-M50%",              # rename detection at 50%, per the brief
        "--numstat",
        "-z",                 # NUL separated, machine readable
        fmt,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_env(),
    )

    commits = []
    pool = {}                 # string interning keeps caches small
    cur = None                # commit being filled
    pending = None            # (added, removed, inline_path, [extra path tokens])

    def intern(value: str) -> str:
        got = pool.get(value)
        if got is None:
            pool[value] = value
            got = value
        return got

    def flush_record():
        nonlocal pending
        if pending is None:
            return
        add, rem, inline, paths = pending
        pending = None
        if cur is None or add is None or rem is None:
            return  # binary or orphaned record
        if inline:
            path = inline
        elif paths:
            # -z rename layout: stats token (empty inline) + src token + dst
            # token; changes are attributed to the new path
            path = paths[-1]
        else:
            return
        if add == 0 and rem == 0:
            return  # pure rename / mode change: nothing measurable
        cur["f"].append((intern(path), add, rem))

    def flush_commit():
        nonlocal cur
        flush_record()
        if cur is not None:
            commits.append(cur)
            cur = None

    buf = b""
    last_report = 0.0
    while True:
        chunk = proc.stdout.read(1 << 20)
        if not chunk:
            break
        buf += chunk
        parts = buf.split(b"\x00")
        buf = parts.pop()  # keep the (possibly incomplete) tail
        for raw in parts:
            tok = raw[1:] if raw[:1] == b"\n" else raw
            if tok.startswith(b"@@"):
                flush_commit()
                fields = tok[2:].split(b"\x1f", 5)
                while len(fields) < 6:
                    fields.append(b"")
                cur = {
                    "h": fields[0].decode("utf-8", "replace"),
                    "t": int(fields[1] or b"0"),
                    "n": intern(fields[2].decode("utf-8", "replace")),
                    "e": intern(fields[3].decode("utf-8", "replace")),
                    "p": fields[4].decode("utf-8", "replace"),
                    "s": fields[5].decode("utf-8", "replace"),
                    "f": [],
                }
                if progress and total and len(commits) - last_report >= 500:
                    last_report = len(commits)
                    progress(len(commits), total)
            elif tok == b"":
                continue
            elif _RECORD_RE.match(tok):
                flush_record()
                parts = tok.split(b"\t", 2)
                a, r = parts[0], parts[1]
                inline = parts[2] if len(parts) > 2 else b""
                pending = (
                    int(a) if a.isdigit() else None,
                    int(r) if r.isdigit() else None,
                    inline.decode("utf-8", "replace") if inline else None,
                    [],
                )
            else:
                if pending is not None:
                    pending[3].append(tok.decode("utf-8", "replace"))
    flush_commit()

    stderr = proc.stderr.read().decode("utf-8", "replace").strip()
    rc = proc.wait()
    if rc != 0:
        raise ScanError(stderr or f"git log exited with code {rc}")
    if progress:
        progress(len(commits), total or len(commits))

    return {
        "v": CACHE_VERSION,
        "sha": sha,
        "commits": commits,
        "mailmap": has_mailmap(repo),
        "shallow": is_shallow(repo),
        "parsed_at": int(time.time()),
    }


# --------------------------------------------------------------------------
# On-disk parse cache (keyed by resolved commit sha; safe to delete anytime)
# --------------------------------------------------------------------------

_cache_lock = threading.Lock()


def cache_path(cache_dir: str, sha: str) -> str:
    return os.path.join(cache_dir, f"{sha}.pkl")


def load_cache(cache_dir: str, sha: str):
    path = cache_path(cache_dir, sha)
    try:
        with _cache_lock, open(path, "rb") as fh:
            data = pickle.load(fh)
        if isinstance(data, dict) and data.get("v") == CACHE_VERSION:
            return data
    except (OSError, pickle.PickleError, EOFError, AttributeError, ValueError):
        pass
    return None


def save_cache(cache_dir: str, data: dict) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    path = cache_path(cache_dir, data["sha"])
    tmp = f"{path}.{os.getpid()}.tmp"
    # runtime-only accumulators (keys starting with "_") are not persisted
    payload = {k: v for k, v in data.items() if not k.startswith("_")}
    try:
        with _cache_lock, open(tmp, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
