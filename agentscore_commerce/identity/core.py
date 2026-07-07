"""Shared AgentScore assess client with TTL caching."""

from __future__ import annotations

import hashlib
import json
import logging
from importlib.metadata import version as _pkg_version
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import quote

import httpx
from agentscore import (
    AgentScore,
    AgentScoreError,
)
from agentscore import (
    InvalidCredentialError as SdkInvalidCredentialError,
)
from agentscore import (
    PaymentRequiredError as SdkPaymentRequiredError,
)
from agentscore import (
    QuotaExceededError as SdkQuotaExceededError,
)
from agentscore import (
    TimeoutError as SdkTimeoutError,
)
from agentscore import (
    TokenExpiredError as SdkTokenExpiredError,
)

from agentscore_commerce.identity._response import (
    WALLET_AUTH_REQUIRES_WALLET_SIGNING_INSTRUCTIONS,
    WALLET_SIGNER_MISMATCH_INSTRUCTIONS,
)
from agentscore_commerce.identity.address import normalize_address
from agentscore_commerce.identity.cache import TTLCache
from agentscore_commerce.identity.types import (
    AgentIdentity,
    AssessResult,
    GateQuotaInfo,
    Network,
    OperatorVerification,
    SignerVerdict,
    VerifyWalletSignerResult,
)

if TYPE_CHECKING:
    from agentscore.types import AipProvenance, AipSignatureMaterial, DecisionPolicy, Signer

    from agentscore_commerce.identity.types import DenialReason

_log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.agentscore.com"
DEFAULT_CACHE_SECONDS = 300


