# RAT — Repo Analysis Tool

RAT is a web dashboard that measures the evolution of git repositories. It ingests a
repository either as a **zip archive** (containing its `.git` directory) or by
**cloning a remote URL**, then computes file, directory, repository, commit-set and
author metrics over a filterable commit set, and visualises them in the browser.

Everything is built with the **Python standard library and vanilla JavaScript** —
no `pip install`, no `npm install`, no CDN resources.

---

## 1. Requirements

* Python **3.9 or newer** (uses only the standard library)
* `git` available on `PATH`
* A modern web browser (Chrome, Firefox, Edge, Safari)
* Internet access only when *cloning* a remote repository

## 2. How to run

```bash
cd git-test          # the folder containing run.py
python3 run.py       # starts the server on http://127.0.0.1:8000
```

Then open <http://127.0.0.1:8000> in a browser.

Optional flags:

```bash
python3 run.py --port 8765          # choose another port
python3 run.py --host 0.0.0.0       # listen on all interfaces
python3 run.py --data ./my-data     # where repositories/caches are stored (default: ./data)
```

The `--data` directory is created automatically. It contains the uploaded/cloned
repositories, the repository registry (`registry.json`) and parse caches. Deleting
it resets the tool.

> The first analysis of a repository parses its whole history once (a few seconds
> per ~10k commits); everything is cached afterwards, so later queries — including
> new filters — are near-instant (see §6).

## 3. Adding a repository

Click **＋ Add repository** in the top bar:

* **Clone remote URL** — a *deep* clone of any git URL
  (`https://…`, `git@…`, `ssh://…`). Progress is reported live.
* **Upload zip** — upload a `.zip` of a repository folder.
  The archive must contain the `.git` directory (the repository may be at the zip
  root or inside a single top-level folder). Zip-slip and symlink entries are
  rejected for safety.

Several repositories can be loaded at the same time; switch between them with the
**Repository** dropdown. Use *delete* in the *Repository info* panel to remove one.

## 4. Using the dashboard

### Reference commit (h\*)
The **Reference commit** field accepts a branch, tag or commit hash (with
autocomplete). All metrics are computed for the **non-merge commits reachable from
that reference** — this is the "specific commit hash" the metrics are measured at.
Default: `HEAD`.

### Commit set (H)
* **All** – every non-merge commit reachable from the reference.
* **Time range** – commits whose *committer date* satisfies
  `from ≤ date < to + 1s` (both ends inclusive, as displayed). Quick buttons
  (30d/90d/1y/5y) build a window ending at the newest commit in the set.
* **Selected commits** – a manual subset chosen from the commit picker in the
  sidebar, from the *Commit set* tab (tick rows, "＋ add all listed",
  "＋ add current range"), or any combination.

### Author filter and merging authors
Pick an author from the dropdown to scope every metric, chart and table to that
developer (`author view`), including per-file/per-directory **ownership**.
**⇄ Merge authors…** merges several raw identities (`Name <email>`) of one person
into a single author. A repository `.mailmap` is applied automatically by git;
manual merges are stored per repository and applied on top at query time.

### File / directory detail
Type a path (with autocomplete) and press **Show detail** — or click any row in the
*Files* / *Directories* tables — to open a drawer with that object's metrics,
per-author ownership breakdown and an activity timeline.

### Tables and charts
* Overview: KPI tiles, churn over time (bucket adapts from hour to month),
  repository size in net lines, top authors/files/directories by churn.
* Files / Directories / Authors: sortable (click a header), searchable tables with
  *Show more* paging and **CSV export** of the filtered rows.
* Commit set: the commit list with |H| and the commit-set totals.

### Theme
The **☀ Light / ☾ Dark** button in the top bar switches the whole dashboard
between a dark and a light theme. The choice is remembered in `localStorage`
and every chart re-colours instantly.

## 5. Metric definitions (as implemented)

Notation: `H` = commit set (subset of the non-merge commits reachable from the
reference), `f` = file, `d` = directory, `h` = a commit, `h[prev]` = its first parent
(the empty tree for an initial commit), `a` = author. `h+`, `h-` are the lines added
and removed by `h` (from `git log --numstat`).

| Metric | Definition |
| --- | --- |
| `h+[f]`, `h-[f]` | lines added / removed in `f` by `h` |
| `g_h[f]` | `h+[f] - h-[f]` (growth of `f` in `h`) |
| `c_h[f]` (churn) | `h+[f] + h-[f]` |
| Directory values | sum of the values of the files inside `d` (this is the "*sum over the immediate children*" definition, and it telescopes to the whole subtree) |
| Repository values | the root directory `""` |
| `H+[x]`, `H-[x]` | `Σ_{h∈H} h+[x]`, `Σ_{h∈H} h-[x]` (x = file or directory) |
| `G_H[x]` | `H+[x] - H-[x]` |
| `C_H[x]` | `H+[x] + H-[x]` |
| `M_H[x]` | number of commits in `H` that modified `x` (`c_h[x] > 0`) |
| modification frequency | `M_H[x] / |H|` |
| churn rate | `C_H[x] / |H|` |
| `M_{H,a}[x]` | `Σ_{h∈H} a(h) · m_h(x)` where `m_h(x)=1` iff `h` modified `x` |
| `C_{H,a}[x]` | `Σ_{h∈H} c_h[x] · a(h)` |
| ownership `O_{H,a}[x]` | `C_{H,a}[x] / C_H[x]` (0 when `C_H[x] = 0`) |

