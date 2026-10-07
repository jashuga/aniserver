
(() => {
'use strict';
const CFG = JSON.parse(document.getElementById('cfg').textContent);
let info = JSON.parse(document.getElementById('info').textContent);
const $ = (s, r = document) => r.querySelector(s);
const P = $('#player'), v = $('#v'), seek = $('.seek'), tip = $('.tip'), panel = $('.panel'), upnext = $('.upnext'), cover = $('.cover');
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const fmt = t => { t = Math.max(0, Math.floor(t || 0)); const h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), s = t % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(s).padStart(2, '0'); };
const store = {
  get(k, d) { try { const x = localStorage.getItem(k); return x === null ? d : JSON.parse(x); } catch (e) { return d; } },
  set(k, x) { try { localStorage.setItem(k, JSON.stringify(x)); } catch (e) {} },
};
const dev = (() => { let d = store.get('htpc-dev', ''); if (!d) { d = 'web-' + Math.random().toString(36).slice(2, 12); store.set('htpc-dev', d); } return d; })();
let quality = store.get('htpc-q-' + CFG.net, 'auto');
if (!CFG.qualities.some(q => q[0] === quality)) quality = 'auto';
let hls = null, psid = '', cur = {a: -1, s: -1}, lastSaved = -1, upDismissed = false, cdTimer = null, started = false;
let netErr = 0, medErr = 0, dragging = false, idleT = null;
let mode = 0, floor = 0, jas = null, jasFailed = false, wdT = null, t0 = 0, loadAt = 0, waitT0 = 0, seekT0 = 0;
let monT = null, loadSeq = 0, stalls = [], lastBwSave = 0, hevcFailed = false;
const useHevc = () => mode === 2 && caps.hevc && !hevcFailed && quality !== '480';
const ladder = () => useHevc() ? CFG.ladderHevc : CFG.ladder;
const MODES = ['direct', 'remux', 'adaptive', 'burn'];
const NET = (location.search.match(/[?&]net=(home|away)/) || [])[1] || CFG.net;     // ?net= is a testing knob
const LEVEL_FOR = {'1080': 0, '720': 2, '480': 3};
const dur = () => (isFinite(v.duration) && v.duration > 0 ? v.duration : info.duration) || 0;
const creditsAt = () => info.credits != null ? info.credits : (info.next && dur() ? Math.max(0, dur() - 45) : null);
const playedAt = t => { const d = dur(); return d > 0 && (t >= d - 4 || (info.credits != null && t >= info.credits) || t / d >= 0.92); };
const mbps = b => (b / 1e6).toFixed(b < 1e7 ? 1 : 0) + ' Mbps';
function modeText() {
  if (mode === 0) return 'Original file · ' + mbps(info.bitrate);
  if (mode === 1) return 'Original quality · ' + mbps(info.bitrate);
  if (mode === 2 && hls && hls.levels && hls.levels[hls.currentLevel]) {
    const l = hls.levels[hls.currentLevel]; return (hls.autoLevelEnabled ? 'Adaptive · ' : '') + l.height + 'p · ' + mbps(l.bitrate - 160000);
  }
  return mode === 3 ? 'Converted · subtitles burned in' : 'Adaptive';
}

// ---------- connection speed, remembered per network (home / away) ----------
const bwKey = 'htpc-bw2-' + (NET === 'home' ? 'home' : CFG.netId);     // remembered per network (campus, phone, ...)
function bwGet(maxAgeMs) { const b = store.get(bwKey, null); return b && Date.now() - b.at < (maxAgeMs || (NET === 'home' ? 24 : 2) * 3600e3) ? b.bps : 0; }
function bwPut(bps) { if (!(bps > 0)) return; const old = bwGet(); store.set(bwKey, {bps: Math.round(old ? old * 0.5 + bps * 0.5 : bps), at: Date.now()}); }
const recentStalls = () => { const now = performance.now(); stalls = stalls.filter(s => now - s < 90000); return stalls.length; };
function bufferedAhead() { let b = 0; for (let i = 0; i < v.buffered.length; i++) if (v.buffered.start(i) <= v.currentTime + .5) b = Math.max(b, v.buffered.end(i) - v.currentTime); return b; }
window.__htpc = () => ({mode: MODES[mode], level: hls ? hls.currentLevel : null, est: hls ? +(hls.bandwidthEstimate / 1e6).toFixed(1) : null, saved: +(bwGet() / 1e6).toFixed(1)});

// ---------- what this browser can play by itself ----------
const caps = (() => {
  const t = document.createElement('video');
  const MS = window.ManagedMediaSource || window.MediaSource;
  const mse = c => { try { return !!(MS && MS.isTypeSupported(c)); } catch (e) { return false; } };
  const can = c => { try { return !!t.canPlayType(c).replace('no', ''); } catch (e) { return false; } };
  const either = c => mse(c) || can(c);
  const ua = navigator.userAgent, mobile = /Mobi|Android|iPhone|iPad/.test(ua);
  return {
    mkv: can('video/x-matroska') || can('video/mkv') || (!mobile && /Edg\/|Chrome\//.test(ua) && !/OPR\//.test(ua)),
    hevc: either('video/mp4; codecs="hvc1.2.4.L153.B0"') || either('video/mp4; codecs="hev1.2.4.L153.B0"'),
    av1: either('video/mp4; codecs="av01.0.08M.10"'), vp9: either('video/mp4; codecs="vp09.00.40.08"'),
    audio: {aac: true, mp3: true, flac: either('audio/mp4; codecs="flac"') || can('audio/flac'), opus: either('audio/mp4; codecs="opus"'),
            eac3: either('audio/mp4; codecs="ec-3"'), ac3: either('audio/mp4; codecs="ac-3"')},
    subs: typeof WebAssembly === 'object' && typeof Worker === 'function' && typeof JASSUB === 'function',
  };
})();
function log(ev, msg) {
  try { navigator.sendBeacon('/api/log', new URLSearchParams({id: info.id, mode: MODES[mode], ev, msg: String(msg == null ? '' : msg).slice(0, 300)})); } catch (e) {}
}

// ---------- tracks ----------
function chooseTracks() {
  let a = info.a, s = info.s;
  const pref = store.get('htpc-tracks-' + info.series, null);
  if (pref) {
    const am = info.audios.find(x => x.lang === pref.alang); if (am) a = am.i;
    if (pref.off) s = -1;
    else if (pref.slang) {
      const sm = info.subs.find(x => x.lang === pref.slang && x.kind === pref.skind) || info.subs.find(x => x.lang === pref.slang);
      if (sm) s = sm.i;
    }
  }
  cur = {a, s};
}
function rememberTracks() {
  const a = info.audios.find(x => x.i === cur.a), s = info.subs.find(x => x.i === cur.s);
  store.set('htpc-tracks-' + info.series, {alang: a ? a.lang : null, off: cur.s < 0, slang: s ? s.lang : null, skind: s ? s.kind : null});
}

// ---------- how to play: original file > repackaged original > converted (like Jellyfin's direct play / direct stream / transcode) ----------
function videoOk() {
  const c = info.video.codec;
  return c === 'h264' ? info.video.depth <= 8 : c === 'hevc' ? caps.hevc : c === 'av1' ? caps.av1 : c === 'vp9' ? caps.vp9 : false;
}
function naturalMode() {
  const s = info.subs.find(x => x.i === cur.s);
  if (s && (!s.text || !caps.subs || jasFailed)) return 3;          // picture subtitles (or no subtitle renderer): burn them in
  if (quality in LEVEL_FOR || !videoOk()) return 2;
  const a = info.audios.find(x => x.i === cur.a), first = info.audios[0];
  const box = ['mkv', 'matroska', 'webm'].includes(info.container) ? caps.mkv : ['mp4', 'm4v', 'mov'].includes(info.container);
  const directOk = box && (!a || (first && a.i === first.i && caps.audio[a.codec]));
  if (NET === 'home') return directOk ? 0 : 1;                      // home network: the original file, instant seeking
  if (quality === 'original') return 1;
  const bw = bwGet(30 * 60e3);                                       // away: original only if a recent measurement beats the file's peak
  return bw && bw >= 1.35 * (info.peak || info.bitrate * 2.2) ? 1 : 2;
}
const FORCE = MODES.indexOf((location.search.match(/[?&]force=([a-z+]+)/) || [])[1]);     // ?force=direct|remux|adaptive|burn (testing)
const wantMode = () => (FORCE >= 0 && floor <= FORCE) ? FORCE : Math.max(naturalMode(), floor);

// ---------- subtitles, drawn in the browser with libass (full anime styling + the release's own fonts) ----------
const subUrl = i => '/stream/' + info.id + '/sub/' + i + '.ass?_t=' + encodeURIComponent(info.tok);
function dropSubs() { if (jas) { try { jas.destroy(); } catch (e) {} jas = null; } }
function syncSubs() {
  const s = info.subs.find(x => x.i === cur.s);
  if (!s || mode === 3 || !s.text) { if (jas) { try { jas.freeTrack(); } catch (e) {} } return; }
  try {
    if (!jas) {
      jas = new JASSUB({video: v, subUrl: subUrl(s.i), workerUrl: CFG.jassub + 'jassub-worker.js', wasmUrl: CFG.jassub + 'jassub-worker.wasm',
        modernWasmUrl: CFG.jassub + 'jassub-worker-modern.wasm', fallbackFont: 'liberation sans',
        availableFonts: Object.assign({'liberation sans': CFG.jassub + 'default.woff2'}, info.fonts),
        ...(/[?&]subsmain/.test(location.search) ? {onDemandRender: false, offscreenRender: false} : {})});   // simpler main-thread mode
      jas.addEventListener('error', e => { log('subs-error', e && (e.error || e.message)); });
    } else jas.setTrackByUrl(subUrl(s.i));
  } catch (e) {
    log('subs-error', e && e.message); jasFailed = true; dropSubs();
    if (wantMode() !== mode) reload();
  }
}

// ---------- stream ----------
function stopStream() {
  if (psid) { navigator.sendBeacon('/api/stop', new URLSearchParams({d: dev, psid})); psid = ''; }
}
const hlsPolicy = (ttfb, max) => ({default: {maxTimeToFirstByteMs: ttfb, maxLoadTimeMs: max,
  timeoutRetry: {maxNumRetry: 2, retryDelayMs: 500, maxRetryDelayMs: 2000}, errorRetry: {maxNumRetry: 4, retryDelayMs: 1000, maxRetryDelayMs: 6000}}});
const hlsIndex = k => ladder().length - 1 - k;              // hls.js orders levels lowest bitrate first; our ladders are best first
function startLevelFor(bw) {
  if (quality in LEVEL_FOR) return hlsIndex(LEVEL_FOR[quality]);
  if (!bw) return hlsIndex(3);                                 // unknown connection: start light, the player ramps up within seconds
  const k = ladder().findIndex(([w, b]) => b + 160000 <= 0.7 * bw);
  // away from home never start above 720p: a fast start matters more than the first few seconds' sharpness
  return hlsIndex(Math.max(k < 0 ? ladder().length - 1 : k, NET === 'away' ? 2 : 0));
}
async function load(at, play) {
  const seq = ++loadSeq;
  stopStream(); clearTimeout(wdT); clearInterval(monT);
  started = false; netErr = medErr = 0; loadAt = at > 0 ? at : 0; t0 = performance.now(); stalls = [];
  P.classList.add('buffering');
  if (hls) { hls.destroy(); hls = null; }
  mode = wantMode();
  const bw = bwGet();
  if (mode === 0) {
    v.src = '/stream/' + info.id + '/file?_t=' + encodeURIComponent(info.tok);
    if (loadAt) v.addEventListener('loadedmetadata', () => { if (mode === 0) v.currentTime = loadAt; }, {once: true});
    wdT = setTimeout(() => { if (mode === 0 && v.readyState < 2 && !v.paused) fallback('no picture after 12 s'); }, 12000);
  } else {
    psid = Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    if (mode === 1 && NET === 'away' && quality === 'auto' && FORCE < 0)
      wdT = setTimeout(() => { if (!started && mode === 1 && !v.paused) stepDown('no picture after 7 s'); }, 7000);
    const q = {m: ['', 'remux', 'abr', 'transcode'][mode], hevc: useHevc() ? 1 : 0, a: cur.a, s: mode === 3 && cur.s >= 0 ? cur.s : '', d: dev, psid,
      q: NET === 'home' || bw >= 13e6 ? 'max' : bw >= 9e6 ? 'high' : bw >= 6e6 || !bw ? '1080' : bw >= 3.5e6 ? '720' : '480',
      vc: ['h264'].concat(['hevc', 'av1', 'vp9'].filter(c => caps[c])).join(','), ac: Object.keys(caps.audio).filter(c => caps.audio[c]).join(','),
      _t: info.tok};
    const url = '/stream/' + info.id + '/master.m3u8?' + new URLSearchParams(q);
    if (window.Hls && Hls.isSupported()) {
      const away = NET === 'away';
      hls = new Hls({startPosition: loadAt || -1, maxBufferLength: away ? 90 : 45, maxMaxBufferLength: 240, maxBufferSize: 200e6,
        backBufferLength: 60, maxBufferHole: 0.5, progressive: mode === 1, startFragPrefetch: true,
        startLevel: mode === 2 ? startLevelFor(bw) : -1, abrEwmaDefaultEstimate: bw || 4e6, abrBandWidthFactor: 0.8, abrBandWidthUpFactor: 0.7,
        manifestLoadPolicy: hlsPolicy(60000, 90000), playlistLoadPolicy: hlsPolicy(60000, 90000), fragLoadPolicy: hlsPolicy(60000, 180000)});
      hls.on(Hls.Events.ERROR, (_, d) => {
        if (!d.fatal) return;
        log('hls-error', d.details + (d.response && d.response.code ? ' ' + d.response.code : ''));
        if (useHevc() && /IncompatibleCodecs|AddCodec|bufferAppend|fragParsing/.test(d.details)) {
          hevcFailed = true; log('hevc-off', d.details); return load(started ? v.currentTime : loadAt, true);
        }
        if (/IncompatibleCodecs|AddCodec/.test(d.details)) return fallback(d.details);
        const resumeAt = started ? v.currentTime : loadAt;
        if (d.type === Hls.ErrorTypes.NETWORK_ERROR && netErr++ < 3) setTimeout(() => hls && hls.startLoad(resumeAt), 1500);
        else if (d.type === Hls.ErrorTypes.MEDIA_ERROR && medErr++ < 1) { hls.recoverMediaError(); v.currentTime = resumeAt; }
        else fallback(d.details);
      });
      if (mode === 2) {
        hls.on(Hls.Events.MANIFEST_PARSED, () => { if (quality in LEVEL_FOR && hls) hls.currentLevel = hlsIndex(LEVEL_FOR[quality]); });
        hls.on(Hls.Events.LEVEL_SWITCHED, (_, d) => { if (started) log('level', modeText()); });
      }
      hls.loadSource(url); hls.attachMedia(v);
    } else if (v.canPlayType('application/vnd.apple.mpegurl')) {
      v.src = url;
      if (loadAt) v.addEventListener('loadedmetadata', () => { v.currentTime = loadAt; }, {once: true});
    } else return fail("This browser can't play video streams.");
  }
  monT = setInterval(monitor, 2000);
  syncSubs();
  if (play) tryPlay(); else { P.classList.remove('buffering'); P.classList.add('paused'); }
}
let lastStats = 0;
function monitor() {
  if (!started) {        // still starting: if the original is clearly too heavy for this connection, switch now
    const est = hls ? hls.bandwidthEstimate : 0, need = 1.15 * (info.peak || info.bitrate * 2.2);
    if (mode === 1 && NET === 'away' && quality === 'auto' && FORCE < 0 && est && est < need) stepDown('starting too slowly: ' + mbps(est) + ' < ' + mbps(need));
    return;
  }
  if (v.paused) return;
  if (performance.now() - lastStats > 60000) {                     // a line a minute in the player log: what quality, how much buffered
    lastStats = performance.now();
    log('stats', modeText() + ' · buffer ' + bufferedAhead().toFixed(0) + ' s · connection ' + (hls && hls.bandwidthEstimate ? mbps(hls.bandwidthEstimate) : '?') + ' · at ' + fmt(v.currentTime));
  }
  if (hls && hls.bandwidthEstimate > 0 && performance.now() - lastBwSave > 10000) { lastBwSave = performance.now(); bwPut(hls.bandwidthEstimate); }
  if (quality !== 'auto' || FORCE >= 0) return;
  const ahead = bufferedAhead(), est = hls ? hls.bandwidthEstimate : 0, need = 1.15 * (info.peak || info.bitrate * 2.2);
  // step down instead of buffering: original file -> original via a big buffer -> adaptive quality
  if (mode === 0 && recentStalls() >= 1) return stepDown('stalled while direct playing');
  if (mode === 1 && (recentStalls() >= 2 || (NET === 'away' && ahead < 12 && est && est < need)))
    return stepDown('connection ' + mbps(est) + ' < needed ' + mbps(need) + ', ' + ahead.toFixed(0) + ' s buffered');
}
function stepDown(why) {
  log('step-down', MODES[mode] + ': ' + why);
  floor = mode + 1;
  if (mode === 1) toast('Adapting quality to your connection');
  const at = started ? v.currentTime : loadAt;
  if (!started) store.set(bwKey, null);      // the remembered speed was wrong for this network: measure afresh
  load(at, true);
}
function fallback(reason) {
  log('fallback', MODES[mode] + ' failed: ' + reason);
  if (mode >= 3) return fail("Couldn't play this (" + reason + ').');
  floor = mode + 1;
  load(started ? v.currentTime : loadAt, true);
}
v.addEventListener('error', () => { if (mode === 0 && v.getAttribute('src')) fallback('direct play error ' + (v.error ? v.error.code + ' ' + (v.error.message || '') : '')); });
v.addEventListener('loadedmetadata', () => { if (mode === 0 && !v.videoWidth) fallback('video codec not supported by this browser'); });
function tryPlay() {
  const p = v.play();
  if (p) p.catch(err => {
    if (err && err.name === 'AbortError') return;
    P.classList.remove('buffering'); P.classList.add('paused');
    if (!started) cover.classList.add('blocked'); else showUI(true);
  });
}
function reload(msg) { const t = started ? v.currentTime : loadAt, play = !v.paused || !started; if (msg) toast(msg); load(t, play); }
function fail(msg) { stopStream(); $('#errmsg').textContent = msg; $('#errsheet').hidden = false; cover.classList.add('gone'); }

// ---------- progress (synced to Jellyfin, per person) ----------
const NOSAVE = /[?&]noprogress/.test(location.search);       // testing knob: don't touch anyone's watch progress
function save(mode) {
  if (!started || NOSAVE) return;
  const t = v.currentTime, played = playedAt(t);
  if (!mode && Math.abs(t - lastSaved) < 4) return;
  if (t < 15 && !played && info.pos < 15) return;
  lastSaved = t;
  const body = new URLSearchParams({id: info.id, pos: t.toFixed(1), played: played ? 1 : 0});
  if (mode === 'beacon') navigator.sendBeacon('/api/progress', body);
  else fetch('/api/progress', {method: 'POST', body, keepalive: true}).catch(() => {});
}
setInterval(() => { if (!v.paused) save(); }, 10000);
document.addEventListener('visibilitychange', () => { if (document.hidden) save('beacon'); });
addEventListener('pagehide', () => { save('beacon'); stopStream(); });

// ---------- render ----------
function render() {
  const t1 = info.code ? info.show : info.show, t2 = info.code ? info.code + ' · ' + info.name : '';
  $('.topbar b').textContent = t1; $('.topbar span').textContent = t2;
  document.title = (info.code ? info.code + ' · ' : '') + info.show;
  $('#back').href = '/title/' + info.series + (info.code ? '#ep-' + info.id : '');
  v.poster = info.still;
  $('.cover .cbg').src = info.backdrop;
  $('.cover .brand').innerHTML = info.logo ? '<img class="logo" src="' + esc(info.logo) + '" alt="' + esc(info.show) + '">' : '<h2>' + esc(info.show) + '</h2>';
  $('.cover .ep').textContent = t2;
  $('#nextbtn').hidden = !info.next;
  if (info.next) {
    $('#nextbtn').title = 'Next episode: ' + info.next.code + ' · ' + info.next.name + ' (N)';
    upnext.querySelector('img').src = info.next.thumb;
    upnext.querySelector('b').textContent = info.next.code + ' · ' + info.next.name;
  }
  upnext.classList.remove('show', 'counting'); P.classList.remove('credits');
  $('#endsheet').hidden = true; $('#tvsheet').hidden = true; $('#errsheet').hidden = true;
  drawChapters();
  if ('mediaSession' in navigator) {
    try {
      navigator.mediaSession.metadata = new MediaMetadata({title: info.code ? info.code + ' · ' + info.name : info.show, artist: info.show,
        artwork: [{src: info.still, sizes: '1280x720'}]});
      const h = (a, f) => { try { navigator.mediaSession.setActionHandler(a, f); } catch (e) {} };
      h('play', () => v.play()); h('pause', () => v.pause());
      h('seekbackward', () => jump(-10)); h('seekforward', () => jump(10));
      h('seekto', d => { v.currentTime = d.seekTime; });
      h('nexttrack', info.next ? () => playNext() : null);
    } catch (e) {}
  }
}
function drawChapters() {
  const box = $('.chaps'); box.innerHTML = ''; const d = dur(); if (!d) return;
  info.chapters.forEach(c => { if (c.t > 1 && c.t < d - 1) { const m = document.createElement('div'); m.className = 'chap'; m.style.left = (100 * c.t / d) + '%'; box.appendChild(m); } });
}
function drawBuffer() {
  const d = dur(); if (!d) return;
  const t = v.currentTime; let end = t;
  for (let i = 0; i < v.buffered.length; i++) if (v.buffered.start(i) <= t + 1 && v.buffered.end(i) > end) end = v.buffered.end(i);
  $('.buf').style.width = (100 * end / d) + '%';
}
function onTime() {
  const t = v.currentTime, d = dur();
  if (!dragging && d) { const p = (100 * t / d) + '%'; $('.played').style.width = p; $('.knob').style.left = p; }
  $('.time').textContent = fmt(t) + ' / ' + fmt(d);
  const i = info.intro, rc = info.recap;
  const inIntro = !!(i && t >= i[0] && t < i[1] - 2), inRecap = !!(rc && t >= rc[0] && t < rc[1] - 2);
  if (inRecap || inIntro) skipLabel.textContent = inRecap ? 'Skip Recap' : 'Skip Opening';
  $('.skip').classList.toggle('show', inIntro || inRecap);
  const c = creditsAt(), show = !!(info.next && c != null && t >= c && !upDismissed && started);
  if (info.next && c != null && t >= c - 90) prefetchNext();
  if (show !== upnext.classList.contains('show')) upnext.classList.toggle('show', show);
  drawBuffer();
}

// ---------- next episode ----------
let nextInfo = null, nextFetching = false;
function prefetchNext() {
  if (!info.next || nextFetching || (nextInfo && nextInfo.id === info.next.id)) return;
  nextFetching = true;
  fetch('/api/playinfo/' + info.next.id).then(r => r.ok ? r.json() : null).then(j => { nextInfo = j; }).catch(() => {}).finally(() => { nextFetching = false; });
}
async function playNext() {
  clearTimeout(cdTimer);
  const target = info.next; if (!target) return;
  if (started) save('force');
  upnext.classList.remove('show', 'counting');
  cover.classList.remove('gone', 'blocked');
  try {
    let fresh = nextInfo && nextInfo.id === target.id ? nextInfo : null;
    if (!fresh) { const r = await fetch('/api/playinfo/' + target.id); if (!r.ok) throw new Error(r.status); fresh = await r.json(); }
    nextInfo = null;
    stopStream(); dropSubs(); info = fresh; floor = mode === 2 && NET === 'away' ? 2 : 0;
  } catch (e) { location.href = '/watch/' + target.id; return; }
  upDismissed = false; lastSaved = -1;
  history.replaceState(null, '', '/watch/' + info.id);
  render(); chooseTracks(); buildPanel(panel.dataset.kind || 'tracks');
  load(info.pos > 5 ? info.pos : 0, true);
}
function onEnded() {
  save('force');
  if (info.next) { upDismissed = false; upnext.classList.add('show', 'counting'); showUI(true); cdTimer = setTimeout(playNext, 8000); }
  else { $('#endsheet').hidden = false; }
}
upnext.querySelector('.go').addEventListener('click', e => { e.stopPropagation(); playNext(); });
upnext.querySelector('.x').addEventListener('click', e => { e.stopPropagation(); clearTimeout(cdTimer); upDismissed = true; upnext.classList.remove('show', 'counting'); });

// ---------- controls ----------
const toggle = () => { if (v.paused) { tryPlay(); } else v.pause(); };
const jump = s => { const d = dur(); v.currentTime = Math.min(Math.max(0, d - 1), Math.max(0, v.currentTime + s)); onTime(); };
const setVol = x => { v.volume = Math.min(1, Math.max(0, x)); v.muted = v.volume === 0; store.set('htpc-vol', v.volume); };
const isFs = () => document.fullscreenElement || document.webkitFullscreenElement;
function toggleFs() {
  if (isFs()) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
  else if (P.requestFullscreen) P.requestFullscreen().then(() => { try { screen.orientation.lock('landscape').catch(() => {}); } catch (e) {} }).catch(() => {});
  else if (P.webkitRequestFullscreen) P.webkitRequestFullscreen();
  else if (v.webkitEnterFullscreen) v.webkitEnterFullscreen();
}
const skipLabel = document.createElement('span'); $('.skip').prepend(skipLabel); $('.skip').childNodes.forEach(n => { if (n.nodeType === 3) n.remove(); });
const skipIntro = () => {
  if (!$('.skip').classList.contains('show')) return;
  const t = v.currentTime, rc = info.recap;
  if (rc && t >= rc[0] && t < rc[1]) { v.currentTime = rc[1]; toast('Skipped the recap'); }
  else if (info.intro) { v.currentTime = info.intro[1]; toast('Skipped the opening'); }
};
$('#pp').onclick = toggle; $('.center .big').onclick = toggle;
$('#rw').onclick = () => jump(-10); $('#ff').onclick = () => jump(10);
$('#crw').onclick = () => { jump(-10); ripple('l'); }; $('#cff').onclick = () => { jump(10); ripple('r'); };
$('#fs').onclick = toggleFs; $('#nextbtn').onclick = () => playNext();
$('.skip').onclick = e => { e.stopPropagation(); skipIntro(); };
$('#mute').onclick = () => { v.muted = !v.muted; if (!v.muted && v.volume === 0) setVol(.6); };
$('#vr').oninput = e => setVol(+e.target.value);
$('#pip').hidden = !(document.pictureInPictureEnabled && v.requestPictureInPicture);
$('#pip').onclick = () => document.pictureInPictureElement ? document.exitPictureInPicture() : v.requestPictureInPicture().catch(() => {});
$('#tapplay').onclick = e => { e.stopPropagation(); cover.classList.remove('blocked'); tryPlay(); };
$('#retry').onclick = () => { $('#errsheet').hidden = true; cover.classList.remove('gone'); reload(); };

function drawPP() { $('#pp').innerHTML = v.paused ? CFG.icons.play : CFG.icons.pause; $('.center .big').innerHTML = v.paused ? CFG.icons.play : CFG.icons.pause; }
function drawVol() { $('#mute').innerHTML = v.muted || v.volume === 0 ? CFG.icons.mute : CFG.icons.vol; $('#vr').value = v.muted ? 0 : v.volume; }
function drawFs() { $('#fs').innerHTML = isFs() ? CFG.icons.fsx : CFG.icons.fs; }
document.addEventListener('fullscreenchange', drawFs); document.addEventListener('webkitfullscreenchange', drawFs);

// ---------- seek bar ----------
const frac = e => { const r = seek.getBoundingClientRect(); return Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)); };
const chapterAt = t => { let n = ''; for (const c of info.chapters) if (c.t <= t + .5) n = c.name; return n; };
function trickAt(t) {         // Netflix-style preview frame from Jellyfin's 10x10 thumbnail tiles
  const k = info.trick, box = $('.tthumb');
  tip.classList.toggle('has-thumb', !!k);
  if (!k) return;
  const i = Math.max(0, Math.min(k.n - 1, Math.floor(t / k.every))), per = k.tw * k.th, tile = Math.floor(i / per), j = i % per;
  const sx = 224 / k.w, sy = 126 / k.h;
  box.style.backgroundImage = 'url(/stream/' + info.id + '/trick/' + k.w + '/' + tile + '.jpg?_t=' + encodeURIComponent(info.tok) + ')';
  box.style.backgroundSize = (k.tw * 224) + 'px ' + (k.th * 126) + 'px';
  box.style.backgroundPosition = (-(j % k.tw) * k.w * sx) + 'px ' + (-Math.floor(j / k.tw) * k.h * sy) + 'px';
}
function hover(e) {
  const f = frac(e), t = f * dur(), w = seek.clientWidth, ch = chapterAt(t);
  $('.ttext', tip).innerHTML = '<span>' + fmt(t) + '</span>' + (ch ? '<small>' + esc(ch) + '</small>' : '');
  trickAt(t);
  tip.style.left = Math.min(w - tip.offsetWidth / 2, Math.max(tip.offsetWidth / 2, f * w)) + 'px';
  $('.hov').style.width = (f * 100) + '%';
  return f;
}
seek.addEventListener('pointermove', e => { const f = hover(e); if (dragging) { $('.played').style.width = $('.knob').style.left = (f * 100) + '%'; } });
seek.addEventListener('pointerleave', () => { $('.hov').style.width = 0; });
seek.addEventListener('pointerdown', e => { e.stopPropagation(); dragging = true; seek.classList.add('drag'); seek.setPointerCapture(e.pointerId);
  const f = hover(e); $('.played').style.width = $('.knob').style.left = (f * 100) + '%'; showUI(true); });
