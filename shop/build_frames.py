#!/usr/bin/env python3
"""Extract one JPEG per VAST catalogue item from its market's clip, at the middle of the 5 s piece where the
item was first seen. Reads ./data/vast_catalog.json, writes ./media/frames/<item_id>.jpg.

    python3 build_frames.py            # skips frames that already exist
    python3 build_frames.py --force
"""
import json
import os
import subprocess
import sys

CLIPS = [("kerobokan", "media/clips/kerobokan.mp4"), ("ban tawai", "media/clips/bantawai.mp4"),
         ("bantawai", "media/clips/bantawai.mp4"), ("chelsea", "media/clips/chelsea.mp4"),
         ("chiang mai", "media/clips/chiangmai.mp4"), ("chiangmai", "media/clips/chiangmai.mp4")]


def shops(data):
    if isinstance(data, dict) and "shops" in data:
        return data["shops"]
    if isinstance(data, list):
        return data
    return [data]


def clip_for(name):
    n = (name or "").lower().replace("-", " ")
    for key, path in CLIPS:
        if key in n:
            return path
    return None


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def main():
    force = "--force" in sys.argv
    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)
    data = json.load(open("data/vast_catalog.json"))
    os.makedirs("media/frames", exist_ok=True)
    made = skipped = missing = 0
    for s in shops(data):
        run = s.get("run") or {}
        items = s.get("items") or []
        name = run.get("shop") or (items[0].get("shop") if items else "") or run.get("shop_slug", "")
        clip = clip_for(name) or clip_for(run.get("shop_slug"))
        if not clip or not os.path.exists(clip):
            print(f"no clip for {name!r}, skipping {len(items)} items")
            missing += len(items)
            continue
        dur = duration(clip)
        # items first seen in the same 5 s piece get different moments inside it, so cards don't repeat
        groups = {}
        for it in items:
            groups.setdefault(float(it.get("first_sec") or 0), []).append(it["id"])
        for it in items:
            out = f"media/frames/{it['id']}.jpg"
            if os.path.exists(out) and not force:
                skipped += 1
                continue
            first = float(it.get("first_sec") or 0)
            ids = groups[first]
            k = ids.index(it["id"])
            t = first + (2.5 if len(ids) == 1 else 0.5 + k * 4.0 / (len(ids) - 1))
            if dur:
                t = min(t, max(0.0, dur - 0.2))
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", clip, "-frames:v", "1",
                            "-vf", "scale=960:-2", "-q:v", "3", out], check=False)
            made += os.path.exists(out)
    print(f"frames: {made} made, {skipped} already there, {missing} without a clip")


if __name__ == "__main__":
    main()
