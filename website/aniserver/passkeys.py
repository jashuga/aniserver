"""Passkeys (WebAuthn): sign in with Windows Hello, Face ID, Touch ID or a phone instead of a password + code.
A passkey lives on the device (not in the browser's cookies), so it keeps working after a browser forgets everything.
Standard library only: a small CBOR reader and ES256 (P-256) / RS256 signature checks. Passkeys need HTTPS (or
localhost), so they work on the public address, not on http://<laptop>:5050."""
import base64
import hashlib
import json
import secrets
import time

from aniserver.auth import remote_users, save_remote_users

CHALLENGES = {}           # challenge -> (purpose, user or None, expiry)
CHALLENGE_SECONDS = 300


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------------- CBOR (just what WebAuthn uses) ----------------
def cbor_decode(data, i=0):
    """Decode one CBOR item at data[i:]: returns (value, index after it)."""
    head = data[i]
    major, info = head >> 5, head & 31
    i += 1
    if info < 24:
        arg = info
    elif info in (24, 25, 26, 27):
        n = 1 << (info - 24)
        arg = int.from_bytes(data[i:i + n], "big")
        i += n
    else:
        raise ValueError("unsupported CBOR length")
    if major == 0:
        return arg, i
    if major == 1:
        return -1 - arg, i
    if major == 2:
        return bytes(data[i:i + arg]), i + arg
    if major == 3:
        return data[i:i + arg].decode(), i + arg
    if major == 4:
        out = []
        for _ in range(arg):
            v, i = cbor_decode(data, i)
            out.append(v)
        return out, i
    if major == 5:
        out = {}
        for _ in range(arg):
            k, i = cbor_decode(data, i)
            out[k], i = cbor_decode(data, i)
        return out, i
    if major == 7 and info in (20, 21, 22):
        return {20: False, 21: True, 22: None}[info], i
    raise ValueError("unsupported CBOR item")


# ---------------- signatures ----------------
P256_P = 2**256 - 2**224 + 2**192 + 2**96 - 1
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
P256_G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
          0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    (x1, y1), (x2, y2) = a, b
    if x1 == x2 and (y1 + y2) % P256_P == 0:
        return None
    if a == b:
        m = (3 * x1 * x1 - 3) * pow(2 * y1, -1, P256_P) % P256_P
    else:
        m = (y2 - y1) * pow(x2 - x1, -1, P256_P) % P256_P
    x3 = (m * m - x1 - x2) % P256_P
    return x3, (m * (x1 - x3) - y1) % P256_P


def _mul(k, point):
    out = None
    while k:
        if k & 1:
            out = _add(out, point)
        point = _add(point, point)
        k >>= 1
    return out


def _der_ints(sig):
    """ECDSA signature (DER SEQUENCE of two INTEGERs) -> (r, s)."""
    if sig[0] != 0x30:
        raise ValueError("bad signature")
    i, out = 2 if sig[1] < 0x80 else 2 + (sig[1] & 0x7F), []
    for _ in range(2):
        if sig[i] != 0x02:
            raise ValueError("bad signature")
        n = sig[i + 1]
        out.append(int.from_bytes(sig[i + 2:i + 2 + n], "big"))
        i += 2 + n
    return out


def verify_es256(x, y, message, sig):
    if (y * y - (x * x * x - 3 * x + P256_B)) % P256_P:
        return False                                    # not a point on the curve
    r, s = _der_ints(sig)
    if not (0 < r < P256_N and 0 < s < P256_N):
        return False
    e = int.from_bytes(hashlib.sha256(message).digest(), "big")
    w = pow(s, -1, P256_N)
    point = _add(_mul(e * w % P256_N, P256_G), _mul(r * w % P256_N, (x, y)))
    return point is not None and point[0] % P256_N == r


SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")   # DigestInfo for SHA-256


def verify_rs256(n, e, message, sig):
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    em = pow(int.from_bytes(sig, "big"), e, n).to_bytes(k, "big")
    digest = SHA256_PREFIX + hashlib.sha256(message).digest()
    return em == b"\x00\x01" + b"\xff" * (k - 3 - len(digest)) + b"\x00" + digest


def verify(key, message, sig):
    if key["alg"] == -7:
        return verify_es256(int(key["x"], 16), int(key["y"], 16), message, sig)
    if key["alg"] == -257:
        return verify_rs256(int(key["n"], 16), int(key["e"], 16), message, sig)
    return False


