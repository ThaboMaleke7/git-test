#!/usr/bin/env python3
"""Correctness tests for the RAT metric engine (stdlib only).

Builds a small repository with hand-computed expected metrics covering:
  * initial commit, modifications, a deleted file, a binary file
  * a pure rename (must not change metrics) and a rename + edit
    (attributed to the new path)
  * a merge commit (excluded) and a side-branch commit (included)
  * .mailmap merging and manual author merging
  * commit-set metrics, modification frequency, churn rate and ownership
  * zip ingestion end-to-end through the store

Run:  python3 tests/test_metrics.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from rat import gitscan, metrics as metrics_mod          # noqa: E402
from rat.store import Store                              # noqa: E402

# timestamps used for every commit (UNIX, UTC)
T1 = 1577836800          # 2020-01-01
T2 = 1577923200          # 2020-01-02
T3 = 1578009600          # 2020-01-03
T3B = 1578052800         # 2020-01-03 12:00
T4 = 1578096000          # 2020-01-04
T5 = 1578182400          # 2020-01-05
T6 = 1578268800          # 2020-01-06
TM = 1578355200          # 2020-01-07 (merge)

RESULTS = []


def check(name, actual, expected):
    ok = actual == expected
    RESULTS.append((ok, name, actual, expected))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + ("" if ok else f"   expected={expected!r} actual={actual!r}"))


def check_close(name, actual, expected, places=9):
    ok = abs(actual - expected) < 10 ** (-places)
    RESULTS.append((ok, name, actual, expected))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + ("" if ok else f"   expected={expected!r} actual={actual!r}"))


def run(args, cwd, env_extra=None, check_rc=True):
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8"})
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(["git", *args], cwd=cwd, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if check_rc and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


def write(path, content, binary=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if binary else "w"
    with open(path, mode) as fh:
        fh.write(content)


def build_repo(path):
    """Create the test repository; returns {label: commit hash}."""
    run(["init", "-q", "-b", "main"], path)
    hashes = {}

    def commit(msg, when, name, email):
        env = {
            "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
            "GIT_AUTHOR_DATE": f"@{when} +0000", "GIT_COMMITTER_DATE": f"@{when} +0000",
        }
        run(["add", "-A"], path, env)
        run(["commit", "-q", "--allow-empty", "-m", msg], path, env)
        return run(["rev-parse", "HEAD"], path).stdout.strip()

    ada = ("Ada", "ada@example.com")
    bob = ("Bob", "bob@example.com")
    carol = ("Carol", "carol@example.com")

    # c1: a.txt (3 lines) + dir/b.txt (2 lines) + binary blob
    write(os.path.join(path, "a.txt"), "one\ntwo\nthree\n")
    write(os.path.join(path, "dir/b.txt"), "b-one\nb-two\n")
    write(os.path.join(path, "bin.dat"), b"\x00\x01\x02\x03binary\x00", binary=True)
    hashes["c1"] = commit("c1", T1, *ada)

    # c2: modify a.txt (+2/-1) and add dir/sub/c.txt (4 lines)
    write(os.path.join(path, "a.txt"), "two\nthree\nfour\nfive\n")
    write(os.path.join(path, "dir/sub/c.txt"), "c1\nc2\nc3\nc4\n")
    hashes["c2"] = commit("c2", T2, *ada)

    # c3: rename dir/sub/c.txt -> dir/sub/d.txt with +1 line; delete dir/b.txt
    os.rename(os.path.join(path, "dir/sub/c.txt"), os.path.join(path, "dir/sub/d.txt"))
    write(os.path.join(path, "dir/sub/d.txt"), "c1\nc2\nc3\nc4\nc5\n")
    os.unlink(os.path.join(path, "dir/b.txt"))
    hashes["c3"] = commit("c3", T3, *bob)

    # f1 (side branch): modify dir/sub/d.txt (+2/-1)
    run(["checkout", "-q", "-b", "feature", hashes["c3"]], path)
    write(os.path.join(path, "dir/sub/d.txt"), "c1\nX\nc3\nc4\nc5\nY\n")  # -c2 +X +Y
    hashes["f1"] = commit("f1", T3B, *carol)

    run(["checkout", "-q", "main"], path)

    # c4: pure rename a.txt -> renamed_a.txt (no content change)
    run(["mv", "a.txt", "renamed_a.txt"], path)
    hashes["c4"] = commit("c4", T4, *bob)

    # c5: binary-only change (must not be measured)
    write(os.path.join(path, "bin.dat"), b"\x00\x01\x02\x03\x04binary!\x00", binary=True)
    hashes["c5"] = commit("c5", T5, *ada)

    # c6: append 3 lines to renamed_a.txt
    write(os.path.join(path, "renamed_a.txt"), "two\nthree\nfour\nfive\nsix\nseven\neight\n")
    hashes["c6"] = commit("c6", T6, *bob)

    # merge feature into main (merge commit must be excluded from H-bar)
    env = {
        "GIT_AUTHOR_NAME": "Bob", "GIT_AUTHOR_EMAIL": "bob@example.com",
        "GIT_COMMITTER_NAME": "Bob", "GIT_COMMITTER_EMAIL": "bob@example.com",
        "GIT_AUTHOR_DATE": f"@{TM} +0000", "GIT_COMMITTER_DATE": f"@{TM} +0000",
    }
    run(["merge", "-q", "--no-ff", "-m", "merge feature", "feature"], path, env)

    # .mailmap is intentionally untracked: git reads it from the worktree
    write(os.path.join(path, ".mailmap"), "Ada Lovelace <ada@wits.ac.za> <ada@example.com>\n")
    return hashes


def total(rows, path, field):
    for row in rows:
        if row["path"] == path:
            return row[field]
    raise AssertionError(f"row {path!r} not found")


def main():
    tmp = tempfile.mkdtemp(prefix="rat-test-")
    repo = os.path.join(tmp, "repo")
    os.makedirs(repo)
    try:
        hashes = build_repo(repo)
        data = gitscan.parse_history(repo, "HEAD")

        print("== parse ==")
        check("merge commit excluded from H-bar", len(data["commits"]), 7)
        check("mailmap detected", data["mailmap"], True)
        scr = {c["s"]: c["f"] for c in data["commits"]}
        check("c1 entries", scr["c1"], [("a.txt", 3, 0), ("dir/b.txt", 2, 0)])
        check("c2 entries", scr["c2"], [("a.txt", 2, 1), ("dir/sub/c.txt", 4, 0)])
        check("c3 entries (rename->new path, delete)", scr["c3"],
              [("dir/b.txt", 0, 2), ("dir/sub/d.txt", 1, 0)])
        check("c4 pure rename produces no change", scr["c4"], [])
        check("c5 binary not measured", scr["c5"], [])
        check("c6 entries", scr["c6"], [("renamed_a.txt", 3, 0)])

        print("== repository (all non-merge commits) ==")
        res = metrics_mod.compute(data, {"mode": "all"}, {})
        t = res["totals"]
        check("commits", t["commits"], 7)
        check("added", t["added"], 17)
        check("removed", t["removed"], 4)
        check("growth", t["growth"], 13)
        check("churn", t["churn"], 21)
        check("root mods", t["mods"], 5)
        check("authors", t["authors"], 3)

        files = {r["path"]: r for r in res["files"]}
        check("a.txt added/removed", (files["a.txt"]["added"], files["a.txt"]["removed"]), (5, 1))
        check("a.txt churn/mods", (files["a.txt"]["churn"], files["a.txt"]["mods"]), (6, 2))
        check_close("a.txt modfreq", files["a.txt"]["modfreq"], 2 / 7)
        check_close("a.txt churnrate", files["a.txt"]["churnrate"], 6 / 7)
        check("renamed_a.txt churn/mods", (files["renamed_a.txt"]["churn"], files["renamed_a.txt"]["mods"]), (3, 1))
        check("dir/sub/c.txt", (files["dir/sub/c.txt"]["added"], files["dir/sub/c.txt"]["mods"]), (4, 1))
        check("dir/sub/d.txt", (files["dir/sub/d.txt"]["added"], files["dir/sub/d.txt"]["removed"]), (3, 1))
        check("dir/b.txt deleted file metrics", (files["dir/b.txt"]["added"], files["dir/b.txt"]["removed"], files["dir/b.txt"]["mods"]), (2, 2, 2))
        check("binary file absent", "bin.dat" in files, False)

        dirs = {r["path"]: r for r in res["dirs"]}
        check("dir aggregate", (dirs["dir"]["added"], dirs["dir"]["removed"], dirs["dir"]["churn"], dirs["dir"]["mods"]), (9, 3, 12, 4))
        check("dir/sub aggregate", (dirs["dir/sub"]["added"], dirs["dir/sub"]["removed"], dirs["dir/sub"]["churn"], dirs["dir/sub"]["mods"]), (7, 1, 8, 3))
        check("root dir = repository", (dirs[""]["added"], dirs[""]["removed"], dirs[""]["churn"], dirs[""]["mods"]), (17, 4, 21, 5))

        authors = {r["name"]: r for r in res["authors"]}
        check("mailmap identity applied", "Ada Lovelace" in authors and authors["Ada Lovelace"]["email"] == "ada@wits.ac.za", True)
        check("Ada commits", authors["Ada Lovelace"]["commits"], 3)
        check("Ada churn/mods", (authors["Ada Lovelace"]["churn"], authors["Ada Lovelace"]["mods"]), (12, 2))
        check("Bob churn/mods", (authors["Bob"]["churn"], authors["Bob"]["mods"]), (6, 2))
        check("Carol churn/mods", (authors["Carol"]["churn"], authors["Carol"]["mods"]), (3, 1))
        check_close("Ada ownership", authors["Ada Lovelace"]["ownership"], 12 / 21)

        print("== commit set: time range [T2,T4) ==")
        res2 = metrics_mod.compute(data, {"mode": "range", "from": T2, "to": T4}, {})
        check("range |H|", res2["commit_set"]["count"], 3)
        check("range added/removed/churn", (res2["totals"]["added"], res2["totals"]["removed"], res2["totals"]["churn"]), (9, 4, 13))
        check("range root mods", res2["totals"]["mods"], 3)
        f2 = {r["path"]: r for r in res2["files"]}
        check_close("range a.txt churnrate", f2["a.txt"]["churnrate"], 3 / 3)
        check_close("range a.txt modfreq", f2["a.txt"]["modfreq"], 1 / 3)

        print("== commit set: manual list [c1, c6] ==")
        res3 = metrics_mod.compute(data, {"mode": "list", "commits": [hashes["c1"], hashes["c6"]]}, {})
        check("list |H|", res3["commit_set"]["count"], 2)
        check("list added/churn", (res3["totals"]["added"], res3["totals"]["churn"]), (8, 8))
        f3 = {r["path"]: r for r in res3["files"]}
        check_close("list renamed_a modfreq", f3["renamed_a.txt"]["modfreq"], 1 / 2)

        print("== reference commit (c3) ==")
        res4 = metrics_mod.compute(
            gitscan.parse_history(repo, hashes["c3"]), {"mode": "all"}, {})
        check("c3 ref |H|", res4["commit_set"]["count"], 3)
        check("c3 ref added/removed", (res4["totals"]["added"], res4["totals"]["removed"]), (12, 3))

        print("== author view + ownership ==")
        ada_key = next(r["key"] for r in res["authors"] if r["name"] == "Ada Lovelace")
        res5 = metrics_mod.compute(data, {"mode": "all", "author": ada_key}, {})
        av = res5["author_view"]
        check("author view commits", av["totals"]["commits"], 3)
        check("author view churn", av["totals"]["churn"], 12)
        avf = {r["path"]: r for r in av["files"]}
        check("author ownership on dir/b.txt", avf["dir/b.txt"]["ownership"], 0.5)
        check("author churn on a.txt", avf["a.txt"]["churn"], 6)

        print("== object detail (dir/sub) ==")
        det = metrics_mod.object_detail(data, {"mode": "all"}, {}, "dir/sub", "dir")
        check("dir/sub totals", (det["totals"]["added"], det["totals"]["removed"], det["totals"]["mods"]), (7, 1, 3))
        owners = {r["name"]: r for r in det["owners"]}
        check_close("dir/sub Ada ownership", owners["Ada Lovelace"]["ownership"], 4 / 8)
        check_close("dir/sub Carol ownership", owners["Carol"]["ownership"], 3 / 8)
        check_close("dir/sub Bob ownership", owners["Bob"]["ownership"], 1 / 8)

        print("== manual author merge (Carol -> Bob) ==")
        carol_key = next(r["key"] for r in res["authors"] if r["name"] == "Carol")
        bob_key = next(r["key"] for r in res["authors"] if r["name"] == "Bob")
        res6 = metrics_mod.compute(data, {"mode": "all"}, {carol_key: bob_key})
        check("authors after merge", res6["totals"]["authors"], 2)
        merged = {r["name"]: r for r in res6["authors"]}
        check("merged Bob commits", merged["Bob"]["commits"], 4)
        check("merged Bob churn", merged["Bob"]["churn"], 9)

        print("== zip ingestion (store) ==")
        zip_base = os.path.join(tmp, "bundle")
        shutil.make_archive(zip_base, "zip", repo)
        with open(zip_base + ".zip", "rb") as fh:
            blob = fh.read()
        store = Store(os.path.join(tmp, "data"))
        job_id = store.upload_zip_async("bundle.zip", blob)
        for _ in range(600):
            job = store.get_job(job_id)
            if job["state"] in ("done", "error"):
                break
            time.sleep(0.1)
        check("zip job state", store.get_job(job_id)["state"], "done")
        repo_id = store.get_job(job_id)["repo_id"]
        res7 = store.metrics(repo_id, "HEAD", {"mode": "all", "from": None, "to": None, "commits": [], "author": None})
        check("zip metrics match", (res7["totals"]["added"], res7["totals"]["churn"], res7["totals"]["mods"]), (17, 21, 5))
        check("zip mailmap flag", res7["meta"]["mailmap"], True)

        failed = [r for r in RESULTS if not r[0]]
        print()
        print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
        return 1 if failed else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
