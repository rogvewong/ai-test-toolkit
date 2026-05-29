"""Ed25519 signing for TDR non-repudiation.

Each reviewer / moderator signature is bound to a deterministic payload
hash so the record cannot be retroactively edited.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    _BACKEND = "cryptography"
except ImportError:  # pragma: no cover
    Ed25519PrivateKey = None  # type: ignore[assignment]
    Ed25519PublicKey = None  # type: ignore[assignment]
    _BACKEND = "fallback"


@dataclass
class KeyPair:
    private_key_pem: bytes
    public_key_pem: bytes


def generate_keypair() -> KeyPair:
    if _BACKEND != "cryptography":
        raise RuntimeError(
            "cryptography not installed — please `pip install cryptography`"
        )
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    return KeyPair(
        private_key_pem=priv.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
        ),
        public_key_pem=pub.public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        ),
    )


def artifact_hash(payload: Any) -> str:
    normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def sign_artifact(payload: Any, *, private_key_pem: bytes) -> str:
    if _BACKEND != "cryptography":
        raise RuntimeError("cryptography not installed")
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    priv = load_pem_private_key(private_key_pem, password=None)
    if not isinstance(priv, Ed25519PrivateKey):
        raise ValueError("key is not Ed25519")
    digest = artifact_hash(payload).encode("utf-8")
    sig = priv.sign(digest)
    return base64.b64encode(sig).decode("ascii")


def verify_signature(
    payload: Any, signature: str, *, public_key_pem: bytes
) -> bool:
    if _BACKEND != "cryptography":
        return False
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    pub = load_pem_public_key(public_key_pem)
    if not isinstance(pub, Ed25519PublicKey):
        return False
    digest = artifact_hash(payload).encode("utf-8")
    try:
        pub.verify(base64.b64decode(signature), digest)
        return True
    except InvalidSignature:
        return False


def write_keypair(kp: KeyPair, dir_path: Path) -> tuple[Path, Path]:
    dir_path.mkdir(parents=True, exist_ok=True)
    priv = dir_path / "tdr_ed25519.pem"
    pub = dir_path / "tdr_ed25519.pub.pem"
    priv.write_bytes(kp.private_key_pem)
    pub.write_bytes(kp.public_key_pem)
    return priv, pub
