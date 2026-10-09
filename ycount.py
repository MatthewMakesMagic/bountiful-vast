#!/usr/bin/env python3
"""
Count anything YOLO saw across your team's pre-built video index. Run it on the workshop VM.

The ingest pipeline already ran YOLO on every segment and stored the counts, so counting is a
lookup (seconds), not GPU work. Then go deeper on the top moments with live YOLO or Cosmos.

    python3 ycount.py                          # every object class found, across all videos
    python3 ycount.py bicycle                  # bicycles: per camera + the top moments
    python3 ycount.py bicycle --with car       # only segments where a bicycle AND a car appear
    python3 ycount.py person --top 5 --live    # re-run YOLO now on the top 5 (shows GPU speed)
    python3 ycount.py bicycle --ask "What is the cyclist doing and how close are the cars?"
    python3 ycount.py truck --csv trucks.csv   # every segment's count, for charts

Standard library only. Reads /config/<team>.config; credentials are never printed or saved.
"""
import argparse
import ast
import base64
import csv
import glob
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GPU_DEFAULT_HOST = "166.19.38.112"
_INSECURE = ssl._create_unverified_context()


def load_config(path=None):
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
    for k in ("INGRESS_URL", "USERNAME", "PASSWORD", "GPU_BEARER_TOKEN", "YOLO_URL",
              "COSMOS3_REASON_URL", "COSMOS3_REASON_MODEL"):
        if not cfg.get(k) and os.environ.get(k):
            cfg[k] = os.environ[k]
    cfg.setdefault("YOLO_URL", f"http://{GPU_DEFAULT_HOST}:8002")
    cfg.setdefault("COSMOS3_REASON_URL", f"http://{GPU_DEFAULT_HOST}:8001")
    for k in ("INGRESS_URL", "YOLO_URL", "COSMOS3_REASON_URL"):
        if cfg.get(k):
            cfg[k] = cfg[k].rstrip("/")
    if not cfg.get("INGRESS_URL"):
        sys.exit("no INGRESS_URL: run this on the workshop VM, or pass --config <team>.config")
    return cfg


