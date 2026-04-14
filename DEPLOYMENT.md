# Deployment Checklist

## Local Run

1. Copy the local env template:

```bash
cp .env.local.example .env
```

2. Fill in `TELEGRAM_BOT_TOKEN`.
3. Run the app:

```bash
cd /Users/eliseev/Development/SwiftUI/Mindset/Mindset
python3 run_xg_master_bot.py
```

## Docker Run

1. Copy the Docker env template:

```bash
cp .env.docker.example .env
```

2. Fill in `TELEGRAM_BOT_TOKEN`.
3. Start the stack:

```bash
cd /Users/eliseev/Development/SwiftUI/Mindset/Mindset/xG_Master_Bot
docker compose up --build
```

## Hugging Face Space

1. Create a new Space on Hugging Face.
2. Select `Docker` as the SDK.
3. Push this repository to the Space remote.
4. In Space settings, add the required secrets and variables.
5. Set `TELEGRAM_WEBHOOK_URL` to your public Space URL plus `/webhook`.
6. Restart the Space after saving secrets.

7. Make sure the Space container listens on port `7860` and runs the webhook server:

```bash
uvicorn xG_Master_Bot.webapp:app --host 0.0.0.0 --port 7860
```

### Required secrets

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `DATABASE_URL`

### Recommended variables

- `AI_PROVIDER`
- `DATA_PROVIDER`
- `OCR_PROVIDER`
- `DEFAULT_BET_PERCENT`
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_MODEL`
- `OPENAI_API_KEY`
- `HF_API_TOKEN`

## Database URL Templates

### Supabase

Use the connection string from the Supabase dashboard. For a persistent backend, Supabase recommends the direct connection when your environment supports IPv6, or the session pooler if it does not.

Direct:

```text
postgresql://postgres:[YOUR-PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres
```

Session pooler:

```text
postgresql://postgres.[PROJECT-REF]:[YOUR-PASSWORD]@aws-0-[REGION].pooler.supabase.com:5432/postgres
```

Transaction pooler:

```text
postgresql://postgres:[YOUR-PASSWORD]@db.[PROJECT-REF].supabase.co:6543/postgres
```

### Neon

Use the connection URI from the Neon console. A typical format looks like this:

```text
postgresql://[ROLE_NAME]:[PASSWORD]@[ENDPOINT_HOST]/[DATABASE_NAME]?sslmode=require
```

For example, Neon connection strings commonly use a host like:

```text
ep-xxxxxx.us-east-1.aws.neon.tech
```

Keep `sslmode=require` on hosted Neon connections.

## Final Hugging Face Space Secrets

Minimum:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `DATABASE_URL`

Recommended:

- `TELEGRAM_WEBHOOK_URL`
- `AI_PROVIDER`
- `DATA_PROVIDER`
- `OCR_PROVIDER`
- `DEFAULT_BET_PERCENT`
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_MODEL`

## Git Push Order

1. Create or switch to a branch:

```bash
git checkout -b codex/xg-master-bot
```

2. Review the changes:

```bash
git status
```

3. Commit the work:

```bash
git add xG_Master_Bot
git commit -m "Build xG master bot deployment stack"
```

4. Push to your remote repository or the Hugging Face Space remote:

```bash
git push -u origin codex/xg-master-bot
```

5. In Hugging Face, connect the Space repository and let it rebuild automatically.
