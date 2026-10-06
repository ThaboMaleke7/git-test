"""Repository registry, ingestion and parse management.

Responsibilities
----------------
* keep a JSON registry of analysed repositories (data/repos.json);
* ingest repositories as **zip uploads** (containing the ``.git`` directory or
  file) or as **deep clones** of remote URLs, as background jobs;
* run / cache the single-pass history scan per reference commit;
* persist manual author merges per repository.

Everything lives under a data directory (default ``./data`` relative to the
project root) so the tool is self contained and easy to reset.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
import zipfile
from collections import OrderedDict

from . import gitscan
from . import metrics as metrics_mod

DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

_CLONE_PROGRESS_RE = re.compile(rb"Receiving objects:\s+(\d+)%|Resolving deltas:\s+(\d+)%")


class StoreError(RuntimeError):
    pass


def _now() -> int:
    return int(time.time())


class Store:
    def __init__(self, data_dir: str = DEFAULT_DATA_DIR):
        self.data_dir = os.path.abspath(data_dir)
        self.repos_dir = os.path.join(self.data_dir, "repos")
        self.cache_dir = os.path.join(self.data_dir, "cache")
        self.registry_path = os.path.join(self.data_dir, "registry.json")
        os.makedirs(self.repos_dir, exist_ok=True)
        os.makedirs(self.cache_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._repos = self._load_registry()
        self._jobs = OrderedDict()
        self._parsed = OrderedDict()          # (repo_id, sha) -> parsed data
        self._refs = {}                       # repo_id -> list_refs() result
        self._tree = {}                       # (repo_id, sha) -> [paths]
        self._parse_locks = {}
        self._metrics_cache = OrderedDict()   # filter results (small LRU)
        self._max_parsed = 3
        self._max_metrics = 24

    # ------------------------------------------------------------------ #
    # registry
    # ------------------------------------------------------------------ #
    def _load_registry(self):
        try:
            with open(self.registry_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict) and r.get("id")]
        except (OSError, ValueError):
            pass
        return []

    def _save_registry(self):
        tmp = f"{self.registry_path}.tmp"
        with self._lock, open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._repos, fh, indent=1, sort_keys=True)
        os.replace(tmp, self.registry_path)

    def list_repos(self) -> list:
        with self._lock:
            out = []
            for r in self._repos:
                out.append({
                    "id": r["id"],
                    "name": r["name"],
                    "kind": r["kind"],
                    "source": r.get("source", ""),
                    "created": r.get("created"),
                    "default_ref": r.get("default_ref", "HEAD"),
                    "has_merges": bool(r.get("merges")),
                })
            return out

    def get_repo(self, repo_id: str) -> dict:
        with self._lock:
            for r in self._repos:
                if r["id"] == repo_id:
                    return r
        raise StoreError(f"unknown repository '{repo_id}'")

    def repo_path(self, entry: dict) -> str:
        path = os.path.join(self.repos_dir, entry["id"], "repo")
        if not os.path.isdir(path):
            raise StoreError(f"repository files for '{entry['name']}' are missing on disk")
        return path

    def delete_repo(self, repo_id: str):
        with self._lock:
            entry = self.get_repo(repo_id)
            self._repos = [r for r in self._repos if r["id"] != repo_id]
            for key in [k for k in self._parsed if k[0] == repo_id]:
                self._parsed.pop(key, None)
            for key in [k for k in self._tree if k[0] == repo_id]:
                self._tree.pop(key, None)
            self._refs.pop(repo_id, None)
        self._save_registry()
        shutil.rmtree(os.path.join(self.repos_dir, repo_id), ignore_errors=True)
        shutil.rmtree(os.path.join(self.cache_dir, repo_id), ignore_errors=True)
        return entry

    # ------------------------------------------------------------------ #
    # jobs
    # ------------------------------------------------------------------ #
    def _new_job(self, label: str) -> dict:
        job = {
            "id": uuid.uuid4().hex[:12],
            "label": label,
            "state": "running",
            "progress": 0,
            "message": "starting...",
            "error": None,
            "repo_id": None,
            "started": _now(),
            "updated": _now(),
        }
        with self._lock:
            self._jobs[job["id"]] = job
            while len(self._jobs) > 50:
                self._jobs.popitem(last=False)
        return job

    def _job_update(self, job: dict, **fields):
        with self._lock:
            job.update(fields)
            job["updated"] = _now()

    def get_job(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise StoreError(f"unknown job '{job_id}'")
            return dict(job)

    # ------------------------------------------------------------------ #
    # ingestion: zip upload
    # ------------------------------------------------------------------ #
    def upload_zip_async(self, filename: str, blob: bytes) -> str:
        job = self._new_job(f"upload {filename}")
        t = threading.Thread(target=self._run_upload, args=(job, filename, blob), daemon=True)
        t.start()
        return job["id"]

    def _run_upload(self, job: dict, filename: str, blob: bytes):
        try:
            name = os.path.splitext(os.path.basename(filename or "upload"))[0] or "upload"
            self._job_update(job, progress=3, message="extracting zip...")
            repo_id = uuid.uuid4().hex[:12]
            target = os.path.join(self.repos_dir, repo_id, "repo")

            tmp_zip = os.path.join(self.repos_dir, repo_id, "upload.zip")
            os.makedirs(os.path.dirname(tmp_zip), exist_ok=True)
            with open(tmp_zip, "wb") as fh:
                fh.write(blob)

            root, members = self._zip_repo_root(tmp_zip)
            if root is None:
                raise StoreError(
                    "the zip does not contain a git repository "
                    "(no .git directory or .git file found)"
                )
            self._job_update(job, progress=10, message="unpacking repository...")
            self._extract_zip_repo(tmp_zip, root, target)
            os.unlink(tmp_zip)

            try:
                self._validate_repo(target, source="zip")
            except StoreError:
                shutil.rmtree(os.path.join(self.repos_dir, repo_id), ignore_errors=True)
                raise
            entry = {
                "id": repo_id,
                "name": name,
                "kind": "zip",
                "source": filename,
                "path": target,
                "created": _now(),
                "default_ref": "HEAD",
                "merges": {},
            }
            with self._lock:
                self._repos.append(entry)
            self._save_registry()
            self._job_update(job, progress=40, message="scanning history...")
            self._warm_scan(entry, job)
            self._job_update(job, state="done", progress=100, message="ready", repo_id=repo_id)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self._job_update(job, state="error", error=str(exc), message="failed")

    @staticmethod
    def _zip_repo_root(zip_path: str):
        """Find the shallowest path prefix that contains a .git entry."""
        try:
            with zipfile.ZipFile(zip_path) as zf:
                members = [m for m in zf.namelist() if not m.endswith("/")]
        except zipfile.BadZipFile as exc:
            raise StoreError("the uploaded file is not a valid zip archive") from exc

        best = None
        best_depth = None
        for m in members:
            parts = posixpath.normpath(m).split("/")
            if ".git" in parts:
                prefix = "/".join(parts[: parts.index(".git")])
                depth = len(prefix.split("/")) if prefix else 0
                if best is None or depth < best_depth:
                    best, best_depth = prefix, depth
        if best is None:
            return None, members
        return best, members

    @staticmethod
    def _extract_zip_repo(zip_path: str, root: str, target: str):
        """Extract only the repository subtree, skipping unsafe/symlink entries."""
        prefix = (root + "/") if root else ""
        os.makedirs(target, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                name = info.filename
                if name.endswith("/"):
                    continue
                norm = posixpath.normpath(name)
                if norm.startswith("/") or norm.startswith("..") or "/../" in norm:
                    continue  # zip-slip guard
                if prefix and not name.startswith(prefix):
                    continue
                rel = name[len(prefix):]
                if not rel or rel.startswith(".."):
                    continue
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode):
                    continue  # worktree symlinks are irrelevant for analysis
                dest = os.path.join(target, *[p for p in rel.split("/") if p not in ("", ".")])
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)

    def _validate_repo(self, path: str, source: str):
        dotgit = os.path.join(path, ".git")
        if os.path.isfile(dotgit):
            # linked worktree: only usable when the target exists locally
            try:
                with open(dotgit, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read().strip()
            except OSError as exc:
                raise StoreError(f"cannot read .git file: {exc}") from exc
            m = re.match(r"gitdir:\s*(.+)$", content)
            goal = m.group(1).strip() if m else ""
            if not goal or not os.path.isdir(os.path.join(path, goal) if not os.path.isabs(goal) else goal):
                raise StoreError(
                    ".git is a file pointing outside the archive (linked worktree). "
                    "Please zip the repository together with its .git directory."
                )
        if not os.path.exists(dotgit):
            raise StoreError("the archive does not contain a .git file or directory")

        proc = gitscan.run_git(["-C", path, "rev-parse", "--verify", "--quiet", "HEAD"])
        if proc.returncode != 0 or not proc.stdout.strip():
            raise StoreError("git repository has no commits on HEAD")

    # ------------------------------------------------------------------ #
    # ingestion: clone
    # ------------------------------------------------------------------ #
    def clone_async(self, url: str, name: str = "") -> str:
        job = self._new_job(f"clone {url}")
        t = threading.Thread(target=self._run_clone, args=(job, url, name), daemon=True)
        t.start()
        return job["id"]

    def _run_clone(self, job: dict, url: str, name: str):
        try:
            url = (url or "").strip()
            if not re.match(r"^(https?://|ssh://|git://|file://|[^@\s]+@[^:\s]+:)", url):
                raise StoreError(
                    "please provide a remote URL, e.g. https://github.com/owner/repo.git "
                    "or git@github.com:owner/repo.git"
                )
            repo_id = uuid.uuid4().hex[:12]
            target = os.path.join(self.repos_dir, repo_id, "repo")
            os.makedirs(os.path.dirname(target), exist_ok=True)

            self._job_update(job, progress=5, message="cloning (deep)...")
            proc = subprocess.Popen(
                ["git", "clone", "--progress", url, target],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=gitscan._git_env(),
            )
            tail = []
            while True:
                line = proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip().replace("\r", " ")
                if text:
                    tail.append(text)
                    tail = tail[-4:]
                m = _CLONE_PROGRESS_RE.search(line.replace(b"\r", b"\n"))
                if m:
                    pct = int(m.group(1)) if m.group(1) else int(m.group(2))
                    if m.group(1):
                        self._job_update(job, progress=5 + int(pct * 0.8), message=text)
                    else:
                        self._job_update(job, progress=85 + int(pct * 0.1), message=text)
            rc = proc.wait()
            if rc != 0 or not os.path.isdir(os.path.join(target, ".git")):
                shutil.rmtree(os.path.join(self.repos_dir, repo_id), ignore_errors=True)
                raise StoreError("git clone failed: " + (" | ".join(tail[-2:]) or "unknown error"))

            display = (name or "").strip() or self._name_from_url(url)
            entry = {
                "id": repo_id,
                "name": display,
                "kind": "clone",
                "source": url,
                "path": target,
                "created": _now(),
                "default_ref": "HEAD",
                "merges": {},
            }
            try:
                self._validate_repo(target, source="clone")
            except StoreError:
                shutil.rmtree(os.path.join(self.repos_dir, repo_id), ignore_errors=True)
                raise
            with self._lock:
                self._repos.append(entry)
            self._save_registry()
            self._job_update(job, progress=90, message="scanning history...")
            self._warm_scan(entry, job)
            self._job_update(job, state="done", progress=100, message="ready", repo_id=repo_id)
        except Exception as exc:  # noqa: BLE001
            self._job_update(job, state="error", error=str(exc), message="failed")

    @staticmethod
    def _name_from_url(url: str) -> str:
        tail = url.rstrip("/").split("/")[-1]
        if tail.endswith(".git"):
            tail = tail[:-4]
        return tail or "repository"

    # ------------------------------------------------------------------ #
    # scanning / parsed data
    # ------------------------------------------------------------------ #
    def _warm_scan(self, entry: dict, job: dict):
        """Parse the default ref while the ingest job is still running."""
        try:
            self.parsed(entry["id"], "HEAD", progress=lambda done, total: self._job_update(
                job,
                progress=40 + int(55 * (done / total if total else 1)),
                message=f"scanning history... {done:,} commits",
            ))
        except Exception:  # noqa: BLE001 - cold scans can be retried later
            pass

    def refs(self, repo_id: str, refresh: bool = False) -> dict:
        entry = self.get_repo(repo_id)
        with self._lock:
            cached = self._refs.get(repo_id)
        if cached and not refresh:
            return cached
        info = gitscan.list_refs(self.repo_path(entry))
        with self._lock:
            self._refs[repo_id] = info
        return info

    def resolve(self, repo_id: str, ref: str) -> str:
        entry = self.get_repo(repo_id)
        return gitscan.resolve_commit(self.repo_path(entry), ref)

    def parsed(self, repo_id: str, ref: str, progress=None) -> dict:
        entry = self.get_repo(repo_id)
        path = self.repo_path(entry)
        sha = gitscan.resolve_commit(path, ref)
        key = (repo_id, sha)

        with self._lock:
            data = self._parsed.get(key)
            if data is not None:
                self._parsed.move_to_end(key)
                return data

        lock = None
        with self._lock:
            lock = self._parse_locks.setdefault(key, threading.Lock())
        with lock:
            with self._lock:
                data = self._parsed.get(key)
                if data is not None:
                    self._parsed.move_to_end(key)
                    return data
            cache_dir = os.path.join(self.cache_dir, repo_id)
            data = gitscan.load_cache(cache_dir, sha)
            if data is None:
                data = gitscan.parse_history(path, ref, progress=progress)
                gitscan.save_cache(cache_dir, data)
            with self._lock:
                self._parsed[key] = data
                self._parsed.move_to_end(key)
                while len(self._parsed) > self._max_parsed:
                    self._parsed.popitem(last=False)
            return data

    def tree_stats(self, repo_id: str, ref: str) -> dict:
        entry = self.get_repo(repo_id)
        path = self.repo_path(entry)
        sha = gitscan.resolve_commit(path, ref)
        key = (repo_id, sha)
        with self._lock:
            files = self._tree.get(key)
        if files is None:
            files = gitscan.tree_files(path, sha)
            with self._lock:
                self._tree[key] = files
        dirs = set()
        for f in files:
            parts = f.split("/")
            for i in range(len(parts) - 1):
                dirs.add("/".join(parts[: i + 1]))
        return {"files": len(files), "dirs": len(dirs)}

    # ------------------------------------------------------------------ #
    # queries
    # ------------------------------------------------------------------ #
    def commits_page(self, repo_id: str, ref: str, offset: int = 0, limit: int = 100, q: str = "") -> dict:
        data = self.parsed(repo_id, ref)
        commits = data["commits"]
        ql = (q or "").strip().lower()
        if ql:
            rows = []
            for c in commits:
                if (c["h"].startswith(ql) or ql in c["s"].lower()
                        or ql in c["n"].lower() or ql in c["e"].lower()):
                    rows.append(c)
        else:
            rows = commits
        total = len(rows)
        offset = max(0, min(offset, total))
        limit = max(1, min(limit, 500))
        page = rows[offset:offset + limit]
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "rows": [
                {"h": c["h"], "t": c["t"], "n": c["n"], "e": c["e"], "s": c["s"],
                 "files": len(c["f"])}
                for c in page
            ],
        }

    def authors(self, repo_id: str, ref: str) -> dict:
        data = self.parsed(repo_id, ref)
        merges = self.get_repo(repo_id).get("merges", {})
        return metrics_mod.author_index(data, merges)

    def authors_light(self, repo_id: str, ref: str) -> list:
        """Cheap identity list (no metrics) used to populate dropdowns."""
        data = self.parsed(repo_id, ref)
        seen = {}
        for c in data["commits"]:
            k = c["n"] + metrics_mod.AUTHOR_SEP + c["e"]
            seen[k] = seen.get(k, 0) + 1
        out = []
        for k, n in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0])):
            name, _, email = k.partition(metrics_mod.AUTHOR_SEP)
            out.append({"key": k, "name": name, "email": email, "commits": n})
        return out

    # ------------------------------------------------------------------ #
    # metrics (with a small per-process result cache)
    # ------------------------------------------------------------------ #
    def _metrics_key(self, repo_id: str, sha: str, filters: dict, extra=()) -> tuple:
        merges = tuple(sorted((self.get_repo(repo_id).get("merges") or {}).items()))
        return (
            repo_id, sha,
            filters.get("mode"), filters.get("from"), filters.get("to"),
            tuple(sorted(filters.get("commits") or [])), filters.get("author"),
            merges, extra,
        )

    def _cached(self, key):
        with self._lock:
            got = self._metrics_cache.get(key)
            if got is not None:
                self._metrics_cache.move_to_end(key)
            return got

    def _cache_put(self, key, value):
        with self._lock:
            self._metrics_cache[key] = value
            self._metrics_cache.move_to_end(key)
            while len(self._metrics_cache) > self._max_metrics:
                self._metrics_cache.popitem(last=False)

    def _meta(self, entry: dict, data: dict, ref: str, filters: dict, took_ms: int) -> dict:
        return {
            "repo_id": entry["id"],
            "repo_name": entry["name"],
            "ref": ref,
            "sha": data["sha"],
            "mailmap": bool(data.get("mailmap")),
            "shallow": bool(data.get("shallow")),
            "commits_scanned": len(data["commits"]),
            "parsed_at": data.get("parsed_at"),
            "filters": filters,
            "took_ms": took_ms,
        }

    def metrics(self, repo_id: str, ref: str, filters: dict) -> dict:
        entry = self.get_repo(repo_id)
        data = self.parsed(repo_id, ref)
        key = self._metrics_key(repo_id, data["sha"], filters)
        cached = self._cached(key)
        if cached is not None:
            return cached
        t0 = time.time()
        result = metrics_mod.compute(data, filters, entry.get("merges") or {})
        result["meta"] = self._meta(entry, data, ref, filters, int((time.time() - t0) * 1000))
        self._cache_put(key, result)
        return result

    def object_detail(self, repo_id: str, ref: str, filters: dict, path: str, kind: str = None) -> dict:
        entry = self.get_repo(repo_id)
        data = self.parsed(repo_id, ref)
        key = self._metrics_key(repo_id, data["sha"], filters, extra=(path, kind))
        cached = self._cached(key)
        if cached is not None:
            return cached
        t0 = time.time()
        result = metrics_mod.object_detail(data, filters, entry.get("merges") or {}, path, kind)
        result["meta"] = self._meta(entry, data, ref, filters, int((time.time() - t0) * 1000))
        self._cache_put(key, result)
        return result

    # ------------------------------------------------------------------ #
    # merges
    # ------------------------------------------------------------------ #
    def set_merges(self, repo_id: str, target: str, sources: list) -> dict:
        entry = self.get_repo(repo_id)
        merges = dict(entry.get("merges") or {})
        sources = [s for s in (sources or []) if s and s != target]
        for s in sources:
            merges[s] = target
        # re-point merges that previously targeted a source to the new target
        for k, v in list(merges.items()):
            if v in sources:
                merges[k] = target
        entry["merges"] = merges
        self._save_registry()
        return merges

    def clear_merges(self, repo_id: str) -> dict:
        entry = self.get_repo(repo_id)
        entry["merges"] = {}
        self._save_registry()
        return {}