def http(method, url, body=None, headers=None, timeout=300, raw=False):
    headers = dict(headers or {})
    headers.setdefault("User-Agent", "vast-video-lab/1.0")
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_INSECURE if url.startswith("https") else None) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} from {url.split('?')[0]}: {e.read().decode('utf-8', 'replace')[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach {url.split('?')[0]}: {e.reason}")
    return payload if raw else json.loads(payload or b"{}")


class Index:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg["INGRESS_URL"]
        r = http("POST", f"{self.base}/api/v1/auth/login",
                 {"username": cfg.get("USERNAME", ""), "password": cfg.get("PASSWORD", "")}, timeout=30)
        self.token = r["access_token"]
        self.auth = {"Authorization": f"Bearer {self.token}"}
        self.gpu = {"Authorization": f"Bearer {cfg['GPU_BEARER_TOKEN']}"} if cfg.get("GPU_BEARER_TOKEN") else {}

    def get(self, path, **params):
        return http("GET", f"{self.base}{path}?{urllib.parse.urlencode(params)}", headers=self.auth, timeout=120)

    def all_videos(self):
        """Every indexed video with its per-segment timeline (fully and partly indexed)."""
        seen, out = set(), []
        for indexed in ("complete", "partial"):
            offset = 0
            while True:
                r = self.get("/api/v1/videos/explore", scope="all", limit=100, offset=offset, indexed=indexed)
                chunks = r.get("chunks") or []
                for c in chunks:
                    if c.get("original_video") not in seen:
                        seen.add(c.get("original_video"))
                        out.append(c)
                offset += len(chunks)
                if not chunks or offset >= (r.get("total") or 0):
                    break
        return out

    def clip(self, source):
        if not hasattr(self, "_clips"):
            self._clips = {}
        if source not in self._clips:
            q = urllib.parse.urlencode({"source": source, "token": self.token})
            self._clips[source] = http("GET", f"{self.base}/api/v1/videos/stream?{q}", raw=True, timeout=120)
        return self._clips[source]

    def yolo(self, source):
        return self.yolo_bytes(self.clip(source))

    def yolo_bytes(self, data):
        t0 = time.time()
        r = http("POST", f"{self.cfg['YOLO_URL']}/v1/infer",
                 {"video_base64": base64.b64encode(data).decode(), "filename": "clip.mp4", "include_frames": True},
                 self.gpu, timeout=300)
        ms = int((time.time() - t0) * 1000)
        perception = r.get("perception_json") if isinstance(r.get("perception_json"), dict) else {}
        frames = r.get("frames") or perception.get("frames") or []
        peak, total = {}, {}
        for f in frames:
            per = {}
            for d in (f or {}).get("detections") or []:
                label = str(d.get("label") or d.get("class") or d.get("name") or "").lower()
                if label:
                    per[label] = per.get(label, 0) + 1
            for k, v in per.items():
                peak[k] = max(peak.get(k, 0), v)
                total[k] = total.get(k, 0) + v
        # the pipeline stores peak-per-frame when it kept frames, else ~average per frame; report both
        avg = {k: -(-v // len(frames)) for k, v in total.items()} if frames else {}
        if not peak:
            peak = parse_counts(r.get("object_counts") or perception.get("object_counts"))
        return peak, avg, len(frames), ms

    def cosmos(self, source, prompt):
        return self.cosmos_bytes(self.clip(source), prompt)

    def cosmos_bytes(self, data, prompt):
        model = self.cfg.get("COSMOS3_REASON_MODEL")
        if not model:
            try:
                model = http("GET", f"{self.cfg['COSMOS3_REASON_URL']}/v1/models", headers=self.gpu, timeout=20)["data"][0]["id"]
            except Exception:
                model = "nvidia/cosmos3-reason"
        t0 = time.time()
        r = http("POST", f"{self.cfg['COSMOS3_REASON_URL']}/v1/chat/completions", {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + base64.b64encode(data).decode()}},
            ]}],
            "max_tokens": 600, "temperature": 0.2,
        }, self.gpu, timeout=300)
        text = ((r.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        return text, int((time.time() - t0) * 1000)


def parse_counts(value):
    """object_counts arrives as JSON, a Python dict repr, or already a dict."""
    if not value:
        return {}
    if isinstance(value, dict):
        d = value
    else:
        try:
            d = json.loads(value)
        except (ValueError, TypeError):
            try:
                d = ast.literal_eval(str(value))
            except (ValueError, SyntaxError):
                return {}
    if not isinstance(d, dict):
        return {}
    out = {}
    for k, v in d.items():
        try:
            out[str(k).strip().lower()] = int(float(v))
        except (TypeError, ValueError):
            pass
    return out


def segments(videos):
    for c in videos:
        cam = c.get("camera_id") or "?"
        loc = c.get("location") or "?"
        for s in c.get("timeline") or []:
            yield {
                "camera": cam, "location": loc, "filename": c.get("filename") or c.get("original_video"),
                "n": s.get("segment_number"), "start": float(s.get("segment_start_sec") or 0),
                "end": float(s.get("segment_end_sec") or 0), "source": s.get("source"),
                "desc": (s.get("reasoning_content") or "").replace("\n", " "),
                "counts": parse_counts(s.get("object_counts")),
            }


def table(rows, headers):
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h)) for i, h in enumerate(headers)]
    line = "  ".join(str(h).ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(r, widths)))


def overview(segs):
    by_label = {}
    for s in segs:
        for k, v in s["counts"].items():
            e = by_label.setdefault(k, [0, 0, set()])
            e[0] += 1
            e[1] = max(e[1], v)
            e[2].add(s["camera"])
    print(f"\n{len(segs)} segments indexed. Objects YOLO found:\n")
    rows = [(k, e[0], e[1], ", ".join(sorted(e[2]))[:70]) for k, e in sorted(by_label.items(), key=lambda x: -x[1][0])]
    table(rows, ["object", "segments", "max in one", "cameras"])
    print("\nNext: python3 ycount.py <object>   e.g. python3 ycount.py bicycle")


