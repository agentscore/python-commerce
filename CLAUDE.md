# agentscore-commerce

Agent commerce SDK for Python. The full merchant-side toolkit: identity gating + payment helpers + 402 builders + discovery + Stripe multichain. One install, submodule imports per concern. Mirror of `@agent-score/commerce` (Node.js) — same surface area, idiomatic Python.

## Submodules

| Submodule | What it is |
|---|---|
| `agentscore_commerce.identity.{fastapi,flask,django,aiohttp,sanic,middleware}` | Trust gate middleware (KYC, age, sanctions, jurisdiction) |
| `agentscore_commerce.payment` | Networks/USDC/rails registries, paymentauth.org directive builders, `create_x402_server` (wraps official `x402[evm,svm]>=2.8` peer dep with v1+v2 dual-register + bazaar extension), `create_mppx_server` (wraps `pympp[server,tempo,stripe]>=0.6` peer dep with Tempo charge/session + Stripe SPT helpers), dispatch-by-network, signer extraction, WWW-Authenticate header, Settlement-Overrides header |
| `agentscore_commerce.discovery` | Discovery probe, Bazaar wrapper, `/.well-known/mpp.json`, `llms.txt` builder, OpenAPI snippets |
| `agentscore_commerce.challenge` | 402-body builders: accepted_methods, identity_metadata, how_to_pay, agent_instructions, build_402_body |
| `agentscore_commerce.stripe_multichain` | Multichain PaymentIntent helper, deposit-address lookup, testnet simulator, mppx Stripe wrapper |
| `agentscore_commerce.api` | Re-exports `AgentScore` from `agentscore` SDK |

## Architecture

Single Python package, hatchling-built, published to PyPI as `agentscore-commerce`. Per-framework identity adapters expose the same surface — `AgentScoreGate` (or `agentscore_gate(app, ...)` for Flask/Sanic), `capture_wallet`, `verify_wallet_signer_match`, `get_assess_data` — with network-aware address normalization (EVM lowercased, Solana base58 preserved verbatim).

| Directory | Contents |
|---|---|
| `agentscore_commerce/identity/` | Per-framework gate adapters (fastapi, flask, django, aiohttp, sanic, middleware/ASGI), shared client, sessions, address normalization, signer extraction, response marshalling, types, cache |
| `agentscore_commerce/payment/` | Payment-protocol helpers |
| `agentscore_commerce/discovery/` | Discovery probe + machine-readable docs |
| `agentscore_commerce/challenge/` | 402-body builders |
| `agentscore_commerce/stripe_multichain/` | Stripe multichain PaymentIntent helpers |
| `agentscore_commerce/api/` | `AgentScore` re-export |
| `tests/` | pytest, one file per surface |

Mirror of node-commerce's structure — every helper lifted from the node version's working production code.

## Identity model

Two identity types: wallet (`X-Wallet-Address`) and operator-token (`X-Operator-Token`). Default checks operator-token first, then wallet. Address normalization is network-aware via `agentscore_commerce/identity/address.py`: EVM lowercased, Solana base58 preserved verbatim — used for cache keys, wallet→operator resolves, and signer-match comparisons.

`DenialReason` codes (`missing_identity`, `identity_verification_required`, `token_expired`, `invalid_credential`, `wallet_signer_mismatch`, `wallet_auth_requires_wallet_signing`, `wallet_not_trusted`, `api_error`, `payment_required`) each carry a structured `agent_instructions` JSON block describing concrete recovery actions. See `agentscore_commerce/identity/_response.py` for the canned action copy.

`create_session_on_missing` auto-mints a verification session when no identity is present and returns 403 with `verify_url` + poll instructions. `verify_wallet_signer_match` (per-adapter) compares the recovered signer against `linked_wallets[]` for cross-chain wallet-stack matching.

Captured wallets: `capture_wallet(...)` is fire-and-forget — reads `operator_token` stashed during gating and POSTs to `/v1/credentials/wallets`. No-ops for wallet-authenticated requests.

## Tooling

- **uv** — package manager.
- **ruff** — linting + formatting.
- **ty** — type checker (Astral).
- **vulture** — dead code detection.
- **pytest** — tests.
- **Lefthook** — pre-commit ruff, pre-push ty + vulture (parallel).

```bash
uv sync --all-extras
uv run lefthook install   # one-time per clone — wires pre-commit + pre-push
uv run ruff check .
uv run ruff format .
uv run ty check agentscore_commerce/
uv run pytest tests/
```

## Workflow

1. Create a branch
2. Make changes
3. Lefthook runs ruff on commit, ty + vulture on push
4. Open a PR — CI runs automatically
5. Merge (squash)

## Rules

- **No silent refactors**
- **Never commit .env files or secrets**
- **Use PRs** — never push directly to main
- **Helpers are protocol translations + configurable opinions, not opinionated frameworks**
- **Mirror node-commerce** — keep the surface area identical so vendors switching languages have the same mental model

## Releasing

1. Update `version` in `pyproject.toml`
2. Commit: `git commit -am "chore: bump to vX.Y.Z"`
3. Tag: `git tag vX.Y.Z`
4. Push: `git push && git push origin vX.Y.Z`

The publish workflow runs on `ubuntu-latest` (required for PyPI trusted publishing), builds, publishes to PyPI via OIDC, and creates a GitHub Release.
