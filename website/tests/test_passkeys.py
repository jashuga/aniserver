"""Passkeys (aniserver/passkeys.py): add one, sign in with it, and refuse anything that doesn't check out.
Signatures are made here with the same math (ES256 like most phones/Macs, RS256 like Windows Hello)."""
import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path

from helpers import Patch, app

pk = app.passkeys
RP = "aniserver.example"


def cbor(v):
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 1 << (8 * size):
                return bytes([major << 5 | info]) + n.to_bytes(size, "big")
    if isinstance(v, bool):
        return bytes([0xF5 if v else 0xF4])
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes):
        return head(2, len(v)) + v
    if isinstance(v, str):
        return head(3, len(v.encode())) + v.encode()
    if isinstance(v, list):
        return head(4, len(v)) + b"".join(cbor(x) for x in v)
    return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())


class EcKey:
    def __init__(self):
        self.d = random.randrange(1, pk.P256_N)
        self.x, self.y = pk._mul(self.d, pk.P256_G)

    def cose(self):
        return {1: 2, 3: -7, -1: 1, -2: self.x.to_bytes(32, "big"), -3: self.y.to_bytes(32, "big")}

    def sign(self, msg):
        e = int.from_bytes(hashlib.sha256(msg).digest(), "big")
        while True:
            k = random.randrange(1, pk.P256_N)
            r = pk._mul(k, pk.P256_G)[0] % pk.P256_N
            s = pow(k, -1, pk.P256_N) * (e + r * self.d) % pk.P256_N
            if r and s:
                break
        der = lambda i: (lambda b: b"\x02" + bytes([len(b)]) + b)(i.to_bytes((i.bit_length() + 8) // 8, "big"))
        body = der(r) + der(s)
        return b"\x30" + bytes([len(body)]) + body


def prime(bits):
    while True:
        n = random.getrandbits(bits) | (1 << bits - 1) | 1
        if all(pow(a, n - 1, n) == 1 for a in (2, 3, 5, 7, 11, 13, 17, 19, 23)):
            return n


class RsaKey:
    def __init__(self):
        p, q = prime(512), prime(512)
        self.n, self.e = p * q, 65537
        self.d = pow(self.e, -1, (p - 1) * (q - 1))

    def cose(self):
        return {1: 3, 3: -257, -1: self.n.to_bytes(128, "big"), -2: self.e.to_bytes(3, "big")}

    def sign(self, msg):
        digest = pk.SHA256_PREFIX + hashlib.sha256(msg).digest()
        em = b"\x00\x01" + b"\xff" * (128 - 3 - len(digest)) + b"\x00" + digest
        return pow(int.from_bytes(em, "big"), self.d, self.n).to_bytes(128, "big")


def auth_data(rp=RP, flags=0x05, count=0, cred=b"", key=None):
    out = hashlib.sha256(rp.encode()).digest() + bytes([flags]) + count.to_bytes(4, "big")
    if key:
        out += b"\x00" * 16 + len(cred).to_bytes(2, "big") + cred + cbor(key.cose())
    return out


def client_data(kind, challenge, origin=f"https://{RP}"):
    return json.dumps({"type": kind, "challenge": challenge, "origin": origin}).encode()


class Passkeys(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        users = Path(self.tmp.name) / "users.json"
        users.write_text(json.dumps({"alice": {"pw": "x", "totp": "y"}, "bob": {"pw": "x", "totp": "y"}}))
        self.patch = Patch(REMOTE_USERS_FILE=users)
        self.patch.__enter__()

    def tearDown(self):
        self.patch.__exit__(None, None, None)
        self.tmp.cleanup()

    def add(self, user, key, cred):
        ch = pk.register_options(user, user.title(), RP)["challenge"]
        att = cbor({"fmt": "none", "attStmt": {}, "authData": auth_data(flags=0x45, cred=cred, key=key)})
        pk.register(user, {"clientDataJSON": pk.b64url(client_data("webauthn.create", ch)), "attestationObject": pk.b64url(att)}, RP, "Windows Hello")

    def sign_in(self, key, cred, rp=RP, origin=f"https://{RP}", count=0, tamper=False, challenge=None):
        ch = challenge or pk.login_options(rp)["challenge"]
        cd, ad = client_data("webauthn.get", ch, origin), auth_data(rp=RP, count=count)
        sig = key.sign(ad + hashlib.sha256(cd).digest())
        if tamper:
            ad = auth_data(rp=RP, count=count + 99)
        return pk.login({"id": pk.b64url(cred), "clientDataJSON": pk.b64url(cd), "authenticatorData": pk.b64url(ad),
                         "signature": pk.b64url(sig)}, rp), ch

    def test_windows_hello_rs256_and_phone_es256_sign_in_the_right_person(self):
        rsa, ec = RsaKey(), EcKey()
        self.add("alice", rsa, b"hello-cred")
        self.add("bob", ec, b"phone-cred")
        self.assertEqual(self.sign_in(rsa, b"hello-cred")[0], "alice")
        self.assertEqual(self.sign_in(ec, b"phone-cred")[0], "bob")
        stored = app.remote_users()["alice"]["passkeys"][0]
        self.assertEqual((stored["name"], stored["key"]["alg"]), ("Windows Hello", -257))

    def test_forged_reused_or_foreign_sign_ins_are_refused(self):
        ec = EcKey()
        self.add("alice", ec, b"c1")
        with self.assertRaises(ValueError):
            self.sign_in(ec, b"c1", tamper=True)                         # signature doesn't match what was sent
        with self.assertRaises(ValueError):
            self.sign_in(EcKey(), b"c1")                                 # someone else's key
        with self.assertRaises(ValueError):
            self.sign_in(ec, b"c1", origin="https://evil.example")       # another site
        _, used = self.sign_in(ec, b"c1")
        with self.assertRaises(ValueError):
            self.sign_in(ec, b"c1", challenge=used)                      # a request can't be replayed
        with self.assertRaises(ValueError):
            self.sign_in(ec, b"unknown")

    def test_a_removed_passkey_no_longer_signs_in(self):
        ec = EcKey()
        self.add("alice", ec, b"c1")
        pk.remove("alice", pk.b64url(b"c1"))
        with self.assertRaises(ValueError):
            self.sign_in(ec, b"c1")


if __name__ == "__main__":
    unittest.main()
