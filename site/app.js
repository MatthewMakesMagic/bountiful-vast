/* Bountiful on VAST: public shop. Catalogue produced on the VAST Builders Challenge stack
   (VAST S3 + VastDB, NVIDIA Cosmos on CoreWeave GPUs, Llama 3.3 on W&B Inference).
   Each piece links to the exact second it appears in the source walkthrough on YouTube. No build step. */
(() => {
  const $ = s => document.querySelector(s);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const mmss = s => { s = Math.max(0, Math.floor(+s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  const MARKETS = [
    { key: 'chelsea', label: 'New York', name: 'Chelsea Flea', place: 'New York City' },
    { key: 'kerobokan', label: 'Bali', name: 'Kerobokan furniture street', place: 'Bali, Indonesia' },
    { key: 'chiangmai', label: 'Chiang Mai', name: 'Chiang Mai night market', place: 'Chiang Mai, Thailand' },
    { key: 'bantawai', label: 'Ban Tawai', name: 'Ban Tawai handicraft village', place: 'Chiang Mai, Thailand' },
  ];
  const market = k => MARKETS.find(m => m.key === k);
  const PRICE = { budget: ['$', 'Under $50'], mid: ['$$', '$50 to $300'], premium: ['$$$', '$300 and up'] };
  const fmtPrice = it => {
    const pr = it.price; if (!pr) return { short: '', long: 'Price on request', local: '' };
    const n = Number(pr.amount).toLocaleString('en-US');
    const usd = '$' + Number(pr.usd).toLocaleString('en-US');
    if (pr.currency === 'USD') return { short: usd, long: usd, local: '' };
    const sym = pr.currency === 'THB' ? '\u0e3f' + n : 'Rp ' + n;
    return { short: usd, long: sym, local: 'about ' + usd };
  };
  let items = [];
  const byId = id => items.find(i => i.id === id);
  const view = () => $('#view');
  const setNav = k => document.querySelectorAll('[data-nav]').forEach(a => a.classList.toggle('is-on', a.dataset.nav === k));

  const img = (it, cls) => `<img class="${cls || ''} is-photo" src="${esc(it.photo)}" alt="${esc(it.name)}" loading="lazy"
      onerror="this.onerror=null;this.classList.remove('is-photo');this.src='${esc(it.still)}'" />`;

  const toks = s => String(s || '').toLowerCase().replace(/[^a-z0-9 ]+/g, ' ').split(/\s+/).filter(w => w.length > 1).map(w => w.replace(/s$/, ''));
  function score(it, q) {
    const hay = toks([it.name, it.material, it.colour, it.category, market(it.market).name, market(it.market).place].join(' '));
    let s = 0;
    for (const w of toks(q)) { if (hay.includes(w)) s += 2; else if (w.length > 3 && hay.some(h => h.startsWith(w.slice(0, 4)))) s += 1; }
    return s;
  }

  function card(it) {
    const m = market(it.market), p = [fmtPrice(it).short];
    return `<a class="st-card" href="#/item/${encodeURIComponent(it.id)}">
      <div class="st-card-img">${img(it)}<i>${esc(m.label)}</i></div>
      <div class="st-card-name">${esc(it.name)}</div>
      <div class="st-card-sub"><span>${esc([it.material, it.colour].filter(Boolean).join(', '))}</span><span class="st-band">${p[0]}</span></div>
    </a>`;
  }

  function renderShop(params) {
    const q = params.get('q') || '', mk = params.get('m') || '';
    setNav(mk ? 'm-' + mk : 'shop');
    $('#q').value = q;
    let list = items.slice();
    if (mk) list = list.filter(i => i.market === mk);
    if (q) list = list.map(i => [i, score(i, q)]).filter(x => x[1] > 0).sort((a, b) => b[1] - a[1]).map(x => x[0]);
    const counts = Object.fromEntries(MARKETS.map(m => [m.key, items.filter(i => i.market === m.key).length]));
    const pick = ['chelsea-flea-005', 'chiang-mai-night-market-019', 'kerobokan-furniture-street-011'].map(byId).filter(Boolean);
    const tab = (key, label, n) => `<button class="${mk === key ? 'is-on' : ''}" data-m="${key}">${esc(label)}<span>${n}</span></button>`;
    view().innerHTML = `<main class="st-page">
      ${q || mk ? '' : `<section class="st-hero"><div class="st-wrap st-hero-in">
        <div><div class="st-kicker">Bountiful</div>
          <h1>Things you can't buy online.</h1>
          <p class="st-lead">${items.length} one-of-a-kind pieces from 4 markets with no online store, from a Chelsea Flea vintage table to a Chiang Mai beetle-specimen stall.</p>
          <p class="st-body">Every piece was filmed once, catalogued by NVIDIA Cosmos on CoreWeave GPUs and stored in VAST. Each listing links to the exact second it appears on camera.</p>
          <div class="st-actions"><a class="st-btn" href="#/?m=chelsea">Shop New York</a><a class="st-link" href="#/?m=chiangmai">Shop Chiang Mai</a></div>
        </div>
        <div class="st-hero-art">${pick.map((it, i) => `<a class="st-hero-${'abc'[i]}" href="#/item/${encodeURIComponent(it.id)}">${img(it)}<span>${esc(it.name)}</span></a>`).join('')}</div>
      </div></section>`}
      <div class="st-filter"><div class="st-wrap st-filter-in">
        <div class="st-tabs" id="tabs">${tab('', 'All markets', items.length)}${MARKETS.map(m => tab(m.key, m.label, counts[m.key] || 0)).join('')}</div>
        <div class="st-count">${q ? `${list.length} results for "${esc(q)}"` : `${list.length} pieces`}</div>
      </div></div>
      <section class="st-wrap st-sec">
        ${list.length ? `<div class="st-grid">${list.map(card).join('')}</div>` : `<p class="st-empty">Nothing matches "${esc(q)}". Try "lamp", "elephant", "beetle" or "leather".</p>`}
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
    const m = market(it.market), fp = fmtPrice(it);
    const more = items.filter(i => i.market === it.market && i.id !== it.id).slice(0, 8);
    view().innerHTML = `<main class="st-page"><div class="st-wrap">
      <div class="st-crumbs"><a href="#/">Shop</a><span>/</span><a href="#/?m=${it.market}">${esc(m.name)}</a><span>/</span><span>${esc(it.name)}</span></div>
      <div class="st-item">
        <div class="st-media">
          <div class="st-photo" data-zoomwrap>${img(it)}</div>
          <div class="st-moment">
            <img src="${esc(it.still)}" alt="${esc(it.name)} at the stall" loading="lazy" data-zoom="${esc(it.still.replace('/stills/', '/stills_hd/'))}" data-cap="${esc(it.name)} at the stall, ${esc(m.name)}, ${mmss(it.yt_at)}" data-yt="${esc(it.youtube)}" />
            <div><div class="st-kicker">At the stall</div><p>Seen at ${mmss(it.yt_at)} in the ${esc(m.name)} walkthrough.</p>
              <a class="st-link" href="${esc(it.youtube)}" target="_blank" rel="noopener">Watch the moment on YouTube &rarr;</a></div>
          </div>
        </div>
        <div class="st-info">
          <div class="st-kicker">${esc(m.place)}</div>
          <h1>${esc(it.name)}</h1>
          <div class="st-price">${esc(fp.long)}${fp.local ? `<span class="st-price-local">${esc(fp.local)}</span>` : ''}<span class="st-price-note">Estimated asking price</span></div>
          <div class="st-avail"><i></i>One of a kind, seen at the stall</div>
          <div class="st-actions" style="margin-top:22px"><button class="st-btn" id="reserve">Reserve this piece</button><a class="st-link" href="#/?m=${it.market}">More from this market</a></div>
          <p class="st-reserved" id="reserved" hidden>Reserved. A local scout will check it is still on the table and send you a photo before you pay.</p>
          <dl class="st-specs">
            <div><dt>Category</dt><dd>${esc(it.category)}</dd></div>
            <div><dt>Material</dt><dd>${esc(it.material || 'Not recorded')}</dd></div>
            <div><dt>Colour</dt><dd>${esc(it.colour || 'Not recorded')}</dd></div>
            <div><dt>Market</dt><dd>${esc(m.name)}</dd></div>
          </dl>
          <p class="st-prov">Catalogued from the walkthrough video by NVIDIA Cosmos Reason on CoreWeave GPUs, written up by Llama 3.3 70B on Weights &amp; Biases Inference, indexed with Cosmos Embed and stored in VastDB. Studio photo re-shot from the video frame.</p>
        </div>
      </div>
      ${more.length ? `<section class="st-sec"><div class="st-sec-head"><h2>More from ${esc(m.name)}</h2><a class="st-link" href="#/?m=${it.market}">All pieces</a></div><div class="st-grid">${more.map(card).join('')}</div></section>` : ''}
    </div></main>`;
    $('#reserve').addEventListener('click', e => { e.currentTarget.textContent = 'Reserved'; e.currentTarget.disabled = true; $('#reserved').hidden = false; });
    scrollTo(0, 0);
  }

  function renderHow() {
    setNav('how');
    view().innerHTML = `<main class="st-page how"><div class="st-wrap">
      <div class="st-kicker">How it works</div><h1>Film once. Shop forever.</h1>
      <p>A walkthrough of a market goes up to VAST. Every 5 seconds of it is watched by NVIDIA Cosmos, turned into catalogue entries by Llama 3.3, given a search fingerprint by Cosmos Embed, and written to VastDB. Built in one day at the VAST Builders Challenge in New York on the event stack.</p>
      <div class="flow">
        <div><small>1 · VAST S3</small><b>Upload</b><span>The walkthrough lands in the team bucket</span><em>About 5 s to land</em></div>
        <div><small>2 · ffmpeg</small><b>5 s pieces</b><span>Cut at 5 frames per second</span><em>Under 2 s per minute</em></div>
        <div><small>3 · NVIDIA Cosmos Reason</small><b>What is for sale?</b><span>On CoreWeave GPUs, one JSON list per piece</span><em>2.5 to 4 s per piece</em></div>
        <div><small>4 · W&amp;B Inference</small><b>Llama 3.3 70B</b><span>Merges sightings into clean, deduplicated entries</span><em>About 20 to 50 s per market</em></div>
        <div><small>5 · Cosmos Embed1</small><b>Fingerprints</b><span>256-d vectors for every moment and every item</span><em>3 to 12 s per market</em></div>
        <div><small>6 · VastDB</small><b>Searchable</b><span>bountiful.items, moments and runs</span><em>Under 1 s to write</em></div>
      </div>
      <div class="how-nums">
        <div><b>97</b><span>catalogue entries from 4 markets, written to VastDB</span></div>
        <div><b>2.5 to 4 s</b><span>for Cosmos to list what is for sale in 5 seconds of video</span></div>
        <div><b>~100 s</b><span>of compute per minute of video on one worker; pieces are independent, so it scales out</span></div>
        <div><b>2.8 h</b><span>for 1,000 shops filmed for 5 minutes each, on 50 parallel workers</span></div>
      </div>
      <div class="how-stack">
        <div><b>VAST AI OS</b><span>S3 for the footage, VastDB for items, moments and vectors</span></div>
        <div><b>NVIDIA Cosmos</b><span>Reason watches the video, Embed makes it searchable</span></div>
        <div><b>CoreWeave</b><span>The GPUs the Cosmos models run on</span></div>
        <div><b>Weights &amp; Biases</b><span>Serverless Llama 3.3 70B writes the catalogue</span></div>
      </div>
      <p style="margin-top:28px">Source walkthroughs are public YouTube videos; every listing links to its moment rather than re-hosting the footage. Code: <a class="st-link" href="https://github.com/MatthewMakesMagic/bountiful-vast">github.com/MatthewMakesMagic/bountiful-vast</a></p>
    </div></main>`;
  }

  // ---------- lightbox: click any product photo or market still to see it large ----------
  function openZoom(src, cap, yt) {
    let lb = document.getElementById('lb');
    if (!lb) {
      lb = document.createElement('div'); lb.id = 'lb'; lb.className = 'lb';
      lb.innerHTML = '<button class="lb-x" aria-label="Close">&times;</button><figure><img alt="" /><figcaption></figcaption></figure>';
      document.body.appendChild(lb);
      lb.addEventListener('click', e => { if (e.target === lb || e.target.classList.contains('lb-x')) lb.classList.remove('on'); });
      addEventListener('keydown', e => { if (e.key === 'Escape') lb.classList.remove('on'); });
    }
    lb.querySelector('img').src = src;
    lb.querySelector('figcaption').innerHTML = esc(cap || '') + (yt ? ` <a class="st-link" href="${esc(yt)}" target="_blank" rel="noopener">Watch the moment on YouTube &rarr;</a>` : '');
    lb.classList.add('on');
  }
  document.addEventListener('click', e => {
    const t = e.target.closest('[data-zoom], [data-zoomwrap] img');
    if (!t) return;
    e.preventDefault();
    const it = byId(decodeURIComponent((location.hash.split('/item/')[1] || '')));
    openZoom(t.dataset.zoom || t.currentSrc || t.src, t.dataset.cap || (it ? it.name : ''), t.dataset.yt || '');
  });

  function route() {
    const h = location.hash.replace(/^#/, '') || '/';
    const [path, qs] = h.split('?');
    if (path.startsWith('/item/')) renderItem(decodeURIComponent(path.slice(6)));
    else if (path === '/how') renderHow();
    else renderShop(new URLSearchParams(qs || ''));
  }
  $('#searchform').addEventListener('submit', e => {
    e.preventDefault();
    const q = $('#q').value.trim();
    location.hash = '#/' + (q ? '?q=' + encodeURIComponent(q) : '');
  });
  fetch('catalog.json').then(r => r.json()).then(d => { items = d.items || []; addEventListener('hashchange', route); route(); });
})();
