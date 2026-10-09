#!/usr/bin/env python3
"""
VAST Builders Challenge: video model lab.

Pick any clip from your team's index (or search for one), then run the event's models on it:
YOLO (objects + boxes), Cosmos Reason (ask anything about the clip), a W&B LLM (decide/act),
and a "simulate live" mode that walks a video segment by segment like a real-time feed.

Run it on the workshop VM (that's where the team's video store is reachable):

    python3 vlab.py                 # reads /config/<team>.config, serves http://127.0.0.1:8765
    python3 vlab.py --config my.config --port 8765

Standard library only. Credentials are read from the team config and never written anywhere.
"""
import argparse
import base64
import glob
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GPU_DEFAULT_HOST = "166.19.38.112"
WANDB_BASE = "https://api.inference.wandb.ai/v1"
_INSECURE = ssl._create_unverified_context()  # internal hackathon hosts may use self-signed certs


# ---------------------------------------------------------------- config

def load_config(path=None):
    if not path:
        found = sorted(glob.glob("/config/*.config"))
        if len(found) > 1:
            sys.exit(f"found several team configs, pick one with --config: {found}")
        path = found[0] if found else None
    cfg = {}
    if path:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    # env fills gaps (WANDB_* are often only in the environment)
    for k, v in os.environ.items():
        if k not in cfg and (k.startswith(("WANDB_", "COSMOS", "YOLO", "CANARY", "GPU_")) or
                             k in ("INGRESS_URL", "USERNAME", "PASSWORD")):
            cfg[k] = v
    cfg.setdefault("COSMOS3_REASON_URL", f"http://{GPU_DEFAULT_HOST}:8001")
    cfg.setdefault("YOLO_URL", f"http://{GPU_DEFAULT_HOST}:8002")
    cfg.setdefault("COSMOS_EMBED1_URL", f"http://{GPU_DEFAULT_HOST}:8003")
    cfg.setdefault("CANARY_1B_URL", f"http://{GPU_DEFAULT_HOST}:8004")
    for k in ("COSMOS3_REASON_URL", "YOLO_URL", "COSMOS_EMBED1_URL", "CANARY_1B_URL", "INGRESS_URL"):
        if cfg.get(k):
            cfg[k] = cfg[k].rstrip("/")
    return cfg, path


# ---------------------------------------------------------------- http helpers

class UpstreamError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:400]}")
        self.status = status


