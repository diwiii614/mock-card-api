"""Walks every operation the Co-Worker can call, in the order it would call them."""

import json
import urllib.request
import urllib.error

B = "http://127.0.0.1:8000"
SESSION = {"token": None}


def call(method, path, body=None, step_up=None, auth=True):
    req = urllib.request.Request(B + path, method=method)
    if auth and SESSION["token"]:
        req.add_header("x-session-token", SESSION["token"])
    if step_up:
        req.add_header("x-step-up-token", step_up)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def login(identifier):
    """send_otp with an identifier, then verify_otp -> session token."""
    _, r = call("POST", "/auth/otp/send", {"identifier": identifier}, auth=False)
    assert r["purpose"] == "login", r
    _, r = call("POST", "/auth/otp/verify", {"challenge_id": r["challenge_id"],
                                             "otp": "123456"}, auth=False)
    SESSION["token"] = r["session_token"]
    return r


def step_up():
    """send_otp with the session and no identifier, then verify_otp -> step up token."""
    _, r = call("POST", "/auth/otp/send", {})
    assert r["purpose"] == "step_up", r
    _, r = call("POST", "/auth/otp/verify", {"challenge_id": r["challenge_id"],
                                             "otp": "123456"}, auth=False)
    return r["step_up_token"]


call("POST", "/admin/reset", auth=False)

print("--- NOTHING WORKS WITHOUT A SESSION ---")
print(*call("GET", "/account"))

print("\n--- SEND AND VERIFY, ONE PAIR FOR BOTH PURPOSES ---")
s, r = call("POST", "/auth/otp/send", {"identifier": "9876543210"}, auth=False)
print("send    ->", r["purpose"], "|", r["message"])
s, w = call("POST", "/auth/otp/verify", {"challenge_id": r["challenge_id"], "otp": "999999"},
            auth=False)
print("wrong   ->", w["verified"], w["message"])
r = login("9876543210")
print("verify  ->", r["purpose"], r["customer_name"], "| token:", r["session_token"][:12] + "...")

print("\n--- ONE READ RETURNS EVERYTHING ---")
s, a = call("GET", "/account")
print("card    :", a["card"]["masked_number"], a["card"]["card_type"], a["card"]["status"])
print("usage   :", f"{a['usage']['used']:,.2f} of {a['usage']['monthly_limit']:,}",
      f"({a['usage']['percent_used']}%), available {a['usage']['available']:,.2f}")
print("bill    :", f"{a['bill']['statement_amount']:,.2f} due {a['bill']['due_date']}",
      f"({a['bill']['days_until_due']} days), minimum {a['bill']['minimum_due']:,.2f}")
print("emi     : eligible", a["emi"]["eligible"], "| fee", a["emi"]["processing_fee"], "|",
      ", ".join(f"{o['tenure_months']}mo at {o['interest_rate_annual_percent']}%"
                for o in a["emi"]["options"]))
print("limits  :", {k: v for k, v in a["limits"].items() if not k.endswith("_limit")})
print("features:", a["features"])
print("address :", a["customer"]["address"])
print("transactions:")
for t in a["transactions"]["items"]:
    print(f"  {t['date']}  {t['merchant']:<24} {t['amount']:>9,.2f}  {t['status']}")
print("pending only:", [t["transaction_id"] for t in
                        call("GET", "/account?transaction_status=pending")[1]["transactions"]["items"]])

print("\n--- ONE CALL, ONE OTP, LIMITS AND FEATURES TOGETHER ---")
print("no step up ->", *call("PATCH", "/card", {"limits": {"atm": 30000}}))
print("over 1 lakh ->", call("PATCH", "/card", {"limits": {"atm": 150000}},
                             step_up=step_up())[0], "rejected by range validation")
tok = step_up()
s, r = call("PATCH", "/card", {"limits": {"atm": 30000, "online": 60000},
                               "features": {"contactless": False,
                                            "international_usage": True}}, step_up=tok)
print("with step up ->", s, r["updated"])
print("token reuse  ->", call("PATCH", "/card", {"limits": {"atm": 1000}}, step_up=tok)[0])

print("\n--- EMI ---")
s, r = call("POST", "/card/emi", {"tenure_months": 6}, step_up=step_up())
print("convert ->", s, r["message"])
print("eligible now ->", call("GET", "/account")[1]["emi"]["eligible"])

print("\n--- PIN CHANGE LIFECYCLE ---")
s, r = call("POST", "/card/pin-reset")
rid, link = r["request_id"], r["secure_link"]
print("link   ->", link)
print("before ->", call("GET", f"/card/pin-reset/{rid}")[1])
with urllib.request.urlopen(link) as resp:
    print("customer opens it ->", resp.status)
print("after  ->", call("GET", f"/card/pin-reset/{rid}")[1])

print("\n--- TICKET AND REPLACEMENT ---")
s, r = call("POST", "/tickets", {"name": "Ananya Sharma",
                                 "address": "42 Brigade Road, Bengaluru, Karnataka 560001",
                                 "subject": "Do not recognise TXN9003"})
print("ticket      ->", s, r["ticket_number"], "|", r["status"])
s, r = call("POST", "/card/replacement", {"delivery_address": "42 Brigade Road, Bengaluru"})
print("replacement ->", s, r["reference"], "fee:", r["fee"], "days:", r["estimated_delivery_days"])

print("\n--- BLOCK, THEN A BLOCKED CARD REJECTS CHANGES ---")
s, r = call("POST", "/card/block", {"reason": "fraud"}, step_up=step_up())
print("block       ->", r["message"])
s, r = call("POST", "/card/block", {"reason": "fraud"}, step_up=step_up())
print("block again ->", r["already_blocked"], r["message"])
print("update      ->", call("PATCH", "/card", {"limits": {"atm": 5000}},
                             step_up=step_up())[1]["detail"])
print("pin reset   ->", call("POST", "/card/pin-reset")[1]["detail"])

print("\n--- THIRD CUSTOMER: BILL TOO SMALL FOR EMI ---")
r = login("9988776655")
a = call("GET", "/account")[1]
print(r["customer_name"], "| bill", a["bill"]["statement_amount"],
      "| emi eligible:", a["emi"]["eligible"])
print("convert ->", *call("POST", "/card/emi", {"tenure_months": 6}, step_up=step_up())[1:])

print("\n--- UNKNOWN IDENTIFIER DOES NOT LEAK ---")
s, r = call("POST", "/auth/otp/send", {"identifier": "0000000000"}, auth=False)
print(s, r["otp_sent"], r["masked_mobile"], r["message"])

print("\n--- RESET ---")
print(*call("POST", "/admin/reset", auth=False)[1:])
print("session cleared ->", call("GET", "/account")[0])
