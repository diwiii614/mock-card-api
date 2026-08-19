import json, urllib.request

B = "http://127.0.0.1:8000"

def call(method, path, body=None):
    req = urllib.request.Request(B+path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

print("--- SESSION START ---")
s, r = call("POST", "/auth/identify", {"identifier": "9876543210"})
print(s, r["message"], "| masked:", r["masked_mobile"])
ch = r["challenge_id"]

s, r = call("POST", "/auth/verify-otp", {"challenge_id": ch, "otp": "999999"})
print("wrong otp ->", r["verified"], r["message"])

s, r = call("POST", "/auth/identify", {"identifier": "9876543210"})
ch = r["challenge_id"]
s, r = call("POST", "/auth/verify-otp", {"challenge_id": ch, "otp": "123456"})
print("right otp ->", r["verified"], r["customer_name"])

s, r = call("GET", "/customers/CUST001/cards")
print("cards:", [(c["card_id"], c["masked_number"], c["status"]) for c in r])

print("\n--- READS ---")
print(call("GET", "/cards/CARD1001/balance")[1])
print(call("GET", "/cards/CARD1001/features")[1]["features"])

print("\n--- BATCHED CHANGES (one OTP, several toggles) ---")
s, r = call("PUT", "/cards/CARD1001/features",
            {"contactless": False, "international_usage": True})
print("updated:", r["updated"])
s, r = call("PUT", "/cards/CARD1001/limits", {"atm": 30000, "online": 60000})
print("updated:", r["updated"])

print("\n--- FRAUD FLOW ORDERING ---")
s, r = call("POST", "/cards/CARD1001/fraud-case", {"transaction_ids": ["TXN9003"]})
print("log before block ->", s, r["detail"])

s, r = call("GET", "/cards/CARD1001/status")
print("status:", r["status"])
s, r = call("POST", "/cards/CARD1001/block", {"reason": "fraud"})
print("block ->", r["message"])
s, r = call("POST", "/cards/CARD1001/block", {"reason": "fraud"})
print("block again ->", r["already_blocked"], r["message"])

s, r = call("POST", "/cards/CARD1001/fraud-case",
            {"transaction_ids": ["TXN9003"], "description": "Not my charge"})
print("log after block ->", s, r["case_reference"], "total:", r["disputed_total"])

print("\n--- BLOCKED CARD REJECTS CHANGES ---")
print("limits ->", call("PUT", "/cards/CARD1001/limits", {"atm": 5000})[1]["detail"])
print("pin    ->", call("POST", "/cards/CARD1001/pin-reset")[1]["detail"])

print("\n--- REPLACEMENT ORDERING (CARD2001, unblocked) ---")
s, r = call("POST", "/cards/CARD2001/replacement",
            {"reason": "lost_or_stolen", "delivery_address": "8 Anna Salai"})
print("lost replacement before block ->", s, r["detail"])
call("POST", "/cards/CARD2001/block", {"reason": "lost"})
s, r = call("POST", "/cards/CARD2001/replacement",
            {"reason": "lost_or_stolen", "delivery_address": "8 Anna Salai"})
print("after block ->", s, r["reference"], "fee:", r["fee"], "days:", r["estimated_delivery_days"])

print("\n--- PIN RESET LIFECYCLE (CARD1002) ---")
s, r = call("POST", "/cards/CARD1002/pin-reset")
rid = r["request_id"]
print("initiated:", rid, r["status"], "| link expires in", r["link_expires_in_minutes"], "min")
print("status:", call("GET", f"/pin-reset/{rid}")[1]["status"])
call("POST", f"/pin-reset/{rid}/simulate-completion")
print("after completion:", call("GET", f"/pin-reset/{rid}")[1]["status"])

print("\n--- UNKNOWN IDENTIFIER DOES NOT LEAK ---")
s, r = call("POST", "/auth/identify", {"identifier": "0000000000"})
print(s, r["otp_sent"], r["message"])
