
(() => {
const box = document.getElementById('dl'), sum = document.getElementById('dlsum');
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const size = b => b >= 1e9 ? (b / 1e9).toFixed(2) + ' GB' : (b / 1e6).toFixed(0) + ' MB';
const speed = b => b >= 1e6 ? (b / 1e6).toFixed(1) + ' MB/s' : (b / 1e3).toFixed(0) + ' KB/s';
const eta = s => s < 60 ? 'under a minute left' : s < 3600 ? Math.round(s / 60) + ' min left' : Math.floor(s / 3600) + ' h ' + Math.round(s % 3600 / 60) + ' min left';
const ago = t => { const s = Date.now() / 1000 - t; return s < 90 ? 'just now' : s < 3600 ? Math.round(s / 60) + ' min ago' : s < 86400 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' days ago'; };
const LABEL = {downloading: 'Downloading', stalled: 'Waiting for peers', finding: 'Finding peers', queued: 'Queued', paused: 'Paused',
  checking: 'Checking', error: 'Problem', importing: 'Finishing up', blocked: 'Couldn’t add', ready: 'Ready'};
function card(i) {
  const pct = (i.progress * 100).toFixed(i.progress < 1 ? 1 : 0);
  let line;
  if (i.state === 'ready') line = '<b class="ok">✓ ' + (i.kind === 'manga' ? 'Ready to read' : 'Ready to watch') + '</b> · finished ' + ago(i.finished || i.added);
  else if (i.state === 'importing') line = 'Downloaded · moving it into your library…';
  else if (i.state === 'blocked') line = 'Downloaded, but it couldn’t be added to your library. If you already have it, it’s cleared automatically; otherwise you get an alert.';
  else if (i.state === 'error') line = 'qBittorrent reports a problem with this download';
  else line = size(i.done) + ' of ' + size(i.size) + (i.speed ? ' · ' + speed(i.speed) : '') + (i.eta ? ' · ' + eta(i.eta) : '') +
    ' · ' + i.seeds + ' seed' + (i.seeds === 1 ? '' : 's') + ' connected' + (i.swarm ? ' (' + i.swarm.toLocaleString() + ' sharing)' : '');
  const poster = i.poster ? '<img src="' + esc(i.poster) + '" alt="" loading="lazy">' : '<div class="ph">' + (i.kind === 'manga' ? '📖' : i.kind === 'movie' ? '🎬' : '📺') + '</div>';
  const title = i.link ? '<a href="' + esc(i.link) + '">' + esc(i.title) + '</a>' : esc(i.title);
  const watch = i.state === 'ready' && i.link ? '<a class="btn small" href="' + esc(i.link) + '">▶ Watch</a>' : '';
  return '<div class="dlc s-' + i.state + '">' + poster + '<div class="dli"><div class="dlt"><span>' + title + '</span><span class="chip st">' + LABEL[i.state] + '</span></div>' +
    (i.sub ? '<div class="dls" title="' + esc(i.release) + '">' + esc(i.sub) + '</div>' : '') +
    (i.state === 'ready' || i.state === 'blocked' ? '' : '<div class="bar" role="progressbar" aria-valuenow="' + pct + '"><i style="width:' + pct + '%"></i></div>') +
    '<div class="dlm"><span>' + (i.state === 'ready' || i.state === 'importing' || i.state === 'blocked' ? '' : '<b>' + pct + '%</b> · ') + line + '</span>' + watch + '</div></div></div>';
}
function render(d) {
  const active = d.items.filter(i => !['ready', 'importing', 'blocked'].includes(i.state)), finishing = d.items.filter(i => i.state === 'importing'),
    blocked = d.items.filter(i => i.state === 'blocked'), ready = d.items.filter(i => i.state === 'ready');
  const sec = (t, list) => list.length ? '<h3 class="dlsec">' + t + '</h3>' + list.map(card).join('') : '';
  box.innerHTML = (d.items.length ? sec('Downloading', active) + sec('Finishing up', finishing) + sec('Couldn’t be added', blocked) + sec('Recently finished', ready)
    : '<p class="muted">Nothing downloading right now.</p>');
  sum.textContent = active.length ? active.length + ' active · ' + speed(d.speed) : '';
}
render(JSON.parse(document.getElementById('dldata').textContent));
let busy = false;
setInterval(async () => {
  if (document.hidden || busy) return;
  busy = true;
  try { const r = await fetch('/api/downloads'); if (r.ok) render(await r.json()); } catch (e) {} finally { busy = false; }
}, 2000);
})();