Rules applied:

* **Merge commits are never measured** — `H` only ever contains non-merge commits
  (the *Commit set* tab lists exactly this universe).
* **Renames** are detected at git's 50% similarity (`-M50%`). A rename+edit is
  attributed to the **new path** (with the changed lines only); a pure rename has
  no line changes and therefore does not alter any metric.
* **Deletions** are recorded as removed lines on the removed path.
* **Binary** files are not measured (git reports no line counts).
* **Commit dates** are committer dates.
* **Author identity** is `Name + email`; `.mailmap` is applied by git, manual
  merges are applied by the tool.

## 6. Architecture

```
run.py                 command-line entry point
rat/
  gitscan.py           one-pass `git log --no-merges -M50% --numstat -z` parser,
                       commit/ref/tree helpers, pickle cache, progress reporting
  metrics.py           metric engine: commit-set selection, aggregation of file /
                       directory / author tables + timelines, ownership
  store.py             repository registry, zip/clone ingestion with live job
                       progress, caches (parsed history LRU + per-sha pickle files
                       + small metrics-result LRU), manual author merges
  server.py            HTTP API on http.server.ThreadingHTTPServer
static/
  index.html           dashboard shell
  css/styles.css       dark theme (responsive)
  js/charts.js         hand-rolled SVG charts (stacked bars, area, mini bars, donut)
  js/app.js            client logic: filters, tables, drawer, modals, job polling
tests/test_metrics.py  correctness tests with hand-computed expectations
```

Design notes:

* **One pass over history.** The whole history is read with a single `git log`
  invocation (NUL-delimited `--numstat -z` output parsed with a streaming state
  machine), independent of how many filters are used. All filtering happens in
  memory, so changing the commit set, author or path is instant after the first
  scan.
* **Caching.** Parsed history is cached per resolved commit hash — in memory (LRU)
  and on disk as a pickle — so revisiting a repository or reference costs nothing.
  Metrics results are memoised per (repo, ref, filters, merges).
* **Manual merges are query-time**, which keeps them cheap to add/remove and
  avoids re-scanning history.
* **API** (JSON): `/api/repos`, `/api/repos/upload`, `/api/repos/clone`,
  `/api/jobs/<id>`, `/api/repos/<id>/refs|commits|authors|metrics|object|stats`,
  `/api/repos/<id>/merges`, `DELETE /api/repos/<id>`.
* **Robustness.** Friendly errors for bad references, non-repository zips,
  zip archives that only contain a `.git` *file* (worktrees), missing git
  binaries, and clones of unreachable URLs; jobs (upload/clone/scan) run in
  worker threads so the UI stays responsive.

### Measured performance

On a **Redis** clone (11,875 non-merge commits, 2,858 files, 1,035 authors):

| Operation | Time |
| --- | --- |
| first scan (whole history, rename detection, cache write) | ≈ 9.1 s |
| …of which raw `git log --numstat` alone (measured separately) | ≈ 8.6 s |
| cached query (metrics + JSON round-trip) | ≈ 16 ms |
| changing filters / commit set after the scan | < 100 ms |

The scan is bound by git's own diff work, not by the tool: the parser, the metric
aggregation and the pickle cache add < 0.6 s on top of the single `git log` call
that git would perform anyway.

## 7. Tests

```bash
python3 tests/test_metrics.py
```

Builds a synthetic repository (initial commit, edits, a deletion, a binary file,
a pure rename, a rename+edit, a side branch and a merge, plus a `.mailmap`) and
checks 57 hand-computed metric results: per-commit parsing, repository/directory
aggregation, modification frequency, churn rate, ownership, time-range and manual
commit sets, a non-HEAD reference commit, author views, manual author merging and
zip ingestion end-to-end.

### Validation against the provided reference data

The three repositories distributed in `repo-references.zip` (cJSON, git, redis)
were used to validate the engine: each repository was queried at its exact
reference commit and compared row-by-row with the reference CSV.

| Repository | Reference commit | Commits | Authors | Files | Directories |
| --- | --- | --- | --- | --- | --- |
| cJSON | `6d9f2443ab07` | 955 | 107 / 107 | 240 / 240 | 45 / 45 |
| git | `5a7d1e8045ce` | 61,101 | 2,498 / 2,498 | 6,410 / 6,410 | 286 / 286 |
| redis | `b540ca49cba8` | 11,874 | 1,034 / 1,034 | 2,856 / 2,856 | 202 / 202 |

Every non-zero value matches the reference exactly — repository totals, per-author
totals (including `ownership`), the file and directory tables (including
`modification_frequency` and `churn_rate`), and per-file author ownership. The only
differences are all-zero placeholder rows: the reference keeps a row for every path
whose only appearance in `git --numstat` is a pure rename (`0` added / `0` removed),
and for commits that changed no lines. Those rows carry no metric values, consistent
with §5 (“a pure rename … does not alter any metric”).

## 8. Known limitations

* Private repositories need credentials configured for `git` on the machine
  (the tool never prompts interactively and never stores credentials).
* Zip archives must contain the `.git` directory; worktree checkouts that use a
  `.git` *file* are rejected with an explanatory message.
* Line metrics are exactly what `git --numstat` reports; binary files and pure
  renames have no line counts, as per the specification.

## 9. AI Declaration

This project was done with the help of Qoder, an artificial intelligence model with the guidance of a human.