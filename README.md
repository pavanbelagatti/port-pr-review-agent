# Port PR Review Agent

A webhook server that automatically reviews pull requests using Port's 
developer portal as context and GPT-4o as the reasoning engine.

Deployed on Railway and triggered by Port's `AI PR Review on Created` 
automation every time a new PR is captured from GitHub.

## How it works
New PR opened on GitHub

↓

Port captures it via GitHub Ocean integration

↓

"AI PR Review on Created" automation fires

↓

POST → Railway (this server) /webhook/pr-review

↓

Fetches service context from Port catalog
(scorecard level, team ownership, workloads, CODEOWNERS, README)

↓

Reasons with GPT-4o → generates verdict + markdown review

↓

Posts review comment directly to the GitHub PR

↓

Writes ai_risk_level + ai_summary back to Port PR entity

## Why this exists alongside Port's native AI Agents
Port's native AI Agents work entirely within the Port catalog. 
This server does two things Port-native agents can't:
- Calls the GitHub API directly to post a comment on the actual PR
- Reads raw service context and reasons with a custom GPT-4o prompt

## Environment variables
| Variable | Description |
|---|---|
| `PORT_CLIENT_ID` | Port API client ID |
| `PORT_CLIENT_SECRET` | Port API client secret |
| `GITHUB_TOKEN` | GitHub personal access token (repo scope) |
| `OPENAI_API_KEY` | OpenAI API key |

## Deploy to Railway
1. Fork this repo
2. Create a new Railway project and connect the repo
3. Add the environment variables above in Railway's Variables tab
4. Railway auto-deploys on every push — the server runs on port `8080`
5. Copy the Railway public URL and set it in Port's `AI PR Review on Created` 
   automation as the webhook URL:
   `https://your-railway-app.up.railway.app/webhook/pr-review`

## Endpoints
- `POST /webhook/pr-review` — main webhook called by Port automation
- `GET /health` — health check

## Tech stack
- Python + Flask
- OpenAI GPT-4o
- Port API
- GitHub REST API
- Deployed on Railway
