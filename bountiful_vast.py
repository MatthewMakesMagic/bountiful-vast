#!/usr/bin/env python3
"""
Bountiful on VAST: every shop, searchable.

Film a shop once; this turns the walkthrough into a searchable catalogue on the event stack:

  cut (ffmpeg, 5s @ 5fps) -> Cosmos Reason (what's for sale, per 5s) -> W&B Inference (normalise +
  dedupe into catalogue entries) -> Cosmos Embed1 (256-d vectors for every 5s moment and every item)
  -> VastDB (schema `bountiful`: tables `items`, `moments`, `runs`) -> search / web page

Runs ON the workshop VM (reads /config/<team>.config; credentials are never printed or saved).

    python3 bountiful_vast.py ingest newest --shop "Kerobokan furniture street" --location "Bali"
    python3 bountiful_vast.py ingest ~/clip.mp4 --shop "Ban Tawai carvers" --location "Chiang Mai"
    python3 bountiful_vast.py ingest s3://team-28-vss-chunks/team-28/2026..._abc.mp4 --shop "..."
    python3 bountiful_vast.py search "black leather armchair" --k 8
    python3 bountiful_vast.py serve --port 8766        # then open http://127.0.0.1:8766 in the VM browser

`newest` = the most recent upload in the team's chunks bucket (what the VSS Upload button writes).
Needs ffmpeg; boto3 for s3 inputs; vastdb + pyarrow for the database (JSON fallback otherwise).
"""
import argparse
import base64
import glob
import json
import math
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GPU_DEFAULT_HOST = "166.19.38.112"
WANDB_BASE = "https://api.inference.wandb.ai/v1"
EMBED_DIM = 256
SCHEMA = "bountiful"
_INSECURE = ssl._create_unverified_context()  # internal hackathon hosts may use self-signed certs

CATEGORIES = ["Seating", "Tables", "Storage", "Beds", "Mirrors & wall decor", "Lighting", "Carvings & art",
              "Textiles", "Baskets & small decor", "Tableware", "Plants & pots", "Other"]

COSMOS_PROMPT = (
    "This 5-second clip is from a walkthrough of a shop{where}. List the distinct items FOR SALE that are "
    "clearly visible. Rules: at most 10 items; each distinct product appears ONCE (never repeat an entry; "
    "use count for multiples); ignore people, vehicles, walls, floor and shop fixtures. Note visible details "
    "that a buyer cares about: frame or finish, cushions or upholstery, carving, weave, legs, shape. "
    'Reply with ONLY a JSON array, no prose: [{"item": "...", "material": "...", "colour": "...", '
    '"details": "...", "count": 1}]'
)


# ---------------------------------------------------------------- config + http (same patterns as ycount.py)

def load_config(path=None, required=True):
    if not path:
        found = sorted(glob.glob("/config/*.config"))
        if len(found) > 1:
            sys.exit(f"found several team configs, pick one with --config: {found}")
        path = found[0] if found else None
    cfg = {}
    if path:
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():  # env fills gaps (WANDB_* are often only in the environment)
        if k not in cfg and (k.startswith(("WANDB_", "COSMOS", "GPU_", "S3_", "VASTDB_")) or
                             k in ("INGRESS_URL", "USERNAME", "PASSWORD", "ACCESS_KEY", "SECRET_KEY")):
            cfg[k] = v
    cfg.setdefault("COSMOS3_REASON_URL", f"http://{GPU_DEFAULT_HOST}:8001")
    cfg.setdefault("COSMOS_EMBED1_URL", f"http://{GPU_DEFAULT_HOST}:8003")
    for k in ("COSMOS3_REASON_URL", "COSMOS_EMBED1_URL", "INGRESS_URL", "S3_ENDPOINT"):
        if cfg.get(k):
            cfg[k] = cfg[k].rstrip("/")
    if required and not cfg.get("GPU_BEARER_TOKEN"):
        sys.exit("no GPU_BEARER_TOKEN: run this on the workshop VM, or pass --config <team>.config")
    return cfg


class UpstreamError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status


def http(method, url, body=None, headers=None, timeout=300):
    headers = dict(headers or {})
    headers.setdefault("User-Agent", "bountiful-vast/1.0")  # Cloudflare (W&B) blocks urllib's default UA
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_INSECURE if url.startswith("https") else None) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise UpstreamError(e.code, e.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        raise UpstreamError(0, f"cannot reach {url.split('?')[0]}: {e.reason}")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:48] or "shop"


# ---------------------------------------------------------------- parsing + dedupe (pure, unit-tested)

def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _to_int(v, default=1):
    try:
        return max(1, int(float(v)))
    except (TypeError, ValueError):
        return default


