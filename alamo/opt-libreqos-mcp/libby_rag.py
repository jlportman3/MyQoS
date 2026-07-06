#!/usr/bin/env python3
"""libby_rag.py — tiny retrieval-augmented-generation helper for local Libby.

Two roles in one file:

  * MODULE:  retrieve(query, k=4) -> [{"text","source","score"}]
             Loads /opt/libreqos-mcp/rag_index.json, embeds `query` via the
             LiteLLM proxy, cosine-ranks every stored chunk, returns the top k.
             Degrades to [] if the index is missing / unreadable — the web
             backend calls this and must never crash when RAG isn't built yet.

  * CLI:     python libby_rag.py build
             Fetches docs/v2.0/*.md from github.com/LibreQoE/LibreQoS, chunks
             them (heading-aware, ~1000-token target, breadcrumb-prefixed),
             embeds via text-embedding-3-large, and writes the index atomically.

Stdlib only (numpy used as an optional fast path if present).  LITELLM_KEY is
read from the environment, sent as a Bearer header, and never printed or stored.
"""
import os
import re
import sys
import json
import math
import tempfile
import subprocess
import urllib.request

LITELLM_URL   = os.environ.get("LITELLM_URL", "http://10.0.60.48:4000")
LITELLM_KEY   = os.environ.get("LITELLM_KEY", "")
EMBED_MODEL   = os.environ.get("LIBBY_EMBED_MODEL", "text-embedding-3-large")
INDEX_PATH    = os.environ.get("LIBBY_RAG_INDEX", "/opt/libreqos-mcp/rag_index.json")
REPO          = "LibreQoE/LibreQoS"
DOCS_SUBDIR   = "docs/v2.0"
BRANCH        = os.environ.get("LIBBY_DOCS_BRANCH", "main")
EXCLUDE       = {"diagram.drawio", "stp-diagram.drawio"}

CHARS_PER_TOK = 4          # crude token estimate
TARGET_TOK    = 1000
MAX_TOK       = 1200
BATCH         = 64

try:
    import numpy as _np      # optional fast path
except Exception:
    _np = None


# --------------------------------------------------------------------------- #
# embeddings
# --------------------------------------------------------------------------- #
def _embed(texts):
    """POST a batch of texts to the LiteLLM embeddings endpoint -> list[vector]."""
    if not LITELLM_KEY:
        raise SystemExit("LITELLM_KEY is not set in the environment.")
    payload = {"model": EMBED_MODEL, "input": texts}
    req = urllib.request.Request(
        LITELLM_URL + "/v1/embeddings",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + LITELLM_KEY,
        },
    )
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode())
            return [d["embedding"] for d in data["data"]]
        except Exception as e:      # transient proxy hiccup -> retry
            last = e
    raise RuntimeError("embeddings request failed: %s" % last)


