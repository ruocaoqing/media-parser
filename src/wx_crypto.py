import base64

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF
from Crypto.Random import get_random_bytes

# HKDF 的 context：把派生出密钥的用途钉死，将来需要别的密钥时加一条新 context 即可，
# 不会和这一条互相干扰
_HKDF_CONTEXT = b"media-parser wx session_key v1"
_KEY_LENGTH = 32
_NONCE_LENGTH = 12
_TAG_LENGTH = 16


def _derive_key(secret_key):
    """由 SECRET_KEY 派生一把专属密钥 —— SECRET_KEY 已由 _load_or_create_secret 持久化。"""
    if not secret_key:
        raise RuntimeError("SECRET_KEY 未配置，无法加密 session_key")
    return HKDF(
        master=secret_key.encode("utf-8"),
        key_len=_KEY_LENGTH,
        salt=None,
        hashmod=SHA256,
        num_keys=1,
        context=_HKDF_CONTEXT,
    )


def encrypt_session_key(session_key, secret_key):
    """AES-GCM 加密，返回 base64(nonce ‖ tag ‖ 密文)。"""
    if not session_key:
        return ""
    cipher = AES.new(_derive_key(secret_key), AES.MODE_GCM, nonce=get_random_bytes(_NONCE_LENGTH))
    ciphertext, tag = cipher.encrypt_and_digest(str(session_key).encode("utf-8"))
    return base64.b64encode(cipher.nonce + tag + ciphertext).decode("ascii")


def decrypt_session_key(blob, secret_key):
    """还原 session_key。密文被篡改或 SECRET_KEY 变了都会抛 ValueError（GCM 认证失败）。"""
    if not blob:
        return ""
    raw = base64.b64decode(blob)
    nonce = raw[:_NONCE_LENGTH]
    tag = raw[_NONCE_LENGTH:_NONCE_LENGTH + _TAG_LENGTH]
    ciphertext = raw[_NONCE_LENGTH + _TAG_LENGTH:]
    cipher = AES.new(_derive_key(secret_key), AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