def _extract_json_array(text):
    """Best-effort JSON array from model output: strips fences, repairs a truncated tail."""
    t = re.sub(r"```(?:json)?", "", text or "").strip()
    i = t.find("[")
    if i < 0:
        return None
    t = t[i:]
    j = t.rfind("]")
    if j >= 0:
        try:
            return json.loads(t[:j + 1])
        except ValueError:
            pass
    k = t.rfind("}")  # truncated mid-array (max_tokens hit during a repetition loop)
    while k > 0:
        try:
            return json.loads(t[:k + 1] + "]")
        except ValueError:
            k = t.rfind("}", 0, k)
    return None


def _parse_prose(text):
    """Fallback for 'Wooden chair, wood, brown, 1. Cane mirror, wood, natural, 2' style output."""
    out = []
    splitter = r"[.;]\s+(?=[A-Z])|(?<=\d)\s+(?=[A-Z])|\n+"
    for chunk in re.split(splitter, text or ""):
        parts = [p.strip(" .") for p in chunk.split(",") if p.strip(" .")]
        if not parts:
            continue
        count = 1
        if parts and re.fullmatch(r"\d+|one|two|three|four|five|few|many|several", parts[-1], re.I):
            last = parts.pop().lower()
            words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
            count = int(last) if last.isdigit() else words.get(last, 3)
        if not parts:
            continue
        out.append({"item": parts[0], "material": parts[1] if len(parts) > 1 else "",
                    "colour": parts[2] if len(parts) > 2 else "", "details": ", ".join(parts[3:]), "count": count})
    return out


def parse_cosmos_items(text, cap=10):
    """Model text -> clean, deduped item list. Repeats of the same (item, material, colour) collapse into
    one entry (count = max seen, not sum: a repetition loop is a glitch, not inventory)."""
    raw = _extract_json_array(text)
    if not isinstance(raw, list):
        raw = _parse_prose(text)
    seen, order, repeats = {}, [], 0
    for r in raw:
        if isinstance(r, str):
            r = {"item": r}
        if not isinstance(r, dict) or not _norm(r.get("item") or r.get("name")):
            continue
        item = {"item": str(r.get("item") or r.get("name")).strip(), "material": str(r.get("material") or "").strip(),
                "colour": str(r.get("colour") or r.get("color") or "").strip(),
                "details": str(r.get("details") or "").strip(), "count": _to_int(r.get("count"))}
        key = (_norm(item["item"]), _norm(item["material"]), _norm(item["colour"]))
        if key in seen:
            repeats += 1
            prev = seen[key]
            prev["count"] = max(prev["count"], item["count"])
            if len(item["details"]) > len(prev["details"]):
                prev["details"] = item["details"]
            continue
        seen[key] = item
        order.append(key)
    items = [seen[k] for k in order][:cap]
    return items, {"raw_entries": len(raw), "kept": len(items), "repeats_dropped": repeats,
                   "looped": repeats >= 3}


def local_merge(catalogue, sightings, shop_slug):
    """Fallback when W&B is unavailable: merge sightings into catalogue by (item, material, colour)."""
    by_key = {(_norm(c["name"]), _norm(c["material"]), _norm(c["colour"])): c for c in catalogue}
    for s in sightings:
        key = (_norm(s["item"]), _norm(s["material"]), _norm(s["colour"]))
        if key in by_key:
            c = by_key[key]
            c["pieces"] = sorted(set(c["pieces"]) | {s["piece"]})
            continue
        c = {"id": f"{shop_slug}-{len(by_key) + 1:03d}", "name": s["item"].capitalize(), "category": "Other",
             "material": s["material"], "colour": s["colour"],
             "description": (s["details"] or f"{s['item']} in {s['material']}").strip().capitalize(),
             "est_price_band": "", "pieces": [s["piece"]]}
        by_key[key] = c
    return list(by_key.values())


def apply_wandb_merge(catalogue, returned, shop_slug):
    """Take the LLM's full updated catalogue; keep stable ids, assign new ones, sanity-check fields."""
    existing = {c["id"]: c for c in catalogue}
    out, used = [], set()
    n = len(catalogue)
    for r in returned if isinstance(returned, list) else []:
        if not isinstance(r, dict) or not str(r.get("name") or "").strip():
            continue
        cid = str(r.get("id") or "")
        if cid not in existing or cid in used:
            n += 1
            cid = f"{shop_slug}-{n:03d}"
        used.add(cid)
        pieces = sorted({int(p) for p in (r.get("pieces") or []) if str(p).lstrip("-").isdigit()} |
                        set(existing.get(cid, {}).get("pieces", [])))
        cat = r.get("category") if r.get("category") in CATEGORIES else "Other"
        out.append({"id": cid, "name": str(r["name"]).strip(), "category": cat,
                    "material": str(r.get("material") or "").strip(), "colour": str(r.get("colour") or "").strip(),
                    "description": str(r.get("description") or "").strip(),
                    "est_price_band": str(r.get("est_price_band") or "").strip().lower(), "pieces": pieces})
    for cid, c in existing.items():  # never silently lose an item the model forgot to echo back...
        if cid in used:
            continue
        renamed = any(_norm(o["material"]) == _norm(c["material"]) and _norm(o["colour"]) == _norm(c["colour"])
                      and set(o["pieces"]) & set(c["pieces"]) for o in out)
        if not renamed:  # ...unless it came back under a new name (same material, colour, pieces)
            out.append(c)
    return out


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else -1.0