seek.addEventListener('pointerup', e => { if (!dragging) return; dragging = false; seek.classList.remove('drag'); v.currentTime = frac(e) * dur(); armIdle(); });
seek.addEventListener('pointercancel', () => { dragging = false; seek.classList.remove('drag'); });

// ---------- show / hide controls, taps ----------
const panelOpen = () => panel.classList.contains('show');
function showUI(stay) { P.classList.remove('idle'); clearTimeout(idleT); if (!stay) armIdle(); }
function armIdle() { clearTimeout(idleT); idleT = setTimeout(() => { if (!v.paused && !panelOpen() && !dragging) P.classList.add('idle'); }, 3200); }
P.addEventListener('pointermove', e => { if (e.pointerType === 'mouse') showUI(); });
let lastTap = 0, tapT = null;
const stage = $('.stage');
stage.addEventListener('click', e => {
  if (panelOpen()) { closePanel(); return; }
  if (e.pointerType === 'mouse' || (!e.pointerType && matchMedia('(hover:hover)').matches)) { toggle(); showUI(); return; }
  const now = Date.now(), x = e.clientX / innerWidth;
  if (now - lastTap < 320) {
    clearTimeout(tapT); lastTap = 0;
    if (x < .38) { jump(-10); ripple('l'); } else if (x > .62) { jump(10); ripple('r'); } else toggleFs();
    return;
  }
  lastTap = now;
  tapT = setTimeout(() => { if (P.classList.contains('idle')) showUI(); else if (!v.paused) P.classList.add('idle'); }, 300);
});
stage.addEventListener('dblclick', e => { if (matchMedia('(hover:hover)').matches) { toggleFs(); } });
function ripple(side) { const r = $('.rip.' + side); r.classList.add('show'); clearTimeout(r._t); r._t = setTimeout(() => r.classList.remove('show'), 450); }
document.addEventListener('keydown', e => {
  if (e.ctrlKey || e.metaKey || e.altKey || e.target.tagName === 'INPUT') return;
  const k = e.key.toLowerCase();
  const map = {' ': toggle, k: toggle, f: toggleFs, m: () => { v.muted = !v.muted; }, arrowleft: () => jump(-10), j: () => jump(-10),
    arrowright: () => jump(10), l: () => jump(10), arrowup: () => setVol(v.volume + .1), arrowdown: () => setVol(v.volume - .1),
    n: () => info.next && playNext(), s: skipIntro, escape: closePanel, c: () => openPanel('tracks')};
  if (map[k]) { e.preventDefault(); map[k](); showUI(); }
});

