"""Ports onboarding/subscribe.ts's guest-JWT + wallet-signature + token
activation flow into Python.

IMPORTANT: /token/activate is one-time-use per on-chain txSig -- verified
live against the real devnet API, which rejects a repeat with "This
transaction has already been used to activate a subscription". Two
consequences:
  1. activate_api_token() is NOT part of txline_auth.py's on-401 refresh
     path -- that only needs a fresh guest JWT (no signature required), see
     its docstring.
  2. It's also not usable as a deployment "bootstrap credentials from env
     vars" step, since the only txSig this project has is the one
     onboarding's on-chain subscribe() already spent activating locally --
     re-running activation for it from a fresh deployed process would hit
     the exact same "already activated" rejection. Deployment instead
     carries the *existing* credentials.json content forward as an env var
     (see config.py's TXLINE_CREDENTIALS_JSON / docs/DEPLOY.md), since the
     API token is long-lived for the whole paid subscription.

This module is kept as a correct, tested (verified to derive the same
wallet public key onboarding recorded) reference implementation for the one
time it would be needed again: activating a subscription from a *new*
on-chain subscribe() transaction, should the current one ever need
renewing. Nothing in the running app calls activate_api_token() today.

Signing matches subscribe.ts exactly: nacl.sign.detached(message, secretKey)
where secretKey is the wallet's 64-byte Solana secret key (32-byte seed +
32-byte public key, tweetnacl/Solana convention). PyNaCl's SigningKey takes
just the 32-byte seed and produces the identical signature over the same
message, since both are plain Ed25519 detached signatures.

Never logs the private key or any derived secret.
"""

from __future__ import annotations

import base64

import base58
import httpx
from nacl.signing import SigningKey
from pydantic import SecretStr


class ActivationError(RuntimeError):
    """Raised on any failure requesting a guest JWT or activating a token.
    Callers decide whether/how to retry.
    """


def _signing_key(wallet_private_key_base58: str) -> SigningKey:
    secret_bytes = base58.b58decode(wallet_private_key_base58)
    seed = secret_bytes[:32]  # Solana/tweetnacl secret key = 32-byte seed + 32-byte public key
    return SigningKey(seed)


def wallet_public_key_from_private(wallet_private_key_base58: str) -> str:
    verify_key = _signing_key(wallet_private_key_base58).verify_key
    return base58.b58encode(bytes(verify_key)).decode("ascii")


async def activate_api_token(
    api_origin: str,
    api_base_url: str,
    tx_sig: str,
    leagues: list[int],
    wallet_private_key_base58: str,
    http_timeout_seconds: float = 20.0,
) -> tuple[SecretStr, SecretStr]:
    """Requests a fresh guest JWT and activates a fresh API token against it,
    signing `{txSig}:{leagues}:{jwt}` with the wallet's secret key exactly as
    subscribe.ts does. Returns (jwt, api_token). Raises ActivationError on
    any failure -- callers own retry/backoff.
    """
    signing_key = _signing_key(wallet_private_key_base58)

    try:
        async with httpx.AsyncClient(timeout=http_timeout_seconds) as client:
            guest_response = await client.post(f"{api_origin}/auth/guest/start")
            guest_response.raise_for_status()
            jwt = guest_response.json()["token"]

            message = f"{tx_sig}:{','.join(str(league) for league in leagues)}:{jwt}".encode()
            signature = signing_key.sign(message).signature
            wallet_signature = base64.b64encode(signature).decode("ascii")

            activation_response = await client.post(
                f"{api_base_url}/token/activate",
                json={"txSig": tx_sig, "walletSignature": wallet_signature, "leagues": leagues},
                headers={"Authorization": f"Bearer {jwt}"},
            )
            activation_response.raise_for_status()
            data = activation_response.json()
            api_token = data["token"] if isinstance(data, dict) else data
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise ActivationError(f"TxLINE token activation failed: {exc}") from exc

    return SecretStr(jwt), SecretStr(api_token)
