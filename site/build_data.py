#!/usr/bin/env python3
"""Turn the VAST catalogue (transcribed from the workshop VM) into the public site's data:
catalog.json, one evidence still per item (from the same local clip the pipeline ingested) and a
YouTube link at the exact second. Then re-photograph every item as a studio shot (APIMODELS GPT Image),
reusing the 11 hero shots already made. Key read from harmony/.env, never printed."""
import json, os, subprocess, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
VB = os.path.dirname(HERE)
MARKETS = {  # code: (slug prefix, key, clip the pipeline ingested, youtube id, offset of the clip in the YouTube video)
    'K': ('kerobokan-furniture-street', 'kerobokan', f'{VB}/bountiful/kerobokan_140-170.mp4', 'NKAJzTRz-Yc', 140),
    'B': ('ban-tawai-handicraft-village', 'bantawai', f'{VB}/markets/th_bantawai_crafts.mp4', 'x42ZJ4_75l4', 415),
    'C': ('chelsea-flea', 'chelsea', f'{VB}/markets/nyc_chelsea_up.mp4', '-wbtsn3MKJ8', 60),
    'M': ('chiang-mai-night-market', 'chiangmai', f'{VB}/markets/th_chiangmai_market_up.mp4', 'hiotLNcEQzo', 290),
}
SKIP = {'M016', 'M021', 'K004'}  # price tags, calculator, bananas: not for sale
HEROES = {'C002': 'button_tray', 'C005': 'diamond_lamps', 'C010': 'black_gold_vase', 'C015': 'bronze_figurine',
          'C022': 'carving_set', 'C026': 'carved_mask', 'M010': 'thai_portrait', 'M017': 'rhino_beetle',
          'M019': 'beetle_case', 'M022': 'morpho_butterfly', 'M020': 'resin_domes'}
BAND = {'budg': 'budget', 'mid': 'mid', 'prem': 'premium'}

items = []
for line in open(f'{HERE}/catalogue.txt'):
    code, first, band, cat, name, mat, col = (line.rstrip('\n').split('|') + [''] * 7)[:7]
    if not code or code in SKIP:
        continue
    slug, key, clip, yt, off = MARKETS[code[0]]
    iid = f'{slug}-{code[1:]}'
    t = int(first)
    items.append({'id': iid, 'code': code, 'market': key, 'first': t, 'last': t + 5, 'band': BAND.get(band, band),
                  'category': cat, 'name': name, 'material': mat, 'colour': col,
                  'youtube': f'https://www.youtube.com/watch?v={yt}&t={off + t}s', 'yt_at': off + t,
                  'still': f'media/stills/{iid}.jpg', 'photo': f'media/photos/{iid}.jpg'})

os.makedirs(f'{HERE}/media/stills', exist_ok=True)
os.makedirs(f'{HERE}/media/photos', exist_ok=True)
os.makedirs(f'{HERE}/work/frames', exist_ok=True)
for it in items:
    clip = MARKETS[it['code'][0]][2]
    full = f"{HERE}/work/frames/{it['id']}.jpg"
    if not os.path.exists(full):
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-ss', str(it['first'] + 2.5), '-i', clip, '-frames:v', '1', '-q:v', '2', full], check=True)
    still = f"{HERE}/{it['still']}"
    if not os.path.exists(still):
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', full, '-vf', 'scale=640:-2', '-q:v', '4', still], check=True)
    hero = HEROES.get(it['code'])
    if hero and not os.path.exists(f"{HERE}/{it['photo']}"):
        subprocess.run(['sips', '-Z', '1100', f'{VB}/restyle/out/{hero}.jpg', '--out', f"{HERE}/{it['photo']}"], capture_output=True)
json.dump({'items': items}, open(f'{HERE}/catalog.json', 'w'), indent=1)
print(f'{len(items)} items, stills done')
if '--no-restyle' in sys.argv:
    sys.exit()

