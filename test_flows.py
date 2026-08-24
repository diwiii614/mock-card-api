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


def step_up_token():
    """Two calls, because every change needs a fresh single use token."""
    _, r = call("POST", "/auth/step-up")
    _, r = call("POST", "/auth/verify-step-up",
                {"challenge_id": r["challenge_id"], "otp": "123456"})
    return r["step_up_token"]


call("POST", "/admin/reset", auth=False)

print("--- NOTHING WORKS WITHOUT A SESSION ---")
s, r = call("GET", "/card/usage")
print(s, r["detail"] if "detail" in r else r)

print("\n--- SESSION START ---")
s, r = call("POST", "/auth/identify", {"identifier": "9876543210"})
print(s, r["message"], "| masked:", r["masked_mobile"])
ch = r["challenge_id"]

s, r = call("POST", "/auth/verify-otp", {"challenge_id": ch, "otp": "999999"})
print("wrong otp ->", r["verified"], r["message"])

s, r = call("POST", "/auth/identify", {"identifier": "9876543210"})
s, r = call("POST", "/auth/verify-otp", {"challenge_id": r["challenge_id"], "otp": "123456"})
SESSION["token"] = r["session_token"]
print("right otp ->", r["verified"], r["customer_name"], "| token:", r["session_token"][:12] + "...")

print("\n--- READS ---")
print("card    :", call("GET", "/card")[1])
print("usage   :", call("GET", "/card/usage")[1])
print("bill    :", call("GET", "/card/bill")[1])
print("limits  :", call("GET", "/card/limits")[1]["limits"])
print("features:", call("GET", "/card/features")[1]["features"])

print("\n--- TRANSACTIONS ---")
s, r = call("GET", "/card/transactions")
for t in r["transactions"]:
    print(f"  {t['date']}  {t['merchant']:<24} {t['amount']:>9,.2f}  {t['status']}")
print("pending only:", [t["transaction_id"]
                        for t in call("GET", "/card/transactions?status=pending")[1]["transactions"]])

print("\n--- CHANGES NEED STEP UP ---")
s, r = call("PUT", "/card/limits", {"atm": 30000})
print("no step up ->", s, r["detail"])
s, r = call("PUT", "/card/limits", {"atm": 150000}, step_up=step_up_token())
print("over 1 lakh ->", s, "rejected by range validation")
s, r = call("PUT", "/card/limits", {"atm": 30000, "online": 60000}, step_up=step_up_token())
print("with step up ->", s, r["updated"])
s, r = call("PUT", "/card/features",
            {"contactless": False, "international_usage": True}, step_up=step_up_token())
print("features ->", s, r["updated"])

tok = step_up_token()
call("PUT", "/card/features", {"contactless": True}, step_up=tok)
s, r = call("PUT", "/card/features", {"contactless": False}, step_up=tok)
print("token reuse ->", s, r["detail"])

print("\n--- TICKET ---")
s, r = call("POST", "/tickets", {"name": "Ananya Sharma",
                                 "address": "42 Brigade Road, Bengaluru, Karnataka 560001",
                                 "subject": "Do not recognise TXN9003"})
tkt = r["ticket_number"]
print(s, r["message"])
print("lookup ->", call("GET", f"/tickets/{tkt}")[1]["status"])

print("\n--- EMI ---")
s, r = call("GET", "/card/emi-options")
print(f"bill {r['bill_amount']:,.2f}, processing fee {r['processing_fee']}")
for o in r["options"]:
    print(f"  {o['tenure_months']:>2} months at {o['interest_rate_annual_percent']}%")
s, r = call("POST", "/card/emi", {"tenure_months": 6}, step_up=step_up_token())
print("convert ->", s, r["message"])
s, r = call("GET", "/card/emi-options")
print("options again ->", s, r["detail"])

print("\n--- PIN CHANGE LIFECYCLE ---")
s, r = call("POST", "/card/pin-reset")
rid, link = r["request_id"], r["secure_link"]
print("link ->", link)
s, r = call("GET", f"/card/pin-reset/{rid}")
print("before customer opens it -> pin_changed:", r["pin_changed"], "| status:", r["status"])
with urllib.request.urlopen(link) as resp:
    print("customer opens link ->", resp.status)
s, r = call("GET", f"/card/pin-reset/{rid}")
print("after                    -> pin_changed:", r["pin_changed"], "| status:", r["status"])
print("unknown request ->", call("GET", "/card/pin-reset/PINNOPE")[1]["detail"])

print("\n--- SERVICING ---")
s, r = call("POST", "/card/replacement", {"delivery_address": "42 Brigade Road, Bengaluru"})
print("replacement ->", s, r["reference"], "fee:", r["fee"], "days:", r["estimated_delivery_days"])

print("\n--- BLOCK, THEN BLOCKED CARD REJECTS CHANGES ---")
s, r = call("POST", "/card/block", {"reason": "fraud"}, step_up=step_up_token())
print("block ->", r["message"])
s, r = call("POST", "/card/block", {"reason": "fraud"}, step_up=step_up_token())
print("block again ->", r["already_blocked"], r["message"])
print("limits ->", call("PUT", "/card/limits", {"atm": 5000}, step_up=step_up_token())[1]["detail"])
print("pin    ->", call("POST", "/card/pin-reset")[1]["detail"])

print("\n--- THIRD CUSTOMER: BILL TOO SMALL FOR EMI ---")
s, r = call("POST", "/auth/identify", {"identifier": "9988776655"}, auth=False)
s, r = call("POST", "/auth/verify-otp", {"challenge_id": r["challenge_id"], "otp": "123456"},
            auth=False)
SESSION["token"] = r["session_token"]
print("session ->", r["customer_name"], "|", call("GET", "/card")[1]["card_id"])
print("usage   ->", call("GET", "/card/usage")[1]["used"])
print("emi     ->", *call("GET", "/card/emi-options")[1:])

print("\n--- UNKNOWN IDENTIFIER DOES NOT LEAK ---")
s, r = call("POST", "/auth/identify", {"identifier": "0000000000"}, auth=False)
print(s, r["otp_sent"], r["message"])

print("\n--- RESET ---")
s, r = call("POST", "/admin/reset", auth=False)
print(s, r["message"])
s, r = call("GET", "/card")
print("session cleared by reset ->", s, r["detail"])
