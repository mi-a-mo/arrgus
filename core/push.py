"""Phone notifications through the Web Push standard (RFC 8030/8291/8292), sent straight from Arrgus to the
notification services of the phones that subscribed in the app (Apple, Google, Mozilla). Messages are
encrypted for each phone, so those services only pass them along.

Needs the `cryptography` package (Ubuntu/Debian: python3-cryptography) for the elliptic-curve maths; without
it the rest of Arrgus works and the app says notifications are unavailable.
State (signing key + subscribed phones) lives in state/push.json, readable only by the Arrgus user.
"""
import base64
import hashlib
import hmac
import json
import os
import queue
import secrets
import struct
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .util import STATE_DIR, log, now_iso

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAVE_CRYPTO = True
except ImportError:  # pragma: no cover
    HAVE_CRYPTO = False

FILE = STATE_DIR / "push.json"

# What a phone can be notified about. `default` = switched on for a newly subscribed phone.
CATEGORIES = {
    "problems": {"label": "Problems Arrgus couldn't fix", "default": True},
    "ai_needs_you": {"label": "The AI needs you", "default": True},
    "fixed": {"label": "Problems fixed or resolved", "default": False},
    "ai_done": {"label": "AI sessions finished", "default": False},
    "updates": {"label": "Updates available", "default": False},
    "reboots": {"label": "Reboots and post-boot checks", "default": False},
}
URGENT = {"problems", "ai_needs_you"}


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hkdf(salt, ikm, info, length):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]  # one block is enough (<= 32 bytes)


def encrypt(payload, p256dh, auth, _as_priv=None, _salt=None):
    """RFC 8291 / RFC 8188 'aes128gcm' encryption of `payload` for one subscription."""
    ua_pub = unb64u(p256dh)
    auth_secret = unb64u(auth)
    if len(ua_pub) != 65 or ua_pub[0] != 4 or len(auth_secret) < 16:
        raise ValueError("bad subscription keys")
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    as_priv = _as_priv or ec.generate_private_key(ec.SECP256R1())  # _as_priv/_salt: only for the RFC test vector
    as_pub = as_priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = as_priv.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = _salt or secrets.token_bytes(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)   # 0x02: last (and only) record
    return salt + struct.pack(">I", 4096) + bytes([len(as_pub)]) + as_pub + body


