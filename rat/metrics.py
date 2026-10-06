"""Metric computation over parsed git history.

Implements the COMS3011A metric definitions.

Commit set
    ``H`` is any subset of ``H-bar`` (non-merge commits reachable from the
    reference commit).  The dashboard builds ``H`` by combining filters:
    an optional time window ``t1 <= committer-date < t2``, an optional manual
    commit list, and (as a separate view) an optional author.

Object universe
    Files are the paths touched by commits in ``H`` (renames attributed to the
    new path, deletions recorded on the removed path).  Directories are every
    ancestor directory of those files, including the repository root ("").

Per-commit file metrics (f = file, h = commit, h[prev] = previous commit)
    h+[f] added lines, h-[f] removed lines, g_h[f] = h+ - h-,
    c_h[f] = h+ + h-.

Per-commit directory metrics (d = directory)
    Sum of the file values over every file inside ``d`` (the brief's
    immediate-child definition telescopes to the whole subtree).

Commit set metrics (x = file or directory)
    H+[x]      = sum over h in H of h+[x]
    H-[x]      = sum over h in H of h-[x]
    G_H[x]     = H+[x] - H-[x]
    C_H[x]     = H+[x] + H-[x]
    M_H[x]     = number of commits in H with c_h[x] > 0
    modfreq    = M_H[x] / |H|          (0 when |H| = 0)
    churnrate  = C_H[x] / |H|          (0 when |H| = 0)

Author metrics (a = author, a(h) = 1 iff h[author] = a)
    M_{H,a}[x] = sum over h in H of a(h) * m_h(x)
    C_{H,a}[x] = sum over h in H of c_h[x] * a(h)
    O_{H,a}[x] = C_{H,a}[x] / C_H[x]   (0 when C_H[x] = 0)
"""

from __future__ import annotations

import datetime as _dt

AUTHOR_SEP = "\x1f"


# --------------------------------------------------------------------------
# Author identity helpers
# --------------------------------------------------------------------------

def raw_author_key(commit: dict) -> str:
    return commit["n"] + AUTHOR_SEP + commit["e"]


def resolve_key(raw: str, merges: dict) -> str:
    """Follow a manual merge chain (guarding against cycles)."""
    key = raw
    seen = set()
    while key in merges and key not in seen:
        seen.add(key)
        key = merges[key]
    return key


def split_key(key: str):
    name, _, email = key.partition(AUTHOR_SEP)
    return name, email


def pack_author(key: str, **fields) -> dict:
    name, email = split_key(key)
    row = {"key": key, "name": name, "email": email}
    row.update(fields)
    return row


def author_index(data: dict, merges: dict) -> dict:
    """The identity universe of a parsed ref.

    ``canonical`` applies manual merges (used for dropdowns/filters), while
    ``identities`` lists raw name/email pairs (used to create merges).
    """
    ident = {}
    canon = {}
    for c in data["commits"]:
        raw = raw_author_key(c)
        e = ident.get(raw)
        if e is None:
            e = ident[raw] = {"commits": 0, "first": c["t"], "last": c["t"]}
        e["commits"] += 1
        e["first"] = min(e["first"], c["t"])
        e["last"] = max(e["last"], c["t"])

        key = resolve_key(raw, merges)
        e = canon.get(key)
        if e is None:
            e = canon[key] = {"commits": 0, "first": c["t"], "last": c["t"]}
        e["commits"] += 1
        e["first"] = min(e["first"], c["t"])
        e["last"] = max(e["last"], c["t"])

    def pack(rows):
        return [
            pack_author(k, **v)
            for k, v in sorted(rows.items(), key=lambda kv: (-kv[1]["commits"], kv[0]))
        ]

    return {"canonical": pack(canon), "identities": pack(ident), "merges": dict(merges)}


# --------------------------------------------------------------------------
# Path helpers
# --------------------------------------------------------------------------

def _ancestors(path: str) -> tuple:
    """Ancestor directories of *path*, deepest first, root "" last."""
    parts = path.split("/")
    dirs = ["/".join(parts[: i + 1]) for i in range(len(parts) - 1)]
    dirs.reverse()
    dirs.append("")
    return tuple(dirs)


def ancestors_of(data: dict, path: str) -> tuple:
    cache = data.get("_anc")
    if cache is None:
        cache = data["_anc"] = {}
    got = cache.get(path)
    if got is None:
        got = cache[path] = _ancestors(path)
    return got


# --------------------------------------------------------------------------
# Commit set selection
# --------------------------------------------------------------------------

def select_commit_set(data: dict, mode: str, from_ts=None, to_ts=None, hashes=None):
    """Return H as a list of commit indexes (newest first)."""
    out = []
    wanted = set(hashes or []) if mode == "list" else None
    for i, c in enumerate(data["commits"]):
        if mode == "range":
            t = c["t"]
            if from_ts is not None and t < from_ts:
                continue
            if to_ts is not None and t >= to_ts:
                continue
        elif mode == "list":
            if c["h"] not in wanted:
                continue
        out.append(i)
    return out