def watch(idx, name_part, since):
    """Poll the index until an uploaded video shows up and is fully indexed; print the timeline."""
    print(f"\nWatching for '{name_part}' in the index (upload time {time.strftime('%H:%M:%S', time.localtime(since))})")
    seen_n = -1
    while time.time() - since < 1200:
        np = name_part.lower()
        hit = [c for c in idx.all_videos() if np in (c.get("filename") or "").lower()
               or np in " ".join(c.get("tags") or []).lower() or np in (c.get("original_video") or "").lower()]
        el = int(time.time() - since)
        if hit:
            c = hit[0]
            tl = c.get("timeline") or []
            n = sum(1 for s in tl if s.get("reasoning_content"))
            if n != seen_n:
                print(f"  +{el:>4}s  indexed {n}/{c.get('total_segments') or len(tl)} segments")
                seen_n = n
            if tl and n >= (c.get("total_segments") or len(tl)):
                print(f"\nSEARCHABLE {el}s after upload. What the pipeline wrote:\n")
                for s in sorted(tl, key=lambda s: s.get("segment_number") or 0):
                    print(f"[{s.get('segment_start_sec', 0):.0f}-{s.get('segment_end_sec', 0):.0f}s] "
                          f"objects {s.get('object_counts')}\n  {' '.join((s.get('reasoning_content') or '').split())[:600]}\n")
                return
        elif seen_n == -1:
            print(f"  +{el:>4}s  not in the index yet")
            seen_n = -2
        time.sleep(15)
    print("Gave up after 20 min: the pipeline did not finish indexing it.")


def raw(idx, cfg, name_part, prompt):
    """Instant lane: fetch the uploaded file straight from the team's S3 chunks bucket, cut it into 5s
    pieces (5 fps), and run YOLO + Cosmos on each piece directly. Needs boto3 + ffmpeg on the VM."""
    import subprocess
    import tempfile
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        sys.exit("boto3 not installed: pip install --user boto3")
    s3 = boto3.client("s3", endpoint_url=cfg["S3_ENDPOINT"], aws_access_key_id=cfg["ACCESS_KEY"],
                      aws_secret_access_key=cfg["SECRET_KEY"], verify=False,
                      config=Config(s3={"addressing_style": "path"}))
    bucket = cfg.get("S3_CHUNKS_BUCKET") or f"{cfg.get('USERNAME')}-vss-chunks"
    allobjs = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        allobjs += [o for o in page.get("Contents", []) if "_segment_" not in o["Key"]]
    objs = [o for o in allobjs if name_part.lower() in o["Key"].lower()]
    if not objs:  # uploads are stored as <team>/<ts>_<uuid>.mp4, so fall back to the newest upload
        objs = allobjs
        print(f"no key contains '{name_part}', using the newest object in {bucket}")
    if not objs:
        sys.exit(f"bucket {bucket} is empty")
    obj = max(objs, key=lambda o: o["LastModified"])
    meta = s3.head_object(Bucket=bucket, Key=obj["Key"]).get("Metadata", {})
    print(f"\nRAW: s3://{bucket}/{obj['Key']} ({obj['Size'] / 1e6:.1f} MB, landed {obj['LastModified']})")
    print(f"metadata: {json.dumps(meta)[:400]}")
    tmp = tempfile.mkdtemp()
    src = os.path.join(tmp, "src.mp4")
    t0 = time.time()
    s3.download_file(bucket, obj["Key"], src)
    subprocess.run(["ffmpeg", "-v", "error", "-i", src, "-vf", "fps=5", "-c:v", "libx264", "-preset", "veryfast",
                    "-an", "-f", "segment", "-segment_time", "5", "-reset_timestamps", "1",
                    os.path.join(tmp, "p%03d.mp4")], check=True)
    pieces = sorted(p for p in os.listdir(tmp) if p.startswith("p"))
    print(f"fetched + cut into {len(pieces)} x 5s pieces at 5 fps in {time.time() - t0:.1f}s\n")
    for i, p in enumerate(pieces):
        data = open(os.path.join(tmp, p), "rb").read()
        peak, _, nf, yms = idx.yolo_bytes(data)
        text, cms = idx.cosmos_bytes(data, prompt) if prompt else ("", 0)
        seen = ", ".join(f"{k}x{v}" for k, v in sorted(peak.items(), key=lambda x: -x[1])) or "nothing"
        print(f"[{i * 5}-{i * 5 + 5}s] YOLO {yms / 1000:.1f}s ({nf} frames): {seen}")
        if text:
            print(f"  Cosmos {cms / 1000:.1f}s: {' '.join(text.split())[:700]}\n")


