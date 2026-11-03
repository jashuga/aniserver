// Passkeys (Windows Hello, Face ID, Touch ID, a phone): sign in on the login page, add/remove on the Account page.
(() => {
  const ok = window.PublicKeyCredential && window.isSecureContext;
  const b64 = buf => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const unb64 = s => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - s.length % 4) % 4)), c => c.charCodeAt(0));
  const post = (url, body) => fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})}).then(r => r.json());
  const say = (el, text, bad) => { if (el) { el.textContent = text; el.className = 'msg ' + (bad ? 'err' : 'ok'); el.hidden = false; } };
  const cancelled = e => e && (e.name === 'NotAllowedError' || e.name === 'AbortError');

  const signin = document.getElementById('pk-signin');
  if (signin) {
    if (!ok) signin.closest('.pk').hidden = true;
    signin.addEventListener('click', async () => {
      const msg = document.getElementById('pk-msg');
      try {
        const o = await post('/api/passkey/login/options');
        const c = await navigator.credentials.get({publicKey: {challenge: unb64(o.challenge), rpId: o.rpId, userVerification: o.userVerification,
                                                               timeout: o.timeout, allowCredentials: []}});
        const r = await post('/api/passkey/login', {id: c.id, clientDataJSON: b64(c.response.clientDataJSON),
          authenticatorData: b64(c.response.authenticatorData), signature: b64(c.response.signature), next: signin.dataset.next || '/'});
        if (r.ok) location.href = r.next; else say(msg, r.msg, true);
      } catch (e) { if (!cancelled(e)) say(msg, "Your device couldn't sign in with a passkey: " + e.message, true); }
    });
  }

  const add = document.getElementById('pk-add');
  if (add) {
    const msg = document.getElementById('pk-msg');
    if (!ok) { add.disabled = true; say(msg, 'Passkeys work on the public address (https), not on this local one.', true); }
    add.addEventListener('click', async () => {
      try {
        const o = await post('/api/passkey/register/options');
        o.challenge = unb64(o.challenge); o.user.id = unb64(o.user.id);
        o.excludeCredentials = o.excludeCredentials.map(x => ({type: x.type, id: unb64(x.id)}));
        const c = await navigator.credentials.create({publicKey: o});
        const name = /Windows/.test(navigator.userAgent) ? 'Windows Hello' : /iPhone|iPad/.test(navigator.userAgent) ? 'iPhone / iPad'
                   : /Mac/.test(navigator.userAgent) ? 'Mac' : /Android/.test(navigator.userAgent) ? 'Android' : 'Passkey';
        const r = await post('/api/passkey/register', {id: c.id, clientDataJSON: b64(c.response.clientDataJSON),
                                                       attestationObject: b64(c.response.attestationObject), name});
        if (r.ok) location.href = '/account?ok=' + encodeURIComponent('Passkey added. Next time, sign in with it.'); else say(msg, r.msg, true);
      } catch (e) {
        if (e && e.name === 'InvalidStateError') say(msg, 'This device already has a passkey here.', true);
        else if (!cancelled(e)) say(msg, "Couldn't add a passkey: " + e.message, true);
      }
    });
  }
})();
