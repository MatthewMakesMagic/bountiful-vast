# Bountiful on VAST: every market, searchable

**Live shop: https://bountiful-vast.vercel.app**

**Film a market once. Every one-of-a-kind item in it becomes a searchable catalogue entry.**

The best things in the world are sold in places with no online store: a carver's stall in Ban Tawai, a
furniture street in Kerobokan, a beetle-specimen table at the Chiang Mai Sunday market, a vintage-lamp
dealer at Chelsea Flea. Bountiful turns a phone walkthrough of those places into a catalogue you can search
("red and gold lacquer lamp", "framed beetle", "black leather armchair") and jump straight to the moment
it appears on camera.

Built in one day at the VAST Builders Challenge NYC (Oct 9, 2026) on the event stack:

| Layer | What we used it for |
|---|---|
| **VAST AI OS: S3 + VastDB** | Walkthrough videos land in the team's VAST S3 bucket; catalogue items, 5-second "moments" and their vectors are written to VastDB (schema `bountiful`) |
| **NVIDIA Cosmos Reason** on **CoreWeave GPUs** | Watches every 5-second piece of video and lists what is for sale (item, material, colour, details, count) |
| **NVIDIA Cosmos Embed1** | 256-d vectors for every video moment and every catalogue item, so text search lands on the right second of footage |
| **Weights & Biases Inference** (Llama 3.3 70B) | Normalises Cosmos's per-piece notes into clean, deduplicated catalogue entries with a category and price band |
| **YOLO11** | Cheap first pass: object counts per piece (and foot traffic per stall) |

## The pipeline

```
phone walkthrough ──upload──► VAST S3 (team chunks bucket)
                                │
            bountiful_vast.py ingest  (mirrors the VSS DataEngine steps)
                                ├─ cut: ffmpeg, 5 s pieces @ 5 fps
                                ├─ understand: Cosmos Reason, tight catalogue prompt (≤10 items, JSON, no repeats)
                                ├─ catalogue: W&B Llama 3.3 70B merges + dedupes into items
                                ├─ index: Cosmos Embed1 vectors (moments = video, items = text)
                                └─ store: VastDB  bountiful.items / bountiful.moments / bountiful.runs
                                │
            bountiful_vast.py search "framed beetle"  /  serve (search page with the matching clip)
```

In production each step is a VAST DataEngine function on the same S3 trigger as the VSS blueprint
(segmenter → reasoner → embedder → writer). On build day the event's ingest trigger was down, so we ran the
identical steps as a client against the same S3, GPUs and VastDB.

## Measured today (shared event GPUs, ~50 teams)

| Market | Video | Pieces | Catalogue items | Compute |
|---|---|---|---|---|
| Kerobokan furniture street, Bali | 30 s | 6 | 20 | 49.8 s (Cosmos 19 s, W&B 26 s, Embed 4 s) |
| Ban Tawai handicraft village, Chiang Mai | 60 s | 12 | 27 | 92.4 s |

- Cosmos Reason: **2.5 to 4 s per 5-second piece** with the tight prompt (a loose prompt looped and listed
  "wooden chair" 20 times; the JSON prompt + dedupe fixed it).
- About **100 s of compute per minute of video on one worker**. Pieces are independent, so it scales
  linearly: 1,000 shops filmed for 5 minutes each (83 hours of video) is ~2.8 hours on 50 parallel workers.
- The event's pre-built VSS index answers a text query over 3,537 segments in **1.2 s**
  (embedding 0.26 s + VastDB search 1.2 s), which is the search layer this plugs into.

## Run it (on the workshop VM)

```bash
pip install --user vastdb pyarrow            # once
python3 bountiful_vast.py ingest s3://<team>-vss-chunks/<key>.mp4 --shop "Chelsea Flea" --location "New York City"
python3 bountiful_vast.py search "framed beetle specimen" --k 8
python3 bountiful_vast.py serve --port 8766  # search page, open in the VM browser
```

Credentials are read from `/config/<team>.config` on the VM and never printed or stored.

Also here: `ycount.py`, the tool we used to check the stack first (counts any YOLO class across the
pre-indexed corpus, re-runs YOLO live to verify stored counts, replays a video as a live feed, and asks
Cosmos about the top moments).

## Footage

Walkthrough clips are short excerpts used for this non-commercial hackathon demo only and are not
redistributed in this repo.