class Push:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.q = queue.Queue()
        self.data = {"vapid": None, "subs": []}
        if FILE.exists():
            try:
                self.data.update(json.loads(FILE.read_text()))
            except ValueError:
                log.warning("push: %s is unreadable; starting with no subscriptions", FILE)
        self._key = None
        if HAVE_CRYPTO:
            self._load_key()
        threading.Thread(target=self._worker, daemon=True).start()

    # ---- keys + storage --------------------------------------------------
    @property
    def available(self):
        return HAVE_CRYPTO and self._key is not None

    def unavailable_reason(self):
        return "" if self.available else "Install the python3-cryptography package on the server, then restart Arrgus."

    def _load_key(self):
        pem = (self.data.get("vapid") or {}).get("private_pem")
        if pem:
            self._key = serialization.load_pem_private_key(pem.encode(), password=None)
        else:
            self._key = ec.generate_private_key(ec.SECP256R1())
            self.data["vapid"] = {"private_pem": self._key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
                "created": now_iso()}
            self._save()

    def public_key(self):
        return b64u(self._key.public_key().public_bytes(serialization.Encoding.X962,
                                                        serialization.PublicFormat.UncompressedPoint)) if self._key else ""

    def _save(self):
        with self.lock:
            FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1))
            tmp.chmod(0o600)
            os.replace(tmp, FILE)

    # ---- subscriptions ---------------------------------------------------
    def subs(self):
        return list(self.data.get("subs", []))

    def find(self, endpoint):
        return next((s for s in self.data["subs"] if s["endpoint"] == endpoint), None)

    def subscribe(self, sub, name, prefs=None):
        ep = (sub or {}).get("endpoint") or ""
        keys = (sub or {}).get("keys") or {}
        u = urlparse(ep)
        if u.scheme != "https" or not u.hostname or len(ep) > 1000:
            raise ValueError("invalid subscription endpoint")
        try:
            ok = len(unb64u(keys.get("p256dh", ""))) == 65 and len(unb64u(keys.get("auth", ""))) >= 16
        except Exception:
            ok = False
        if not ok:
            raise ValueError("invalid subscription keys")
        p = {k: bool((prefs or {}).get(k, v["default"])) for k, v in CATEGORIES.items()}
        with self.lock:
            old = self.find(ep)
            if old:
                old.update(keys=keys, name=name[:60] or old.get("name"), prefs=p if prefs else old["prefs"])
                entry = old
            else:
                entry = {"id": secrets.token_hex(4), "endpoint": ep, "keys": keys, "name": name[:60] or "Phone",
                         "created": now_iso(), "prefs": p, "last": None}
                self.data["subs"].append(entry)
        self._save()
        return entry

    def set_prefs(self, endpoint, prefs):
        s = self.find(endpoint)
        if not s:
            raise KeyError("not subscribed")
        s["prefs"] = {k: bool(prefs.get(k, s["prefs"].get(k, v["default"]))) for k, v in CATEGORIES.items()}
        self._save()
        return s

    def remove(self, endpoint=None, sid=None):
        with self.lock:
            before = len(self.data["subs"])
            self.data["subs"] = [s for s in self.data["subs"] if s["endpoint"] != endpoint and s["id"] != sid]
            gone = before != len(self.data["subs"])
        if gone:
            self._save()
        return gone

    # ---- sending ---------------------------------------------------------
    def _vapid(self, endpoint):
        u = urlparse(endpoint)
        claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 3600,
                  "sub": (self.cfg.get("notify") or {}).get("push_contact") or "mailto:arrgus@example.com"}
        head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        body = b64u(json.dumps(claims, separators=(",", ":")).encode())
        der = self._key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return f"vapid t={head}.{body}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}, k={self.public_key()}"

    def deliver(self, sub, message, urgent=False, ttl=86400):
        """Send one message now. Returns (ok, detail). Removes subscriptions the service says are gone."""
        payload = json.dumps(message, separators=(",", ":")).encode()
        if len(payload) > 3000:
            message = dict(message, body=message.get("body", "")[:400] + "…")
            payload = json.dumps(message, separators=(",", ":")).encode()[:3000]
        try:
            data = encrypt(payload, sub["keys"]["p256dh"], sub["keys"]["auth"])
            req = urllib.request.Request(sub["endpoint"], data=data, method="POST", headers={
                "Authorization": self._vapid(sub["endpoint"]), "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream", "TTL": str(ttl), "Urgency": "high" if urgent else "normal"})
            with urllib.request.urlopen(req, timeout=20) as r:
                code, detail = r.status, "delivered"
        except urllib.error.HTTPError as e:
            code, detail = e.code, (e.read() or b"")[:300].decode("utf-8", "replace") or e.reason
        except Exception as e:
            code, detail = 0, str(e)[:300]
        ok = 200 <= code < 300
        sub["last"] = {"at": now_iso(), "ok": ok, "detail": f"HTTP {code}: {detail}" if code else detail}
        if code in (404, 410):     # the phone unsubscribed or the app was removed
            self.remove(endpoint=sub["endpoint"])
            log.info("push: removed expired subscription %s (%s)", sub.get("name"), code)
        else:
            self._save()
        if not ok:
            log.warning("push to %s failed: %s", sub.get("name"), sub["last"]["detail"])
        return ok, sub["last"]["detail"]

    def send(self, categories, title, body="", url="/app", tag=None):
        """Queue a notification for every phone that wants any of `categories`."""
        if not self.available:
            return
        cats = [categories] if isinstance(categories, str) else list(categories)
        targets = [s for s in self.data["subs"] if any(s["prefs"].get(c) for c in cats)]
        if not targets:
            return
        box = self.cfg["box"]["name"]
        msg = {"title": f"Arrgus · {box}", "body": (title + ("\n" + body if body else ""))[:600],
               "url": url, "tag": tag or f"arrgus-{cats[0]}-{secrets.token_hex(3)}"}
        for s in targets:
            self.q.put((s, msg, any(c in URGENT for c in cats)))

    def _worker(self):
        while True:
            s, msg, urgent = self.q.get()
            try:
                if self.find(s["endpoint"]):
                    self.deliver(s, msg, urgent)
            except Exception:
                log.exception("push worker")