# --------------------------------------------------------------------------
# Timeline bucketing
# --------------------------------------------------------------------------

def pick_bucket(span_seconds: float) -> str:
    if span_seconds <= 2 * 86400:
        return "hour"
    if span_seconds <= 120 * 86400:
        return "day"
    if span_seconds <= 3 * 365 * 86400:
        return "week"
    return "month"


def _bucket_key(ts: int, bucket: str):
    if bucket == "hour":
        return ts - ts % 3600
    if bucket == "day":
        return ts - ts % 86400
    if bucket == "week":
        return ts - ts % 604800
    d = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
    return int(_dt.datetime(d.year, d.month, 1, tzinfo=_dt.timezone.utc).timestamp())


def _pack_timeline(src: dict, bucket: str) -> dict:
    return {
        "bucket": bucket,
        "points": [
            {"t": ts, "added": a, "removed": r, "commits": c}
            for ts, (a, r, c) in sorted(src.items())
        ],
    }


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def _canonical_author_cache(data: dict, merges: dict):
    """Per-commit canonical author keys (cached until merges change)."""
    sig = tuple(sorted(merges.items()))
    cached = data.get("_ak_cache")
    if cached and cached[0] == sig:
        return cached[1]
    lookup = {}
    keys = []
    for c in data["commits"]:
        raw = raw_author_key(c)
        key = lookup.get(raw)
        if key is None:
            key = lookup[raw] = resolve_key(raw, merges)
        keys.append(key)
    data["_ak_cache"] = (sig, keys)
    return keys


def _aggregate(data: dict, indexes, all_keys, author_filter, bucket):
    """One pass over the commit set, accumulating every metric table.

    Returns (files, dirs, authors, timeline, n_commits_with_activity).
    """
    files = {}    # path -> [added, removed, mods]
    dirs = {}     # dir  -> [added, removed, mods]
    authors = {}  # akey -> [commits, added, removed, churn, mods]
    tl = {}
    commits = data["commits"]

    for i in indexes:
        c = commits[i]
        akey = all_keys[i]
        if author_filter is not None and akey != author_filter:
            continue
        ts = c["t"]
        bk = _bucket_key(ts, bucket)
        trow = tl.get(bk)
        if trow is None:
            trow = tl[bk] = [0, 0, 0]
        trow[2] += 1

        arow = authors.get(akey)
        if arow is None:
            arow = authors[akey] = [0, 0, 0, 0, 0]
        arow[0] += 1

        ddelta = {}
        for path, add, rem in c["f"]:
            churn = add + rem
            trow[0] += add
            trow[1] += rem

            frow = files.get(path)
            if frow is None:
                files[path] = [add, rem, 1]
            else:
                frow[0] += add
                frow[1] += rem
                frow[2] += 1

            for d in ancestors_of(data, path):
                drow = ddelta.get(d)
                if drow is None:
                    ddelta[d] = [add, rem]
                else:
                    drow[0] += add
                    drow[1] += rem

            arow[1] += add
            arow[2] += rem
            arow[3] += churn

        for d, (add, rem) in ddelta.items():
            drow = dirs.get(d)
            if drow is None:
                dirs[d] = [add, rem, 1]
            else:
                drow[0] += add
                drow[1] += rem
                drow[2] += 1
        if c["f"]:
            arow[4] += 1

    return files, dirs, authors, tl


def _pack_objects(src: dict, n_commits: int, denom: dict | None = None) -> list:
    rows = []
    for path, (add, rem, mods) in src.items():
        row = {
            "path": path,
            "added": add,
            "removed": rem,
            "growth": add - rem,
            "churn": add + rem,
            "mods": mods,
            "modfreq": (mods / n_commits) if n_commits else 0.0,
            "churnrate": ((add + rem) / n_commits) if n_commits else 0.0,
        }
        if denom is not None:
            base = denom.get(path)
            base_churn = (base[0] + base[1]) if base else 0
            row["ownership"] = ((add + rem) / base_churn) if base_churn else 0.0
        rows.append(row)
    return rows


