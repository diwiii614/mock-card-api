"""Regression checks for the mock card API.

Start the server, then run this. It walks every behaviour the coworker relies
on, including the ordering rules that the API enforces rather than trusting the
model to follow.

    uvicorn main:app --port 8000
    python3 test_flows.py
"""

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"

passed = failed = 0


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def section(name):
    print(f"\n{name}")


# ------------------------------------------------------------------ session
section("Session")
call("POST", "/testing/reset")

s, r = call("POST", "/auth/verify-otp", {"identifier": "9876543210", "otp": "123456"})
check("verify before identify is refused", "No OTP is outstanding" in r["message"], r)

call("POST", "/auth/identify", {"identifier": "9876543210"})
s, r = call("POST", "/auth/verify-otp", {"identifier": "9876543210", "otp": "999999"})
check("wrong code refused", r["verified"] is False)

call("POST", "/auth/identify", {"identifier": "9876543210"})
s, r = call("POST", "/auth/verify-otp", {"identifier": "9876543210", "otp": "123456"})
check("correct code accepted", r["verified"] and r["customer_id"] == "CUST001")

s, r = call("POST", "/auth/verify-otp", {"identifier": "9876543210", "otp": "123456"})
check("codes are single use", r["verified"] is False)

s, r = call("POST", "/auth/identify", {"identifier": "0000000000"})
check("unknown number does not leak", r["otp_sent"] is True)

cards = call("GET", "/customers/CUST001/cards")[1]
check("cards return expiry", all("days_until_expiry" in c for c in cards))
check("expiring card is flagged",
      [c for c in cards if c["card_id"] == "CARD1002"][0]["expiring_soon"])

# --------------------------------------------------------- identifiers
section("Identifier resolution")
check("balance by last four", call("GET", "/cards/4521/balance")[1]["card_id"] == "CARD1001")
check("address by card id",
      call("GET", "/customers/CARD1001/address")[1]["customer_id"] == "CUST001")
check("cards by mobile",
      len(call("GET", "/customers/9876543210/cards")[1]) == 2)
check("unknown card gives a useful error",
      "Use the card_id" in call("GET", "/cards/9999/balance")[1]["detail"])

# --------------------------------------------------------------- limits
section("Limits")
lim = call("GET", "/cards/CARD1001/limits")[1]
check("card limit is exposed", lim["card_limit"] == 150000)
check("within ceiling accepted",
      call("PUT", "/cards/CARD1001/limits", {"online": 120000})[0] == 200)
check("above ceiling rejected",
      call("PUT", "/cards/CARD1001/limits", {"online": 200000})[0] == 422)
call("PUT", "/cards/CARD1001/limits", {"atm": 44444, "online": 999999})
check("rejection is atomic",
      call("GET", "/cards/CARD1001/limits")[1]["limits"]["atm"] != 44444)
check("negative rejected", call("PUT", "/cards/CARD1001/limits", {"atm": -1})[0] == 422)
check("above absolute max rejected",
      call("PUT", "/cards/CARD1001/limits", {"atm": 5_000_000})[0] == 422)
check("batched change applies together",
      call("PUT", "/cards/CARD1001/limits",
           {"atm": 30000, "online": 60000})[1]["updated"] == {"atm": 30000, "online": 60000})

# -------------------------------------------------------------- features
section("Features")
check("batched toggles apply together",
      call("PUT", "/cards/CARD1001/features",
           {"contactless": False, "international_usage": True})[1]["updated"] ==
      {"contactless": False, "international_usage": True})

# -------------------------------------------------------------- declines
section("Declined payments")
d = call("GET", "/cards/CARD1001/declined?limit=25")[1]
check("declines are present", d["declined_count"] == 8, d["declined_count"])
check("every decline has a fix", all("suggested_fix" in t for t in d["transactions"]))
check("merchant filter works",
      call("GET", "/cards/CARD1001/declined?merchant=booking")[1]["declined_count"] == 1)
reasons = {t["decline_reason"] for t in d["transactions"]}
check("insufficient balance case exists", "insufficient_balance" in reasons)
check("contactless case exists on CARD2001",
      any(t["decline_reason"] == "contactless_disabled"
          for t in call("GET", "/cards/CARD2001/declined")[1]["transactions"]))

# ---------------------------------------------------------- transactions
section("Transactions")
t = call("GET", "/cards/CARD1001/transactions?limit=500")[1]
check("page size is capped at 25", t["returned"] == 25 and t["has_more"])
july = call("GET",
            "/cards/CARD1001/transactions?from_date=2026-07-01&to_date=2026-07-31&limit=25")[1]
check("date filter works", all(x["date"].startswith("2026-07") for x in july["transactions"]))
z = call("GET", "/cards/CARD1001/transactions?merchant=Zomato&limit=25")[1]["transactions"]
check("duplicate charge pair present", len([x for x in z if x["amount"] == 742.0]) == 2)
check("fraud demo transaction present",
      call("GET", "/cards/CARD1001/transactions?merchant=UNKNOWN")[1]["total_matching"] == 1)

# ------------------------------------------------------------- ordering
section("Ordering rules enforced by the API")
call("POST", "/testing/reset")
check("fraud case refused before block",
      call("POST", "/cards/CARD1001/fraud-case", {"transaction_ids": ["TXN9003"]})[0] == 409)
check("lost replacement refused before block",
      call("POST", "/cards/CARD1001/replacement",
           {"reason": "lost_or_stolen", "delivery_address": "x"})[0] == 409)
check("expired replacement allowed without block",
      call("POST", "/cards/CARD1001/replacement",
           {"reason": "expired", "delivery_address": "42 Brigade Road"})[0] == 200)

call("POST", "/cards/CARD1001/block", {"reason": "fraud"})
check("blocking twice is not an error",
      call("POST", "/cards/CARD1001/block", {"reason": "fraud"})[1]["already_blocked"])
check("fraud case allowed after block",
      call("POST", "/cards/CARD1001/fraud-case", {"transaction_ids": ["TXN9003"]})[0] == 200)
check("blocked card refuses limit change",
      call("PUT", "/cards/CARD1001/limits", {"atm": 1000})[0] == 409)
check("blocked card refuses PIN reset",
      call("POST", "/cards/CARD1001/pin-reset")[0] == 409)

# ------------------------------------------------------------------ pin
section("PIN reset lifecycle")
call("POST", "/testing/reset")
rid = call("POST", "/cards/CARD1001/pin-reset")[1]["request_id"]
check("starts pending", call("GET", f"/pin-reset/{rid}")[1]["status"] == "pending")
call("POST", f"/pin-reset/{rid}/simulate-completion")
check("completes", call("GET", f"/pin-reset/{rid}")[1]["status"] == "completed")

# -------------------------------------------------------------- testing
section("Testing endpoints")
check("sensitive data endpoint returns maskable values",
      "cvv" in call("GET", "/testing/card-full-details/CARD1001")[1])
call("POST", "/cards/CARD2001/block", {"reason": "lost"})
call("POST", "/testing/reset")
check("reset restores card state",
      call("GET", "/cards/CARD2001/status")[1]["status"] == "active")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)