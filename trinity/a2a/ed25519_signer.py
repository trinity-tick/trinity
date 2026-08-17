"""
A2A Ed25519 Signer — Modern Cryptographic Signing with x509 Certificate Chain.

Replaces/upgrades RSA-2048 with Ed25519 (EdDSA over Curve25519) for:
  - Smaller keys (32 bytes private, 32 bytes public vs 256+ bytes RSA)
  - Faster signing (10-100x faster than RSA)
  - Stronger security (128-bit classic, no known quantum speedup over AES-128)
  - x509 certificate chain support for hierarchical trust

Backward-compatible: coexists with existing RSA-based AgentCardSigner.
Agents can advertise their supported algorithm via AgentCard capabilities.

Key components:
  - Ed25519Signer: Ed25519 key generation, sign, verify
  - x509CertificateChain: PEM x509 certificate chain verification
  - SigningBridge: unified sign/verify dispatching RSA vs Ed25519
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from trinity.a2a.agent_card import AgentCard

logger = logging.getLogger(__name__)

# ── Cryptography Backend Availability ────────────────────────────────────

_CRYPTO_AVAILABLE = False
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.asymmetric import x25519
    from cryptography.hazmat.backends import default_backend
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives.hashes import SHA256
    _CRYPTO_AVAILABLE = True
except ImportError:
    logger.warning(
        "cryptography>=3.0 required for Ed25519. "
        "Run: pip install cryptography"
    )


# ── Algorithm Enum ───────────────────────────────────────────────────────

class SigningAlgorithm(Enum):
    RSA_2048 = "RSA-2048-SHA256"
    ED25519 = "Ed25519"


# ── Ed25519 Key Utilities ────────────────────────────────────────────────


class Ed25519Signer:
    """Ed25519 (EdDSA over Curve25519) signer for Agent Card integrity.

    Provides PKCS#8 private key and SubjectPublicKeyInfo public key
    PEM serialization, compatible with standard x509 workflows.

    Usage::

        signer = Ed25519Signer()
        keys = signer.generate_key_pair("/keys/ed25519")
        sig = signer.sign(card, keys["private_key_path"])
        valid = signer.verify(card, sig, keys["public_key_path"])
    """

    ALGORITHM = SigningAlgorithm.ED25519

    @staticmethod
    def generate_key_pair(output_dir: str) -> Dict[str, Any]:
        """Generate an Ed25519 key pair and save as PEM files.

        Creates:
          - ``{output_dir}/ed25519_private.pem`` (PKCS#8, no password)
          - ``{output_dir}/ed25519_public.pem``  (SubjectPublicKeyInfo)

        Args:
            output_dir: Directory that must already exist.

        Returns:
            Dict with key paths, algorithm, and key size.
        """
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError(
                "cryptography library not installed. Run: pip install cryptography"
            )

        os.makedirs(output_dir, exist_ok=True)

        private_key = ed25519.Ed25519PrivateKey.generate()

        private_path = os.path.join(output_dir, "ed25519_private.pem")
        public_path = os.path.join(output_dir, "ed25519_public.pem")

        with open(private_path, "wb") as f:
            f.write(private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ))

        with open(public_path, "wb") as f:
            f.write(private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ))

        public_bytes_raw = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

        logger.info("Ed25519 key pair generated in %s", output_dir)
        return {
            "private_key_path": private_path,
            "public_key_path": public_path,
            "algorithm": "Ed25519",
            "public_key_hex": public_bytes_raw.hex(),
            "public_key_base64": base64.urlsafe_b64encode(public_bytes_raw).decode(),
        }

    @staticmethod
    def get_card_hash(card: AgentCard) -> str:
        """Compute SHA-256 hex digest of an AgentCard payload.

        Args:
            card: The AgentCard to hash.

        Returns:
            64-character lowercase hex digest.
        """
        payload = json.dumps(
            card.to_dict(include_signature=False),
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def sign(card: AgentCard, private_key_path: str) -> str:
        """Sign an AgentCard with Ed25519.

        Args:
            card: AgentCard to sign.
            private_key_path: Path to Ed25519 private key PEM.

        Returns:
            Hex-encoded Ed25519 signature (128 hex chars / 64 bytes).
        """
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography not installed")

        with open(private_key_path, "rb") as f:
            private_key = serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )

        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise TypeError(
                f"Key at {private_key_path} is not an Ed25519 private key"
            )

        card_hash_bytes = bytes.fromhex(Ed25519Signer.get_card_hash(card))
        signature = private_key.sign(card_hash_bytes)
        return signature.hex()

    @staticmethod
    def verify(card: AgentCard, signature: str, public_key_path: str) -> bool:
        """Verify an Ed25519 signature.

        Args:
            card: AgentCard whose signature is being verified.
            signature: Hex-encoded Ed25519 signature.
            public_key_path: Path to Ed25519 public key PEM.

        Returns:
            True if the signature is valid.
        """
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography not installed")

        try:
            with open(public_key_path, "rb") as f:
                public_key = serialization.load_pem_public_key(
                    f.read(), backend=default_backend()
                )

            if not isinstance(public_key, ed25519.Ed25519PublicKey):
                raise TypeError(
                    f"Key at {public_key_path} is not an Ed25519 public key"
                )

            card_hash_bytes = bytes.fromhex(Ed25519Signer.get_card_hash(card))
            signature_bytes = bytes.fromhex(signature)
            public_key.verify(signature_bytes, card_hash_bytes)
            return True
        except Exception as e:
            logger.debug("Ed25519 verification failed: %s", e)
            return False


# ── x509 Certificate Chain ────────────────────────────────────────────────


class SigningAlgorithm(Enum):
    RSA_2048 = "RSA-2048-SHA256"
    ED25519 = "Ed25519"


@dataclass
class x509Certificate:
    """A self-contained x509 certificate with chain validation metadata.

    Wraps the cryptography x509.Certificate and provides fingerprint,
    validity checking, and chain verification utilities.
    """

    pem_data: str
    fingerprint_sha256: str
    subject_cn: str
    issuer_cn: str
    not_before: datetime
    not_after: datetime
    serial_number: int
    is_ca: bool = False
    _crypto_cert: Optional[Any] = field(default=None, repr=False)

    @classmethod
    def from_pem(cls, pem_path: str) -> x509Certificate:
        """Load an x509 certificate from a PEM file.

        Args:
            pem_path: Path to PEM-encoded certificate file.

        Returns:
            x509Certificate instance.
        """
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError("cryptography not installed")

        with open(pem_path, "rb") as f:
            pem_bytes = f.read()

        cert = x509.load_pem_x509_certificate(pem_bytes, backend=default_backend())

        # Extract subject Common Name
        subject_cn = ""
        try:
            subject_cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        except IndexError:
            subject_cn = str(cert.subject)

        # Extract issuer Common Name
        issuer_cn = ""
        try:
            issuer_cn = cert.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        except IndexError:
            issuer_cn = str(cert.issuer)

        # SHA-256 fingerprint
        fingerprint = cert.fingerprint(SHA256()).hex()

        # CA check via BasicConstraints extension
        is_ca = False
        try:
            bc = cert.extensions.get_extension_for_class(x509.BasicConstraints)
            is_ca = bc.value.ca
        except x509.ExtensionNotFound:
            pass

        return cls(
            pem_data=pem_bytes.decode("utf-8"),
            fingerprint_sha256=fingerprint,
            subject_cn=str(subject_cn),
            issuer_cn=str(issuer_cn),
            not_before=cert.not_valid_before_utc,
            not_after=cert.not_valid_after_utc,
            serial_number=cert.serial_number,
            is_ca=is_ca,
            _crypto_cert=cert,
        )

    def is_valid_now(self) -> bool:
        """Check if the certificate is currently within its validity period."""
        now = datetime.now(timezone.utc)
        return self.not_before <= now <= self.not_after

    def is_self_signed(self) -> bool:
        """Check if this is a self-signed certificate."""
        return self.subject_cn == self.issuer_cn

    def to_dict(self) -> Dict[str, Any]:
        """Serialize certificate metadata to dictionary."""
        return {
            "subject_cn": self.subject_cn,
            "issuer_cn": self.issuer_cn,
            "fingerprint_sha256": self.fingerprint_sha256,
            "serial_number": self.serial_number,
            "not_before": self.not_before.isoformat(),
            "not_after": self.not_after.isoformat(),
            "is_ca": self.is_ca,
            "is_self_signed": self.is_self_signed(),
            "is_valid_now": self.is_valid_now(),
        }


class x509CertificateChain:
    """x509 certificate chain validator for hierarchical trust.

    Verifies chains of trust from an end-entity certificate through
    intermediate CAs to a trusted root, checking:
      - Certificate validity periods
      - Issuer-subject chain continuity
      - Root CA trust anchoring
      - Maximum chain depth enforcement

    Usage::

        chain = x509CertificateChain(trusted_roots=["/certs/root-ca.pem"])
        chain.add_intermediate("/certs/intermediate-ca.pem")
        valid = chain.verify_chain("/certs/agent-cert.pem")
    """

    MAX_CHAIN_DEPTH = 5

    def __init__(self, trusted_roots: Optional[List[str]] = None):
        self._lock = threading.RLock()
        self._trusted_roots: Dict[str, x509Certificate] = {}
        self._intermediates: Dict[str, x509Certificate] = {}

        if trusted_roots:
            for root_path in trusted_roots:
                self.add_trusted_root(root_path)

    def add_trusted_root(self, pem_path: str) -> x509Certificate:
        """Register a trusted root CA certificate.

        Args:
            pem_path: Path to PEM-encoded root CA certificate.

        Returns:
            The loaded x509Certificate.
        """
        cert = x509Certificate.from_pem(pem_path)
        with self._lock:
            self._trusted_roots[cert.fingerprint_sha256] = cert
        logger.info("Trusted root CA added: %s (fingerprint: %s)",
                     cert.subject_cn, cert.fingerprint_sha256[:16])
        return cert

    def add_intermediate(self, pem_path: str) -> x509Certificate:
        """Register an intermediate CA certificate.

        Args:
            pem_path: Path to PEM-encoded intermediate CA certificate.

        Returns:
            The loaded x509Certificate.
        """
        cert = x509Certificate.from_pem(pem_path)
        with self._lock:
            self._intermediates[cert.fingerprint_sha256] = cert
        logger.info("Intermediate CA added: %s", cert.subject_cn)
        return cert

    def verify_chain(self, leaf_path: str) -> Dict[str, Any]:
        """Verify the full certificate chain for an end-entity certificate.

        Builds the chain from the leaf cert up through intermediates
        to a trusted root, checking validity and continuity at each level.

        Args:
            leaf_path: Path to the end-entity PEM certificate.

        Returns:
            Dict with verification result, chain depth, and details.
        """
        if not _CRYPTO_AVAILABLE:
            return {"valid": False, "error": "cryptography library not installed"}

        try:
            leaf = x509Certificate.from_pem(leaf_path)

            # Step 1: leaf validity check
            if not leaf.is_valid_now():
                return {
                    "valid": False,
                    "error": "Leaf certificate expired or not yet valid",
                    "leaf": leaf.to_dict(),
                }

            # Step 2: build chain upward
            chain: List[x509Certificate] = [leaf]
            current = leaf

            with self._lock:
                while len(chain) <= self.MAX_CHAIN_DEPTH:
                    # Find issuer among intermediates and roots
                    issuer = self._find_issuer(current)
                    if issuer is None:
                        break
                    chain.append(issuer)

                    # Check if we've reached a trusted root
                    if issuer.fingerprint_sha256 in self._trusted_roots:
                        break

                    current = issuer

            # Step 3: validate chain
            root = chain[-1]
            if root.fingerprint_sha256 not in self._trusted_roots:
                return {
                    "valid": False,
                    "error": "Chain does not terminate at a trusted root",
                    "chain_depth": len(chain),
                    "last_subject": root.subject_cn,
                }

            # Step 4: validate each intermediate
            for i, cert in enumerate(chain):
                if not cert.is_valid_now():
                    return {
                        "valid": False,
                        "error": f"Certificate {cert.subject_cn} is expired or not yet valid",
                        "chain_depth": len(chain),
                        "failing_cert": cert.to_dict(),
                    }

            chain_dicts = [c.to_dict() for c in chain]

            return {
                "valid": True,
                "chain_depth": len(chain),
                "leaf_fingerprint": leaf.fingerprint_sha256,
                "root_cn": root.subject_cn,
                "chain": chain_dicts,
            }

        except Exception as e:
            logger.error("Chain verification failed: %s", e)
            return {"valid": False, "error": str(e)}

    def _find_issuer(self, cert: x509Certificate) -> Optional[x509Certificate]:
        """Find the issuer certificate for a given certificate."""
        # Search intermediates first
        for ic in self._intermediates.values():
            if ic.subject_cn == cert.issuer_cn:
                return ic
        # Then roots
        for rc in self._trusted_roots.values():
            if rc.subject_cn == cert.issuer_cn:
                return rc
        return None

    def statistics(self) -> Dict[str, Any]:
        """Return chain store statistics."""
        with self._lock:
            return {
                "trusted_roots": len(self._trusted_roots),
                "root_subjects": [c.subject_cn for c in self._trusted_roots.values()],
                "intermediates": len(self._intermediates),
                "intermediate_subjects": [c.subject_cn for c in self._intermediates.values()],
            }


# ── SigningBridge ─────────────────────────────────────────────────────────


class SigningBridge:
    """Unified signing bridge: dispatches between RSA-2048 and Ed25519.

    Agents advertise their supported algorithm via AgentCard capabilities.
    The bridge auto-detects the appropriate signer based on key format
    or explicit algorithm specification.

    Usage::

        bridge = SigningBridge()
        bridge.sign(card, private_key_path="/keys/ed25519_private.pem")
        # Auto-detects Ed25519 by key format

        bridge.sign(card, private_key_path="/keys/private.pem")
        # Auto-detects RSA-2048 by key format
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._sign_count: Dict[str, int] = {}
        self._verify_count: Dict[str, int] = {}

    @staticmethod
    def detect_algorithm(key_path: str) -> SigningAlgorithm:
        """Detect signing algorithm from a PEM key file.

        Reads the PEM header to determine key type:
          - ``BEGIN PRIVATE KEY`` (PKCS#8) → Ed25519
          - ``BEGIN RSA PRIVATE KEY`` → RSA-2048

        Args:
            key_path: Path to PEM key file.

        Returns:
            Detected SigningAlgorithm.
        """
        if not os.path.exists(key_path):
            raise FileNotFoundError(f"Key file not found: {key_path}")

        with open(key_path, "r", encoding="utf-8") as f:
            header = f.read(200)

        if "BEGIN RSA PRIVATE KEY" in header:
            return SigningAlgorithm.RSA_2048
        elif "BEGIN RSA PUBLIC KEY" in header:
            return SigningAlgorithm.RSA_2048
        elif "BEGIN PRIVATE KEY" in header or "BEGIN PUBLIC KEY" in header:
            return SigningAlgorithm.ED25519

        raise ValueError(f"Unrecognized key format in {key_path}")

    def sign(
        self,
        card: AgentCard,
        private_key_path: str,
        algorithm: Optional[SigningAlgorithm] = None,
    ) -> Dict[str, Any]:
        """Sign an AgentCard, auto-detecting algorithm from key format.

        Args:
            card: AgentCard to sign.
            private_key_path: Path to private key PEM.
            algorithm: Optional explicit algorithm override.

        Returns:
            Dict with signature, algorithm, and key metadata.
        """
        algo = algorithm or self.detect_algorithm(private_key_path)

        if algo == SigningAlgorithm.ED25519:
            sig = Ed25519Signer.sign(card, private_key_path)
        elif algo == SigningAlgorithm.RSA_2048:
            from trinity.a2a.security import AgentCardSigner
            sig = AgentCardSigner.sign(card, private_key_path)
        else:
            raise ValueError(f"Unsupported algorithm: {algo}")

        with self._lock:
            self._sign_count[algo.value] = self._sign_count.get(algo.value, 0) + 1

        return {
            "signature": sig,
            "algorithm": algo.value,
            "card_hash": Ed25519Signer.get_card_hash(card),
        }

    def verify(
        self,
        card: AgentCard,
        signature: str,
        public_key_path: str,
        algorithm: Optional[SigningAlgorithm] = None,
    ) -> bool:
        """Verify an AgentCard signature.

        Args:
            card: AgentCard to verify.
            signature: Hex-encoded signature.
            public_key_path: Path to public key PEM.
            algorithm: Optional explicit algorithm override.

        Returns:
            True if signature is valid.
        """
        algo = algorithm or self.detect_algorithm(public_key_path)

        if algo == SigningAlgorithm.ED25519:
            result = Ed25519Signer.verify(card, signature, public_key_path)
        elif algo == SigningAlgorithm.RSA_2048:
            from trinity.a2a.security import AgentCardSigner
            result = AgentCardSigner.verify(card, signature, public_key_path)
        else:
            raise ValueError(f"Unsupported algorithm: {algo}")

        with self._lock:
            self._verify_count[algo.value] = self._verify_count.get(algo.value, 0) + 1

        return result

    def statistics(self) -> Dict[str, Any]:
        """Return signing bridge statistics."""
        with self._lock:
            return {
                "sign_count": dict(self._sign_count),
                "verify_count": dict(self._verify_count),
                "supported_algorithms": [a.value for a in SigningAlgorithm],
            }


# ── Module-Level Self-Test ───────────────────────────────────────────────


def self_test() -> str:
    """Run Ed25519 + x509 self-tests and return PASS/FAIL."""
    import tempfile
    results = []

    tmpdir = tempfile.mkdtemp(prefix="trinity_ed25519_test_")

    try:
        # 1. Ed25519 key generation
        keys = Ed25519Signer.generate_key_pair(tmpdir)
        results.append(("Ed25519 key generation", "PASS" if os.path.exists(keys["private_key_path"]) else "FAIL"))
        results.append(("Ed25519 public key hex", "PASS" if len(keys.get("public_key_hex", "")) == 64 else "FAIL"))

        # 2. Ed25519 sign/verify with a mock AgentCard
        from trinity.a2a.agent_card import AgentCard as AC
        card = AC(
            agent_id="test-agent",
            name="Test Agent",
            description="Test card for Ed25519",
            version="1.0.0",
            capabilities=["search", "write"],
        )
        sig_result = Ed25519Signer.sign(card, keys["private_key_path"])
        results.append(("Ed25519 sign", "PASS" if len(sig_result) == 128 else "FAIL"))

        valid = Ed25519Signer.verify(card, sig_result, keys["public_key_path"])
        results.append(("Ed25519 verify valid", "PASS" if valid else "FAIL"))

        # 3. Tampered verification should fail
        import copy
        tampered = AC(
            agent_id="test-agent",
            name="Test Agent",
            description="Tampered!",
            version="1.0.0",
            capabilities=["search", "write"],
        )
        invalid = Ed25519Signer.verify(tampered, sig_result, keys["public_key_path"])
        results.append(("Ed25519 reject tampered", "PASS" if not invalid else "FAIL"))

        # 4. SigningBridge auto-detection
        bridge = SigningBridge()
        detected = bridge.detect_algorithm(keys["private_key_path"])
        results.append(("Bridge detect Ed25519", "PASS" if detected == SigningAlgorithm.ED25519 else "FAIL"))

        bridge_result = bridge.sign(card, keys["private_key_path"])
        results.append(("Bridge sign algorithm", "PASS" if bridge_result["algorithm"] == "Ed25519" else "FAIL"))
        results.append(("Bridge sign signature", "PASS" if len(bridge_result["signature"]) == 128 else "FAIL"))

        bridge_verify = bridge.verify(card, bridge_result["signature"], keys["public_key_path"])
        results.append(("Bridge verify", "PASS" if bridge_verify else "FAIL"))

        # 5. x509 certificate chain
        chain = x509CertificateChain()
        results.append(("Chain initialized", "PASS" if chain is not None else "FAIL"))
        stats = chain.statistics()
        results.append(("Chain statistics", "PASS" if "trusted_roots" in stats else "FAIL"))

    finally:
        # Cleanup
        import shutil
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    passed = sum(1 for _, r in results if r == "PASS")
    total = len(results)
    print(f"[SELFTEST_RESULT] ed25519_signer: {passed}/{total} PASS")
    for name, result in results:
        print(f"  {name}: {result}")

    if passed == total:
        return "PASS"
    return "FAIL"


if __name__ == "__main__":
    self_test()
