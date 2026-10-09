#!/usr/bin/env python3
"""Find each catalogue item in its market frame with YOLO-World (open vocabulary, prompted with the item's
name) and draw a circle around it on the evidence still. Run with catalogue-lab/.venv/bin/python."""
import json, os, re
from ultralytics import YOLO
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
model = YOLO('/Users/alchemist/Projects/founder/catalogue-lab/yolov8x-worldv2.pt')
COLOURS = {'black', 'white', 'brown', 'red', 'blue', 'green', 'yellow', 'gold', 'golden', 'gray', 'grey', 'natural', 'tan',
           'light', 'dark', 'multicolored', 'multicoloured', 'colored', 'coloured', 'pink', 'orange', 'navy', 'cream', 'silver',
           'purple', 'smooth', 'simple', 'large', 'small', 'medium', 'and', 'with', 'of', 'gold-colored'}


def prompts(name):
    n = name.lower().replace('-', ' ')
    words = [w for w in re.findall(r'[a-z]+', n)]
    core = [w for w in words if w not in COLOURS]
    head = ' '.join(core[-2:]) if len(core) >= 2 else ' '.join(core)
    last = core[-1] if core else n
    last = last[:-1] if last.endswith('s') and not last.endswith('ss') else last
    return list(dict.fromkeys([n, ' '.join(core), head, last]))


d = json.load(open(f'{HERE}/catalog.json'))
found = 0
for it in d['items']:
    src = f"{HERE}/work/frames/{it['id']}.jpg"
    im = Image.open(src).convert('RGB')
    W, H = im.size
    model.set_classes(prompts(it['name']))
    r = model.predict(src, conf=0.03, iou=0.5, imgsz=960, verbose=False, device='cpu', max_det=8)[0]
    best = None
    for b, c in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist()):
        x1, y1, x2, y2 = b
        area = (x2 - x1) * (y2 - y1) / (W * H)
        if area > 0.85 or area < 0.002:
            continue
        if best is None or c > best[1]:
            best = (b, c)
    it.pop('circle', None)
    if best and best[1] >= 0.2:
        (x1, y1, x2, y2), c = best
        px, py = (x2 - x1) * 0.14 + 6, (y2 - y1) * 0.14 + 6
        box = [max(2, x1 - px), max(2, y1 - py), min(W - 3, x2 + px), min(H - 3, y2 + py)]
        dr = ImageDraw.Draw(im)
        w = max(3, round(H / 110))
        dr.ellipse([box[0] - 1, box[1] - 1, box[2] + 1, box[3] + 1], outline=(30, 22, 14), width=w + 3)
        dr.ellipse(box, outline=(242, 166, 90), width=w)
        it['circle'] = {'conf': round(c, 3), 'box': [round(v / s, 4) for v, s in zip(box, (W, H, W, H))]}
        found += 1
    im.save(f"{HERE}/media/stills_hd/{it['id']}.jpg", quality=88)
    im.copy().resize((640, round(640 * H / W))).save(f"{HERE}/{it['still']}", quality=80)
json.dump(d, open(f'{HERE}/catalog.json', 'w'), indent=1)
print(f'circled {found} of {len(d["items"])}')