// ---------- audio / subtitles / quality ----------
function openPanel(kind) {
  if (panelOpen() && panel.dataset.kind === kind) return closePanel();
  buildPanel(kind); panel.classList.add('show'); showUI(true);
}
function closePanel() { panel.classList.remove('show'); armIdle(); }
function col(title, opts, sel, pick) {
  const c = document.createElement('div'); c.className = 'col';
  c.innerHTML = '<h5>' + esc(title) + '</h5>';
  opts.forEach(([val, label, sub]) => {
    const b = document.createElement('button'); b.className = 'opt' + (val === sel ? ' on' : '');
    b.innerHTML = CFG.icons.check.replace('class="ic"', 'class="ic ck"') + '<span>' + esc(label) + (sub ? '<small>' + esc(sub) + '</small>' : '') + '</span>';
    b.onclick = e => { e.stopPropagation(); if (val !== sel) pick(val); closePanel(); };
    c.appendChild(b);
  });
  return c;
}
function buildPanel(kind) {
  panel.dataset.kind = kind; panel.innerHTML = '';
  if (kind === 'tracks') {
    panel.appendChild(col('Audio', info.audios.map(a => [a.i, a.label, a.detail]), cur.a, i => { cur.a = i; rememberTracks();
      const sub = info.subs.find(x => x.i === cur.s), au = info.audios.find(x => x.i === i);
      if (info.anime && au && sub && au.lang.startsWith('en') && sub.lang === 'eng' && sub.kind !== 'signs') {
        const signs = info.subs.find(x => x.lang === 'eng' && x.kind === 'signs'); cur.s = signs ? signs.i : -1; rememberTracks();
      } else if (info.anime && au && !au.lang.startsWith('en') && (!sub || sub.kind === 'signs')) {
        const full = info.subs.find(x => x.lang === 'eng' && x.kind === 'full'); if (full) { cur.s = full.i; rememberTracks(); }
      }
      reload('Switching audio…'); }));
    panel.appendChild(col('Subtitles', [[-1, 'Off', '']].concat(info.subs.map(s => [s.i, s.label, s.detail || (s.kind === 'signs' ? 'Signs & Songs' : '')])),
      cur.s, i => { cur.s = i; rememberTracks();
        if (wantMode() !== mode) reload(i < 0 ? 'Subtitles off' : 'Switching subtitles…');
        else { syncSubs(); toast(i < 0 ? 'Subtitles off' : 'Subtitles: ' + (info.subs.find(x => x.i === i) || {}).label); } }));
  } else {
    const q = col('Quality', CFG.qualities.map(([k, l, s]) => [k, l, s]), quality, k => { quality = k; floor = 0; store.set('htpc-q-' + CFG.net, k); reload('Switching quality…'); });
    const now = document.createElement('div'); now.className = 'now';
    now.innerHTML = '<b>Now playing</b>' + esc(modeText()) + (bwGet() ? '<br>Connection ≈ ' + mbps(bwGet()) : '');
    q.insertBefore(now, q.children[1]);
    panel.appendChild(q);
    panel.appendChild(col('Speed', [[.75, '0.75×'], [1, 'Normal'], [1.25, '1.25×'], [1.5, '1.5×'], [2, '2×']].map(([x, l]) => [x, l, '']),
      v.playbackRate, x => { v.playbackRate = x; toast(x === 1 ? 'Normal speed' : x + '× speed'); }));
  }
}
$('#ccbtn').onclick = e => { e.stopPropagation(); openPanel('tracks'); };
$('#setbtn').onclick = e => { e.stopPropagation(); openPanel('quality'); };
panel.addEventListener('click', e => e.stopPropagation());

