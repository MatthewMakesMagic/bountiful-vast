/* Bountiful on VAST: static shop over the VAST pipeline output (data/vast_catalog.json) plus a side-by-side
   comparison with our own Bountiful catalogue (data/bountiful_ref.json). No build step. */
(function () {
  'use strict';
  const $ = (s, r) => (r || document).querySelector(s);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const mmss = s => { s = Math.max(0, Math.floor(+s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  const cap = s => { s = String(s || '').trim(); return s ? s[0].toUpperCase() + s.slice(1) : ''; };

  const MARKETS = [
    { key: 'kerobokan', label: 'Bali', name: 'Kerobokan furniture street', place: 'Bali, Indonesia', clip: 'media/clips/kerobokan.mp4', words: ['kerobokan', 'bali'] },
    { key: 'bantawai', label: 'Ban Tawai', name: 'Ban Tawai handicraft village', place: 'Chiang Mai, Thailand', clip: 'media/clips/bantawai.mp4', words: ['ban tawai', 'bantawai', 'tawai'] },
    { key: 'chiangmai', label: 'Chiang Mai', name: 'Chiang Mai night market', place: 'Chiang Mai, Thailand', clip: 'media/clips/chiangmai.mp4', words: ['chiang mai night', 'chiangmai', 'sunday walking', 'night market'] },
    { key: 'chelsea', label: 'New York', name: 'Chelsea Flea', place: 'New York City', clip: 'media/clips/chelsea.mp4', words: ['chelsea', 'new york', 'nyc'] },
  ];
  const MEASURED = { kerobokan: 43.7, bantawai: 92.4 }; // seconds, measured on the event stack today
  const marketOf = name => {
    const n = String(name || '').toLowerCase().replace(/-/g, ' ');
    return MARKETS.find(m => m.words.some(w => n.includes(w))) || null;
  };

  // ---------- data ----------
  const state = { items: [], runs: {}, ref: null, placeholder: false };

  function shopsOf(data) {
    if (!data) return [];
    if (Array.isArray(data)) return data;
    if (Array.isArray(data.shops)) return data.shops;
    return [data];
  }
  function normItem(it, run) {
    const shop = it.shop || run.shop || run.shop_slug || '';
    const m = marketOf(shop) || marketOf(it.shop_slug) || marketOf(run.location);
    let pieces = it.pieces;
    if (typeof pieces === 'string') { try { pieces = JSON.parse(pieces); } catch (e) { pieces = []; } }
    const first = it.first_sec != null ? +it.first_sec : (Array.isArray(pieces) && pieces.length ? Math.min(...pieces) * 5 : 0);
    return {
      id: String(it.id || `${m ? m.key : 'item'}-${Math.random().toString(36).slice(2, 7)}`),
      name: cap(it.name || 'Untitled piece'), category: it.category || 'Other', material: it.material || '',
      colour: it.colour || it.color || '', description: it.description || '', band: (it.est_price_band || '').toLowerCase(),
      first: first, last: it.last_sec != null ? +it.last_sec : first + 5, market: m ? m.key : 'other',
      shop: shop, location: it.location || run.location || (m ? m.place : ''),
    };
  }
  async function load() {
    const [vast, ref] = await Promise.all([
      fetch('data/vast_catalog.json').then(r => r.ok ? r.json() : null).catch(() => null),
      fetch('data/bountiful_ref.json').then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    state.placeholder = !!(vast && vast.placeholder);
    for (const s of shopsOf(vast)) {
      const run = s.run || {};
      const items = (s.items || []).map(it => normItem(it, run));
      const m = marketOf(run.shop || run.shop_slug || (items[0] && items[0].shop));
      let timings = s.timings || null;
      if (!timings && run.timings_json) { try { timings = JSON.parse(run.timings_json); } catch (e) { timings = null; } }
      const total = timings ? Object.values(timings).reduce((a, b) => a + (+b || 0), 0) : null;
      if (m) state.runs[m.key] = { total, timings, video: run.video_sec, perMin: run.compute_s_per_video_min, created: run.created };
      state.items.push(...items);
    }
    state.ref = ref;
    applyPhotos();
  }

  // ---------- studio photos: restyled heroes (GPT Image) + Bountiful's photos for matched pieces ----------
  const HEROES = [
    ['chelsea', /tiffany|stained|leaded/i, 'tiffany_lamp'], ['chelsea', /ceramic.*lamp|lamp.*ceramic|diamond/i, 'diamond_lamps'],
    ['chelsea', /porcelain.*lamp|bird|floral lamp|ginger/i, 'bird_lamp'], ['chelsea', /(black|gold)[^,]*vase|vase[^,]*(black|gold)/i, 'black_gold_vase'],
    ['chelsea', /mask/i, 'carved_mask'], ['chelsea', /carving|knife|fork|silver|cutlery|utensil/i, 'carving_set'],
    ['chelsea', /button|pin|badge|brooch/i, 'button_tray'], ['chelsea', /painting|seascape|landscape|framed art|artwork/i, 'seascape_painting'],
    ['chelsea', /figurine|sculpture|statue|bronze/i, 'bronze_figurine'], ['chelsea', /lamp/i, 'tiffany_lamp'],
    ['chiangmai', /rhino|horned/i, 'rhino_beetle'], ['chiangmai', /beetle|insect|specimen|bug/i, 'beetle_case'],
    ['chiangmai', /beetle|insect|specimen|bug/i, 'rhino_beetle'], ['chiangmai', /butterfl|moth/i, 'morpho_butterfly'],
    ['chiangmai', /portrait|painting|framed|picture/i, 'thai_portrait'], ['chiangmai', /resin|dome|paperweight|flower/i, 'resin_domes'],
  ];
  function applyPhotos() {
    for (const m of (state.ref && state.ref.markets) || []) {
      for (const row of compareMarket(m).rows) if (row.verdict === 'f' && row.vast && !row.vast.photo && row.ref.image) row.vast.photo = row.ref.image;
    }
    const used = new Set();
    for (const [mk, re, slug] of HEROES) {
      if (used.has(slug)) continue;
      const it = state.items.find(i => i.market === mk && !i.photo && re.test([i.name, i.material, i.description].join(' ')));
      if (it) { it.photo = `media/hero/${slug}.jpg`; used.add(slug); }
    }
  }
  const byId = id => state.items.find(i => i.id === id);
  const market = key => MARKETS.find(m => m.key === key);

  // ---------- images: pre-built frame, else grab one from the clip in the browser ----------
  const frameCache = {};
  function grabFrame(clip, t) {
    const k = clip + '@' + t;
    if (frameCache[k]) return frameCache[k];
    frameCache[k] = new Promise(resolve => {
      const v = document.createElement('video');
      v.muted = true; v.preload = 'auto'; v.src = clip;
      v.addEventListener('loadedmetadata', () => { v.currentTime = Math.min(t, Math.max(0, (v.duration || t) - 0.2)); }, { once: true });
      v.addEventListener('seeked', () => {
        try {
          const c = document.createElement('canvas'); c.width = v.videoWidth; c.height = v.videoHeight;
          c.getContext('2d').drawImage(v, 0, 0); resolve(c.toDataURL('image/jpeg', 0.82));
        } catch (e) { resolve(''); }
      }, { once: true });
      v.addEventListener('error', () => resolve(''), { once: true });
    });
    return frameCache[k];
  }
  window.__bfFallback = async function (img) {
    if (img.dataset.tried) return; img.dataset.tried = '1';
    const url = await grabFrame(img.dataset.clip, +img.dataset.t || 0);
    if (url) img.src = url;
  };
  const imgTag = (it, cls) => {
    const m = market(it.market);
    if (it.photo) return `<img class="${cls || ''} is-photo" src="${esc(it.photo)}" alt="${esc(it.name)}" loading="lazy"
      onerror="this.onerror=null;this.src='media/frames/${encodeURIComponent(it.id)}.jpg'" />`;
    return `<img class="${cls || ''}" src="media/frames/${encodeURIComponent(it.id)}.jpg" alt="${esc(it.name)}" loading="lazy"
      data-clip="${m ? m.clip : ''}" data-t="${(it.first + 2.5).toFixed(1)}" onerror="__bfFallback(this)" />`;
  };

  // ---------- search ----------
  const SYN = {
    rattan: 'weave', wicker: 'weave', woven: 'weave', cane: 'weave', rope: 'weave', seagrass: 'weave', weave: 'weave',
    statue: 'figure', statues: 'figure', figure: 'figure', figures: 'figure', figurine: 'figure', figurines: 'figure', sculpture: 'figure',
    sculptures: 'figure', carving: 'figure', carvings: 'figure', carved: 'figure', deity: 'figure', idol: 'figure', doll: 'doll', dolls: 'doll', buddha: 'figure',
    chair: 'chair', chairs: 'chair', armchair: 'chair', armchairs: 'chair', seat: 'chair', seating: 'chair', lounge: 'chair', lounger: 'chair',
    stool: 'stool', stools: 'stool', bench: 'bench', benches: 'bench', sofa: 'sofa', couch: 'sofa', daybed: 'sofa',
    mirror: 'mirror', mirrors: 'mirror', table: 'table', tables: 'table', desk: 'table', sideboard: 'storage', armoire: 'storage', cabinet: 'storage',
    wardrobe: 'storage', storage: 'storage', elephant: 'elephant', elephants: 'elephant', lacquer: 'lacquer', lacquered: 'lacquer',
    lamp: 'lamp', lamps: 'lamp', lantern: 'lamp', lighting: 'lamp', rooster: 'rooster', chicken: 'rooster', pedestal: 'pedestal', stand: 'pedestal',
    offering: 'pedestal', mask: 'mask', masks: 'mask', horse: 'horse', urn: 'urn', vase: 'vase', vases: 'vase', bowl: 'bowl', bowls: 'bowl',
    basket: 'basket', baskets: 'basket', frame: 'frame', framed: 'frame', teak: 'wood', wood: 'wood', wooden: 'wood', timber: 'wood', suar: 'wood',
    painted: 'painted', colorful: 'painted', colourful: 'painted', multicolored: 'painted', multicoloured: 'painted', hand: 'painted',
    leather: 'leather', stone: 'stone', clay: 'clay', ceramic: 'clay', terracotta: 'clay', metal: 'metal', brass: 'metal', iron: 'metal',
    black: 'black', gold: 'gold', golden: 'gold', red: 'red', white: 'white', grey: 'grey', gray: 'grey', green: 'green', blue: 'blue',
  };
  const TYPES = new Set(['figure', 'chair', 'stool', 'bench', 'sofa', 'mirror', 'table', 'storage', 'elephant', 'lamp', 'rooster', 'pedestal',
    'mask', 'horse', 'urn', 'vase', 'bowl', 'basket', 'frog', 'doll']);
  const GENERIC = new Set(['figure']);
  const STOP = new Set(['and', 'with', 'the', 'a', 'an', 'of', 'in', 'on', 'for', 'natural', 'tone', 'light', 'dark', 'small', 'large', 'big',
    'other', 'decor', 'objects', 'art', 'small', 'piece', 'pieces', 'set', 'pair']);
  const toks = s => String(s || '').toLowerCase().replace(/[^a-z0-9 ]+/g, ' ').split(/\s+/).filter(w => w && !STOP.has(w)).map(w => SYN[w] || w.replace(/s$/, ''));

  function searchScore(it, q) {
    const qs = toks(q); if (!qs.length) return 1;
    const hay = toks([it.name, it.material, it.colour, it.category, it.description, it.shop, it.location].join(' '));
    const raw = [it.name, it.material, it.colour, it.category, it.description].join(' ').toLowerCase();
    let s = 0;
    for (const w of qs) {
      if (hay.includes(w)) s += TYPES.has(w) ? 3 : 2;
      else if (raw.includes(w)) s += 1;
      else if (w.length > 3 && hay.some(h => h.startsWith(w.slice(0, 4)))) s += 0.5;
    }
    return s;
  }

  // ---------- comparison ----------
  // Item type comes from names only (a colour like "brown frame" or a shared category must not make a mirror
  // match a chair). Material, colour, descriptors and category count as attributes.
  function matchScore(ref, v) {
    const typesA = new Set(toks([ref.name, (ref.tags || []).join(' ')].join(' ')).filter(w => TYPES.has(w)));
    const typesB = new Set(toks(v.name).filter(w => TYPES.has(w)));
    const attrA = new Set(toks([ref.name, ref.material, ref.colour].join(' ')).filter(w => !TYPES.has(w)));
    const attrB = new Set(toks([v.name, v.material, v.colour].join(' ')).filter(w => !TYPES.has(w)));
    let type = 0, attr = 0;
    for (const w of typesA) if (typesB.has(w)) type++;
    for (const w of attrA) if (attrB.has(w)) attr++;
    if (ref.category && v.category && ref.category.toLowerCase() === v.category.toLowerCase()) attr++;
    let score = type ? 3 + (type - 1) + Math.min(attr, 3) : 0;
    const near = Math.abs((+ref.offset_sec || 0) - (v.first + 2.5)) <= 7;
    if (near && score > 0) score += 1;
    // "figure" is generic: a rooster figure that only matches an elephant figure on "figure" is Partial at best
    const specA = [...typesA].filter(w => !GENERIC.has(w));
    if (score && specA.length && !specA.some(w => typesB.has(w))) score = Math.min(score, 4);
    return { score, type, attr, near };
  }
  function compareMarket(m) {
    const ref = m.items || [];
    const vast = state.items.filter(i => i.market === m.key);
    const pairs = [];
    ref.forEach((r, ri) => vast.forEach((v, vi) => { const s = matchScore(r, v); if (s.type > 0) pairs.push({ ri, vi, ...s }); }));
    pairs.sort((x, y) => y.score - x.score);
    const best = new Array(ref.length).fill(null), usedV = new Set();
    for (const p of pairs) if (!best[p.ri] && !usedV.has(p.vi)) { best[p.ri] = p; usedV.add(p.vi); }
    for (const p of pairs) if (!best[p.ri]) best[p.ri] = p; // allow sharing (one VAST row for "4 chairs")
    const rows = ref.map((r, i) => {
      const p = best[i];
      const verdict = !p ? 'm' : (p.score >= 5 ? 'f' : (p.score >= 3 ? 'p' : 'm'));
      return { ref: r, vast: p && verdict !== 'm' ? vast[p.vi] : null, p, verdict };
    });
    const matchedV = new Set(rows.filter(r => r.vast).map(r => r.vast.id));
    const extra = vast.filter(v => !matchedV.has(v.id));
    return { rows, extra, vast };
  }

  // ---------- views ----------
  const view = () => $('#view');
  function setNav(k) { document.querySelectorAll('[data-nav]').forEach(a => a.classList.toggle('is-on', a.dataset.nav === k)); }

  function priceLabel(band) {
    const b = (band || '').toLowerCase();
    if (b.includes('budget') || b.includes('low')) return { short: '$', long: '$  ·  under $50' };
    if (b.includes('premium') || b.includes('high') || b.includes('lux')) return { short: '$$$', long: '$$$  ·  $300 and up' };
    if (b.includes('mid')) return { short: '$$', long: '$$  ·  $50 to $300' };
    return { short: '', long: 'Price on request' };
  }

  function card(it) {
    const m = market(it.market);
    return `<a class="st-card" href="#/item/${encodeURIComponent(it.id)}">
      <div class="st-card-img">${imgTag(it)}<i>${esc(m ? m.label : '')}</i></div>
      <div class="st-card-name">${esc(it.name)}</div>
      <div class="st-card-sub"><span>${esc([it.material, it.colour].filter(Boolean).join(', '))}</span><span class="st-band">${esc(priceLabel(it.band).short)}</span></div>
    </a>`;
  }

  function renderShop(params) {
    const q = params.get('q') || '', mk = params.get('m') || '';
    setNav(mk ? 'm-' + mk : 'shop');
    $('#q').value = q;
    let list = state.items.slice();
    if (mk) list = list.filter(i => i.market === mk);
    if (q) list = list.map(i => [i, searchScore(i, q)]).filter(x => x[1] >= 1).sort((a, b) => b[1] - a[1]).map(x => x[0]);
    else list.sort((a, b) => ((b.photo ? 1 : 0) - (a.photo ? 1 : 0)) || (a.market === b.market ? a.first - b.first : MARKETS.findIndex(m => m.key === a.market) - MARKETS.findIndex(m => m.key === b.market)));
    const counts = Object.fromEntries(MARKETS.map(m => [m.key, state.items.filter(i => i.market === m.key).length]));
    const heroWant = [/leather armchair/i, /ornamental lamp|lamp/i, /stone statue|statue/i, /carved wooden elephant|elephant/i, /armoire/i];
    const heroPick = state.items.filter(i => i.photo && /hero/.test(i.photo)).slice(0, 3); for (const re of heroWant) { const it = state.items.find(i => re.test(i.name) && !heroPick.includes(i)); if (it && heroPick.length < 3) heroPick.push(it); }
    for (const it of state.items) { if (heroPick.length >= 3) break; if (!heroPick.includes(it)) heroPick.push(it); }
    const tab = (key, label, n) => `<button class="${mk === key ? 'is-on' : ''}" data-m="${key}">${esc(label)}<span>${n}</span></button>`;
    const pending = mk && !counts[mk] ? market(mk) : null;
    view().innerHTML = `<main class="st-page">
      ${q || mk ? '' : `<section class="st-hero"><div class="st-wrap st-hero-in">
        <div><div class="st-kicker">Bountiful</div>
          <h1>Things you can't buy online.</h1>
          <p class="st-lead">${state.items.length} one-of-a-kind pieces from ${Object.values(counts).filter(Boolean).length} markets with no online store, from a Chelsea Flea vintage table to a Chiang Mai beetle-specimen stall.</p>
          <p class="st-body">Every piece was filmed once, catalogued by NVIDIA Cosmos on CoreWeave GPUs and stored in VAST. Tap any piece to see the exact second it appears on camera.</p>
          <div class="st-actions"><a class="st-btn" href="#/?m=chelsea">Shop New York</a><a class="st-link" href="#/?m=chiangmai">Shop Chiang Mai</a></div>
        </div>
        <div class="st-hero-art">${heroPick.map((it, i) => `<a class="st-hero-${'abc'[i]}" href="#/item/${encodeURIComponent(it.id)}">${imgTag(it)}<span>${esc(it.name)}</span></a>`).join('')}</div>
      </div></section>`}
      <div class="st-filter"><div class="st-wrap st-filter-in">
        <div class="st-tabs" id="tabs">${tab('', 'All markets', state.items.length)}${MARKETS.map(m => tab(m.key, m.label, counts[m.key] || 0)).join('')}</div>
        <div class="st-count">${q ? `${list.length} results for "${esc(q)}"` : `${list.length} pieces`}</div>
      </div></div>
      <section class="st-wrap st-sec">
        ${pending ? `<div class="st-processing"><video src="${pending.clip}" muted autoplay loop playsinline></video><div><div class="st-kicker">${esc(pending.place)}</div><p style="margin-top:8px">${esc(pending.name)} is filmed and stored in VAST. Its catalogue is still being written by the pipeline.</p></div></div>` : ''}
        ${list.length ? `<div class="st-grid">${list.map(card).join('')}</div>` : (pending ? '' : `<p class="st-empty">Nothing matches "${esc(q)}" yet. Try "elephant", "leather", "lamp" or "rattan".</p>`)}
        ${state.placeholder ? `<p class="st-note">Showing the catalogue transcribed from today's runs. The live pipeline output replaces it automatically.</p>` : ''}
      </section></main>`;
    $('#tabs').addEventListener('click', e => {
      const b = e.target.closest('button'); if (!b) return;
      const p = new URLSearchParams(); if (b.dataset.m) p.set('m', b.dataset.m); if (q) p.set('q', q);
      location.hash = '#/' + (p.toString() ? '?' + p : '');
    });
  }

  function renderItem(id) {
    setNav('shop');
    const it = byId(id);
    if (!it) { view().innerHTML = `<main class="st-page st-wrap"><p class="st-empty">That piece is not in the catalogue. <a class="st-link" href="#/">Back to the shop</a></p></main>`; return; }
    const m = market(it.market) || { label: '', name: it.shop, place: it.location, clip: '' };
    const more = state.items.filter(i => i.market === it.market && i.id !== it.id).slice(0, 8);
    const run = state.runs[it.market];
    view().innerHTML = `<main class="st-page"><div class="st-wrap">
      <div class="st-crumbs"><a href="#/">Shop</a><span>/</span><a href="#/?m=${it.market}">${esc(m.name)}</a><span>/</span><span>${esc(it.name)}</span></div>
      <div class="st-item">
        <div class="st-media">
          <div class="st-media-main"><video id="clip" src="${m.clip}" muted playsinline controls></video></div>
          <div class="st-media-bar"><span>Seen at ${mmss(it.first)} in the walkthrough of ${esc(m.name)}</span><button id="replay">Replay the moment</button></div>
          <div class="st-frame">${imgTag(it)}</div>
        </div>
        <div class="st-info">
          <div class="st-kicker">${esc(m.place || it.location)}</div>
          <h1>${esc(it.name)}</h1>
          <div class="st-price">${esc(priceLabel(it.band).long)}<span class="st-price-note">${it.band ? 'Estimate' : ''}</span></div>
          <div class="st-avail"><i></i>One of a kind, seen at the stall</div>
          <div class="st-actions" style="margin-top:22px"><button class="st-btn" id="reserve">Reserve this piece</button><a class="st-link" href="#/?m=${it.market}">More from this stall</a></div>
          <p class="st-reserved" id="reserved" hidden>Reserved. A local scout will check it is still on the table and send you a photo before you pay.</p>
          ${it.description ? `<p class="st-desc">${esc(it.description)}</p>` : ''}
          <dl class="st-specs">
            <div><dt>Category</dt><dd>${esc(it.category)}</dd></div>
            <div><dt>Material</dt><dd>${esc(it.material || 'Not recorded')}</dd></div>
            <div><dt>Colour</dt><dd>${esc(it.colour || 'Not recorded')}</dd></div>
            <div><dt>On camera</dt><dd>${mmss(it.first)} to ${mmss(it.last)}</dd></div>
          </dl>
          <p class="st-prov">Catalogued from the walkthrough video by NVIDIA Cosmos Reason on CoreWeave GPUs, written up by Llama 3.3 70B on W&amp;B Inference, indexed with Cosmos Embed and stored in VastDB${run && run.total ? `. This market took ${run.total.toFixed(1)} s of compute` : ''}.</p>
        </div>
      </div>
      ${more.length ? `<section class="st-sec"><div class="st-sec-head"><h2>More from ${esc(m.name)}</h2><a class="st-link" href="#/?m=${it.market}">All pieces</a></div><div class="st-grid">${more.map(card).join('')}</div></section>` : ''}
    </div></main>`;
    const v = $('#clip');
    const seek = () => { try { v.currentTime = it.first; v.play().catch(() => {}); } catch (e) {} };
    v.addEventListener('loadedmetadata', seek, { once: true });
    v.addEventListener('timeupdate', () => { if (v.currentTime > it.last + 0.5) v.pause(); });
    $('#replay').addEventListener('click', seek);
    $('#reserve').addEventListener('click', e => { e.currentTarget.textContent = 'Reserved'; e.currentTarget.disabled = true; $('#reserved').hidden = false; });
  }

  function renderCompare() {
    setNav('compare');
    const markets = (state.ref && state.ref.markets) || [];
    const blocks = markets.map(rm => {
      const m = market(rm.key) || { name: rm.shop, place: '', clip: '' };
      const { rows, extra, vast } = compareMarket(rm);
      const n = rows.length, f = rows.filter(r => r.verdict === 'f').length, p = rows.filter(r => r.verdict === 'p').length, miss = n - f - p;
      const pct = n ? Math.round(100 * (f + p) / n) : 0;
      const run = state.runs[rm.key];
      const secs = run && run.total ? run.total : MEASURED[rm.key];
      const win = rm.window ? `${mmss(rm.window[0])} to ${mmss(rm.window[1])} of the walkthrough` : '';
      const refCell = r => `<div class="cmp-cell">${r.image ? `<img src="${esc(r.image)}" alt="" loading="lazy" />` : '<img alt="" />'}<div><b>${esc(r.name)}</b><span>${esc([r.material, r.colour].filter(Boolean).join(', '))}</span><span>At ${mmss(r.offset_sec)}${r.price && r.price.amount ? ` · ${esc(r.price.currency || '')} ${Number(r.price.amount).toLocaleString('en')}` : ''}</span></div></div>`;
      const vastCell = row => row.vast
        ? `<a class="cmp-cell" href="#/item/${encodeURIComponent(row.vast.id)}">${imgTag(row.vast)}<div><b>${esc(row.vast.name)}</b><span>${esc([row.vast.material, row.vast.colour].filter(Boolean).join(', '))}</span><span>At ${mmss(row.vast.first)}${row.p && row.p.near ? ', same moment' : ''}</span></div></a>`
        : `<div class="cmp-cell is-none">Not in the VAST catalogue</div>`;
      const label = { f: 'Found', p: 'Partial', m: 'Missed' };
      return `<section class="cmp-market">
        <div class="cmp-sum">
          <div><div class="st-kicker">${esc(m.place || '')}${win ? ` · ${win}` : ''}</div><h2>${esc(m.name)}</h2></div>
          <dl class="cmp-stats">
            <div><dt>VAST found</dt><dd>${f + p} of ${n} (${pct}%)</dd></div>
            <div><dt>Extra items</dt><dd>${extra.length}</dd></div>
            <div><dt>VAST catalogue</dt><dd>${vast.length} items</dd></div>
            <div><dt>Compute</dt><dd>${secs ? secs.toFixed(1) + ' s' : 'n/a'}</dd></div>
          </dl>
        </div>
        <div class="cmp-bar"><b class="f" style="width:${n ? 100 * f / n : 0}%"></b><b class="p" style="width:${n ? 100 * p / n : 0}%"></b><b class="m" style="width:${n ? 100 * miss / n : 0}%"></b></div>
        <div class="cmp-legend"><span><i style="background:var(--leaf)"></i>Found ${f}</span><span><i style="background:var(--teak-2)"></i>Partial ${p}</span><span><i style="background:var(--rust);opacity:.55"></i>Missed ${miss}</span></div>
        <div class="cmp-cols">
          <div class="cmp-colhead">Bountiful pipeline (our catalogue)</div><div class="cmp-colhead" style="text-align:center">Match</div><div class="cmp-colhead">VAST stack (Cosmos + Llama 3.3 on W&amp;B)</div>
          ${rows.map(r => `<div class="cmp-row">${refCell(r.ref)}<div class="cmp-verdict"><span class="pill ${r.verdict}" title="${r.p ? `score ${r.p.score}: ${r.p.type} type, ${r.p.attr} attribute matches${r.p.near ? ', within 7 s' : ''}` : 'no shared item type'}">${label[r.verdict]}</span></div>${vastCell(r)}</div>`).join('')}
        </div>
        ${extra.length ? `<div class="cmp-extra"><h3>Also found by VAST, not in our catalogue</h3><div class="cmp-chips">${extra.map(v => `<a href="#/item/${encodeURIComponent(v.id)}">${esc(v.name)}</a>`).join('')}</div></div>` : ''}
      </section>`;
    }).join('');
    view().innerHTML = `<main class="st-page"><div class="st-wrap">
      <div class="cmp-head"><div class="st-kicker">Catalogue quality</div><h1>Two pipelines, one walkthrough.</h1>
        <p>The same stretch of footage, catalogued twice. Left: our Bountiful pipeline (open-vocabulary detection, CLIP clustering, written up by Claude, re-photographed). Right: the VAST stack built today (NVIDIA Cosmos Reason on CoreWeave GPUs, Llama 3.3 on W&amp;B, stored in VastDB). Matches are scored automatically on item type, material and timing; hover a verdict to see why.</p></div>
      ${blocks || '<p class="st-empty">No reference catalogue loaded.</p>'}
      <p class="st-note">Found means the same kind of object with matching material or colour. Partial means the same kind of object only. Our catalogue keeps only the clearest pieces it can photograph, so VAST listing extra items is expected; the question is whether it finds the pieces a buyer would want.${state.placeholder ? ' VAST results shown are transcribed from today\'s runs until the live output is loaded.' : ''}</p>
    </div></main>`;
  }

  function renderHow() {
    setNav('how');
    const r = state.runs;
    const k = r.kerobokan || {}, b = r.bantawai || {};
    const kt = k.total || MEASURED.kerobokan, bt = b.total || MEASURED.bantawai;
    view().innerHTML = `<main class="st-page how"><div class="st-wrap">
      <div class="st-kicker">How it works</div><h1>Film once. Search forever.</h1>
      <p>A phone walkthrough of a market goes up to VAST. Every 5 seconds of it is watched by NVIDIA Cosmos, turned into catalogue entries by Llama 3.3, given a search fingerprint by Cosmos Embed, and written to VastDB. The same steps run as VAST DataEngine functions on the S3 trigger in production; on build day the event's ingest trigger was down, so we ran them as a client against the same S3, GPUs and VastDB.</p>
      <div class="flow">
        <div><small>1 · VAST S3</small><b>Upload</b><span>The walkthrough lands in the team bucket</span><em>About 5 s to land</em></div>
        <div><small>2 · ffmpeg</small><b>5 s pieces</b><span>Cut at 5 frames per second</span><em>Under 2 s per minute</em></div>
        <div><small>3 · NVIDIA Cosmos Reason</small><b>What is for sale?</b><span>On CoreWeave GPUs, tight JSON prompt, no repeats</span><em>2.5 to 4 s per piece</em></div>
        <div><small>4 · W&amp;B Inference</small><b>Llama 3.3 70B</b><span>Merges sightings into clean, deduplicated entries</span><em>About 20 s per market</em></div>
        <div><small>5 · Cosmos Embed1</small><b>Fingerprints</b><span>256-d vectors for every moment and every item</span><em>3 to 12 s per market</em></div>
        <div><small>6 · VastDB</small><b>Searchable</b><span>bountiful.items, moments and runs</span><em>Under 1 s to write</em></div>
      </div>
      <div class="how-nums">
        <div><b>${kt.toFixed(1)} s</b><span>to catalogue 30 s of the Kerobokan furniture street</span></div>
        <div><b>${bt.toFixed(1)} s</b><span>to catalogue 60 s of the Ban Tawai carving village</span></div>
        <div><b>~100 s</b><span>of compute per minute of video on one worker; pieces are independent, so it scales out</span></div>
        <div><b>2.8 h</b><span>for 1,000 shops filmed for 5 minutes each, on 50 parallel workers</span></div>
      </div>
      <div class="how-stack">
        <div><b>VAST AI OS</b><span>S3 for the footage, VastDB for items, moments and vectors</span></div>
        <div><b>NVIDIA Cosmos</b><span>Reason watches the video, Embed makes it searchable</span></div>
        <div><b>CoreWeave</b><span>The GPUs the Cosmos models run on</span></div>
        <div><b>Weights &amp; Biases</b><span>Serverless Llama 3.3 70B writes the catalogue</span></div>
      </div>
    </div></main>`;
  }

  function route() {
    const h = location.hash || '#/';
    const [path, qs] = h.slice(1).split('?');
    const params = new URLSearchParams(qs || '');
    if (path.startsWith('/item/')) renderItem(decodeURIComponent(path.slice(6)));
    else if (path === '/compare') { location.hash = '#/'; return; }
    else if (path === '/how') renderHow();
    else renderShop(params);
    window.scrollTo({ top: 0 });
  }

  $('#searchform').addEventListener('submit', e => {
    e.preventDefault();
    const q = $('#q').value.trim();
    location.hash = '#/' + (q ? '?q=' + encodeURIComponent(q) : '');
  });

  load().then(() => { addEventListener('hashchange', route); route(); });
})();
