#!/usr/bin/env python3
"""Re-photograph hero products from market walkthrough frames as studio shots (APIMODELS image edit).
Same API flow as catalogue-lab/pipeline/restyle.py; the key is read from harmony/.env and never printed.
usage: python3 restyle_frames.py [model]   -> writes out/<slug>.jpg
"""
import json, os, subprocess, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
API = 'https://api.apimodels.app/v1'
ENV = os.path.join(HERE, '..', '..', 'harmony', '.env')
KEY = next(l.split('=', 1)[1].strip().strip('"') for l in open(ENV) if l.startswith('APIMODELS_API_KEY='))
MODEL = sys.argv[1] if len(sys.argv) > 1 else 'gpt-image-2.5-flare'

HEROES = [  # slug, frame, which item in the frame, set
    ('tiffany_lamp', 'nyc_4', 'the Tiffany-style leaded stained-glass table lamp on the left, with its bronze base', 'vintage'),
    ('diamond_lamps', 'nyc_12', 'the pair of tall diamond-pattern ceramic table lamps with brass fittings in the centre (show both lamps together)', 'vintage'),
    ('bird_lamp', 'nyc_18', 'the hand-painted white porcelain ginger-jar table lamp with a bird and flower motif in the centre', 'vintage'),
    ('black_gold_vase', 'nyc_18', 'the tall black and gold decorated porcelain vase on the right', 'vintage'),
    ('carved_mask', 'nyc_44', 'the dark carved wooden mask lying on the table, centre right', 'vintage'),
    ('carving_set', 'nyc_44', 'the silver carving knife and fork set in its red presentation box, centre left', 'vintage'),
    ('button_tray', 'nyc_1', 'the dark tray holding a large collection of vintage enamel buttons and pins on the left', 'vintage'),
    ('seascape_painting', 'nyc_9', 'the gold-framed seascape oil painting of a beach and sea, hanging at the top centre', 'vintage'),
    ('bronze_figurine', 'nyc_24', 'the dark bronze figurine sculpture of a figure on a stand, centre left', 'vintage'),
    ('rhino_beetle', 'cm_41', 'one framed rhinoceros beetle specimen in a wooden shadow box, the large brown horned beetle at the bottom centre', 'craft'),
    ('beetle_case', 'cm_53', 'the large wooden display case holding a collection of pinned beetles and insects, centre', 'craft'),
    ('morpho_butterfly', 'cm_53', 'the framed iridescent blue morpho butterfly specimen at the top right', 'craft'),
    ('thai_portrait', 'cm_22', 'one framed hand-painted Thai portrait of a woman in traditional dress in a gilded wooden frame, centre right', 'craft'),
    ('resin_domes', 'cm_47', 'the colourful resin dome paperweights with flowers set inside, on the left', 'craft'),
]
FIDELITY = ("Keep the object exactly as it is in the reference: same shape, proportions, material, colour, pattern, finish, "
            "wear and any printed text. Do not redesign, idealise, add or remove parts. Remove everything that is not the item: "
            "other products, tables, tents, price tags, people and hands. If part of it is hidden, complete it plausibly.")
SETS = {
    'vintage': "Photograph it as a high-end vintage and antiques catalogue shot: the single piece on a warm off-white seamless "
               "sweep, soft window light from the left, gentle fill, a soft natural shadow, rich true-to-life colour.",
    'craft': "Photograph it as a high-end craft catalogue shot: the single piece on a warm limestone-and-linen seamless sweep, "
             "soft directional light, gentle fill, a long soft natural shadow, subtle texture detail.",
}


def call(method, path, body=None, timeout=60):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'})
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        return {'http_error': e.code, 'body': e.read().decode()[:500]}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return {'net_error': str(e)}


def upload(path):
    d = call('POST', '/files', {'filename': os.path.basename(path), 'contentType': 'image/jpeg'})
    d = d.get('data', d)
    put = urllib.request.Request(d['uploadUrl'], method='PUT', data=open(path, 'rb').read(), headers={'Content-Type': 'image/jpeg'})
    urllib.request.urlopen(put, timeout=120).read()
    return d['publicUrl']


def run(h):
    slug, frame, what, kind = h
    out = os.path.join(HERE, 'out', slug + '.jpg')
    if os.path.exists(out):
        return slug, 'exists'
    ref = upload(os.path.join(HERE, 'src', frame + '.jpg'))
    prompt = (f"Image 1 is a photo of a market stall. The item for sale is {what}. Re-photograph only that item. "
              + FIDELITY + ' ' + SETS[kind] + " Photorealistic, no text overlays, no watermark.")
    t0 = time.time()
    r = call('POST', '/images/generations', {'model': MODEL, 'prompt': prompt, 'n': 1, 'image_url': ref, 'image_urls': [ref],
                                             'aspect_ratio': '4:5', 'resolution': '1K'})
    tid = (r.get('data') or {}).get('taskId') if isinstance(r.get('data'), dict) else None
    if not tid:
        return slug, f'FAIL submit {json.dumps(r)[:300]}'
    while True:
        time.sleep(5)
        d = call('GET', f'/images/generations?task_id={tid}').get('data') or {}
        if d.get('state') in ('completed', 'failed', 'fail', 'error'):
            break
        if time.time() - t0 > 600:
            return slug, 'TIMEOUT'
    if d.get('state') != 'completed':
        return slug, f"FAIL {d.get('failCode')} {d.get('failMsg')}"
    urls = json.loads(d.get('resultJson') or '{}').get('resultUrls') or []
    if not urls:
        return slug, 'FAIL no url'
    subprocess.run(['curl', '-s', '-L', '-A', 'Mozilla/5.0', '-o', out, urls[0]], check=True)
    return slug, f'ok {time.time() - t0:.0f}s ${d.get("credits")}'


os.makedirs(os.path.join(HERE, 'out'), exist_ok=True)
with ThreadPoolExecutor(7) as ex:
    for slug, msg in ex.map(run, HEROES):
        print(MODEL, slug, msg, flush=True)
