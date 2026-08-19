# Deploying to Render

## 1. Push to GitHub

Create a repo and push these files:

```
main.py
requirements.txt
render.yaml
README.md
.gitignore
```

`openapi.json` and `test_flows.py` are useful to keep but not needed by the server.

## 2. Create the service on Render

1. Go to dashboard.render.com, New, Web Service
2. Connect the GitHub repo
3. Render reads `render.yaml` and fills in the settings. Confirm they look like this:
   - Runtime: Python
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - Plan: Free
4. Create Web Service

First build takes a couple of minutes. You get a URL like `https://mock-card-api.onrender.com`.

## 3. Set the API key

The service is public, so anyone with the URL could call `block_card`. To require a key:

Render dashboard, your service, Environment, Add Environment Variable:

```
Key:   API_KEY
Value: pick something long and random
```

Save, and Render redeploys. Every request then needs an `x-api-key` header. `/docs`, `/openapi.json`, and `/health` stay open so you can still copy the spec.

Leave `API_KEY` unset and the API stays open, which is fine if it is only up briefly.

## 4. Check it is live

```
https://your-service.onrender.com/health
https://your-service.onrender.com/docs
```

## 5. Add it to Neo

Foundry, Integrations, Custom Tools, Create:

| Field | Value |
|---|---|
| Name | Mock Card Management API |
| Description | Card servicing operations: identification, OTP, balance, transactions, limits, features, blocking, replacement, PIN reset, fraud cases |
| API specification | paste `openapi.json`, or fetch it from `/openapi.json` |
| Endpoint | `https://your-service.onrender.com` |
| Authentication | API Key Header if you set one, header name `x-api-key`. Otherwise None |

Then Test API on `get_balance` with `card_id` set to `CARD1001`. A 200 with a balance means you are wired up.

## Things to know about the free tier

**It sleeps.** After about 15 minutes idle the instance shuts down, and the next request takes roughly 30 seconds to wake it. Mid-conversation that looks like a timeout, so hit `/health` a minute before a demo to warm it up.

**Sleeping wipes state.** Data is in memory, so a card you blocked earlier goes back to active after a sleep. Not a problem for a demo, worth knowing if results look inconsistent.

**Not for anything real.** Fake data only. Do not point this at real customer records or extend it to talk to a real card system.

## Base URL, not a path

The endpoint field takes the host only. The spec already contains paths like `/cards/{card_id}/balance`, so entering `https://your-service.onrender.com/cards` would produce a doubled path and 404s.