// ---------- continue on the TV ----------
$('#tvbtn').onclick = async e => {
  e.stopPropagation();
  const t = v.currentTime || info.pos || 0;
  v.pause(); save('force'); toast('Starting on the TV…');
  try {
    const r = await fetch('/api/tv', {method: 'POST', body: new URLSearchParams({id: info.id, pos: t.toFixed(1)})});
    const j = await r.json();
    if (!j.ok) return toast(j.msg);
    stopStream(); if (hls) { hls.destroy(); hls = null; }
    $('#tvmsg').textContent = (info.code ? info.code + ' · ' : '') + (info.name || info.show) + ' from ' + fmt(t);
    $('#tvsheet').hidden = false; P.classList.remove('buffering');
  } catch (err) { toast("Couldn't reach the TV."); }
};
$('#here').onclick = () => { $('#tvsheet').hidden = true; load(v.currentTime || info.pos, true); };

// ---------- toast ----------
let toastT = null;
function toast(msg) { const t = $('.toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove('show'), 2200); }

// ---------- video events ----------
v.addEventListener('playing', () => {
  const now = performance.now();
  if (!started) {
    started = true; clearTimeout(wdT);
    log('start', ((now - t0) / 1000).toFixed(2) + ' s to first frame at ' + fmt(loadAt) + ' (' + (info.bitrate / 1e6).toFixed(1) + ' Mbps file, ' + info.video.codec + ')');
    if (mode === 0 && info.audios.length) setTimeout(() => {      // Chromium plays silently if it can't decode the audio
      if (mode === 0 && 'webkitAudioDecodedByteCount' in v && v.webkitAudioDecodedByteCount === 0 && !v.paused) fallback('audio not decodable');
    }, 4000);
  }
  if (seekT0) { const d = (now - seekT0) / 1000; if (d > 1.2) log('seek', d.toFixed(2) + ' s'); seekT0 = 0; }
  else if (waitT0) { const d = (now - waitT0) / 1000; if (d > 1) { log('stall', d.toFixed(2) + ' s at ' + fmt(v.currentTime) + ' · ' + modeText()); stalls.push(performance.now()); } }
  waitT0 = 0;
  P.classList.remove('buffering', 'paused'); cover.classList.add('gone'); cover.classList.remove('blocked'); drawPP(); armIdle();
});
v.addEventListener('waiting', () => { P.classList.add('buffering'); if (started && !waitT0 && !seekT0) waitT0 = performance.now(); });
v.addEventListener('seeking', () => { P.classList.add('buffering'); if (started) seekT0 = performance.now(); });
v.addEventListener('canplay', () => P.classList.remove('buffering'));
v.addEventListener('seeked', () => { P.classList.remove('buffering'); save(); if (v.paused) seekT0 = 0; });
v.addEventListener('pause', () => { P.classList.add('paused'); drawPP(); showUI(true); save('force'); });
v.addEventListener('play', () => { P.classList.remove('paused'); drawPP(); armIdle(); });
v.addEventListener('timeupdate', onTime);
v.addEventListener('progress', drawBuffer);
v.addEventListener('durationchange', () => { drawChapters(); onTime(); });
v.addEventListener('ended', onEnded);
v.addEventListener('volumechange', drawVol);

const vol = store.get('htpc-vol', 1); v.volume = Math.min(1, Math.max(0, +vol || 1));
drawPP(); drawVol(); drawFs(); render(); chooseTracks(); buildPanel('tracks');
const startAt = CFG.start != null ? CFG.start : (info.pos > 5 ? info.pos : 0);
load(startAt, true);
if (startAt > 5) toast('Resuming from ' + fmt(startAt));
showUI();
})();
