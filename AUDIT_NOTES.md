# V78.7.2 multi-source search + portfolio sector audit

## Fixed in this build
- Korean symbol search is no longer dependent on a single FinanceDataReader KRX master.
- Search order now combines local/curated KRX metadata, FinanceDataReader KRX listing, Naver mobile autocomplete/legacy autocomplete, and Yahoo for US symbols.
- Verified fallback added for 우리기술 (032820) so it remains searchable even if the KRX master is temporarily unavailable on a cloud host.
- 우리기술 is mapped to the app sector `전력/원전` with source industry context `코스닥 전기·전자 / 원전 계측제어` and theme tags 원전/SMR/계측제어.
- Exact Korean stock lookups try Naver stock profile metadata as a secondary source for canonical name/exchange/source industry.
- Market filters now distinguish domestic stocks vs domestic ETFs instead of mixing them in the autocomplete result.

## My Stocks / sector improvements
- Portfolio records now persist `sector`, `sector_major`, `source_sector`, `theme_tags`, and `sector_updated_at` on both device and server.
- New holdings are resolved again at save time, so typing a name without clicking the autocomplete item is less likely to save an unresolved name-only key.
- New holdings are analyzed in the background after save to populate current price and sector automatically.
- Existing holdings missing sector are backfilled in the background from `/api/symbols` after account sync, then saved back to device/server.
- Portfolio cards show mapped app sector, source industry (when available), and theme tags.
- `내 종목 섹터` summary shows sector distribution and flags unclassified holdings for refresh.
- Portfolio merge now preserves non-empty canonical name/code/sector metadata and prefers the newer analysis snapshot, preventing an older device copy from blanking server-side sector metadata.
- Cloud-save verification signature now includes canonical/security/sector metadata, not only avg price and quantity.

## Upgrade/data safety retained
- Permanent browser key remains `v78-portfolio-account-v1`; app version upgrades do not change the portfolio storage key.
- Server/device portfolios are merged non-destructively on sync.
- Browser and server backup protections remain enabled.
- Render still needs a persistent disk at `/var/data` with `DATA_DIR=/var/data` for server-side persistence across redeploys.

## Validation performed
- Python syntax: app.py / engine.py
- JavaScript syntax: static/app.js
- manifest JSON parse
- HTML duplicate ID scan
- Offline provider-failure simulation: `우리기술` resolves to `032820`, KOSDAQ, app sector `전력/원전` even when FDR/Naver network calls are unavailable.
