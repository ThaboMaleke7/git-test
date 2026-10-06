"""RAT - Repo Analysis Tool (COMS3011A practical test).

A dependency-free (Python standard library only) web dashboard that measures
git repository metrics for repositories, directories, files, commit sets and
authors, per the COMS3011A test brief.

Modules
-------
gitscan   : single-pass ``git log`` scanner + on-disk parse cache.
metrics   : metric computation over commit sets / objects / authors.
store     : repository registry, zip/clone ingestion, background jobs, merges.
server    : stdlib HTTP server exposing the JSON API and the web dashboard.
"""

__version__ = "1.0.0"