class AgentScoreCore:
    """Shared client for calling the AgentScore assess API.

    Manages caching and policy construction. Used by all framework adapters.
    Wraps the official ``agentscore`` SDK so HTTP/retry/quota/typed-error logic
    stays consistent across consumers.
    """

    def __init__(
        self,
        *,
        api_key: str,
        require_kyc: bool | None = None,
        require_sanctions_clear: bool | None = None,
        min_age: int | None = None,
        blocked_jurisdictions: list[str] | None = None,
        allowed_jurisdictions: list[str] | None = None,
        fail_open: bool = False,
        cache_seconds: int = DEFAULT_CACHE_SECONDS,
        base_url: str = DEFAULT_BASE_URL,
        chain: str | None = None,
        user_agent: str | None = None,
        aip_trusted_issuers: list[str] | None = None,
    ) -> None:
        if not api_key:
            msg = "AgentScore API key is required. Get one at https://www.agentscore.com/sign-up"
            raise ValueError(msg)

        self.fail_open = fail_open
        self._api_key = api_key
        self._base_url = base_url
        # Public accessor so adapters can build agent_memory hints pointing at the same API.
        self.base_url = base_url
        # Issuers whose AIP Agent Identity Tokens this gate accepts. When set, the missing-identity
        # recovery instructions + agent_memory hint advertise the AIT path so agents holding one
        # learn they can present it. Set by Checkout from gate.aip.trusted_issuers; the actual AIT
        # verification happens at the edge (Checkout) before evaluate. Public so adapters can thread
        # it into build_missing_identity_reason().
        self.aip_trusted_issuers = aip_trusted_issuers
        self._chain = chain
        default_ua = f"agentscore-commerce/{_pkg_version('agentscore-commerce')}"
        self.user_agent = f"{user_agent} ({default_ua})" if user_agent else default_ua
        self._cache: TTLCache[AssessResult] = TTLCache(cache_seconds)
        # Parallel cache of the raw /v1/assess response dict — populated alongside the
        # projected AssessResult cache (same signer-aware key) so a fresh response for a new
        # signer never overwrites another signer's raw blob. Same TTL semantics as _cache.
        # Signer verdicts (signer_match + signer_sanctions) from the most recent assess call that
        # carried a signer, keyed by the normalized claimed wallet address. Kept OUTSIDE the TTL
        # response cache: get_signer_verdict() is the gate's inline wallet-binding enforcement for
        # the CURRENT request, so it must not depend on the TTL entry surviving (with
        # cache_seconds=0 the entry expires within the same tick, which would non-deterministically
        # drop the verdict and let a signer-mismatch fall through to settlement). The slot always
        # holds the LATEST signer's verdict, so a 2nd request with a different signer (a cache miss
        # under the signer-aware key) overwrites it — get_signer_verdict can never return a verdict
        # computed for a stale signer. Mirrors the reference `lastSignerRaw`.
        self._last_signer_raw: dict[str, dict[str, Any]] = {}

        self._policy: dict[str, Any] = {}
        if require_kyc is not None:
            self._policy["require_kyc"] = require_kyc
        if require_sanctions_clear is not None:
            self._policy["require_sanctions_clear"] = require_sanctions_clear
        if min_age is not None:
            self._policy["min_age"] = min_age
        if blocked_jurisdictions is not None:
            self._policy["blocked_jurisdictions"] = blocked_jurisdictions
        if allowed_jurisdictions is not None:
            self._policy["allowed_jurisdictions"] = allowed_jurisdictions

        self._sdk = AgentScore(
            api_key=api_key,
            base_url=base_url,
            user_agent=self.user_agent,
        )

    @property
    def _sync_client(self) -> Any:
        """Underlying httpx Client used by the wrapped SDK.

        Exposed for tests that patch transport behavior directly via ``unittest.mock.patch.object``.
        """
        return self._sdk._get_sync_client()

    @property
    def _async_client(self) -> Any:
        """Underlying httpx AsyncClient used by the wrapped SDK.

        Exposed for tests that patch transport behavior directly via ``unittest.mock.patch.object``.
        """
        return self._sdk._get_async_client()

    def _cache_key(
        self,
        address: str | None = None,
        operator_token: str | None = None,
        aip_token: str | None = None,
        signer: dict[str, str] | None = None,
    ) -> str:
        # AIT cache key: hash the raw token so the (short-lived) JWT isn't held verbatim as a
        # dict key. AITs are seconds-to-minutes TTL anyway; this just dedupes repeated presents
        # within the cache window. AIT takes precedence, then operator_token, then address.
        if aip_token:
            identity_key = f"aip:{hashlib.sha256(aip_token.encode()).hexdigest()}"
        elif operator_token:
            # operator_token is opaque ASCII — lowercasing is safe.
            identity_key = operator_token.lower()
        else:
            # Wallet addresses go through normalize_address so Solana base58 (case-sensitive)
            # isn't corrupted into a cache miss.
            identity_key = normalize_address(address) if address else ""
        # Fold the payment signer into the key whenever one is present on the request. The API's
        # per-request signer_match + signer_sanctions verdicts (and the unconditional signer-OFAC
        # screen) are computed for THIS signer; without the signer in the key, a 2nd request that
        # claims the same identity but signs with a DIFFERENT wallet would hit the cache and return
        # the prior signer's verdict — a sanctioned signer could ride a stale `clear` to settlement.
        # Keying on the normalized signer makes a different signer a cache MISS that re-screens.
        # Requests with no signer keep the identity-only key (operator-token / discovery legs).
        # Every part is percent-encoded (delimiter-proof): the claimed X-Wallet-Address is
        # attacker-controlled and normalize_address passes invalid input through verbatim, so a
        # crafted address embedding the literal `|sig:` joiner could otherwise collide with a
        # different (identity, signer) pair's key and poison its cached signer verdicts.
        if signer is not None:
            signer_addr = normalize_address(signer.get("address", ""))
            signer_net = signer.get("network", "")
            return f"{quote(identity_key, safe='')}|sig:{quote(signer_addr, safe='')}:{quote(signer_net, safe='')}"
        return quote(identity_key, safe="")

    def _build_body(
        self,
        address: str | None = None,
        chain: str | None = None,
        operator_token: str | None = None,
        signer: dict[str, str] | None = None,
        aip_token: str | None = None,
        aip_signature: AipSignatureMaterial | None = None,
    ) -> dict[str, Any]:
        """Construct the assess request body.

        Testable helper for the policy/chain wiring contract — pinned so a future SDK
        body-shape regression would fail the gate's own tests as well.
        """
        body: dict[str, Any] = {}
        if address:
            body["address"] = address
        if operator_token:
            body["operator_token"] = operator_token
        # AIP Agent Identity Token path: the API re-verifies the IdP signature + RFC 9421
        # proof-of-possession server-side and evaluates policy against the attested claims.
        if aip_token:
            body["aip_token"] = aip_token
        if aip_signature is not None:
            body["aip_signature"] = dict(aip_signature)
        effective_chain = chain or self._chain
        if effective_chain:
            body["chain"] = effective_chain
        if self._policy:
            body["policy"] = self._policy
        if signer is not None:
            body["signer"] = signer
        return body

    def _headers(self) -> dict[str, str]:
        """Construct the canonical assess request headers.

        Testable helper for the X-API-Key + User-Agent contract — pinned independently
        so a regression on either header would fail the gate's own tests.
        """
        return {
            "X-API-Key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }

    def _parse_response(self, resp: Any) -> AssessResult:
        """Parse a raw httpx Response into an AssessResult.

        Testable helper for the gate's status-code → typed-error mapping contract.
        """
        status = resp.status_code
        if status == 402:
            raise PaymentRequiredError
        if status == 429:
            _log.warning("[gate] /v1/assess returned 429")
            raise QuotaExceededError("quota_exceeded")
        if status == 401:
            try:
                err_body = resp.json()
            except (ValueError, json.JSONDecodeError) as parse_err:
                _log.warning("[gate] /v1/assess 401 body parse failed: %s", parse_err)
                err_body = {}
            error = err_body.get("error") if isinstance(err_body, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            if code == "token_expired":
                raise TokenDeniedError(err_body if isinstance(err_body, dict) else {})
            if code == "invalid_credential":
                raise InvalidCredentialError()
            if code:
                _log.warning(
                    "[gate] /v1/assess returned 401 %s — no specific handler, surfacing as RuntimeError.",
                    code,
                )
            msg = f"AgentScore API returned {status}"
            raise RuntimeError(msg)
        if not resp.is_success:
            msg = f"AgentScore API returned {status}"
            raise RuntimeError(msg)
        data: dict[str, Any] = resp.json()
        return self._project(data)

    def _project(self, data: dict[str, Any]) -> AssessResult:
        decision = data.get("decision")
        reasons: list[str] = data.get("decision_reasons", [])
        allow = decision == "allow" or decision is None

        ov_data = data.get("operator_verification")
        operator_verification = (
            OperatorVerification(
                level=ov_data.get("level", "none"),
                operator_type=ov_data.get("operator_type"),
                verified_at=ov_data.get("verified_at"),
            )
            if isinstance(ov_data, dict)
            else None
        )

        av_data = data.get("account_verification")
        account_verification = av_data if isinstance(av_data, dict) else None

        # IdP provenance, present only when identity_method == "aip_token". Surfaced as the SDK's
        # raw `aip` block (issuer/subject/trust_level/agent_provider/pop_verified); not re-shaped,
        # same as account_verification / policy_result.
        aip_data = data.get("aip")
        aip = aip_data if isinstance(aip_data, dict) else None

        # SDK populates `quota` on the AssessResponse from X-Quota-* headers. Surface up
        # to adapters so merchants can monitor approach-to-cap proactively.
        quota_raw = data.get("quota")
        quota = (
            GateQuotaInfo(
                limit=quota_raw.get("limit"),
                used=quota_raw.get("used"),
                reset=quota_raw.get("reset"),
            )
            if isinstance(quota_raw, dict)
            else None
        )

        return AssessResult(
            allow=allow,
            decision=decision,
            reasons=reasons,
            identity_method=data.get("identity_method"),
            operator_verification=operator_verification,
            account_verification=account_verification,
            resolved_operator=data.get("resolved_operator"),
            verify_url=data.get("verify_url"),
            policy_result=data.get("policy_result"),
            aip=cast("AipProvenance | None", aip),
            quota=quota,
            raw=data,
        )

    def check(
        self,
        address: str | None = None,
        chain: str | None = None,
        operator_token: str | None = None,
        signer: dict[str, str] | None = None,
        aip_token: str | None = None,
        aip_signature: AipSignatureMaterial | None = None,
    ) -> AssessResult:
        """Synchronous assess call with caching. Accepts address, operator_token, or AIP token.

        When ``signer`` is provided (extracted by the adapter middleware from the
        inbound request's payment credential), the API composes ``signer_match`` and
        ``signer_sanctions`` verdicts on the response in one round trip.

        ``aip_token`` (+ ``aip_signature``) supplies an AIP Agent Identity Token in place of
        ``address`` / ``operator_token``: the API re-verifies the issuer signature AND the RFC 9421
        proof-of-possession authoritatively (the edge is not trusted as the authority), then
        evaluates policy against the attested claims. ``aip_token`` requires ``aip_signature``.
        """
        if aip_token is not None and aip_signature is None:
            msg = "AgentScoreCore.check: aip_token requires aip_signature (RFC 9421 proof-of-possession material)."
            raise ValueError(msg)
        key = self._cache_key(address, operator_token, aip_token, signer)

        cached = self._cache.get(key)
        if cached is not None:
            return cached

        effective_chain = chain or self._chain
        # SDK typed errors map onto commerce's bespoke 401/402/429 exception surface.
        try:
            data = self._sdk.assess(
                address=address,
                operator_token=operator_token,
                chain=effective_chain,
                policy=cast("DecisionPolicy | None", self._policy or None),
                signer=cast("Signer | None", signer),
                aip_token=aip_token,
                aip_signature=aip_signature,
            )
        except SdkPaymentRequiredError as exc:
            raise PaymentRequiredError from exc
        except SdkQuotaExceededError as exc:
            _log.warning("[gate] /v1/assess returned 429")
            raise QuotaExceededError("quota_exceeded") from exc
        except SdkTokenExpiredError as exc:
            raise TokenDeniedError(getattr(exc, "details", {}) or {}) from exc
        except SdkInvalidCredentialError as exc:
            raise InvalidCredentialError() from exc
        except SdkTimeoutError as exc:
            # Re-raise as httpx.TimeoutException so adapters keep their existing
            # `except httpx.TimeoutException` clauses for `infra_reason='network_timeout'`
            # without each having to learn about the SDK's typed timeout class.
            raise httpx.TimeoutException(str(exc)) from exc
        except AgentScoreError as exc:
            # Defensive: SDK only routes 429 → QuotaExceededError when body has
            # `error.code='quota_exceeded'`. Real API always emits the code, but a
            # mock or proxy returning bare `429` falls through to generic. Reroute by
            # status_code so the gate's fail_open path still surfaces 'quota_exceeded'.
            if exc.status_code == 429:
                _log.warning("[gate] /v1/assess returned 429 (untyped — defensive)")
                raise QuotaExceededError("quota_exceeded") from exc
            # Wraps any other 401 (schema drift), 5xx, network errors, body-parse failures.
            # Surface code so ops notice schema-drift cases instead of a silent 503.
            _log.warning("[gate] /v1/assess call failed (%s): %s", exc.code, exc)
            # Message format pinned for downstream merchant log scrapers.
            status = exc.status_code or 0
            raise RuntimeError(f"AgentScore API returned {status}: {exc}") from exc

        raw = cast("dict[str, Any]", data)
        result = self._project(raw)
        self._cache.set(key, result)
        # Cache the raw response under the same (signer-aware) key so get_signer_verdict() can read
        # signer_match + signer_sanctions verdicts that the projector doesn't expose.
        self._stash_signer_raw(address, operator_token, aip_token, signer, raw)
        return result

    async def acheck(
        self,
        address: str | None = None,
        chain: str | None = None,
        operator_token: str | None = None,
        signer: dict[str, str] | None = None,
        aip_token: str | None = None,
        aip_signature: AipSignatureMaterial | None = None,
    ) -> AssessResult:
        """Asynchronous assess call with caching. Accepts address, operator_token, or AIP token.

        See :meth:`check` for the ``signer`` and ``aip_token`` / ``aip_signature`` contracts.
        """
        if aip_token is not None and aip_signature is None:
            msg = "AgentScoreCore.acheck: aip_token requires aip_signature (RFC 9421 proof-of-possession material)."
            raise ValueError(msg)
        key = self._cache_key(address, operator_token, aip_token, signer)

        cached = self._cache.get(key)
        if cached is not None:
            return cached

        effective_chain = chain or self._chain
        try:
            data = await self._sdk.aassess(
                address=address,
                operator_token=operator_token,
                chain=effective_chain,
                policy=cast("DecisionPolicy | None", self._policy or None),
                signer=cast("Signer | None", signer),
                aip_token=aip_token,
                aip_signature=aip_signature,
            )
        except SdkPaymentRequiredError as exc:
            raise PaymentRequiredError from exc
        except SdkQuotaExceededError as exc:
            _log.warning("[gate] /v1/assess returned 429")
            raise QuotaExceededError("quota_exceeded") from exc
        except SdkTokenExpiredError as exc:
            raise TokenDeniedError(getattr(exc, "details", {}) or {}) from exc
        except SdkInvalidCredentialError as exc:
            raise InvalidCredentialError() from exc
        except SdkTimeoutError as exc:
            # Same re-raise pattern as the sync path; see :meth:`check`.
            raise httpx.TimeoutException(str(exc)) from exc
        except AgentScoreError as exc:
            if exc.status_code == 429:
                _log.warning("[gate] /v1/assess returned 429 (untyped — defensive)")
                raise QuotaExceededError("quota_exceeded") from exc
            _log.warning("[gate] /v1/assess call failed (%s): %s", exc.code, exc)
            # Message format pinned for downstream merchant log scrapers.
            status = exc.status_code or 0
            raise RuntimeError(f"AgentScore API returned {status}: {exc}") from exc

        raw = cast("dict[str, Any]", data)
        result = self._project(raw)
        self._cache.set(key, result)
        # Cache the raw response under the same (signer-aware) key so get_signer_verdict() can read
        # signer_match + signer_sanctions verdicts that the projector doesn't expose.
        self._stash_signer_raw(address, operator_token, aip_token, signer, raw)
        return result

    def check_identity(
        self,
        identity: AgentIdentity,
        chain: str | None = None,
        signer: dict[str, str] | None = None,
    ) -> AssessResult:
        """Convenience method to check using an AgentIdentity object.

        When ``identity.aip_token`` is set it's the sole identity input (the API re-verifies
        the token + proof-of-possession and evaluates policy against the attested claims);
        ``aip_signature`` is forwarded with it. See :meth:`check`.
        """
        return self.check(
            address=identity.address,
            chain=chain,
            operator_token=identity.operator_token,
            signer=signer,
            aip_token=identity.aip_token,
            aip_signature=identity.aip_signature,
        )

    async def acheck_identity(
        self,
        identity: AgentIdentity,
        chain: str | None = None,
        signer: dict[str, str] | None = None,
    ) -> AssessResult:
        """Async convenience method to check using an AgentIdentity object.

        See :meth:`check_identity` for the AIP-token forwarding contract.
        """
        return await self.acheck(
            address=identity.address,
            chain=chain,
            operator_token=identity.operator_token,
            signer=signer,
            aip_token=identity.aip_token,
            aip_signature=identity.aip_signature,
        )

    def _stash_signer_raw(
        self,
        address: str | None,
        operator_token: str | None,
        aip_token: str | None,
        signer: dict[str, str] | None,
        raw: dict[str, Any],
    ) -> None:
        """Stash the raw response in the dedicated signer slot for synchronous read-back.

        Keyed by the normalized claimed wallet address. ONLY when the wallet is the EFFECTIVE
        identity (no operator_token, no aip_token) AND a signer was supplied AND the response
        actually carried signer verdicts — matching the gate's enforcement guard. With an
        operator-token / AIT present, that identity wins and signer-match is deliberately NOT
        enforced, so we must not surface a verdict for that wallet. Mirrors the reference implementation.
        """
        if (
            address is not None
            and operator_token is None
            and aip_token is None
            and signer is not None
            and (raw.get("signer_match") is not None or raw.get("signer_sanctions") is not None)
        ):
            self._last_signer_raw[normalize_address(address)] = raw

    def project_signer_verdict(self, raw: dict[str, Any] | None, claimed_address: str) -> SignerVerdict | None:
        """Project ``signer_match`` + ``signer_sanctions`` from a SPECIFIC raw assess response.

        Pure / request-scoped: takes the exact ``/v1/assess`` response dict THIS request got
        (``AssessResult.raw``) and the claimed wallet — it reads NO shared state. Adapters call
        this with their per-request raw and stash the result on the per-request state, so two
        concurrent requests that claim the same wallet but sign with different wallets each see
        their OWN verdict (the shared ``_last_signer_raw`` slot would race — see
        :meth:`get_signer_verdict`). Returns ``None`` when the response carried no signer
        verdicts (operator-token-only paths, discovery legs).
        """
        claimed_norm = normalize_address(claimed_address)
        if not isinstance(raw, dict):
            return None
        signer_match = raw.get("signer_match")
        signer_sanctions = raw.get("signer_sanctions")
        if not signer_match and not signer_sanctions:
            return None
        actual_signer = signer_match.get("actual_signer") if isinstance(signer_match, dict) else None
        signer_norm = actual_signer if isinstance(actual_signer, str) else claimed_norm
        return SignerVerdict(
            signer_match=(
                self._project_signer_match(signer_match, claimed_norm, signer_norm)
                if isinstance(signer_match, dict)
                else None
            ),
            signer_sanctions=signer_sanctions if signer_sanctions else None,
        )

    def get_signer_verdict(self, claimed_address: str) -> SignerVerdict | None:
        """Synchronous read of the cached signer verdicts (signer_match + signer_sanctions).

        Both verdicts were composed by the gate's primary /v1/assess call on this
        request — single round trip. Returns ``None`` when the gate didn't run with
        a signer (operator-token-only paths, discovery legs).

        Reads the dedicated, non-expiring signer slot (keyed by normalized claimed address),
        which holds the LATEST signer's verdict for that wallet. NOTE: this slot lives on the
        SHARED core (the gate is module-scoped), so under concurrency two requests claiming the
        same wallet with DIFFERENT signers race here — the verdict you read may have been
        computed for the other request's signer. Adapters therefore do NOT use this method for
        per-request enforcement; they stash the request-scoped verdict (projected from THIS
        request's ``AssessResult.raw`` via :meth:`project_signer_verdict`) on the per-request
        state and read it back from there. This method is retained for direct callers that hold a
        per-request core (e.g. the one Checkout builds inside ``_run_gate``) and for backward
        compatibility.

        Wallet-OFAC SDN enforcement is unconditional whenever a signer is in the
        request — SDN wallet-address hits are already enforced by the gate
        (decision -> deny before the handler runs); merchant code typically only
        needs this for the signer_match wallet-binding verdict.
        """
        raw = self._last_signer_raw.get(normalize_address(claimed_address))
        return self.project_signer_verdict(raw, claimed_address)

    def capture_wallet(
        self,
        operator_token: str,
        wallet_address: str,
        network: Network,
        idempotency_key: str | None = None,
    ) -> None:
        """Report a wallet seen paying under an operator credential.

        Fire-and-forget: silently swallows non-fatal errors. ``idempotency_key`` (payment intent
        id, tx hash, …) lets the server dedupe agent retries of the same logical payment.
        """
        try:
            self._sdk.associate_wallet(
                operator_token=operator_token,
                wallet_address=wallet_address,
                network=network,
                idempotency_key=idempotency_key,
            )
        except Exception as err:
            _log.warning("capture_wallet failed: %s", err)

    async def acapture_wallet(
        self,
        operator_token: str,
        wallet_address: str,
        network: Network,
        idempotency_key: str | None = None,
    ) -> None:
        """Async variant of :meth:`capture_wallet`."""
        try:
            await self._sdk.aassociate_wallet(
                operator_token=operator_token,
                wallet_address=wallet_address,
                network=network,
                idempotency_key=idempotency_key,
            )
        except Exception as err:
            _log.warning("acapture_wallet failed: %s", err)

    # ------------------------------------------------------------------
    # Wallet-auth signer binding
    # ------------------------------------------------------------------

    def _project_signer_match(
        self, sm: dict[str, Any], claimed_norm: str, signer_norm: str
    ) -> VerifyWalletSignerResult:
        """Project the API's ``signer_match`` block onto :class:`VerifyWalletSignerResult`.

        The API authors agent_instructions, claimed/signer operators, and the linked-wallet
        set (deny-guarded server-side); commerce just shapes those fields.
        """
        kind = sm.get("kind")
        if kind == "pass":
            return VerifyWalletSignerResult(
                kind="pass",
                claimed_operator=sm.get("claimed_operator"),
                signer_operator=sm.get("signer_operator"),
            )
        if kind == "wallet_auth_requires_wallet_signing":
            return VerifyWalletSignerResult(
                kind="wallet_auth_requires_wallet_signing",
                claimed_wallet=sm.get("claimed_wallet") or claimed_norm,
                agent_instructions=sm.get("agent_instructions") or WALLET_AUTH_REQUIRES_WALLET_SIGNING_INSTRUCTIONS,
            )
        # Default: wallet_signer_mismatch
        linked_raw = sm.get("linked_wallets")
        linked = [w for w in linked_raw if isinstance(w, str)] if isinstance(linked_raw, list) else []
        return VerifyWalletSignerResult(
            kind="wallet_signer_mismatch",
            claimed_operator=sm.get("claimed_operator"),
            actual_signer_operator=sm.get("signer_operator"),
            expected_signer=sm.get("expected_signer") or claimed_norm,
            actual_signer=sm.get("actual_signer") or signer_norm,
            linked_wallets=linked,
            agent_instructions=sm.get("agent_instructions") or WALLET_SIGNER_MISMATCH_INSTRUCTIONS,
        )

    def _infer_signer_network(self, signer: str) -> str:
        return "evm" if signer.startswith("0x") else "solana"


class PaymentRequiredError(Exception):
    """Raised when the AgentScore API returns 402."""


class QuotaExceededError(RuntimeError):
    """Raised when /v1/assess returns 429.

    Distinct from a generic 5xx so adapters with ``fail_open=True`` can surface
    ``infra_reason='quota_exceeded'`` to merchant logs/alerts. Compliance denials
    are unaffected — those still deny regardless of fail_open.

    Subclasses ``RuntimeError`` so a broad ``except RuntimeError`` still catches the
    429 case; specific code that wants to distinguish 429 from generic 5xx catches
    ``QuotaExceededError`` directly.
    """


class TokenDeniedError(Exception):
    """Raised when /v1/assess returns 401 token_expired.

    Covers both revoked and TTL-expired credentials — the API does not distinguish; it doesn't
    disclose which. Carries the full response body so the adapter can forward the
    auto-minted session fields (verify_url, session_id, poll_secret, poll_url,
    next_steps, agent_memory) to the agent instead of collapsing to wallet_not_trusted.
    """

    def __init__(self, body: dict[str, Any]) -> None:
        super().__init__("token_expired")
        self.code: Literal["token_expired"] = "token_expired"
        self.body: dict[str, Any] = body


def build_token_denied_reason(err: TokenDeniedError) -> DenialReason:
    """Project a TokenDeniedError into a DenialReason with forwarded auto-session fields.

    Every adapter's 403 body then surfaces verify_url + poll data identically to bootstrap.
    """
    from agentscore_commerce.identity.types import DenialReason

    body = err.body
    return DenialReason(
        code=err.code,
        verify_url=body.get("verify_url") if isinstance(body.get("verify_url"), str) else None,
        session_id=body.get("session_id") if isinstance(body.get("session_id"), str) else None,
        poll_secret=body.get("poll_secret") if isinstance(body.get("poll_secret"), str) else None,
        poll_url=body.get("poll_url") if isinstance(body.get("poll_url"), str) else None,
        agent_instructions=(json.dumps(body["next_steps"]) if isinstance(body.get("next_steps"), dict) else None),
    )


# Permanent — the operator_token doesn't exist (typo, never minted, fabricated).
# Distinct from TokenDeniedError: no auto-session is issued because the agent may
# have other valid tokens to try first. Agents should switch tokens or drop the
# header to bootstrap a fresh session.
INVALID_CREDENTIAL_INSTRUCTIONS = json.dumps(
    {
        "action": "switch_token_or_restart_session",
        "steps": [
            "The X-Operator-Token you sent does not match any credential. This is a permanent "
            "state — retrying with the same token will keep failing.",
            "If you have other stored opc_... credentials, retry with one of them.",
            "Otherwise drop X-Operator-Token and retry with no identity header — the merchant "
            "will mint a fresh verification session in the 403 body (verify_url + poll_secret) "
            "so the user can re-verify and you can poll for a new operator_token.",
        ],
        "user_message": (
            "The operator_token is not recognized. Use a different stored token, or restart the "
            "verification session flow to mint a new one."
        ),
    }
)


class InvalidCredentialError(Exception):
    """Raised when /v1/assess returns 401 invalid_credential.

    The token doesn't exist at all (typo, never minted, fabricated). No auto-session
    is issued — agents should switch to a different stored token or drop the header
    to bootstrap a fresh session via the merchant's createSessionOnMissing path.
    """

    def __init__(self) -> None:
        super().__init__("invalid_credential")
        self.code: Literal["invalid_credential"] = "invalid_credential"


def build_invalid_credential_reason() -> DenialReason:
    """Project an InvalidCredentialError into a DenialReason.

    No session fields — the API didn't mint one. Adapters render this as a 403 with
    agent_instructions that point the agent at recovery (try a different token or
    restart the session flow).
    """
    from agentscore_commerce.identity.types import DenialReason

    return DenialReason(
        code="invalid_credential",
        agent_instructions=INVALID_CREDENTIAL_INSTRUCTIONS,
    )
