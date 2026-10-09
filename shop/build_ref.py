#!/usr/bin/env python3
"""Build ./data/bountiful_ref.json: our own Bountiful catalogue for the same clip windows the VAST stack saw."""
import json, os, shutil
LAB = "/Users/alchemist/Projects/founder/catalogue-lab"
WINDOWS = [  # (shop json, market key, clip, window start, window end)
    ("kerobokan-furniture-street", "kerobokan", "Kerobokan furniture street", 140, 170),
    ("ban-tawai-craft-village", "bantawai", "Ban Tawai handicraft village", 415, 475),
]
out = {"markets": []}
for sid, key, title, a, b in WINDOWS:
    d = json.load(open(f"{LAB}/data/{sid}.json"))
    items = []
    for it in d["items"]:
        sec = int(it["id"].split("/")[1].split("_")[0])
        if not (a <= sec <= b):
            continue
        t = float(it.get("seen_at_s") or sec)
        img = None
        for k in ("photo", "studio", "image"):
            p = it.get(k)
            if p and os.path.exists(f"{LAB}/web/public{p}"):
                img = f"{LAB}/web/public{p}"
                break
        rel = None
        if img:
            rel = f"media/ref/{key}_{os.path.basename(img)}"
            shutil.copyfile(img, rel)
        price = it.get("price") or {}
        items.append({"id": it["id"], "name": it.get("name", ""), "category": it.get("category", ""),
                      "material": it.get("material", ""), "colour": it.get("colour", ""),
                      "description": it.get("description", ""), "tags": it.get("tags", []),
                      "price": price, "offset_sec": round(max(0.0, t - a), 1), "image": rel})
    items.sort(key=lambda x: x["offset_sec"])
    out["markets"].append({"key": key, "shop": title, "window": [a, b], "items": items})
json.dump(out, open("data/bountiful_ref.json", "w"), indent=1)
print({m["key"]: len(m["items"]) for m in out["markets"]}, "images:", sum(1 for m in out["markets"] for i in m["items"] if i["image"]))
