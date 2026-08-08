# Telegram Bot API benchmark dashboard

`stats` is the public, dependency-light benchmark dashboard comparing the
official [Telegram Bot API server repository](https://github.com/tdlib/telegram-bot-api)
with [TRBotApi](https://github.com/tdd761914-arch/TRBotApi).

The hourly GitHub Actions job:

1. builds the TRBotApi release binary;
2. builds the official `tdlib/telegram-bot-api` release binary;
3. starts two isolated instances of each server on the same runner;
4. authenticates two disposable bot sessions on Telegram Test DC 2;
5. measures both local HTTP servers through their Test-DC `/test/` routes;
4. records latency, success rate, RSS/PSS/HWM and binary size without storing
   tokens;
5. commits `data/benchmarks.json` and deploys this directory to GitHub Pages.

The official test environment is separate from production. Create the accounts
and bots in Telegram's Test Server first. The official server receives test
requests at `http://127.0.0.1:<port>/bot<TOKEN>/test/METHOD_NAME`; TRBotApi uses
the same test path semantics at its local port. The workflow does not compare
against the cloud endpoint and does not convert production tokens into test
tokens.

## Required repository secrets

Configure these under **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
| --- | --- |
| `TRBOTAPI_BOT_TOKEN_A` | Test Bot API token for the first bot |
| `TRBOTAPI_BOT_TOKEN_B` | Test Bot API token for the second bot |
| `TRBOTAPI_API_ID` | Telegram application API ID passed to both local servers |
| `TRBOTAPI_API_HASH` | Telegram application API hash passed to both local servers |
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

The official column is the locally built `tdlib/telegram-bot-api` process and
the TRBotApi column is the locally built Rust process. Both receive the same
test token, JSON body, localhost workload and Test-DC route, so the comparison
does not mix in public Internet latency. `getMe` is an allocation-light local
edge path in TRBotApi; the `sendChatAction` row is a compatibility probe and a
failed result is retained in the history instead of being hidden.

RSS is process resident memory, PSS is the more useful private-memory estimate,
and HWM is the peak RSS observed by Linux. Two processes are used because the
current reference binary accepts one bot token per process; code pages can be
shared when a production reactor hosts many bots.

## Local run

```bash
git clone https://github.com/tdd761914-arch/stats.git
cd stats
git clone --depth 1 https://github.com/tdd761914-arch/TRBotApi.git trbotapi
git clone --depth 1 --recurse-submodules https://github.com/tdlib/telegram-bot-api.git telegram-bot-api
cargo build --release --locked --manifest-path trbotapi/Cargo.toml -p trbotapi-server
cmake -S telegram-bot-api -B telegram-bot-api/build -DCMAKE_BUILD_TYPE=Release
cmake --build telegram-bot-api/build --target telegram-bot-api --parallel 2

export TRBOTAPI_BOT_TOKEN_A='test-token-a'
export TRBOTAPI_BOT_TOKEN_B='test-token-b'
export TRBOTAPI_API_ID='your-api-id'
export TRBOTAPI_API_HASH='your-api-hash'
export TRBOTAPI_CHAT_ID='test-chat-id'
python3 scripts/benchmark.py \
  --trbotapi-dir trbotapi \
  --official-dir telegram-bot-api \
  --history data/benchmarks.json
```

The dashboard is a static file and has no CDN, npm or runtime dependency.