def simulate(idx, segs, name_part, label, need, ask):
    """Treat one video as a live feed: segment i 'arrives' when its end time passes, is processed
    as soon as the previous one is done, and lag = done time - arrival time (virtual clock)."""
    feed = sorted([s for s in segs if name_part.lower() in (s["filename"] or "").lower()],
                  key=lambda s: (s["filename"], s["n"] or 0))
    if not feed:
        print(f"\nNo video matching '{name_part}'.")
        return
    files = sorted({s["filename"] for s in feed})
    if len(files) > 1:
        print(f"\n'{name_part}' matches {len(files)} videos, using {files[0]}. Be more specific to pick another.")
        feed = [s for s in feed if s["filename"] == files[0]]
    t_feed0 = feed[0]["start"]
    print(f"\nLIVE SIMULATION: {feed[0]['camera']} {files[0]}, {len(feed)} segments "
          f"({feed[-1]['end'] - t_feed0:.0f}s of video). YOLO on every segment; Cosmos when {label}"
          f"{' + ' + ' + '.join(need) if need else ''} is seen{'' if ask else ' (no --ask, so Cosmos is skipped)'}.\n")
    print(f"{'seg':>4} {'arrives':>8} {'fetch':>7} {'yolo':>7} {'cosmos':>7} {'done':>7} {'lag':>7}  seen / Cosmos says")
    done_prev, lags, busy = 0.0, [], 0.0
    for s in feed:
        arrives = s["end"] - t_feed0
        t0 = time.time()
        idx.clip(s["source"])  # storage fetch, timed separately from the GPU calls
        fetch = time.time() - t0
        peak, _, _, yolo_ms = idx.yolo(s["source"])  # clip is cached; yolo_ms is the GPU round trip
        cos_ms, said = 0, ""
        if ask and peak.get(label) and all(peak.get(w) for w in need):
            text, cos_ms = idx.cosmos(s["source"], ask)
            said = " | " + " ".join(text.split())[:110]
        work = fetch + (yolo_ms + cos_ms) / 1000
        busy += work
        done = max(arrives, done_prev) + work
        lag = done - arrives
        lags.append(lag)
        done_prev = done
        seen = ", ".join(f"{k}x{v}" for k, v in sorted(peak.items(), key=lambda x: -x[1])[:4]) or "nothing"
        print(f"{s['n']:>4} {arrives:>7.0f}s {fetch:>6.1f}s {yolo_ms / 1000:>6.1f}s {cos_ms / 1000:>6.1f}s "
              f"{done:>6.1f}s {lag:>6.1f}s  {seen}{said}")
    video_s = feed[-1]["end"] - t_feed0
    print(f"\nProcessed {video_s:.0f}s of video with {busy:.0f}s of work: "
          f"{'KEEPS UP with real time' if busy <= video_s else 'FALLS BEHIND real time'} "
          f"(last result {lags[-1]:.0f}s after its moment happened, worst lag {max(lags):.0f}s).")
    print("For comparison, the full VAST pipeline (upload -> searchable) is estimated at ~1-2 min for a 30s clip.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("label", nargs="?", help="YOLO class to count, e.g. person, bicycle, car, truck, bus")
    ap.add_argument("--with", dest="with_", default="", help="comma-separated classes that must also appear")
    ap.add_argument("--camera", help="only this camera_id (substring match)")
    ap.add_argument("--top", type=int, default=10, help="how many top moments to list")
    ap.add_argument("--live", action="store_true", help="re-run YOLO now on the top moments and time it")
    ap.add_argument("--ask", help="ask Cosmos Reason this question about the top moment(s)")
    ap.add_argument("--ask-top", type=int, default=1, help="how many top moments to ask Cosmos about")
    ap.add_argument("--csv", help="write every matching segment's count to this CSV")
    ap.add_argument("--simulate", metavar="FILENAME_PART",
                    help="replay one indexed video as if it were arriving live: YOLO on every 5s segment, "
                         "Cosmos (--ask) only when <label> (+ --with) is seen; reports lag behind real time")
    ap.add_argument("--watch", metavar="FILENAME_PART", help="poll until an uploaded video is fully indexed")
    ap.add_argument("--since", help="upload time HH:MM:SS (local VM time) for --watch timing")
    ap.add_argument("--raw", metavar="FILENAME_PART", help="run YOLO (+ Cosmos via --ask) directly on an "
                    "uploaded file in the chunks bucket, skipping the pipeline")
    ap.add_argument("--config")
    a = ap.parse_args()

    cfg = load_config(a.config)
    t0 = time.time()
    idx = Index(cfg)
    if a.raw:
        return raw(idx, cfg, a.raw, a.ask)
    if a.watch:
        since = time.time()
        if a.since:
            h, m, s = (int(x) for x in a.since.split(":"))
            lt = time.localtime()
            since = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, s, 0, 0, -1))
        return watch(idx, a.watch, since)
    videos = idx.all_videos()
    segs = list(segments(videos))
    if a.camera:
        segs = [s for s in segs if a.camera.lower() in s["camera"].lower()]
    print(f"Loaded {len(videos)} videos / {len(segs)} segments from team {cfg.get('USERNAME', '?')} "
          f"in {time.time() - t0:.1f}s (stored YOLO results, no GPU used)")
    if not a.label:
        return overview(segs)

    label = a.label.lower()
    need = [w.strip().lower() for w in a.with_.split(",") if w.strip()]
    if a.simulate:
        return simulate(idx, segs, a.simulate, label, need, a.ask)
    hits =[s for s in segs if s["counts"].get(label) and all(s["counts"].get(w) for w in need)]
    what = label + (f" with {' + '.join(need)}" if need else "")
    if not hits:
        seen = sorted({k for s in segs for k in s["counts"]})
        print(f"\nNo segments with {what}. Classes in the index: {', '.join(seen) or 'none'}")
        return

    secs = sum(max(0.0, s["end"] - s["start"]) for s in hits)
    print(f"\n{what}: in {len(hits)} of {len(segs)} segments (~{secs / 60:.1f} min of footage), "
          f"max {max(s['counts'][label] for s in hits)} in a single segment\n")

    by_cam = {}
    for s in hits:
        e = by_cam.setdefault((s["location"], s["camera"]), [0, 0, set()])
        e[0] += 1
        e[1] = max(e[1], s["counts"][label])
        e[2].add(s["filename"])
    table([(loc, cam, n, mx, len(f)) for (loc, cam), (n, mx, f) in sorted(by_cam.items(), key=lambda x: -x[1][0])],
          ["location", "camera", "segments", f"max {label}", "videos"])

    top = sorted(hits, key=lambda s: (-s["counts"][label], s["filename"], s["n"] or 0))[:a.top]
    print(f"\nTop {len(top)} moments by {label} count:\n")
    for i, s in enumerate(top, 1):
        others = ", ".join(f"{k}×{v}" for k, v in sorted(s["counts"].items(), key=lambda x: -x[1]) if k != label)
        print(f"{i:>2}. {label}×{s['counts'][label]}  {s['camera']}  {s['filename']}  "
              f"{s['start']:.0f}-{s['end']:.0f}s  (also: {others or 'nothing'})")
        if s["desc"]:
            print(f"    pipeline said: {s['desc'][:160]}")

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["location", "camera", "filename", "segment", "start_sec", "end_sec", label, "all_counts", "source"])
            for s in sorted(hits, key=lambda s: (s["camera"], s["filename"], s["n"] or 0)):
                w.writerow([s["location"], s["camera"], s["filename"], s["n"], s["start"], s["end"],
                            s["counts"][label], json.dumps(s["counts"]), s["source"]])
        print(f"\nWrote {len(hits)} rows to {a.csv}")

    if a.live:
        print(f"\nRe-running YOLO live on the top {len(top)} (CoreWeave GPU):\n")
        for i, s in enumerate(top, 1):
            try:
                peak, avg, nframes, ms = idx.yolo(s["source"])
                seg_ms = int((s["end"] - s["start"]) * 1000)
                pace = "faster than real time" if seg_ms and ms < seg_ms else "slower than real time"
                stored = s["counts"][label]
                verdict = "MATCH" if stored in (peak.get(label, 0), avg.get(label, 0)) else "differs"
                print(f"{i:>2}. stored {label}×{stored}  live peak {peak.get(label, 0)} / avg {avg.get(label, 0)}  "
                      f"[{verdict}]  {nframes} frames in {ms} ms for a {seg_ms} ms clip, {pace}")
            except Exception as e:
                print(f"{i:>2}. YOLO failed: {e}")

    if a.ask:
        for i, s in enumerate(top[:a.ask_top], 1):
            print(f"\nCosmos Reason on #{i} ({s['camera']} {s['start']:.0f}-{s['end']:.0f}s): {a.ask}")
            try:
                text, ms = idx.cosmos(s["source"], a.ask)
                print(f"({ms} ms)\n{text}")
            except Exception as e:
                print(f"Cosmos failed: {e}")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(f"error: {e}")
