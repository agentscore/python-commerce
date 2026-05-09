"""Shared AgentScore assess client with TTL caching."""

from __future__ import annotations

import json
import logging
from importlib.metadata import version as _pkg_version
from typing import TYPE_CHECKING, Any, Literal, cast

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
    VerifyWalletSignerMatchOptions,
    VerifyWalletSignerResult,
)

if TYPE_CHECKING:
    from agentscore.types import DecisionPolicy, ResolveSigner

    from agentscore_commerce.identity.types import DenialReason

_log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.agentscore.sh"
DEFAULT_CACHE_SECONDS = 300


class GateClient:
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
    ) -> None:
        if not api_key:
            msg = "AgentScore API key is required. Get one at https://agentscore.sh/sign-up"
            raise ValueError(msg)

        self.fail_open = fail_open
        self._api_key = api_key
        self._base_url = base_url
        # Public accessor so adapters can build agent_memory hints pointing at the same API.
        self.base_url = base_url
        self._chain = chain
        default_ua = f"agentscore-commerce/{_pkg_version('agentscore-commerce')}"
        self.user_agent = f"{user_agent} ({default_ua})" if user_agent else default_ua
        self._cache: TTLCache[AssessResult] = TTLCache(cache_seconds)

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

    def _cache_key(self, address: str | None = None, operator_token: str | None = None) -> str:
        # operator_token is opaque ASCII — lowercasing is safe. Wallet addresses go through
        # normalize_address so Solana base58 (case-sensitive) isn't corrupted into a cache miss.
        if operator_token:
            return operator_token.lower()
        return normalize_address(address) if address else ""

    def _build_body(
        self, address: str | None = None, chain: str | None = None, operator_token: str | None = None
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
        effective_chain = chain or self._chain
        if effective_chain:
            body["chain"] = effective_chain
        if self._policy:
            body["policy"] = self._policy
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
            quota=quota,
            raw=data,
        )

    def check(
        self, address: str | None = None, chain: str | None = None, operator_token: str | None = None
    ) -> AssessResult:
        """Synchronous assess call with caching. Accepts address and/or operator_token."""
        key = self._cache_key(address, operator_token)

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

        result = self._project(cast("dict[str, Any]", data))
        self._cache.set(key, result)
        return result

    async def acheck(
        self, address: str | None = None, chain: str | None = None, operator_token: str | None = None
    ) -> AssessResult:
        """Asynchronous assess call with caching. Accepts address and/or operator_token."""
        key = self._cache_key(address, operator_token)

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

        result = self._project(cast("dict[str, Any]", data))
        self._cache.set(key, result)
        return result

    def check_identity(self, identity: AgentIdentity, chain: str | None = None) -> AssessResult:
        """Convenience method to check using an AgentIdentity object."""
        return self.check(address=identity.address, chain=chain, operator_token=identity.operator_token)

    async def acheck_identity(self, identity: AgentIdentity, chain: str | None = None) -> AssessResult:
        """Async convenience method to check using an AgentIdentity object."""
        return await self.acheck(address=identity.address, chain=chain, operator_token=identity.operator_token)

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

    def _resolve_from_cache(self, wallet: str) -> tuple[bool, str | None, list[str]]:
        """Look up a wallet in either cache. Returns (hit, operator, linked_wallets)."""
        for key in (wallet, f"resolve:{wallet}"):
            cached = self._cache.get(key)
            if cached is not None:
                raw = cached.raw or {}
                op = raw.get("resolved_operator")
                links_raw = raw.get("linked_wallets")
                links = [w for w in links_raw if isinstance(w, str)] if isinstance(links_raw, list) else []
                if op is None or isinstance(op, str):
                    return True, op, links
        return False, None, []

    def _resolve_wallet_to_operator(self, wallet_address: str) -> tuple[bool, str | None, list[str]]:
        """Resolve a wallet to its operator id via /v1/assess.

        Returns ``(ok, operator)``:
        - ``(True, <id>)``: wallet linked to that operator
        - ``(True, None)``: wallet is valid but unlinked
        - ``(False, None)``: transient API failure (network / non-2xx). Caller should emit
          an ``api_error`` result rather than silently assert the wallet is unlinked.

        Checks both the main evaluate cache and the resolve-specific cache before calling
        the API — saves a second /v1/assess when the gate already resolved this wallet
        during identity evaluation.

        Returns ``(ok, operator, linked_wallets)``. ``linked_wallets`` is the set of wallets
        sharing the same operator (both wallet-claim and captured-signer links); echoed back to
        agents on ``wallet_signer_mismatch`` denials so they know which wallets they can
        legitimately sign with.
        """
        # Network-aware: lowercase EVM, preserve Solana base58 case. The DB stores both
        # formats verbatim in operator_credential_wallets.wallet_address; lowercasing a
        # Solana address would never match. The cache key uses the same normalized form.
        wallet = normalize_address(wallet_address)
        hit, op, links = self._resolve_from_cache(wallet)
        if hit:
            return True, op, links
        try:
            data = self._sdk.assess(address=wallet)
        except AgentScoreError:
            return False, None, []
        data_dict = cast("dict[str, Any]", data)
        self._cache.set(f"resolve:{wallet}", AssessResult(allow=True, raw=data_dict))
        op_value = data_dict.get("resolved_operator")
        linked_raw = data_dict.get("linked_wallets")
        linked = [w for w in linked_raw if isinstance(w, str)] if isinstance(linked_raw, list) else []
        return True, (op_value if isinstance(op_value, str) else None), linked

    async def _aresolve_wallet_to_operator(self, wallet_address: str) -> tuple[bool, str | None, list[str]]:
        # Same network-aware normalization as the sync path; see _resolve_wallet_to_operator.
        wallet = normalize_address(wallet_address)
        hit, op, links = self._resolve_from_cache(wallet)
        if hit:
            return True, op, links
        try:
            data = await self._sdk.aassess(address=wallet)
        except AgentScoreError:
            return False, None, []
        data_dict = cast("dict[str, Any]", data)
        self._cache.set(f"resolve:{wallet}", AssessResult(allow=True, raw=data_dict))
        op_value = data_dict.get("resolved_operator")
        linked_raw = data_dict.get("linked_wallets")
        linked = [w for w in linked_raw if isinstance(w, str)] if isinstance(linked_raw, list) else []
        return True, (op_value if isinstance(op_value, str) else None), linked

    def _report_signer_event_sync(self, kind: str) -> None:
        """Fire-and-forget telemetry post. Never raises.

        The SDK's ``telemetry_signer_match`` already swallows all errors internally —
        this method is just the commerce-side dispatch.
        """
        self._sdk.telemetry_signer_match({"kind": kind})

    async def _report_signer_event_async(self, kind: str) -> None:
        """Async variant. SDK swallows all errors internally."""
        await self._sdk.atelemetry_signer_match({"kind": kind})

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

    def verify_wallet_signer_match(self, options: VerifyWalletSignerMatchOptions) -> VerifyWalletSignerResult:
        """Verify payment signer resolves to the same operator as the claimed wallet.

        Single-call path: makes one ``/v1/assess`` request with ``resolve_signer`` set;
        the response carries a ``signer_match`` verdict the gate projects directly. Falls
        back to a two-resolve path when the response has no ``signer_match`` so the gate
        still produces a verdict.

        Returns:
            ``kind='pass'`` when the signer is the claimed wallet (byte-equal) or both resolve
            to the same operator. ``kind='wallet_signer_mismatch'`` when operators differ.
            ``kind='wallet_auth_requires_wallet_signing'`` when ``signer`` is ``None`` (SPT/card).
            ``kind='api_error'`` when /v1/assess resolve failed — caller should retry or surface
            as 503; distinct from mismatch so legitimate users aren't rejected on network flakes.
        """
        signer = options.signer
        if signer is None:
            self._report_signer_event_sync("wallet_auth_requires_wallet_signing")
            return VerifyWalletSignerResult(
                kind="wallet_auth_requires_wallet_signing",
                claimed_wallet=options.claimed_wallet,
                agent_instructions=WALLET_AUTH_REQUIRES_WALLET_SIGNING_INSTRUCTIONS,
            )
        # Network-aware normalization: lowercase EVM, preserve Solana base58. Both the
        # byte-equal short-circuit AND the cache key derive from this — lowercasing
        # Solana would corrupt both and make every Solana signer-match return api_error.
        claimed = normalize_address(options.claimed_wallet)
        signer_norm = normalize_address(signer)
        if claimed == signer_norm:
            self._report_signer_event_sync("pass")
            return VerifyWalletSignerResult(kind="pass")

        # Cache hit: a prior call for this same (claimed, signer) pair populated signer_match.
        # Skip both the round trip AND the SDK telemetry post (the API recorded it last time).
        cached_entry = self._cache.get(claimed)
        if cached_entry is not None:
            cached_match = cached_entry.signer_match_by_signer.get(signer_norm)
            if cached_match is not None:
                return self._project_signer_match(cached_match, claimed, signer_norm)

        # Single resolve_signer-aware assess call — server-side resolves both wallets and
        # returns a verdict in one round trip.
        network = options.network or self._infer_signer_network(signer_norm)
        try:
            data = self._sdk.assess(
                address=claimed,
                resolve_signer=cast("ResolveSigner", {"address": signer_norm, "network": network}),
            )
        except AgentScoreError as exc:
            _log.warning("[gate] verify_wallet_signer_match assess failed: %s", exc)
            self._report_signer_event_sync("api_error")
            return VerifyWalletSignerResult(kind="api_error", claimed_wallet=claimed)

        data_dict = cast("dict[str, Any]", data)
        sm = data_dict.get("signer_match")
        if isinstance(sm, dict):
            if cached_entry is not None:
                # Mutate in place — TTLCache.get() returns a reference, so the stored
                # entry sees the new sub-dict without a set() call. This preserves the
                # gate's original cache TTL window (set() would reset it forward,
                # causing the gate verdict to be served past its intended freshness).
                cached_entry.signer_match_by_signer[signer_norm] = sm
            else:
                # No prior gate cache for this wallet — create a fresh entry with the
                # verdict attached so a subsequent same-pair call hits cache.
                entry = AssessResult(allow=True, raw=data_dict)
                entry.signer_match_by_signer[signer_norm] = sm
                self._cache.set(claimed, entry)
            return self._project_signer_match(sm, claimed, signer_norm)

        # API response had no signer_match (server didn't compute one). Two-resolve
        # path produces a verdict from the same operator graph.
        claimed_ok, claimed_op, claimed_links = self._resolve_wallet_to_operator(claimed)
        signer_ok, signer_op, _ = self._resolve_wallet_to_operator(signer_norm)
        if not claimed_ok or not signer_ok:
            self._report_signer_event_sync("api_error")
            return VerifyWalletSignerResult(kind="api_error", claimed_wallet=claimed)
        if claimed_op and signer_op and claimed_op == signer_op:
            self._report_signer_event_sync("pass")
            return VerifyWalletSignerResult(kind="pass", claimed_operator=claimed_op, signer_operator=signer_op)
        self._report_signer_event_sync("wallet_signer_mismatch")
        return VerifyWalletSignerResult(
            kind="wallet_signer_mismatch",
            claimed_operator=claimed_op,
            actual_signer_operator=signer_op,
            expected_signer=claimed,
            actual_signer=signer_norm,
            linked_wallets=claimed_links,
            agent_instructions=WALLET_SIGNER_MISMATCH_INSTRUCTIONS,
        )

    async def averify_wallet_signer_match(self, options: VerifyWalletSignerMatchOptions) -> VerifyWalletSignerResult:
        """Async variant of :meth:`verify_wallet_signer_match`."""
        signer = options.signer
        if signer is None:
            await self._report_signer_event_async("wallet_auth_requires_wallet_signing")
            return VerifyWalletSignerResult(
                kind="wallet_auth_requires_wallet_signing",
                claimed_wallet=options.claimed_wallet,
                agent_instructions=WALLET_AUTH_REQUIRES_WALLET_SIGNING_INSTRUCTIONS,
            )
        claimed = normalize_address(options.claimed_wallet)
        signer_norm = normalize_address(signer)
        if claimed == signer_norm:
            await self._report_signer_event_async("pass")
            return VerifyWalletSignerResult(kind="pass")

        cached_entry = self._cache.get(claimed)
        if cached_entry is not None:
            cached_match = cached_entry.signer_match_by_signer.get(signer_norm)
            if cached_match is not None:
                return self._project_signer_match(cached_match, claimed, signer_norm)

        network = options.network or self._infer_signer_network(signer_norm)
        try:
            data = await self._sdk.aassess(
                address=claimed,
                resolve_signer=cast("ResolveSigner", {"address": signer_norm, "network": network}),
            )
        except AgentScoreError as exc:
            _log.warning("[gate] averify_wallet_signer_match assess failed: %s", exc)
            await self._report_signer_event_async("api_error")
            return VerifyWalletSignerResult(kind="api_error", claimed_wallet=claimed)

        data_dict = cast("dict[str, Any]", data)
        sm = data_dict.get("signer_match")
        if isinstance(sm, dict):
            if cached_entry is not None:
                # Async mirror of the in-place-mutate optimization in :meth:`verify_wallet_signer_match`.
                cached_entry.signer_match_by_signer[signer_norm] = sm
            else:
                entry = AssessResult(allow=True, raw=data_dict)
                entry.signer_match_by_signer[signer_norm] = sm
                self._cache.set(claimed, entry)
            return self._project_signer_match(sm, claimed, signer_norm)

        # Legacy fallback — async mirror of the sync path.
        claimed_ok, claimed_op, claimed_links = await self._aresolve_wallet_to_operator(claimed)
        signer_ok, signer_op, _ = await self._aresolve_wallet_to_operator(signer_norm)
        if not claimed_ok or not signer_ok:
            await self._report_signer_event_async("api_error")
            return VerifyWalletSignerResult(kind="api_error", claimed_wallet=claimed)
        if claimed_op and signer_op and claimed_op == signer_op:
            await self._report_signer_event_async("pass")
            return VerifyWalletSignerResult(kind="pass", claimed_operator=claimed_op, signer_operator=signer_op)
        await self._report_signer_event_async("wallet_signer_mismatch")
        return VerifyWalletSignerResult(
            kind="wallet_signer_mismatch",
            claimed_operator=claimed_op,
            actual_signer_operator=signer_op,
            expected_signer=claimed,
            actual_signer=signer_norm,
            linked_wallets=claimed_links,
            agent_instructions=WALLET_SIGNER_MISMATCH_INSTRUCTIONS,
        )


# Re-export the timeout error class so adapters can recognize SDK-side timeouts
# without having to import it from the underlying SDK directly.
__all_sdk_timeout__ = SdkTimeoutError


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

    Covers both revoked and TTL-expired credentials — the API deliberately doesn't
    disclose which. Carries the full response body so the adapter can forward the
    auto-minted session fields (verify_url, session_id, poll_secret, poll_url,
    next_steps, agent_memory) to the agent instead of collapsing to wallet_not_trusted.
    """

    def __init__(self, body: dict[str, Any]) -> None:
        super().__init__("token_expired")
        self.code: Literal["token_expired"] = "token_expired"
        self.body: dict[str, Any] = body
        # Legacy accessor for callers that read .next_steps directly.
        self.next_steps = body.get("next_steps") if isinstance(body, dict) else None


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
        agent_instructions=json.dumps(err.next_steps) if err.next_steps else None,
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
