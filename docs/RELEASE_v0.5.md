# Release v0.5 (pre multi-expert)

Snapshot of the working single-expert (阿FU) product before 1.0 multi-expert work.

## Git

- Tag: `v0.5`
- Branch: `release/v0.5` (same commit as the tag)
- Typical commit: monthly token quota + admin dashboard + conversational style

## Rollback (code)

```bash
git fetch origin
git checkout release/v0.5
python scripts/deploy.py
```

Or deploy a specific commit via your hosting panel if the branch is pushed.

## Data / knowledge

- **Accounts & chats**: Cloudflare R2 backup (`BACKUP_R2_*` / `users.db` zip). Force sync: `POST /api/admin/sync-users-db` with `X-Sync-Secret`.
- **Knowledge index**: GitHub Release `index-v1` asset used by `INDEX_BUNDLE_URL` in `deploy-config.json`.

## Live URL

https://relationship-expert.ai-builders.space/

## Notes

- v0.5 is single consultant 阿FU; one vault/index; one shared `user_memory` blob.
- Do not delete R2 objects or `index-v1` while `release/v0.5` may still need rollback.