def cose_key(cose):
    """COSE public key (decoded CBOR map) -> what we store."""
    alg, kty = cose.get(3), cose.get(1)
    if alg == -7 and kty == 2 and cose.get(-1) == 1:
        return {"alg": -7, "x": cose[-2].hex(), "y": cose[-3].hex()}
    if alg == -257 and kty == 3:
        return {"alg": -257, "n": cose[-1].hex(), "e": cose[-2].hex()}
    raise ValueError("unsupported key type")


# ---------------- ceremonies ----------------
def new_challenge(purpose, user=None):
    now = time.time()
    for c in [c for c, v in CHALLENGES.items() if v[2] < now]:
        CHALLENGES.pop(c, None)
    c = b64url(secrets.token_bytes(32))
    CHALLENGES[c] = (purpose, user, now + CHALLENGE_SECONDS)
    return c


def check_client_data(raw, kind, purpose, rp_id, user=None):
    """The browser's own record of what it signed: right kind, one of our challenges (used once), our site."""
    cd = json.loads(raw)
    held = CHALLENGES.pop(cd.get("challenge", ""), None)
    if cd.get("type") != kind or not held or held[0] != purpose or held[2] < time.time() or (user and held[1] != user):
        raise ValueError("This sign-in request expired. Try again.")
    allowed = {f"https://{rp_id}"} | ({"http://localhost", "http://localhost:5050"} if rp_id == "localhost" else set())
    if cd.get("origin") not in allowed:
        raise ValueError("Passkeys only work on this site's own address.")


def check_auth_data(auth, rp_id):
    if auth[:32] != hashlib.sha256(rp_id.encode()).digest():
        raise ValueError("That passkey belongs to a different site.")
    flags = auth[32]
    if not flags & 0x01 or not flags & 0x04:              # user present + verified (PIN, face, fingerprint)
        raise ValueError("Your device didn't confirm it's you.")
    return flags, int.from_bytes(auth[33:37], "big")


def register_options(user, display, rp_id):
    have = (remote_users().get(user) or {}).get("passkeys") or []
    return {"challenge": new_challenge("register", user), "rp": {"id": rp_id, "name": "aniserver"},
            "user": {"id": b64url(user.encode()), "name": user, "displayName": display},
            "pubKeyCredParams": [{"type": "public-key", "alg": -7}, {"type": "public-key", "alg": -257}],
            "authenticatorSelection": {"residentKey": "required", "requireResidentKey": True, "userVerification": "required"},
            "excludeCredentials": [{"type": "public-key", "id": k["id"]} for k in have],
            "attestation": "none", "timeout": 120000}


def register(user, body, rp_id, name):
    check_client_data(unb64url(body["clientDataJSON"]), "webauthn.create", "register", rp_id, user)
    att, _ = cbor_decode(unb64url(body["attestationObject"]))
    auth = att["authData"]
    flags, count = check_auth_data(auth, rp_id)
    if not flags & 0x40:
        raise ValueError("Your device didn't send a passkey.")
    n = int.from_bytes(auth[53:55], "big")
    cred_id = auth[55:55 + n]
    cose, _ = cbor_decode(auth, 55 + n)
    users = remote_users()
    rec = users[user]
    rec.setdefault("passkeys", []).append({"id": b64url(cred_id), "key": cose_key(cose), "count": count,
                                           "name": name[:60] or "Passkey", "added": int(time.time())})
    save_remote_users(users)


def login_options(rp_id):
    return {"challenge": new_challenge("login"), "rpId": rp_id, "userVerification": "required", "timeout": 120000,
            "allowCredentials": []}


def login(body, rp_id):
    """Check a passkey sign-in. Returns the person's login name."""
    users = remote_users()
    cred = body.get("id", "")
    user, key = next(((u, k) for u, rec in users.items() for k in rec.get("passkeys") or [] if k["id"] == cred), (None, None))
    if not user:
        raise ValueError("This passkey isn't registered here (it may have been removed).")
    raw_cd = unb64url(body["clientDataJSON"])
    check_client_data(raw_cd, "webauthn.get", "login", rp_id)
    auth = unb64url(body["authenticatorData"])
    _, count = check_auth_data(auth, rp_id)
    if not verify(key["key"], auth + hashlib.sha256(raw_cd).digest(), unb64url(body["signature"])):
        raise ValueError("That passkey's signature didn't check out.")
    if count and key.get("count") and count <= key["count"]:
        raise ValueError("That passkey looks copied. Remove it on the Account page and add it again.")
    key["count"], key["used"] = count, int(time.time())
    save_remote_users(users)
    return user


def remove(user, cred_id):
    users = remote_users()
    rec = users.get(user) or {}
    rec["passkeys"] = [k for k in rec.get("passkeys") or [] if k["id"] != cred_id]
    save_remote_users(users)