# ---------------------------------------------------------------- models

class Models:
    def __init__(self, cfg, wandb_model=None):
        self.cfg = cfg
        self.gpu = {"Authorization": f"Bearer {cfg['GPU_BEARER_TOKEN']}"} if cfg.get("GPU_BEARER_TOKEN") else {}
        self._cosmos_model = cfg.get("COSMOS3_REASON_MODEL")
        self._embed_model = cfg.get("COSMOS_EMBED1_MODEL") or "nvidia/cosmos-embed1"
        self._wandb_model = wandb_model
        self._penalty_ok = True

    def cosmos_model(self):
        if not self._cosmos_model:
            try:
                self._cosmos_model = http("GET", f"{self.cfg['COSMOS3_REASON_URL']}/v1/models",
                                          headers=self.gpu, timeout=20)["data"][0]["id"]
            except Exception:
                self._cosmos_model = "nvidia/cosmos3-reason"
        return self._cosmos_model

    def cosmos(self, video, prompt, max_tokens=500):
        body = {"model": self.cosmos_model(), "max_tokens": max_tokens, "temperature": 0.1,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," +
                                                        base64.b64encode(video).decode()}}]}]}
        if self._penalty_ok:
            body["frequency_penalty"] = 0.4  # damps the "Wooden chair, wood, brown, 1" x20 loop
        try:
            r = http("POST", f"{self.cfg['COSMOS3_REASON_URL']}/v1/chat/completions", body, self.gpu, timeout=300)
        except UpstreamError as e:
            if e.status != 400 or not self._penalty_ok:
                raise
            self._penalty_ok = False  # server rejected the param; retry plain
            body.pop("frequency_penalty", None)
            r = http("POST", f"{self.cfg['COSMOS3_REASON_URL']}/v1/chat/completions", body, self.gpu, timeout=300)
        return ((r.get("choices") or [{}])[0].get("message") or {}).get("content", "")

    def _embed(self, payload, timeout=180):
        payload = {"model": self._embed_model, "encoding_format": "float", **payload}
        r = http("POST", f"{self.cfg['COSMOS_EMBED1_URL']}/v1/embeddings", payload, self.gpu, timeout=timeout)
        vecs = [d.get("embedding") for d in r.get("data", []) if d.get("embedding")]
        if not vecs:
            raise RuntimeError("Cosmos-Embed1 returned no embeddings")
        return vecs

    def embed_video(self, video):  # mirrors vss-blueprint CosmosEmbed1Client.embed_video_mp4
        return self._embed({"input": "data:video/mp4;base64," + base64.b64encode(video).decode("ascii"),
                            "request_type": "query"})[0]

    def embed_query(self, text):  # mirrors embed_texts(for_query=True) for a single text
        return self._embed({"input": text, "request_type": "query"}, timeout=60)[0]

    def embed_texts(self, texts):  # mirrors embed_texts(): bulk_text for documents
        out = []
        for i in range(0, len(texts), 16):
            batch = texts[i:i + 16]
            out += self._embed({"input": batch, "request_type": "bulk_text"} if len(batch) > 1
                               else {"input": batch[0], "request_type": "query"}, timeout=120)
        return out

    def wandb_headers(self):
        h = {"Authorization": f"Bearer {self.cfg.get('WANDB_API_KEY', '')}"}
        if self.cfg.get("WANDB_TEAM") and self.cfg.get("WANDB_PROJECT"):
            h["OpenAI-Project"] = f"{self.cfg['WANDB_TEAM']}/{self.cfg['WANDB_PROJECT']}"
        return h

    def wandb_model(self):
        if not self._wandb_model:
            ids = sorted(m["id"] for m in http("GET", f"{WANDB_BASE}/models", headers=self.wandb_headers(),
                                               timeout=20).get("data", []))
            prefs = [r"llama-3\.3-70b", r"qwen3.*(235|32)b", r"gpt-oss-120b", r"70b", r"qwen", r"llama"]
            self._wandb_model = next((m for p in prefs for m in ids if re.search(p, m, re.I)), ids[0] if ids else "")
        return self._wandb_model

    def wandb_merge(self, shop, location, catalogue, sightings):
        prompt = (
            f"Shop: {shop}{' (' + location + ')' if location else ''}.\n"
            f"EXISTING CATALOGUE (JSON):\n{json.dumps(catalogue)}\n\n"
            f"NEW SIGHTINGS from 5-second video pieces (JSON; 'piece' = index of the 5s piece):\n"
            f"{json.dumps(sightings)}\n\n"
            "Return the FULL updated catalogue as JSON: {\"items\": [...]}. Merge sightings that are the same "
            "product (same type + material + colour) into one entry and union their 'pieces'. Keep the id of an "
            "existing entry when merging into it; use \"new\" for new entries. Each entry: {\"id\", \"name\" "
            "(short shop-catalogue name, e.g. 'Teak and rope lounge chair'), \"category\" (one of "
            f"{json.dumps(CATEGORIES)}), \"material\", \"colour\", \"description\" (one factual sentence from the "
            "sightings' details), \"est_price_band\" (budget|mid|premium, your estimate), \"pieces\" (ints)}. "
            "Never invent items that are not in the sightings or catalogue. Output JSON only."
        )
        r = http("POST", f"{WANDB_BASE}/chat/completions", {
            "model": self.wandb_model(), "temperature": 0.1, "max_tokens": 4000,
            "messages": [{"role": "system", "content": "You maintain a product catalogue for a shop. JSON only."},
                         {"role": "user", "content": prompt}]}, self.wandb_headers(), timeout=180)
        text = ((r.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        text = re.sub(r"```(?:json)?", "", text).strip()
        a, b = text.find("{"), text.rfind("}")
        data = json.loads(text[a:b + 1]) if a >= 0 and b > a else {}
        return data.get("items") if isinstance(data, dict) else None


# ---------------------------------------------------------------- VastDB

def _vec_type():
    import pyarrow as pa
    # same column type the VSS writer uses for `vectors` (vastdb_client.py)
    return pa.list_(pa.field(name="item", type=pa.float32(), nullable=False), EMBED_DIM)


def table_schemas():
    import pyarrow as pa
    common = [("run_id", pa.utf8()), ("shop", pa.utf8()), ("shop_slug", pa.utf8()), ("location", pa.utf8())]
    return {
        "items": pa.schema(common + [
            ("id", pa.utf8()), ("name", pa.utf8()), ("category", pa.utf8()), ("material", pa.utf8()),
            ("colour", pa.utf8()), ("description", pa.utf8()), ("est_price_band", pa.utf8()),
            ("pieces", pa.utf8()), ("first_sec", pa.float32()), ("last_sec", pa.float32()), ("clip", pa.utf8()),
            ("vec", _vec_type()), ("vec_json", pa.utf8())]),
        "moments": pa.schema(common + [
            ("piece", pa.int32()), ("start_sec", pa.float32()), ("end_sec", pa.float32()),
            ("items_json", pa.utf8()), ("summary", pa.utf8()), ("clip", pa.utf8()),
            ("vec", _vec_type()), ("vec_json", pa.utf8())]),
        "runs": pa.schema(common + [
            ("created", pa.utf8()), ("source", pa.utf8()), ("video_sec", pa.float32()), ("pieces", pa.int32()),
            ("items", pa.int32()), ("timings_json", pa.utf8()), ("compute_s_per_video_min", pa.float32())]),
    }


def _connect(cfg):
    import vastdb
    ep = cfg["S3_ENDPOINT"]
    ep = ep if ep.startswith(("http://", "https://")) else f"http://{ep}"
    return vastdb.connect(endpoint=ep, access=cfg["ACCESS_KEY"], secret=cfg["SECRET_KEY"], ssl_verify=False)


def _fixvec(v):
    return [float(x) for x in v] if isinstance(v, list) and len(v) == EMBED_DIM else None


def db_write(cfg, rows_by_table):
    """Insert rows into bountiful.<table>, creating schema/tables if missing (vastdb-write skill pattern)."""
    import pyarrow as pa
    schemas = table_schemas()
    session = _connect(cfg)
    with session.transaction() as tx:
        bucket = tx.bucket(cfg["VASTDB_BUCKET"])
        schema = bucket.schema(SCHEMA, fail_if_missing=False)
        if schema is None:
            schema = bucket.create_schema(SCHEMA, fail_if_exists=False)
        for name, rows in rows_by_table.items():
            if not rows:
                continue
            table = schema.table(name, fail_if_missing=False)
            if table is None:
                table = schema.create_table(name, columns=schemas[name], fail_if_exists=False)
            clean = []
            for r in rows:
                r = dict(r)
                if "vec" in r:
                    r["vec"] = _fixvec(r["vec"])
                clean.append({f.name: r.get(f.name) for f in schemas[name]})
            table.insert(pa.Table.from_pylist(clean, schema=schemas[name]))


def db_read(cfg, name):
    """Select every non-vector column (vector columns can crash SDK select; vec_json carries the vector)."""
    session = _connect(cfg)
    rows = []
    with session.transaction() as tx:
        table = tx.bucket(cfg["VASTDB_BUCKET"]).schema(SCHEMA).table(name)
        cols = table.columns
        cols = cols() if callable(cols) else cols
        names = [c.name if hasattr(c, "name") else str(c) for c in cols]
        for batch in table.select(columns=[n for n in names if n != "vec"]):
            rows.extend(batch.to_pylist())
    return rows


# ---------------------------------------------------------------- ingest

def fetch_source(cfg, src, workdir):
    """Local path, s3://bucket/key, a bare key in the chunks bucket, or 'newest'. Returns (path, label)."""
    if os.path.exists(os.path.expanduser(src)):
        return os.path.expanduser(src), src
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        sys.exit("boto3 not installed (pip install --user boto3); or pass a local file path")
    s3 = boto3.client("s3", endpoint_url=cfg["S3_ENDPOINT"], aws_access_key_id=cfg["ACCESS_KEY"],
                      aws_secret_access_key=cfg["SECRET_KEY"], verify=False,
                      config=Config(s3={"addressing_style": "path"}))
    bucket = cfg.get("S3_CHUNKS_BUCKET") or f"{cfg.get('USERNAME')}-vss-chunks"
    if src.startswith("s3://"):
        bucket, _, key = src[5:].partition("/")
    elif src == "newest":
        objs = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            objs += [o for o in page.get("Contents", []) if "_segment_" not in o["Key"]]
        if not objs:
            sys.exit(f"bucket {bucket} is empty")
        key = max(objs, key=lambda o: o["LastModified"])["Key"]
    else:
        key = src
    dst = os.path.join(workdir, "source.mp4")
    s3.download_file(bucket, key, dst)
    return dst, f"s3://{bucket}/{key}"


def cut(src, outdir, seconds=5, fps=5):
    os.makedirs(outdir, exist_ok=True)
    for f in glob.glob(os.path.join(outdir, "p*.mp4")):
        os.remove(f)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vf", f"fps={fps},scale='min(960,iw)':-2",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-an",
                    "-force_key_frames", f"expr:gte(t,n_forced*{seconds})", "-f", "segment",
                    "-segment_time", str(seconds), "-reset_timestamps", "1",
                    os.path.join(outdir, "p%03d.mp4")], check=True)
    return sorted(glob.glob(os.path.join(outdir, "p*.mp4")))


def duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, check=True).stdout
        return float(out.strip())
    except Exception:
        return 0.0


def ingest(cfg, a):
    shop, location, shop_slug = a.shop, a.location or "", slug(a.shop)
    out = a.out
    run_id = time.strftime("%Y%m%dT%H%M%S")
    models = Models(cfg, a.wandb_model)
    T = {"cut": 0.0, "cosmos": 0.0, "wandb": 0.0, "embed": 0.0, "write": 0.0}
    work = tempfile.mkdtemp()
    t0 = time.time()
    src, label = fetch_source(cfg, a.source, work)
    fetch_s = time.time() - t0
    video_sec = duration(src)
    clipdir = os.path.join(out, "clips", shop_slug)
    t0 = time.time()
    pieces = cut(src, clipdir)
    T["cut"] = time.time() - t0
    if a.max_pieces:
        pieces = pieces[:a.max_pieces]
        video_sec = min(video_sec or 1e9, 5.0 * len(pieces))
    print(f"\nBOUNTIFUL ON VAST: {shop} ({location or 'n/a'})  source {label}")
    print(f"  {video_sec:.0f}s of video -> {len(pieces)} x 5s pieces in {T['cut']:.1f}s (fetch {fetch_s:.1f}s)\n")

    catalogue, moments, pending = [], [], []
    where = f" in {location}" if location else ""
    for i, p in enumerate(pieces):
        data = open(p, "rb").read()
        t0 = time.time()
        try:
            text = models.cosmos(data, COSMOS_PROMPT.replace("{where}", where))
        except Exception as e:
            text = ""
            print(f"  [{i * 5:>4}s] Cosmos failed: {e}")
        dt = time.time() - t0
        T["cosmos"] += dt
        items, stats = parse_cosmos_items(text)
        flag = f", dropped {stats['repeats_dropped']} repeats" if stats["repeats_dropped"] else ""
        print(f"  [{i * 5:>4}s] Cosmos {dt:4.1f}s  {len(items)} items{flag}: "
              f"{'; '.join(x['item'] for x in items)[:110]}")
        moments.append({"piece": i, "start_sec": i * 5.0, "end_sec": i * 5.0 + 5, "items": items,
                        "clip": os.path.relpath(p, out), "summary": "; ".join(
                            f"{x['item']} ({x['material']}, {x['colour']})" for x in items)})
        pending += [dict(x, piece=i) for x in items]
        if pending and (len(pending) >= 24 or i == len(pieces) - 1 or (i + 1) % 6 == 0):
            t0 = time.time()
            try:
                merged = models.wandb_merge(shop, location, catalogue, pending)
                catalogue = apply_wandb_merge(catalogue, merged, shop_slug) if merged else \
                    local_merge(catalogue, pending, shop_slug)
                how = f"W&B {models.wandb_model()}"
            except Exception as e:
                catalogue = local_merge(catalogue, pending, shop_slug)
                how = f"local merge (W&B failed: {str(e)[:80]})"
            T["wandb"] += time.time() - t0
            print(f"         catalogue now {len(catalogue)} items via {how} ({time.time() - t0:.1f}s)")
            pending = []

    print("\n  Embedding moments (video) and items (text) with Cosmos Embed1...")
    t0 = time.time()
    for m in moments:
        try:
            m["vec"] = models.embed_video(open(os.path.join(out, m["clip"]), "rb").read())
        except Exception as e:
            m["vec"] = None
            print(f"    moment {m['piece']} embed failed: {str(e)[:100]}")
    texts = [f"{c['name']}. {c['material']}, {c['colour']}. {c['description']}" for c in catalogue]
    try:
        vecs = models.embed_texts(texts) if texts else []
    except Exception as e:
        vecs = [None] * len(texts)
        print(f"    item embed failed: {str(e)[:100]}")
    for c, v in zip(catalogue, vecs):
        c["vec"] = v
    T["embed"] = time.time() - t0

    for c in catalogue:
        ps = c.get("pieces") or [0]
        c.update(first_sec=min(ps) * 5.0, last_sec=max(ps) * 5.0 + 5,
                 clip=moments[min(ps)]["clip"] if moments and min(ps) < len(moments) else "")
    common = {"run_id": run_id, "shop": shop, "shop_slug": shop_slug, "location": location}
    rows = {
        "items": [dict(common, **{k: c.get(k) for k in ("id", "name", "category", "material", "colour",
                                                          "description", "est_price_band", "first_sec",
                                                          "last_sec", "clip", "vec")},
                       pieces=json.dumps(c.get("pieces")), vec_json=json.dumps(c.get("vec")))
                  for c in catalogue],
        "moments": [dict(common, piece=m["piece"], start_sec=m["start_sec"], end_sec=m["end_sec"],
                         items_json=json.dumps(m["items"]), summary=m["summary"], clip=m["clip"],
                         vec=m["vec"], vec_json=json.dumps(m["vec"])) for m in moments],
    }
    t0 = time.time()
    db_status = "ok"
    try:
        db_write(cfg, rows)
    except Exception as e:
        db_status = f"FAILED ({type(e).__name__}: {str(e)[:120]}); JSON fallback only"
    T["write"] = time.time() - t0
    compute = sum(T.values())
    per_min = compute / (video_sec / 60) if video_sec else 0.0
    run = dict(common, created=time.strftime("%Y-%m-%d %H:%M:%S"), source=label, video_sec=video_sec,
               pieces=len(pieces), items=len(catalogue), timings_json=json.dumps({k: round(v, 2) for k, v in T.items()}),
               compute_s_per_video_min=round(per_min, 1))
    try:
        db_write(cfg, {"runs": [run]})
    except Exception:
        pass
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, f"{shop_slug}.json"), "w") as f:
        json.dump({"run": run, "timings": T, "items": rows["items"], "moments": rows["moments"]}, f)
    shutil.rmtree(work, ignore_errors=True)

    print(f"\n  {len(catalogue)} catalogue items, {len(moments)} searchable moments. VastDB write: {db_status}")
    print("  " + "  ".join(f"{k} {v:.1f}s" for k, v in T.items()) + f"  | total {compute:.1f}s")
    print(f"  => {per_min:.0f}s of compute per minute of video "
          f"({'faster' if per_min < 60 else 'slower'} than real time, one worker; pieces are independent so "
          f"N workers ~ N x throughput)\n")
    for c in catalogue:
        print(f"  {c['id']}  {c['name']}  [{c['category']}]  {c['material']}, {c['colour']}  "
              f"@ {c['first_sec']:.0f}s  {c.get('est_price_band') or ''}")


