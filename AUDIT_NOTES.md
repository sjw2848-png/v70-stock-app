# V78.7.1 upgrade-safe portfolio audit

- Portfolio browser key is now permanent (`v78-portfolio-account-v1`) and no longer changes with app versions.
- Automatically migrates V78.6.0–V78.6.3 per-account browser keys into the permanent key.
- Cloud refresh is non-destructive: server + device copies are merged before display/upload, so an empty/stale server cannot wipe a non-empty device portfolio during redeploy.
- Browser keeps a previous portfolio backup before each overwrite.
- Server keeps `portfolio.backup.json` before replacing `portfolio.json`; writes are atomic and fsynced.
- Existing account separation and mobile/PC sync remain enabled.
- IMPORTANT: Render still requires a persistent disk mounted at `/var/data` with `DATA_DIR=/var/data`; code cannot make an ephemeral filesystem persistent.