def http(method, url, body=None, headers=None, timeout=300, raw=False):
    headers = dict(headers or {})
    headers.setdefault("User-Agent", "vast-video-lab/1.0")  # Cloudflare (W&B) blocks urllib's default UA
    data = None
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
        else:
            data = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = _INSECURE if url.startswith("https") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            payload = r.read()
    except urllib.error.HTTPError as e:
        raise UpstreamError(e.code, e.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        raise UpstreamError(0, f"cannot reach {url.split('?')[0]}: {e.reason}")
    return payload if raw else json.loads(payload or b"{}")


class Lab:
    def __init__(self, cfg):
        self.cfg = cfg
        self._token = None
        self._lock = threading.Lock()
        self._clips = {}  # source -> bytes (small LRU)
        self._clip_order = []
        self._cosmos_model = cfg.get("COSMOS3_REASON_MODEL")

    # ---- auth headers
    def gpu_headers(self):
        t = self.cfg.get("GPU_BEARER_TOKEN", "")
        return {"Authorization": f"Bearer {t}"} if t else {}

    def wandb_headers(self):
        h = {"Authorization": f"Bearer {self.cfg.get('WANDB_API_KEY', '')}"}
        team, proj = self.cfg.get("WANDB_TEAM"), self.cfg.get("WANDB_PROJECT")
        if team and proj:
            h["OpenAI-Project"] = f"{team}/{proj}"
        return h

    # ---- VSS backend
    def token(self, refresh=False):
        with self._lock:
            if self._token and not refresh:
                return self._token
            if not self.cfg.get("INGRESS_URL"):
                raise UpstreamError(0, "INGRESS_URL missing from team config")
            r = http("POST", f"{self.cfg['INGRESS_URL']}/api/v1/auth/login",
                     {"username": self.cfg.get("USERNAME", ""), "password": self.cfg.get("PASSWORD", "")},
                     timeout=30)
            self._token = r["access_token"]
            return self._token

    def backend(self, method, path, body=None, raw=False, timeout=120):
        for attempt in (0, 1):
            tok = self.token(refresh=attempt == 1)
            url = f"{self.cfg['INGRESS_URL']}{path}"
            try:
                return http(method, url, body, {"Authorization": f"Bearer {tok}"}, timeout=timeout, raw=raw)
            except UpstreamError as e:
                if e.status != 401 or attempt == 1:
                    raise

    def clip(self, source):
        if source in self._clips:
            return self._clips[source]
        q = urllib.parse.urlencode({"source": source, "token": self.token()})
        try:
            data = http("GET", f"{self.cfg['INGRESS_URL']}/api/v1/videos/stream?{q}", raw=True, timeout=120)
        except UpstreamError as e:
            if e.status != 401:
                raise
            q = urllib.parse.urlencode({"source": source, "token": self.token(refresh=True)})
            data = http("GET", f"{self.cfg['INGRESS_URL']}/api/v1/videos/stream?{q}", raw=True, timeout=120)
        self._clips[source] = data
        self._clip_order.append(source)
        while len(self._clip_order) > 40:
            self._clips.pop(self._clip_order.pop(0), None)
        return data

    # ---- shaping
    @staticmethod
    def slim_chunk(c):
        segs = []
        for s in c.get("timeline") or []:
            segs.append({
                "n": s.get("segment_number"),
                "start": s.get("segment_start_sec"),
                "end": s.get("segment_end_sec"),
                "source": s.get("source"),
                "desc": s.get("reasoning_content") or "",
                "objects": s.get("object_counts") or "",
                "match": bool(s.get("is_search_match") or s.get("is_best_match")),
            })
        segs.sort(key=lambda s: (s["n"] is None, s["n"]))
        return {
            "video": c.get("original_video"),
            "filename": c.get("filename"),
            "location": c.get("location"),
            "camera_id": c.get("camera_id"),
            "capture_type": c.get("capture_type"),
            "segments_total": c.get("total_segments"),
            "best_segment": c.get("best_segment_number"),
            "score": c.get("similarity_score"),
            "segments": segs,
        }

    def explore(self, offset=0, location=None):
        params = {"scope": "all", "limit": 48, "offset": offset, "indexed": "complete"}
        if location:
            params["location"] = location
        r = self.backend("GET", "/api/v1/videos/explore?" + urllib.parse.urlencode(params))
        if not r.get("chunks") and offset == 0:
            params["indexed"] = "partial"
            r = self.backend("GET", "/api/v1/videos/explore?" + urllib.parse.urlencode(params))
        return {
            "total": r.get("total"),
            "locations": r.get("locations") or [],
            "videos": [self.slim_chunk(c) for c in r.get("chunks") or []],
            "message": r.get("table_message"),
        }

    def search(self, query):
        r = self.backend("POST", "/api/v1/search", {"query": query, "top_k": 30, "llm_top_n": 1})
        return {"videos": [self.slim_chunk(c) for c in r.get("chunk_results") or []], "total": r.get("chunk_total")}

    # ---- models
    def yolo(self, source):
        data = self.clip(source)
        t0 = time.time()
        r = http("POST", f"{self.cfg['YOLO_URL']}/v1/infer",
                 {"video_base64": base64.b64encode(data).decode(), "filename": "clip.mp4", "include_frames": True},
                 self.gpu_headers(), timeout=300)
        ms = int((time.time() - t0) * 1000)
        perception = r.get("perception_json") if isinstance(r.get("perception_json"), dict) else {}
        frames_in = r.get("frames") or perception.get("frames") or []
        frames, peak = [], {}
        for f in frames_in:
            if not isinstance(f, dict):
                continue
            dets, per = [], {}
            for d in f.get("detections") or []:
                label = str(d.get("label") or d.get("class") or d.get("name") or "").lower()
                if not label:
                    continue
                per[label] = per.get(label, 0) + 1
                dets.append({"label": label, "conf": d.get("confidence") or d.get("conf"), "bbox": d.get("bbox")})
            for k, v in per.items():
                peak[k] = max(peak.get(k, 0), v)
            frames.append({"t": f.get("time_sec"), "shape": f.get("shape"), "dets": dets})
        if not peak:
            raw_counts = r.get("object_counts") or perception.get("object_counts") or {}
            if isinstance(raw_counts, str):
                try:
                    raw_counts = json.loads(raw_counts)
                except ValueError:
                    raw_counts = {}
            peak = {str(k).lower(): v for k, v in (raw_counts or {}).items()}
        return {"ms": ms, "counts": peak, "frames": frames, "frame_count": len(frames)}

    def cosmos_model(self):
        if not self._cosmos_model:
            try:
                r = http("GET", f"{self.cfg['COSMOS3_REASON_URL']}/v1/models", headers=self.gpu_headers(), timeout=20)
                self._cosmos_model = r["data"][0]["id"]
            except Exception:
                self._cosmos_model = "nvidia/cosmos3-reason"
        return self._cosmos_model

    def cosmos(self, source, prompt, max_tokens=700):
        data = self.clip(source)
        t0 = time.time()
        r = http("POST", f"{self.cfg['COSMOS3_REASON_URL']}/v1/chat/completions", {
            "model": self.cosmos_model(),
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + base64.b64encode(data).decode()}},
            ]}],
            "max_tokens": int(max_tokens), "temperature": 0.2,
        }, self.gpu_headers(), timeout=300)
        ms = int((time.time() - t0) * 1000)
        text = ((r.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        return {"ms": ms, "text": text, "usage": r.get("usage")}

    def canary(self, source):
        data = self.clip(source)
        boundary = uuid.uuid4().hex
        parts = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nnvidia/canary-1b\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"clip.mp4\"\r\n"
            f"Content-Type: video/mp4\r\n\r\n".encode() + data + b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
        t0 = time.time()
        r = http("POST", f"{self.cfg['CANARY_1B_URL']}/v1/audio/transcriptions", b"".join(parts),
                 {**self.gpu_headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"}, timeout=300)
        return {"ms": int((time.time() - t0) * 1000), "text": r.get("text", json.dumps(r)[:2000])}

    def wandb_models(self):
        r = http("GET", f"{WANDB_BASE}/models", headers=self.wandb_headers(), timeout=20)
        return sorted(m["id"] for m in r.get("data", []))

    def agent(self, model, instruction, context):
        t0 = time.time()
        r = http("POST", f"{WANDB_BASE}/chat/completions", {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a real-time video agent. You get machine perception of a "
                 "short video clip (YOLO object counts and a Cosmos Reason description). Be concise and decisive."},
                {"role": "user", "content": f"{instruction}\n\nPERCEPTION:\n{json.dumps(context, indent=1)}"},
            ],
            "max_tokens": 400, "temperature": 0.2,
        }, self.wandb_headers(), timeout=120)
        text = ((r.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        return {"ms": int((time.time() - t0) * 1000), "text": text, "model": model}

    def status(self):
        out = {}
        def check(name, fn):
            try:
                fn()
                out[name] = {"ok": True}
            except Exception as e:
                out[name] = {"ok": False, "error": str(e)[:300]}
        threads = [
            threading.Thread(target=check, args=("backend", lambda: self.token(refresh=True))),
            threading.Thread(target=check, args=("yolo", lambda: http("GET", f"{self.cfg['YOLO_URL']}/healthz",
                                                                    headers=self.gpu_headers(), timeout=10))),
            threading.Thread(target=check, args=("cosmos", self.cosmos_model)),
            threading.Thread(target=check, args=("cosmos", lambda: http(
                "GET", f"{self.cfg['COSMOS3_REASON_URL']}/v1/health/ready", headers=self.gpu_headers(), timeout=10))),
            threading.Thread(target=check, args=("canary", lambda: http(
                "GET", f"{self.cfg['CANARY_1B_URL']}/v1/health/ready", headers=self.gpu_headers(), timeout=10))),
            threading.Thread(target=check, args=("wandb", self.wandb_models)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(25)
        out["team"] = self.cfg.get("USERNAME")
        return out


# ---------------------------------------------------------------- server

def make_handler(lab):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send_json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def guarded(self, fn):
            try:
                self.send_json(fn())
            except UpstreamError as e:
                self.send_json({"error": str(e)}, 502)
            except Exception as e:
                self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            if u.path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/status":
                self.guarded(lab.status)
            elif u.path == "/api/videos":
                self.guarded(lambda: lab.explore(int(q.get("offset", 0)), q.get("location") or None))
            elif u.path == "/api/wandb-models":
                self.guarded(lab.wandb_models)
            elif u.path == "/api/clip":
                self.serve_clip(q.get("source", ""))
            else:
                self.send_json({"error": "not found"}, 404)

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            b = self.body()
            routes = {
                "/api/search": lambda: lab.search(b["query"]),
                "/api/yolo": lambda: lab.yolo(b["source"]),
                "/api/cosmos": lambda: lab.cosmos(b["source"], b.get("prompt") or "Describe this clip.",
                                                  b.get("max_tokens", 700)),
                "/api/canary": lambda: lab.canary(b["source"]),
                "/api/agent": lambda: lab.agent(b["model"], b.get("instruction", ""), b.get("context", {})),
            }
            if u.path in routes:
                self.guarded(routes[u.path])
            else:
                self.send_json({"error": "not found"}, 404)

        def serve_clip(self, source):
            try:
                data = lab.clip(source)
            except Exception as e:
                return self.send_json({"error": str(e)}, 502)
            size, start, end = len(data), 0, len(data) - 1
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                a, _, z = rng[6:].partition("-")
                start = int(a) if a else 0
                end = min(int(z), size - 1) if z else size - 1
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
    return H


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Video Model Lab</title>
<style>
:root{--bg:#14151a;--panel:#1c1e25;--line:#2c2f39;--text:#ece8df;--dim:#9a978f;--acc:#f2a65a;--ok:#7bd88f;--bad:#ff7a7a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,system-ui,Segoe UI,Roboto,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:15px;margin:0 8px 0 0;letter-spacing:.02em}.chip{font-size:12px;padding:2px 9px;border-radius:99px;border:1px solid var(--line);color:var(--dim)}
.chip.ok{color:var(--ok);border-color:#2f5a3a}.chip.bad{color:var(--bad);border-color:#5a2f2f;cursor:help}
main{display:grid;grid-template-columns:340px 1fr 380px;height:calc(100vh - 49px)}
section{overflow:auto;border-right:1px solid var(--line);padding:12px}
input,textarea,select{width:100%;background:#111217;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px;font:inherit}
textarea{min-height:70px;resize:vertical}
button{background:var(--acc);color:#1a1307;border:0;border-radius:99px;padding:7px 14px;font-weight:600;cursor:pointer;font:inherit;font-weight:600}
button.ghost{background:transparent;color:var(--text);border:1px solid var(--line)}button:disabled{opacity:.5;cursor:wait}
.row{display:flex;gap:8px;align-items:center;margin:8px 0;flex-wrap:wrap}.lbl{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin:14px 0 6px}
.vid{border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin:6px 0;cursor:pointer;background:var(--panel)}
.vid:hover{border-color:#4a4e5c}.vid .meta{color:var(--dim);font-size:12px}
.seg{font-size:12px;padding:5px 8px;border-radius:6px;cursor:pointer;color:var(--dim);border:1px solid transparent}
.seg:hover{background:#23252e;color:var(--text)}.seg.on{border-color:var(--acc);color:var(--text)}.seg.match{color:var(--acc)}
.stage{position:relative;background:#000;border-radius:10px;overflow:hidden}
video{width:100%;display:block;max-height:52vh;background:#000}canvas{position:absolute;left:0;top:0;pointer-events:none}
.out{white-space:pre-wrap;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px;margin:6px 0;font-size:13px}
.counts span{display:inline-block;margin:2px 6px 2px 0;padding:2px 8px;border-radius:99px;background:#262833;font-size:12px}
.preset{font-size:12px;padding:3px 9px;border-radius:99px;border:1px solid var(--line);cursor:pointer;color:var(--dim)}
.preset:hover{color:var(--text);border-color:#4a4e5c}
.log div{font-size:12px;padding:6px 8px;border-bottom:1px solid var(--line)}.log .hit{border-left:3px solid var(--acc)}
.dim{color:var(--dim)}.err{color:var(--bad)}
</style></head><body>
<header><h1>Video Model Lab</h1><span id="team" class="chip">team</span>
<span class="chip" id="s-backend">backend</span><span class="chip" id="s-yolo">YOLO</span><span class="chip" id="s-cosmos">Cosmos</span>
<span class="chip" id="s-canary">Canary</span><span class="chip" id="s-wandb">W&amp;B</span></header>
<main>
<section>
  <div class="row"><input id="q" placeholder="search the index, e.g. cyclist near a car"></div>
  <div class="row"><button id="go">Search</button><button class="ghost" id="browse">Browse all</button></div>
  <div id="locs" class="row"></div>
  <div id="list"><p class="dim">Loading your team's videos…</p></div>
  <div class="row"><button class="ghost" id="more" style="display:none">Load more</button></div>
</section>
<section>
  <div id="title" class="dim">Pick a clip on the left.</div>
  <div class="stage"><video id="v" controls muted playsinline></video><canvas id="cv"></canvas></div>
  <div class="row">
    <button id="b-yolo">Run YOLO</button><button id="b-cosmos">Ask Cosmos</button>
    <button class="ghost" id="b-canary">Transcribe audio (Canary)</button>
    <label class="dim"><input type="checkbox" id="boxes" checked style="width:auto"> show boxes</label>
  </div>
  <div class="lbl">Question for Cosmos Reason</div>
  <div class="row" id="presets"></div>
  <textarea id="prompt">Describe what happens in this clip. Mention every person, cyclist and vehicle and how close they get to each other.</textarea>
  <div class="lbl">YOLO <span id="yolo-ms" class="dim"></span></div><div id="yolo" class="out dim">not run</div>
  <div class="lbl">Cosmos Reason <span id="cosmos-ms" class="dim"></span></div><div id="cosmos" class="out dim">not run</div>
  <div class="lbl">What the pipeline already wrote for this segment</div><div id="pipe" class="out dim">n/a</div>
</section>
<section>
  <div class="lbl" style="margin-top:0">Agent (W&amp;B Inference)</div>
  <select id="model"><option>loading models…</option></select>
  <textarea id="instr">Decide if this clip needs a safety alert. Reply as JSON: {"alert": true|false, "severity": "low|medium|high", "reason": "...", "action": "..."}</textarea>
  <div class="row"><button id="b-agent">Run agent on this clip</button></div>
  <div id="agent" class="out dim">uses the latest YOLO + Cosmos output</div>
  <div class="lbl">Simulate live feed</div>
  <p class="dim" style="margin:0 0 6px">Walks the selected video segment by segment, like a camera feed: YOLO on every
  segment, Cosmos only when all trigger objects appear, then the agent.</p>
  <input id="trig" value="person,car" title="comma-separated YOLO labels that must all appear">
  <div class="row"><label class="dim"><input type="checkbox" id="live-agent" checked style="width:auto"> run agent on hits</label>
  <button id="b-live">Start</button><button class="ghost" id="b-stop">Stop</button></div>
  <div id="log" class="log"></div>
</section>
</main>
<script>
const $=id=>document.getElementById(id);
const api=async(path,body)=>{const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
  const j=await r.json();if(!r.ok||j.error)throw new Error(j.error||r.status);return j};
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let videos=[],offset=0,cur=null,curVideo=null,yoloRes=null,cosmosRes=null,stop=false,loc=null;

api('/api/status').then(s=>{$('team').textContent=s.team||'team';
  for(const k of ['backend','yolo','cosmos','canary','wandb']){const el=$('s-'+k),v=s[k];if(!v)continue;
    el.classList.add(v.ok?'ok':'bad');if(!v.ok)el.title=v.error}}).catch(e=>console.warn(e));
api('/api/wandb-models').then(ms=>{const pick=ms.find(m=>/llama.*8b/i.test(m))||ms[0];
  $('model').innerHTML=ms.map(m=>`<option ${m===pick?'selected':''}>${esc(m)}</option>`).join('')})
  .catch(e=>{$('model').innerHTML='<option>W&B unavailable</option>';$('agent').innerHTML='<span class="err">'+esc(e.message)+'</span>'});

const PRESETS={'Describe':'Describe what happens in this clip. Mention every person, cyclist and vehicle and how close they get to each other.',
 'Near-miss?':'Is any person or cyclist dangerously close to a moving vehicle in this clip? Answer YES or NO first, then explain with timestamps.',
 'Count':'Count the vehicles in this clip by type (car, truck, bus, bicycle, motorcycle) and say which direction each is moving.',
 'Cyclist':'Is there a cyclist? If so: where are they relative to cars, are they in a bike lane, and is anything blocking them?',
 'Anything unusual?':'What, if anything, is unusual or worth a human looking at in this clip? Be specific.'};
$('presets').innerHTML=Object.keys(PRESETS).map(k=>`<span class="preset" data-k="${k}">${k}</span>`).join('');
$('presets').onclick=e=>{const k=e.target.dataset.k;if(k)$('prompt').value=PRESETS[k]};

function render(append){const html=videos.map((v,i)=>`<div class="vid" data-i="${i}"><b>${esc(v.filename||v.video)}</b>
  <div class="meta">${esc([v.location,v.camera_id,v.capture_type].filter(Boolean).join(' · '))} · ${v.segments.length} segs${v.score?` · score ${(+v.score).toFixed(2)}`:''}</div>
  <div class="segs" id="segs-${i}" style="display:none"></div></div>`).join('');
  $('list').innerHTML=html||'<p class="dim">Nothing found.</p>'}
async function browse(more){try{if(!more){offset=0;videos=[]}
  const r=await api('/api/videos?offset='+offset+(loc?'&location='+encodeURIComponent(loc):''));
  videos=videos.concat(r.videos);offset+=r.videos.length;render();
  $('more').style.display=r.total&&offset<r.total?'':'none';
  if(r.locations&&!$('locs').dataset.done){$('locs').dataset.done=1;
    $('locs').innerHTML=r.locations.map(l=>`<span class="preset" data-l="${esc(l.label)}">${esc(l.label)} (${l.chunk_count})</span>`).join('')}
  if(r.message&&!r.videos.length)$('list').innerHTML='<p class="err">'+esc(r.message)+'</p>'}
  catch(e){$('list').innerHTML='<p class="err">'+esc(e.message)+'</p>'}}
$('locs').onclick=e=>{const l=e.target.dataset.l;if(l===undefined)return;loc=(loc===l)?null:l;browse()};
$('browse').onclick=()=>{loc=null;browse()};$('more').onclick=()=>browse(true);
$('go').onclick=async()=>{const q=$('q').value.trim();if(!q)return;$('list').innerHTML='<p class="dim">Searching…</p>';
  try{const r=await api('/api/search',{query:q});videos=r.videos;render();$('more').style.display='none'}
  catch(e){$('list').innerHTML='<p class="err">'+esc(e.message)+'</p>'}};
$('q').onkeydown=e=>{if(e.key==='Enter')$('go').click()};
$('list').onclick=e=>{const seg=e.target.closest('.seg');if(seg){pick(+seg.dataset.i,+seg.dataset.j);return}
  const card=e.target.closest('.vid');if(!card)return;const i=+card.dataset.i,box=$('segs-'+i);
  if(box.style.display==='none'){box.innerHTML=videos[i].segments.map((s,j)=>`<div class="seg ${s.match?'match':''}" data-i="${i}" data-j="${j}">
    #${s.n} · ${(+s.start||0).toFixed(0)}–${(+s.end||0).toFixed(0)}s · ${esc((s.desc||'').slice(0,90))}</div>`).join('');box.style.display=''}
  else box.style.display='none'};

function pick(i,j){curVideo=videos[i];cur=curVideo.segments[j];yoloRes=cosmosRes=null;
  document.querySelectorAll('.seg.on').forEach(x=>x.classList.remove('on'));
  const el=document.querySelector(`.seg[data-i="${i}"][data-j="${j}"]`);if(el)el.classList.add('on');
  $('title').innerHTML=`<b>${esc(curVideo.filename)}</b> · segment #${cur.n} (${(+cur.start||0).toFixed(1)}–${(+cur.end||0).toFixed(1)}s) <span class="dim">${esc(curVideo.camera_id||'')}</span>`;
  $('v').src='/api/clip?source='+encodeURIComponent(cur.source);$('v').play().catch(()=>{});
  $('yolo').innerHTML='not run';$('cosmos').innerHTML='not run';$('yolo-ms').textContent=$('cosmos-ms').textContent='';
  $('pipe').innerHTML=esc(cur.desc||'(empty)')+(cur.objects?'\n\nobjects: '+esc(cur.objects):'');clearBoxes()}

async function runYolo(src){const r=await api('/api/yolo',{source:src});return r}
async function runCosmos(src,prompt){return api('/api/cosmos',{source:src,prompt})}
function showYolo(r){yoloRes=r;$('yolo-ms').textContent=r.ms+' ms · '+r.frame_count+' frames';
  const c=Object.entries(r.counts).sort((a,b)=>b[1]-a[1]);
  $('yolo').innerHTML=c.length?'<div class="counts">'+c.map(([k,v])=>`<span>${esc(k)} × ${v}</span>`).join('')+'</div><div class="dim" style="margin-top:6px">peak count per class in any single frame</div>':'no objects detected';
  $('yolo').classList.remove('dim')}
$('b-yolo').onclick=async()=>{if(!cur)return;const b=$('b-yolo');b.disabled=true;$('yolo').textContent='running…';
  try{showYolo(await runYolo(cur.source))}catch(e){$('yolo').innerHTML='<span class="err">'+esc(e.message)+'</span>'}b.disabled=false};
$('b-cosmos').onclick=async()=>{if(!cur)return;const b=$('b-cosmos');b.disabled=true;$('cosmos').textContent='thinking…';
  try{const r=await runCosmos(cur.source,$('prompt').value);cosmosRes=r;$('cosmos').textContent=r.text;$('cosmos').classList.remove('dim');
    $('cosmos-ms').textContent=r.ms+' ms'}catch(e){$('cosmos').innerHTML='<span class="err">'+esc(e.message)+'</span>'}b.disabled=false};
$('b-canary').onclick=async()=>{if(!cur)return;$('cosmos').textContent='transcribing…';
  try{const r=await api('/api/canary',{source:cur.source});$('cosmos').textContent='Canary transcript:\n'+(r.text||'(no speech)')}
  catch(e){$('cosmos').innerHTML='<span class="err">Canary: '+esc(e.message)+'\n(most of the provided footage has no audio track)</span>'}};

function context(seg,y,c){return{camera:curVideo&&curVideo.camera_id,location:curVideo&&curVideo.location,
  segment:seg&&{n:seg.n,start_sec:seg.start,end_sec:seg.end},yolo_peak_counts:y&&y.counts,cosmos:c&&c.text,
  pipeline_description:seg&&seg.desc}}
async function runAgent(seg,y,c){return api('/api/agent',{model:$('model').value,instruction:$('instr').value,context:context(seg,y,c)})}
$('b-agent').onclick=async()=>{if(!cur)return;const b=$('b-agent');b.disabled=true;$('agent').textContent='deciding…';
  try{const r=await runAgent(cur,yoloRes,cosmosRes);$('agent').textContent=r.text+'\n\n'+r.model+' · '+r.ms+' ms';$('agent').classList.remove('dim')}
  catch(e){$('agent').innerHTML='<span class="err">'+esc(e.message)+'</span>'}b.disabled=false};

// live simulation
$('b-stop').onclick=()=>{stop=true};
$('b-live').onclick=async()=>{if(!curVideo){alertLog('pick a video first');return}stop=false;$('log').innerHTML='';
  const trig=$('trig').value.split(',').map(s=>s.trim().toLowerCase()).filter(Boolean);
  const vi=videos.indexOf(curVideo);const startJ=Math.max(0,curVideo.segments.indexOf(cur));
  for(let j=startJ;j<curVideo.segments.length&&!stop;j++){pick(vi,j);const seg=curVideo.segments[j];const t0=performance.now();
    const row=document.createElement('div');row.textContent=`#${seg.n} ${(+seg.start||0).toFixed(0)}s · YOLO…`;$('log').prepend(row);
    try{const y=await runYolo(seg.source);showYolo(y);
      const hit=trig.length&&trig.every(t=>y.counts[t]);let line=`#${seg.n} ${(+seg.start||0).toFixed(0)}s · ${Object.entries(y.counts).map(([k,v])=>k+'×'+v).join(' ')||'nothing'} (${y.ms}ms)`;
      if(hit){row.className='hit';row.textContent=line+' · TRIGGER → Cosmos…';const c=await runCosmos(seg.source,$('prompt').value);cosmosRes=c;
        $('cosmos').textContent=c.text;$('cosmos-ms').textContent=c.ms+' ms';line+=`\nCosmos (${c.ms}ms): ${c.text.slice(0,220)}`;
        if($('live-agent').checked){row.textContent=line+'\nagent…';const a=await runAgent(seg,y,c);line+=`\nAgent (${a.ms}ms): ${a.text.slice(0,240)}`}}
      const total=Math.round(performance.now()-t0),segLen=Math.round(((+seg.end||0)-(+seg.start||0))*1000);
      line+=`\n${total}ms to process a ${segLen}ms segment → ${segLen&&total<=segLen?'keeps up with real time':'slower than real time'}`;
      row.textContent=line}catch(e){row.innerHTML='<span class="err">'+esc(e.message)+'</span>'}}};
function alertLog(m){$('log').innerHTML='<div class="err">'+esc(m)+'</div>'}

// YOLO box overlay
const v=$('v'),cv=$('cv');
function clearBoxes(){cv.getContext('2d').clearRect(0,0,cv.width,cv.height)}
function draw(){requestAnimationFrame(draw);if(!yoloRes||!yoloRes.frames.length||!$('boxes').checked||!v.videoWidth){clearBoxes();return}
  const W=v.clientWidth,H=v.clientHeight;if(cv.width!==W||cv.height!==H){cv.width=W;cv.height=H;cv.style.width=W+'px';cv.style.height=H+'px'}
  const s=Math.min(W/v.videoWidth,H/v.videoHeight),dw=v.videoWidth*s,dh=v.videoHeight*s,ox=(W-dw)/2,oy=(H-dh)/2;
  let f=yoloRes.frames[0],best=1e9;for(const fr of yoloRes.frames){const d=Math.abs((fr.t||0)-v.currentTime);if(d<best){best=d;f=fr}}
  const srcH=(f.shape&&f.shape[0])||v.videoHeight,srcW=(f.shape&&f.shape[1])||v.videoWidth,kx=dw/srcW,ky=dh/srcH;
  const g=cv.getContext('2d');g.clearRect(0,0,W,H);g.lineWidth=2;g.font='12px system-ui';
  for(const d of f.dets){if(!d.bbox||d.bbox.length<4)continue;const[x1,y1,x2,y2]=d.bbox;
    const x=ox+x1*kx,y=oy+y1*ky,w=(x2-x1)*kx,h=(y2-y1)*ky;g.strokeStyle='#f2a65a';g.strokeRect(x,y,w,h);
    const t=d.label+(d.conf?' '+Math.round(d.conf*100)+'%':'');g.fillStyle='rgba(20,21,26,.8)';g.fillRect(x,y-15,g.measureText(t).width+8,15);
    g.fillStyle='#f2a65a';g.fillText(t,x+4,y-3)}}
draw();browse();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="team config file (default: the single /config/*.config)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    cfg, path = load_config(args.config)
    missing = [k for k in ("INGRESS_URL", "USERNAME", "PASSWORD", "GPU_BEARER_TOKEN") if not cfg.get(k)]
    print(f"config: {path or '(none found, using environment)'}  team: {cfg.get('USERNAME', '?')}")
    if missing:
        print(f"warning: missing {', '.join(missing)}; those parts of the lab will show errors")
    if not cfg.get("WANDB_API_KEY"):
        print("warning: WANDB_API_KEY not set; the agent panel will be disabled")
    lab = Lab(cfg)
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(lab))
    print(f"Video Model Lab running: http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