# ---------------------------------------------------------------- search

def load_rows(cfg, out, name):
    if cfg.get("S3_ENDPOINT") and cfg.get("VASTDB_BUCKET"):
        try:
            rows, src = db_read(cfg, name), "VastDB"
            if rows:
                return rows, src
        except Exception:
            pass
    rows = []
    for f in sorted(glob.glob(os.path.join(out, "*.json"))):
        try:
            rows += json.load(open(f)).get(name, [])
        except (ValueError, OSError):
            pass
    return rows, "local JSON"


def latest_runs(rows):
    last = {}
    for r in rows:
        last[r.get("shop_slug")] = max(last.get(r.get("shop_slug"), ""), r.get("run_id") or "")
    return [r for r in rows if (r.get("run_id") or "") == last.get(r.get("shop_slug"))]


def run_search(cfg, out, query, k=8):
    items, src = load_rows(cfg, out, "items")
    moments, _ = load_rows(cfg, out, "moments")
    items, moments = latest_runs(items), latest_runs(moments)
    qv, mode = None, "Cosmos Embed1 cosine"
    try:
        qv = Models(cfg).embed_query(query)
    except Exception as e:
        mode = f"keyword fallback (embed failed: {str(e)[:60]})"

    def vec(r):
        v = r.get("vec_json")
        try:
            return json.loads(v) if isinstance(v, str) else v
        except ValueError:
            return None

    def kw(r, fields):
        words = [w for w in re.findall(r"[a-z]+", query.lower()) if len(w) > 2]
        text = " ".join(str(r.get(f) or "") for f in fields).lower()
        return sum(w in text for w in words) / max(1, len(words))

    for r in items:
        r["score"] = cosine(qv, vec(r)) if qv else kw(r, ("name", "material", "colour", "description", "category"))
    for r in moments:
        r["score"] = cosine(qv, vec(r)) if qv else kw(r, ("summary",))
    top_i = sorted(items, key=lambda r: -r["score"])[:k]
    top_m = sorted(moments, key=lambda r: -r["score"])[:k]
    strip = lambda r: {kk: v for kk, v in r.items() if kk not in ("vec", "vec_json")}
    return {"query": query, "mode": mode, "source": src, "items": [strip(r) for r in top_i],
            "moments": [strip(r) for r in top_m]}