def compute(data: dict, filters: dict, merges: dict | None = None) -> dict:
    """Aggregate the filtered commit set into dashboard-ready tables."""
    merges = merges or {}
    mode = filters.get("mode") or "all"
    author_filter = filters.get("author") or None
    from_ts = filters.get("from")
    to_ts = filters.get("to")
    hashes = filters.get("commits") or []

    all_keys = _canonical_author_cache(data, merges)
    H = select_commit_set(data, mode, from_ts, to_ts, hashes)
    n_commits = len(H)

    commits = data["commits"]
    ts_of = [commits[i]["t"] for i in H]
    lo_ts = min(ts_of, default=None)
    hi_ts = max(ts_of, default=None)
    span = (hi_ts - lo_ts) if (lo_ts is not None and hi_ts is not None) else 0
    bucket = pick_bucket(span) if H else "day"

    files, dirs, authors, tl = _aggregate(data, H, all_keys, None, bucket)

    total_added = sum(v[0] for v in files.values())
    total_removed = sum(v[1] for v in files.values())
    total_churn = total_added + total_removed

    author_rows = []
    for akey, (n, add, rem, churn, mods) in authors.items():
        author_rows.append(pack_author(
            akey, commits=n, added=add, removed=rem, growth=add - rem,
            churn=churn, mods=mods,
            ownership=(churn / total_churn) if total_churn else 0.0,
        ))

    result = {
        "commit_set": {
            "count": n_commits,
            "from": lo_ts,
            "to": hi_ts,
            "contributors": len(authors),
        },
        "totals": {
            "commits": n_commits,
            "authors": len(authors),
            "files": len(files),
            "dirs": len(dirs),
            "added": total_added,
            "removed": total_removed,
            "growth": total_added - total_removed,
            "churn": total_churn,
            "mods": dirs.get("", [0, 0, 0])[2],
        },
        "timeline": _pack_timeline(tl, bucket),
        "files": _pack_objects(files, n_commits),
        "dirs": _pack_objects(dirs, n_commits),
        "authors": author_rows,
        "author_view": None,
    }

    if author_filter is not None:
        a_files, a_dirs, a_authors, a_tl = _aggregate(data, H, all_keys, author_filter, bucket)
        a_n = a_authors.get(author_filter, [0, 0, 0, 0, 0])
        result["author_view"] = {
            "author": pack_author(author_filter),
            "totals": {
                "commits": a_n[0],
                "files": len(a_files),
                "dirs": len(a_dirs),
                "added": a_n[1],
                "removed": a_n[2],
                "growth": a_n[1] - a_n[2],
                "churn": a_n[3],
                "mods": a_n[4],
            },
            "timeline": _pack_timeline(a_tl, bucket),
            "files": _pack_objects(a_files, n_commits, denom=files),
            "dirs": _pack_objects(a_dirs, n_commits, denom=dirs),
        }

    return result


def object_detail(data: dict, filters: dict, merges: dict, path: str, kind: str = None) -> dict:
    """Per-author ownership + activity timeline for one file or directory."""
    merges = merges or {}
    all_keys = _canonical_author_cache(data, merges)
    H = select_commit_set(
        data,
        filters.get("mode") or "all",
        filters.get("from"),
        filters.get("to"),
        filters.get("commits") or [],
    )
    author_filter = filters.get("author") or None

    if kind not in ("file", "dir"):
        kind = "dir"

    commits = data["commits"]
    ts_of = [commits[i]["t"] for i in H]
    span = (max(ts_of) - min(ts_of)) if ts_of else 0
    bucket = pick_bucket(span) if H else "day"

    prefix = (path + "/") if (kind == "dir" and path) else None

    def in_scope(p: str) -> bool:
        if kind == "file":
            return p == path
        if path == "":
            return True
        return p == path or (prefix is not None and p.startswith(prefix))

    total = [0, 0, 0]  # added, removed, mods
    owners = {}
    tl = {}
    n_commits = 0

    for i in H:
        c = commits[i]
        akey = all_keys[i]
        if author_filter is not None and akey != author_filter:
            continue
        n_commits += 1

        add_s = rem_s = 0
        touched = False
        for p, add, rem in c["f"]:
            if not in_scope(p):
                continue
            touched = True
            add_s += add
            rem_s += rem
        if not touched:
            continue

        total[0] += add_s
        total[1] += rem_s
        total[2] += 1
        o = owners.get(akey)
        if o is None:
            owners[akey] = [add_s + rem_s, 1]
        else:
            o[0] += add_s + rem_s
            o[1] += 1

        r = tl.get(_bucket_key(c["t"], bucket))
        if r is None:
            r = tl[_bucket_key(c["t"], bucket)] = [0, 0, 0]
        r[0] += add_s
        r[1] += rem_s
        r[2] += 1

    churn_total = total[0] + total[1]
    rows = [
        pack_author(akey, churn=churn, mods=mods,
                    ownership=(churn / churn_total) if churn_total else 0.0)
        for akey, (churn, mods) in owners.items()
    ]
    rows.sort(key=lambda r: -r["churn"])

    return {
        "path": path,
        "kind": kind,
        "scope_commits": n_commits,
        "totals": {
            "added": total[0],
            "removed": total[1],
            "growth": total[0] - total[1],
            "churn": churn_total,
            "mods": total[2],
            "modfreq": (total[2] / n_commits) if n_commits else 0.0,
            "churnrate": (churn_total / n_commits) if n_commits else 0.0,
        },
        "owners": rows,
        "timeline": _pack_timeline(tl, bucket),
    }
