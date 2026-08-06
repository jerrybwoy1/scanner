# QikReach Mobile — Cloudflare Free Deployment

Cloudflare-native browser version. It uses Workers, static assets, and D1, and does not require a computer to remain on.

## One-time Cloudflare setup

1. In Cloudflare, open **Workers & Pages** and connect this GitHub repository.
2. Create a D1 database named `qikreach-vault`.
3. Copy its database ID into `wrangler.jsonc`, replacing `REPLACE_WITH_D1_DATABASE_ID`.
4. In the Worker settings, add encrypted secrets:
   - `QIKREACH_PASSWORD`: your login password
   - `QIKREACH_SESSION_SECRET`: a long random value of at least 32 characters
5. Deploy the Worker.
6. Initialize D1 once by running `schema.sql` in Cloudflare's D1 console, or run `npm run db:init` with Wrangler authenticated.

The username defaults to `admin` and may be changed in `wrangler.jsonc`.

## Free-build limits

- `.xlsx` uploads only
- 10 MiB maximum upload
- 1,000 rows maximum per batch
- The enriched workbook is returned immediately rather than retained as a file
- Lead records persist in D1

## Local verification

```bash
npm install
npx wrangler d1 create qikreach-vault
# Put the returned database ID in wrangler.jsonc
npx wrangler secret put QIKREACH_PASSWORD
npx wrangler secret put QIKREACH_SESSION_SECRET
npm run dev
```