def print_search(res):
    print(f"\nSearch: {res['query']!r}  ({res['mode']}, rows from {res['source']})\n\nItems:")
    for r in res["items"]:
        print(f"  {r['score']:.3f}  {r.get('name')}  [{r.get('shop')}]  {r.get('material')}, {r.get('colour')}  "
              f"@ {float(r.get('first_sec') or 0):.0f}-{float(r.get('last_sec') or 0):.0f}s")
    print("\nMoments (5s of video):")
    for r in res["moments"]:
        print(f"  {r['score']:.3f}  [{r.get('shop')}] {float(r.get('start_sec') or 0):.0f}-"
              f"{float(r.get('end_sec') or 0):.0f}s  {str(r.get('summary') or '')[:100]}")


# ---------------------------------------------------------------- serve

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Bountiful on VAST</title>
<style>
:root{--bg:#121318;--panel:#1b1d24;--line:#2b2e38;--text:#efe9dd;--dim:#9b978e;--acc:#f0a35e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 -apple-system,system-ui,Segoe UI,Roboto,sans-serif}
header{padding:22px 24px 10px}.eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim)}
h1{margin:4px 0 2px;font-size:26px;font-weight:650}.sub{color:var(--dim)}
.stages{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0 4px}.stage{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:8px 12px;font-size:13px}
.stage b{display:block;font-size:12px;color:var(--dim);font-weight:500}.arrow{align-self:center;color:var(--dim)}
.kpi{margin-top:8px;color:var(--acc);font-weight:600}
form{display:flex;gap:10px;padding:8px 24px 4px}input{flex:1;background:#0e0f13;border:1px solid var(--line);color:var(--text);border-radius:99px;padding:11px 16px;font:inherit}
button{background:var(--acc);color:#20140a;border:0;border-radius:99px;padding:10px 20px;font:inherit;font-weight:650;cursor:pointer}
.meta{padding:2px 24px 0;color:var(--dim);font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:14px;padding:14px 24px 30px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.card video{width:100%;aspect-ratio:16/9;object-fit:cover;background:#000;display:block}
.card .b{padding:10px 12px 12px}.card h3{margin:0 0 2px;font-size:15px}.shop{font-size:12px;color:var(--acc)}
.card p{margin:6px 0 0;font-size:13px;color:var(--dim)}.tags span{display:inline-block;font-size:11px;border:1px solid var(--line);border-radius:99px;padding:1px 8px;margin:6px 4px 0 0;color:var(--dim)}
h2{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);margin:6px 24px 0}
</style></head><body>
<header><div class="eyebrow">Bountiful on VAST AI OS</div><h1>Every shop, searchable</h1>
<div class="sub">Film a shop once. Cosmos reads every 5 seconds, W&amp;B builds the catalogue, Cosmos Embed + VastDB make it searchable.</div>
<div class="stages" id="stages"></div><div class="kpi" id="kpi"></div></header>
<form id="f"><input id="q" placeholder="try: black leather armchair, carved stone statue, rattan chair, gold mirror" autofocus><button>Search</button></form>
<div class="meta" id="meta"></div>
<h2>Products</h2><div class="grid" id="items"></div>
<h2>Moments in the footage</h2><div class="grid" id="moments"></div>
<script>
const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
fetch('/api/runs').then(r=>r.json()).then(runs=>{if(!runs.length)return;const t={};let vid=0,comp=0;
 for(const r of runs){const x=JSON.parse(r.timings_json||'{}');for(const k in x)t[k]=(t[k]||0)+x[k];vid+=+r.video_sec||0}
 const names=[['cut','Cut 5s @ 5fps','ffmpeg on VAST'],['cosmos','Cosmos Reason','NVIDIA on CoreWeave'],['wandb','Catalogue merge','W&B Inference'],['embed','Cosmos Embed1','256-d vectors'],['write','VastDB','items + moments']];
 $('stages').innerHTML=names.map(([k,a,b],i)=>`${i?'<span class="arrow">→</span>':''}<div class="stage"><b>${b}</b>${a} · ${(t[k]||0).toFixed(1)}s</div>`).join('');
 comp=Object.values(t).reduce((a,b)=>a+b,0);
 $('kpi').textContent=`${runs.length} shop${runs.length>1?'s':''} · ${Math.round(vid)}s of video · ${(comp/(vid/60||1)).toFixed(0)}s compute per video-minute (one worker; 5s pieces scale out)`});
function card(r,isItem){const t=isItem?`${(+r.first_sec||0).toFixed(0)}s`:`${(+r.start_sec||0).toFixed(0)}–${(+r.end_sec||0).toFixed(0)}s`;
 return `<div class="card"><video src="/clips/${encodeURI(r.clip||'')}" muted loop autoplay playsinline></video><div class="b">
 <div class="shop">${esc(r.shop)} · ${t} · ${(+r.score).toFixed(2)}</div>
 <h3>${esc(isItem?r.name:'Moment')}</h3><p>${esc(isItem?r.description:r.summary)}</p>
 ${isItem?`<div class="tags"><span>${esc(r.category)}</span><span>${esc(r.material)}</span><span>${esc(r.colour)}</span>${r.est_price_band?`<span>est. ${esc(r.est_price_band)}</span>`:''}</div>`:''}</div></div>`}
async function go(q){$('meta').textContent='searching…';const r=await (await fetch('/api/search?q='+encodeURIComponent(q))).json();
 $('meta').textContent=`${r.mode} · rows from ${r.source}`;$('items').innerHTML=r.items.map(x=>card(x,true)).join('');$('moments').innerHTML=r.moments.map(x=>card(x,false)).join('')}
$('f').onsubmit=e=>{e.preventDefault();if($('q').value.trim())go($('q').value.trim())};go('chair');
</script></body></html>"""


def serve(cfg, a):
    out = os.path.abspath(a.out)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, ctype="application/json", status=200):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            try:
                if u.path == "/":
                    return self.send(PAGE.encode(), "text/html; charset=utf-8")
                if u.path == "/api/search":
                    return self.send(run_search(cfg, out, q.get("q", ""), int(q.get("k", 12))))
                if u.path == "/api/runs":
                    runs = []
                    for f in sorted(glob.glob(os.path.join(out, "*.json"))):
                        try:
                            runs.append(json.load(open(f))["run"])
                        except (ValueError, KeyError, OSError):
                            pass
                    return self.send(runs)
                if u.path.startswith("/clips/"):
                    return self.clip(urllib.parse.unquote(u.path[len("/clips/"):]))
                self.send({"error": "not found"}, status=404)
            except Exception as e:
                self.send({"error": f"{type(e).__name__}: {e}"}, status=500)

        def clip(self, rel):
            path = os.path.abspath(os.path.join(out, rel))
            if not path.startswith(out + os.sep) or not os.path.isfile(path):
                return self.send({"error": "no clip"}, status=404)
            data = open(path, "rb").read()
            size, start, end = len(data), 0, len(data) - 1
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                s, _, e = rng[6:].partition("-")
                start = int(s) if s else 0
                end = min(int(e), size - 1) if e else size - 1
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            try:
                self.wfile.write(data[start:end + 1])
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"Bountiful on VAST: http://127.0.0.1:{a.port}  (data: {out}; Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--out", default="bountiful_out", help="local folder for clips + JSON copies")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest", help="video -> catalogue + searchable moments")
    i.add_argument("source", help="local file, s3://bucket/key, key in the chunks bucket, or 'newest'")
    i.add_argument("--shop", required=True)
    i.add_argument("--location", default="")
    i.add_argument("--wandb-model", help="W&B Inference model id (default: best available)")
    i.add_argument("--max-pieces", type=int, help="only the first N x 5s pieces (quick demo)")
    s = sub.add_parser("search", help="semantic search across every ingested shop")
    s.add_argument("query")
    s.add_argument("--k", type=int, default=8)
    v = sub.add_parser("serve", help="search page with the clips")
    v.add_argument("--port", type=int, default=8766)
    a = ap.parse_args()
    if a.cmd == "ingest":
        ingest(load_config(a.config), a)
    elif a.cmd == "search":
        print_search(run_search(load_config(a.config, required=False), a.out, a.query, a.k))
    else:
        serve(load_config(a.config, required=False), a)


if __name__ == "__main__":
    try:
        main()
    except UpstreamError as e:
        sys.exit(f"error: {e}")
