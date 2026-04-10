# Android Branch Audit - 2026-03-25

## Confirmed findings
- The VkusVill APK `26.4.6` exposes a real mobile backend surface at `https://mobile.vkusvill.ru/api/`.
- Auth flow includes `guest/createAnonymousCard`, `v1/user/otp/account-creating`, `v1/user/otp/auth`, and the matching confirm routes.
- The app stores auth state through `storedAccessToken`, `storedRefreshToken`, and `storedSessionInfo` symbols, so the mobile runtime is stateful without any Chrome profile.
- SSL pinning is enabled by default, and the APK contains debug-pref flags for disabling it.
- `mobile.vkusvill.ru` and `mobile-longpoll.vkusvill.ru` both resolve, but this Windows desktop currently times out at TLS handshake, so desktop Schannel is still a blocker for live auth attempts here.

## What this means
- Chrome is not the core runtime for the mobile branch.
- The mobile branch should be modeled as a token/state-driven runtime with durable refresh support.
- For a future VPS migration, the portable state should be carried as a session snapshot, not as a browser profile.

## Current implementation direction
- Keep `src/mobile_api.py` as the token/state layer.
- Persist a portable `data/mobile_state.json` snapshot alongside `.env`.
- Continue treating Android auth as the real contract, and the current desktop TLS path as an environment blocker rather than the actual product architecture.

## Next step
- Get a live Android runtime path or device-backed verification path so the APK contract can be exercised outside the Windows desktop TLS stack.
