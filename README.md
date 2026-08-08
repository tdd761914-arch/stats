# Telegram Bot API benchmark dashboard

`stats` is the public, dependency-light benchmark dashboard for the Telegram
Test Bot API and [TRBotApi](https://github.com/tdd761914-arch/TRBotApi).

The hourly GitHub Actions job:

1. builds the pinned TRBotApi release binary;
2. authenticates two disposable bot sessions on Telegram Test DC 2;
3. measures the official Test Bot API (`/test/`) and the local TRBotApi edge;
4. records latency, success rate, RSS/PSS/HWM and binary size without storing
   tokens;
5. commits `data/benchmarks.json` and deploys this directory to GitHub Pages.

The official test environment is separate from production. Test bot requests
must use `https://api.telegram.org/bot<TOKEN>/test/METHOD_NAME`; create the
accounts and bots in Telegram's Test Server first. The workflow does not try to
convert production tokens into test tokens.

## Required repository secrets

Configure these under **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
| --- | --- |
| `TRBOTAPI_BOT_TOKEN_A` | Test Bot API token for the first bot |
| `TRBOTAPI_BOT_TOKEN_B` | Test Bot API token for the second bot |
| `TRBOTAPI_API_ID` | Telegram application API ID used by TRLib MTProto |
| `TRBOTAPI_API_HASH` | Telegram application API hash used by TRLib MTProto |
| `TRBOTAPI_CHAT_ID` | Test user/group id for one safe `sendChatAction` comparison |

Tokens, API credentials and the chat id are read only by the runner process.
They are never written to JSON, HTML, artifacts or git history. Do not print
environment variables in a workflow step. Rotate any credential that has been
posted publicly.

## GitHub Pages

The workflow uses the repository's Pages setting with **GitHub Actions** as the
source. After the first successful run, the page is available at:

`https://tdd761914-arch.github.io/stats/`

The workflow is both scheduled (`0 * * * *`) and manually runnable with
**Run workflow**. `BENCHMARK_SAMPLES` can be supplied on a manual run to change
the number of latency samples (default: 5).

## Metric interpretation

Official `getMe` includes a real HTTPS round trip to Telegram's Test Bot API.
TRBotApi `getMe` is currently the allocation-light local edge fast path; it
therefore measures the HTTP parser/router and is not a fake claim of Telegram
network latency. The `sendChatAction` row is a compatibility probe: a failed
TRBotApi result is retained in the history instead of being hidden.

RSS is process resident memory, PSS is the more useful private-memory estimate,
and HWM is the peak RSS observed by Linux. Two processes are used because the
current reference binary accepts one bot token per process; code pages can be
shared when a production reactor hosts many bots.

## Local run

```bash
git clone https://github.com/tdd761914-arch/stats.git
cd stats
git clone --depth 1 https://github.com/tdd761914-arch/TRBotApi.git trbotapi
cargo build --release --locked --manifest-path trbotapi/Cargo.toml -p trbotapi-server

export TRBOTAPI_BOT_TOKEN_A='test-token-a'
export TRBOTAPI_BOT_TOKEN_B='test-token-b'
export TRBOTAPI_API_ID='your-api-id'
export TRBOTAPI_API_HASH='your-api-hash'
export TRBOTAPI_CHAT_ID='test-chat-id'
python3 scripts/benchmark.py --trbotapi-dir trbotapi --history data/benchmarks.json
```

The dashboard is a static file and has no CDN, npm or runtime dependency.