API = 'https://api.apimodels.app/v1'
KEY = next(l.split('=', 1)[1].strip().strip('"') for l in open(f'{VB}/../harmony/.env') if l.startswith('APIMODELS_API_KEY='))
SET = {'kerobokan': 'furniture', 'bantawai': 'craft', 'chelsea': 'vintage', 'chiangmai': 'craft'}
SETS = {
    'furniture': "Photograph it as a furniture catalogue hero: the single piece on a warm off-white seamless studio sweep, large "
                 "softbox key light, soft fill, grounded contact shadow, true-to-life colour, three-quarter view if the reference allows.",
    'craft': "Photograph it as a high-end craft catalogue shot: the single piece on a warm limestone-and-linen seamless sweep, soft "
             "directional light, gentle fill, a long soft natural shadow, subtle texture detail.",
    'vintage': "Photograph it as a high-end vintage and antiques catalogue shot: the single piece on a warm off-white seamless sweep, "
               "soft window light from the left, gentle fill, a soft natural shadow, rich true-to-life colour.",
}
FIDELITY = ("Keep the object exactly as it is in the reference: same shape, proportions, material, colour, pattern, finish, wear and "
            "any printed text. Do not redesign, idealise, add or remove parts. Remove everything that is not the item: other products, "
            "tables, shelves, tents, price tags, people and hands. If part of it is hidden, complete it plausibly.")


def call(method, path, body=None, timeout=60):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': 'Bearer ' + KEY, 'Content-Type': 'application/json'})
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        return {'http_error': e.code, 'body': e.read().decode()[:300]}
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return {'net_error': str(e)}


def upload(path):
    d = call('POST', '/files', {'filename': os.path.basename(path), 'contentType': 'image/jpeg'})
    d = d.get('data', d)
    urllib.request.urlopen(urllib.request.Request(d['uploadUrl'], method='PUT', data=open(path, 'rb').read(),
                                                  headers={'Content-Type': 'image/jpeg'}), timeout=120).read()
    return d['publicUrl']


def run(it):
    out = f"{HERE}/{it['photo']}"
    if os.path.exists(out):
        return it['code'], 'exists'
    for attempt in range(3):
        try:
            ref = upload(f"{HERE}/work/frames/{it['id']}.jpg")
            what = f"the {it['name'].lower()}" + (f" ({', '.join(x for x in (it['material'], it['colour']) if x)})" if it['material'] or it['colour'] else '')
            prompt = (f"Image 1 is a photo of a market stall. The item for sale is {what}. Re-photograph only that item. "
                      + FIDELITY + ' ' + SETS[SET[it['market']]] + " Photorealistic, no text overlays, no watermark.")
            r = call('POST', '/images/generations', {'model': 'gpt-image-2.5-flare', 'prompt': prompt, 'n': 1, 'image_url': ref,
                                                     'image_urls': [ref], 'aspect_ratio': '4:5', 'resolution': '1K'})
            tid = (r.get('data') or {}).get('taskId') if isinstance(r.get('data'), dict) else None
            if not tid:
                time.sleep(10); continue
            t0 = time.time()
            while time.time() - t0 < 300:
                time.sleep(5)
                d = call('GET', f'/images/generations?task_id={tid}').get('data') or {}
                if d.get('state') in ('completed', 'failed', 'fail', 'error'):
                    break
            urls = json.loads(d.get('resultJson') or '{}').get('resultUrls') or [] if d.get('state') == 'completed' else []
            if urls:
                subprocess.run(['curl', '-s', '-L', '-A', 'Mozilla/5.0', '-o', out, urls[0]], check=True)
                return it['code'], f'ok {time.time() - t0:.0f}s'
        except Exception as e:  # keep going; the site falls back to the still
            err = str(e)[:80]
        time.sleep(5)
    return it['code'], 'FAIL (site will use the still)'


todo = [it for it in items if not os.path.exists(f"{HERE}/{it['photo']}")]
print(f'restyling {len(todo)} items', flush=True)
with ThreadPoolExecutor(10) as ex:
    for code, msg in ex.map(run, todo):
        print(code, msg, flush=True)
