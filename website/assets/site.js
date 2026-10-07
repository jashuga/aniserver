
const dlg = document.getElementById('details');
function busy(root) {
  root.querySelectorAll('form[data-busy]').forEach(f => f.addEventListener('submit', e => {
    if (f.dataset.sent) { e.preventDefault(); return; }   // no double adds; keep the clicked button enabled so its mode is sent
    f.dataset.sent = '1';
    const b = e.submitter || f.querySelector('button'); b.textContent = 'Adding…';
  }));
}
document.querySelectorAll('[data-t]').forEach(card => card.addEventListener('click', () => {
  dlg.innerHTML = document.getElementById(card.dataset.t).innerHTML;
  busy(dlg);
  dlg.querySelector('.close').addEventListener('click', () => dlg.close());
  dlg.showModal();
}));
dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });
busy(document);
const openId = new URLSearchParams(location.search).get('open');
if (openId) { const b = document.querySelector('[data-al="' + CSS.escape(openId) + '"]'); if (b) b.click(); }
;

(() => {
const top = document.querySelector('.top.over');
if (top) { const f = () => top.classList.toggle('solid', scrollY > 40); addEventListener('scroll', f, {passive: true}); f(); }
const hero = document.querySelector('.hero');
if (hero) {
  const slides = [...hero.querySelectorAll('.slide')], dots = [...hero.querySelectorAll('.dots button')];
  let n = 0, timer = null;
  const show = k => {
    slides[n].classList.remove('on'); if (dots[n]) dots[n].classList.remove('on');
    n = (k + slides.length) % slides.length;
    const img = slides[n].querySelector('img[data-src]');
    if (img) { img.src = img.dataset.src; img.removeAttribute('data-src'); }
    slides[n].classList.add('on'); if (dots[n]) dots[n].classList.add('on');
    const next = slides[(n + 1) % slides.length].querySelector('img[data-src]');
    if (next) { const pre = new Image(); pre.src = next.dataset.src; }
  };
  const go = () => { clearInterval(timer); if (slides.length > 1) timer = setInterval(() => { if (!document.hidden && !hero.matches(':hover')) show(n + 1); }, 9000); };
  dots.forEach((d, k) => d.addEventListener('click', () => { show(k); go(); }));
  let x0 = null, y0 = null;
  hero.addEventListener('touchstart', e => { x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; }, {passive: true});
  hero.addEventListener('touchend', e => {
    if (x0 === null) return;
    const dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0;
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) && slides.length > 1) { show(n + (dx < 0 ? 1 : -1)); go(); }
    x0 = null;
  });
  go();
  if (slides.length > 1) setTimeout(() => { const s = slides[1].querySelector('img[data-src]'); if (s) { const pre = new Image(); pre.src = s.dataset.src; } }, 1500);
}
document.querySelectorAll('.row').forEach(row => {
  const rail = row.querySelector('.rail'); if (!rail) return;
  row.querySelectorAll('.nav-arrow').forEach(b => b.addEventListener('click', () =>
    rail.scrollBy({left: (b.classList.contains('l') ? -1 : 1) * rail.clientWidth * .86, behavior: 'smooth'})));
  const upd = () => { row.classList.toggle('at-start', rail.scrollLeft < 8); row.classList.toggle('at-end', rail.scrollLeft + rail.clientWidth > rail.scrollWidth - 8); };
  rail.addEventListener('scroll', upd, {passive: true}); addEventListener('resize', upd); upd();
});
let tt;
const toast = msg => {
  let t = document.querySelector('.toast');
  if (!t) { t = document.createElement('div'); t.className = 'toast'; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add('show'); clearTimeout(tt); tt = setTimeout(() => t.classList.remove('show'), 3200);
};
const post = (url, data) => fetch(url, {method: 'POST', body: new URLSearchParams(data)}).then(r => r.json());
document.addEventListener('click', async e => {
  const tv = e.target.closest('[data-tv]');
  if (tv) {
    e.preventDefault(); if (tv.dataset.busy) return; tv.dataset.busy = 1;
    toast('Starting on the TV…');
    try { const j = await post('/api/tv', {id: tv.dataset.tv}); toast(j.msg); } catch (err) { toast("Couldn't reach the TV."); }
    delete tv.dataset.busy; return;
  }
  const pl = e.target.closest('[data-played]');
  if (pl) {
    e.preventDefault();
    const on = pl.getAttribute('aria-pressed') !== 'true';
    try {
      const j = await post('/api/played', {id: pl.dataset.played, on: on ? 1 : 0});
      if (!j.ok) return toast(j.msg);
      if (pl.dataset.scope) { location.reload(); return; }
      pl.setAttribute('aria-pressed', on); pl.title = on ? 'Mark as unwatched' : 'Mark as watched';
      const row = pl.closest('.er'); if (row) { row.classList.toggle('played', on); const bar = row.querySelector('.pbar'); if (bar) bar.remove(); }
      toast(on ? 'Marked as watched' : 'Marked as unwatched');
    } catch (err) { toast("That didn't work."); }
    return;
  }
  const del = e.target.closest('[data-delete]');
  if (del) {
    e.preventDefault();
    const d = del.dataset, dlg = document.getElementById('details');
    const what = d.what === 'movie' ? 'movie' : 'show';
    dlg.innerHTML = '<div class="confirm"><h3>Delete ' + d.name.replace(/[&<>]/g, '') + '?</h3><p>This deletes the ' + what +
      (d.count > 0 ? ' and its ' + (what === 'movie' ? 'file' : d.count + ' downloaded episode' + (d.count == 1 ? '' : 's')) + ' (' + d.size + ')' : '') +
      ' from the server, cancels any downloads for it, and stops new episodes from downloading. This can’t be undone.</p>' +
      '<div class="row"><button class="ghost" data-x>Cancel</button><button class="danger" data-go>Delete ' + what + '</button></div></div>';
    dlg.classList.add('small');
    dlg.addEventListener('close', () => dlg.classList.remove('small'), {once: true});
    dlg.querySelector('[data-x]').onclick = () => dlg.close();
    dlg.querySelector('[data-go]').onclick = async ev => {
      ev.target.disabled = true; ev.target.textContent = 'Deleting…';
      try {
        const j = await post('/api/delete', {key: d.delete});
        if (j.ok) location.href = '/shows?' + new URLSearchParams({ok: j.msg}); else { toast(j.msg); dlg.close(); }
      } catch (err) { toast("That didn't work."); dlg.close(); }
    };
    dlg.showModal();
    return;
  }
  const st = e.target.closest('[data-season]');
  if (st) {
    document.querySelectorAll('[data-season]').forEach(b => b.classList.toggle('on', b === st));
    document.querySelectorAll('.eplist').forEach(l => l.hidden = l.dataset.list !== st.dataset.season);
    document.querySelectorAll('.sact [data-played]').forEach(b => b.hidden = b.dataset.for !== st.dataset.season);
    history.replaceState(null, '', '?season=' + st.dataset.season);
    return;
  }
  const ov = e.target.closest('.thero .ov');
  if (ov) ov.classList.toggle('open');
});
const hash = location.hash && document.querySelector(location.hash);
if (hash) hash.scrollIntoView({block: 'center'});
})();
