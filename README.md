# Mock Card Management API

Stands in for a real card management system so the Card Operations Coworker can be built and tested before any live backend exists. State is held in memory, so blocking a card really does change its status. Restarting resets everything.

## Run

```bash
pip install fastapi uvicorn
uvicorn main:app --reload --port 8000
```

- Interactive docs: http://localhost:8000/docs
- OpenAPI spec: http://localhost:8000/openapi.json

## Adding it to Neo

Foundry, Integrations, Custom Tools, Create. Paste the contents of `openapi.json`, set the endpoint to wherever this is hosted, leave authentication as None. Neo extracts all 20 operations as individually callable tools.

The endpoint must be reachable from the Neo deployment, so localhost only works if Neo runs on the same machine. Otherwise deploy the container somewhere Neo can reach.

## Test data

| Customer | Mobile | Cards |
|---|---|---|
| Ananya Sharma (CUST001) | 9876543210 | CARD1001 ending 4521, CARD1002 ending 8834 |
| Rohit Menon (CUST002) | 9123456780 | CARD2001 ending 7710 |

OTP is always `123456`.

CARD1001 has a suspicious pending transaction, `TXN9003`, for 18,999 from "UNKNOWN MERCHANT 44821". Use it to demo the fraud flow.

## What the API enforces

These constraints are in the API rather than left to the coworker's judgement, so the ordering rules in the plan cannot be skipped:

- A fraud case cannot be logged unless the card is already blocked
- A replacement for a lost or stolen card cannot be requested unless the card is already blocked
- Limits, features, and PIN reset are rejected on a blocked card
- Blocking twice returns `already_blocked` rather than erroring
- An unknown identifier still reports that an OTP was sent, so the endpoint cannot be used to discover which numbers are registered
- OTP challenges are single use and expire after 5 minutes

## What the API deliberately cannot do

There is no operation to set a PIN, retrieve a full card number, or handle a CVV. The full card number does not exist in the data at all, only the masked form and last four digits. So the coworker cannot leak or misuse these values, because it has no way to obtain them.

## Testing the PIN lifecycle

`initiate_pin_reset` returns a link the customer would use on the bank's own screen. Since that screen does not exist here, `POST /pin-reset/{request_id}/simulate-completion` stands in for the customer finishing it, so `get_pin_reset_status` can be demonstrated returning `completed`.

That endpoint is tagged Testing and is not for the coworker to call.

## Test script

```bash
python3 test_flows.py
```

Walks the session start, batched changes, both flow orderings, the blocked-card rejections, and the PIN lifecycle.