def _cosine(q, m):
    if _np is not None:
        q = _np.asarray(q, dtype="float32")
        m = _np.asarray(m, dtype="float32")
        qn = q / (_np.linalg.norm(q) or 1.0)
        mn = m / (_np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
        return (mn @ qn).tolist()
    # pure-python fallback
    qn = math.sqrt(sum(x * x for x in q)) or 1.0
    out = []
    for row in m:
        dot = sum(a * b for a, b in zip(q, row))
        rn = math.sqrt(sum(x * x for x in row)) or 1.0
        out.append(dot / (qn * rn))
    return out


# --------------------------------------------------------------------------- #
# module API — retrieve
# --------------------------------------------------------------------------- #
_INDEX = None   # lazy-loaded cache


def _load_index():
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            _INDEX = json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        _INDEX = []
    if not isinstance(_INDEX, list):
        _INDEX = []
    return _INDEX


def retrieve(query, k=4):
    """Return the top-k doc chunks for `query` as [{text, source, score}]."""
    idx = _load_index()
    if not idx or not query:
        return []
    try:
        qvec = _embed([query])[0]
    except Exception as e:
        sys.stderr.write("[libby_rag] query embed failed: %s\n" % e)
        return []
    mat = [c.get("embedding") or [] for c in idx]
    scores = _cosine(qvec, mat)
    ranked = sorted(zip(scores, idx), key=lambda t: t[0], reverse=True)[:k]
    return [
        {
            "text": c.get("text", ""),
            "source": c.get("source_url") or c.get("source") or "unknown",
            "score": round(float(s), 4),
        }
        for s, c in ranked
    ]


# --------------------------------------------------------------------------- #
# CLI — build the index
# --------------------------------------------------------------------------- #
def _fetch_docs_git(dest):
    """Sparse-checkout docs/v2.0. Returns (files: {path: text}, commit_sha)."""
    subprocess.run(
        ["git", "clone", "--depth", "1", "--filter=blob:none",
         "--sparse", "--branch", BRANCH,
         "https://github.com/%s.git" % REPO, dest],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(["git", "-C", dest, "sparse-checkout", "set", DOCS_SUBDIR],
                   check=True, capture_output=True, text=True)
    sha = subprocess.run(["git", "-C", dest, "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True).stdout.strip()
    files = {}
    docroot = os.path.join(dest, DOCS_SUBDIR)
    for root, _dirs, names in os.walk(docroot):
        for n in names:
            if not n.endswith(".md") or n in EXCLUDE:
                continue
            full = os.path.join(root, n)
            rel = os.path.relpath(full, dest)
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                files[rel] = f.read()
    return files, sha


def _fetch_docs_api():
    """Fallback: GitHub tree API + raw fetch. Returns (files, commit_sha)."""
    def _get(url):
        req = urllib.request.Request(url, headers={"User-Agent": "libby-rag"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode()

    ref = json.loads(_get(
        "https://api.github.com/repos/%s/commits/%s" % (REPO, BRANCH)))
    sha = ref["sha"]
    tree = json.loads(_get(
        "https://api.github.com/repos/%s/git/trees/%s?recursive=1" % (REPO, sha)))
    files = {}
    for item in tree.get("tree", []):
        p = item.get("path", "")
        if item.get("type") != "blob":
            continue
        if not p.startswith(DOCS_SUBDIR + "/") or not p.endswith(".md"):
            continue
        if os.path.basename(p) in EXCLUDE:
            continue
        raw = "https://raw.githubusercontent.com/%s/%s/%s" % (REPO, sha, p)
        files[p] = _get(raw)
    return files, sha


def _chunk(path, text):
    """Heading-aware chunks with a breadcrumb prefix. Never splits code fences."""
    lines = text.splitlines()
    h1 = os.path.splitext(os.path.basename(path))[0]
    heads = {1: "", 2: "", 3: ""}
    sections = []          # (heading_path, [body lines])
    buf, cur_head = [], ""
    in_fence = False

    def flush():
        if buf:
            sections.append((cur_head, list(buf)))

    for ln in lines:
        if ln.strip().startswith("```"):
            in_fence = not in_fence
        m = re.match(r"^(#{1,3})\s+(.*)$", ln) if not in_fence else None
        if m:
            flush()
            buf = []
            lvl = len(m.group(1))
            heads[lvl] = m.group(2).strip()
            for deeper in range(lvl + 1, 4):
                heads[deeper] = ""
            if lvl == 1:
                h1 = heads[1]
            cur_head = " > ".join(x for x in (heads[1], heads[2], heads[3]) if x)
        else:
            buf.append(ln)
    flush()

    chunks = []
    for head_path, body in sections:
        crumb = "source: %s\n%s\n" % (path, head_path or h1)
        body_txt = "\n".join(body).strip()
        if not body_txt:
            continue
        budget = MAX_TOK * CHARS_PER_TOK
        if len(body_txt) <= budget:
            chunks.append(crumb + body_txt)
            continue
        # oversized: split on blank lines, packing to ~TARGET_TOK, keep fences whole
        paras, acc, fence = [], [], False
        for para in re.split(r"\n\s*\n", body_txt):
            paras.append(para)
        piece = ""
        for para in paras:
            if len(piece) + len(para) > TARGET_TOK * CHARS_PER_TOK and piece:
                chunks.append(crumb + piece.strip())
                piece = ""
            piece += para + "\n\n"
        if piece.strip():
            chunks.append(crumb + piece.strip())
    return chunks


def build():
    if not LITELLM_KEY:
        raise SystemExit("LITELLM_KEY is not set — cannot embed. Aborting.")
    tmpd = tempfile.mkdtemp(prefix="libby-docs-")
    try:
        try:
            files, sha = _fetch_docs_git(tmpd)
            method = "git-sparse"
        except Exception as e:
            sys.stderr.write("[libby_rag] git fetch failed (%s); using API\n" % e)
            files, sha = _fetch_docs_api()
            method = "github-api"
    finally:
        subprocess.run(["rm", "-rf", tmpd], check=False)

    if not files:
        raise SystemExit("No docs fetched — aborting (method=%s)." % method)
    sys.stderr.write("[libby_rag] %d doc files via %s @ %s\n"
                     % (len(files), method, sha[:8]))

    records = []
    for path in sorted(files):
        for ci, chunk in enumerate(_chunk(path, files[path])):
            src_url = "https://github.com/%s/blob/%s/%s" % (REPO, sha, path)
            records.append({"text": chunk, "source": path,
                            "source_url": src_url, "commit_sha": sha,
                            "chunk_index": ci})
    sys.stderr.write("[libby_rag] %d chunks; embedding...\n" % len(records))

    for i in range(0, len(records), BATCH):
        batch = records[i:i + BATCH]
        vecs = _embed([r["text"] for r in batch])
        for r, v in zip(batch, vecs):
            r["embedding"] = v
        sys.stderr.write("[libby_rag]   embedded %d/%d\n"
                         % (min(i + BATCH, len(records)), len(records)))

    d = os.path.dirname(INDEX_PATH) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(records, f)
    os.replace(tmp, INDEX_PATH)
    try:
        os.chmod(INDEX_PATH, 0o644)
    except OSError:
        pass
    sys.stderr.write("[libby_rag] wrote %s (%d chunks)\n"
                     % (INDEX_PATH, len(records)))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build()
    elif len(sys.argv) > 2 and sys.argv[1] == "query":
        for h in retrieve(sys.argv[2], k=4):
            print("%.4f  %s\n%s\n" % (h["score"], h["source"], h["text"][:300]))
    else:
        print("usage: libby_rag.py build | query <text>")
