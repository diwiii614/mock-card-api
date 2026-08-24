""""
Mock Card Management API
------------------------
Stands in for a real card management system so the Card Operations Coworker
can be built and tested before any live backend exists.

State is held in memory, so blocking a card really does change its status
within a session. Restarting the server resets everything.

Operation descriptions are written for the coworker to read when choosing
a tool, so they say WHEN to use each endpoint, not just what it does.

Run:  uvicorn main:app --reload --port 8000
Spec: http://localhost:8000/openapi.json
Docs: http://localhost:8000/docs

"""

import copy
import os
import random
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Optional API key. Set API_KEY as an environment variable to require an
# x-api-key header on every request. Leave it unset and the API is open,
# which is fine locally but not for a public deployment.
API_KEY = os.getenv("API_KEY")

app = FastAPI(
    title="Mock Card Management API",
    version="1.0.0",
    description=(
        "Card servicing operations for the Card Operations Coworker. "
        "Covers customer identification, OTP verification, card listing, "
        "balance and transactions, limits, feature toggles, blocking, "
        "replacement, PIN reset initiation, and fraud case logging."
    ),
)


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Enforce the API key when one is configured. Docs and spec stay open
    so the OpenAPI definition can still be copied into Neo."""
    open_paths = ("/docs", "/redoc", "/openapi.json", "/health")
    if API_KEY and not request.url.path.startswith(open_paths):
        if request.headers.get("x-api-key") != API_KEY:
            return JSONResponse(status_code=401,
                                content={"detail": "Missing or invalid x-api-key header"})
    return await call_next(request)


# ---------------------------------------------------------------- enums


class CardStatus(str, Enum):
    active = "active"
    blocked = "blocked"
    expired = "expired"


class BlockReason(str, Enum):
    lost = "lost"
    stolen = "stolen"
    fraud = "fraud"
    damaged = "damaged"


class ReplacementReason(str, Enum):
    lost_or_stolen = "lost_or_stolen"
    expired = "expired"
    damaged = "damaged"


class PinResetStatus(str, Enum):
    pending = "pending"
    completed = "completed"
    expired = "expired"


# ------------------------------------------------------------- storage
# Two fake customers. Card numbers are stored masked only: the full PAN
# does not exist anywhere in this service, so it can never be returned.

DB: Dict[str, dict] = {
    "CUST001": {
        "customer_id": "CUST001",
        "name": "Ananya Sharma",
        "mobile": "9876543210",
        "address": "42 Brigade Road, Bengaluru, Karnataka 560001",
        "cards": {
            "CARD1001": {
                "card_id": "CARD1001",
                "masked_number": "**** **** **** 4521",
                "last_four": "4521",
                "card_type": "Visa Debit",
                "status": CardStatus.active,
                "expiry": "2029-04-30",
                "balance": 24500.75,
                "card_limit": 150000,
                "limits": {"atm": 25000, "pos": 100000, "online": 50000,
                           "international": 0},
                "features": {"contactless": True, "online_payments": True,
                             "international_usage": False, "atm_withdrawals": True},
                "holds": [],
            },
            "CARD1002": {
                "card_id": "CARD1002",
                "masked_number": "**** **** **** 8834",
                "last_four": "8834",
                "card_type": "Mastercard Credit",
                "status": CardStatus.active,
                "expiry": "2026-10-31",
                "balance": 8200.00,
                "card_limit": 250000,
                "limits": {"atm": 15000, "pos": 200000, "online": 150000,
                           "international": 100000},
                "features": {"contactless": True, "online_payments": True,
                             "international_usage": True, "atm_withdrawals": True},
                "holds": [],
            },
        },
    },
    "CUST002": {
        "customer_id": "CUST002",
        "name": "Rohit Menon",
        "mobile": "9123456780",
        "address": "8 Anna Salai, Chennai, Tamil Nadu 600002",
        "cards": {
            "CARD2001": {
                "card_id": "CARD2001",
                "masked_number": "**** **** **** 7710",
                "last_four": "7710",
                "card_type": "Visa Credit",
                "status": CardStatus.active,
                "expiry": "2027-06-30",
                "balance": 15750.40,
                "card_limit": 200000,
                "limits": {"atm": 20000, "pos": 150000, "online": 75000,
                           "international": 50000},
                "features": {"contactless": False, "online_payments": True,
                             "international_usage": False, "atm_withdrawals": True},
                "holds": [],
            },
        },
    },
}

_INITIAL_DB = copy.deepcopy(DB)

# Transaction history. The five named transactions below are kept so earlier
# demos still work, in particular TXN9003 for the fraud walkthrough. Several
# months of additional history is generated underneath them with a fixed seed,
# so the data is the same on every restart.

_SEED_TRANSACTIONS: Dict[str, List[dict]] = {
    "CARD1001": [
        {"transaction_id": "TXN9001", "date": "2026-08-16", "merchant": "Blue Tokai Coffee",
         "category": "Food and Beverage", "amount": 480.00, "status": "settled",
         "recurring": False},
        {"transaction_id": "TXN9002", "date": "2026-08-15", "merchant": "Netflix",
         "category": "Subscription", "amount": 649.00, "status": "settled",
         "recurring": True},
        {"transaction_id": "TXN9003", "date": "2026-08-15", "merchant": "UNKNOWN MERCHANT 44821",
         "category": "Unclassified", "amount": 18999.00, "status": "pending",
         "recurring": False},
        {"transaction_id": "TXN9004", "date": "2026-08-14", "merchant": "More Supermarket",
         "category": "Groceries", "amount": 2340.50, "status": "settled",
         "recurring": False},
        {"transaction_id": "TXN9005", "date": "2026-08-12", "merchant": "Indian Oil",
         "category": "Fuel", "amount": 3000.00, "status": "settled", "recurring": False},
        {"transaction_id": "TXN9006", "date": "2026-08-17", "merchant": "Zomato",
         "category": "Food and Beverage", "amount": 742.00, "status": "settled",
         "recurring": False},
        {"transaction_id": "TXN9007", "date": "2026-08-17", "merchant": "Zomato",
         "category": "Food and Beverage", "amount": 742.00, "status": "settled",
         "recurring": False},
    ],
    "CARD1002": [
        {"transaction_id": "TXN9101", "date": "2026-08-16", "merchant": "Amazon India",
         "category": "Retail", "amount": 5499.00, "status": "settled", "recurring": False},
        {"transaction_id": "TXN9102", "date": "2026-08-13", "merchant": "Spotify",
         "category": "Subscription", "amount": 119.00, "status": "settled", "recurring": True},
    ],
    "CARD2001": [
        {"transaction_id": "TXN9201", "date": "2026-08-16", "merchant": "Swiggy",
         "category": "Food and Beverage", "amount": 720.00, "status": "settled",
         "recurring": False},
    ],
}

_MERCHANTS = [
    ("Swiggy", "Food and Beverage", 250, 900),
    ("Zomato", "Food and Beverage", 200, 850),
    ("Blue Tokai Coffee", "Food and Beverage", 300, 600),
    ("More Supermarket", "Groceries", 800, 3500),
    ("BigBasket", "Groceries", 1200, 4200),
    ("Indian Oil", "Fuel", 1500, 3500),
    ("Shell", "Fuel", 1000, 3000),
    ("Amazon India", "Retail", 500, 8000),
    ("Myntra", "Retail", 900, 5000),
    ("Croma", "Retail", 2000, 25000),
    ("Uber", "Transport", 120, 700),
    ("BMTC Metro", "Transport", 40, 200),
    ("Apollo Pharmacy", "Health", 200, 1800),
    ("PVR Cinemas", "Entertainment", 400, 1400),
    ("BESCOM Electricity", "Utilities", 900, 2600),
    ("Airtel Broadband", "Utilities", 1099, 1099),
]

# Charges that repeat every month on the same day.
_RECURRING = [
    ("Netflix", "Subscription", 649.00, 15),
    ("Spotify", "Subscription", 119.00, 13),
    ("Airtel Postpaid", "Subscription", 799.00, 5),
    ("Adobe Creative Cloud", "Subscription", 1675.00, 22),
]

# Declines are deliberate rather than random, so every reason is consistent with
# the limits and features actually set on that card. That makes the "why did my
# payment fail" journey coherent: the coworker can look up the reason, check the
# matching setting, explain it, and offer the fix.
#
# CARD1001  atm 25,000  pos 100,000  online 50,000  international 0
#           contactless on, online on, international OFF, atm on, balance 24,500
# CARD1002  atm 15,000  pos 200,000  online 150,000  international 100,000
#           all features on
# CARD2001  atm 20,000  pos 150,000  online 75,000  international 50,000
#           contactless OFF, international OFF

_DECLINED: Dict[str, List[dict]] = {
    "CARD1001": [
        {"transaction_id": "TXN5901", "date": "2026-08-18", "merchant": "Booking.com Amsterdam",
         "category": "Travel", "amount": 34500.00,
         "decline_reason": "international_usage_disabled",
         "decline_detail": "International usage is switched off for this card",
         "suggested_fix": "Turn on international usage, then try again"},
        {"transaction_id": "TXN5902", "date": "2026-08-14", "merchant": "Croma",
         "category": "Retail", "amount": 62400.00,
         "decline_reason": "exceeds_online_limit",
         "decline_detail": "Amount was above the 50,000 online limit on this card",
         "suggested_fix": "Raise the online limit above the purchase amount"},
        {"transaction_id": "TXN5903", "date": "2026-08-08", "merchant": "HDFC ATM Koramangala",
         "category": "Cash Withdrawal", "amount": 30000.00,
         "decline_reason": "exceeds_atm_limit",
         "decline_detail": "Amount was above the 25,000 daily ATM limit on this card",
         "suggested_fix": "Raise the ATM limit, or withdraw in two goes"},
        {"transaction_id": "TXN5904", "date": "2026-08-03", "merchant": "Reliance Digital",
         "category": "Retail", "amount": 41200.00,
         "decline_reason": "insufficient_balance",
         "decline_detail": "Available balance was lower than the transaction amount",
         "suggested_fix": "No card change needed. Top up the account and try again"},
        {"transaction_id": "TXN5905", "date": "2026-07-22", "merchant": "AliExpress",
         "category": "Retail", "amount": 4820.00,
         "decline_reason": "international_usage_disabled",
         "decline_detail": "International usage is switched off for this card",
         "suggested_fix": "Turn on international usage, then try again"},
        {"transaction_id": "TXN5906", "date": "2026-07-09", "merchant": "Apple Store Online",
         "category": "Retail", "amount": 89900.00,
         "decline_reason": "exceeds_online_limit",
         "decline_detail": "Amount was above the 50,000 online limit on this card",
         "suggested_fix": "Raise the online limit above the purchase amount"},
        {"transaction_id": "TXN5907", "date": "2026-06-26", "merchant": "Steam Games",
         "category": "Entertainment", "amount": 2199.00,
         "decline_reason": "international_usage_disabled",
         "decline_detail": "International usage is switched off for this card",
         "suggested_fix": "Turn on international usage, then try again"},
        {"transaction_id": "TXN5908", "date": "2026-06-12", "merchant": "SBI ATM MG Road",
         "category": "Cash Withdrawal", "amount": 26000.00,
         "decline_reason": "exceeds_atm_limit",
         "decline_detail": "Amount was above the 25,000 daily ATM limit on this card",
         "suggested_fix": "Raise the ATM limit, or withdraw in two goes"},
    ],
    "CARD1002": [
        {"transaction_id": "TXN6901", "date": "2026-08-17", "merchant": "Apple Store Online",
         "category": "Retail", "amount": 184000.00,
         "decline_reason": "exceeds_online_limit",
         "decline_detail": "Amount was above the 150,000 online limit on this card",
         "suggested_fix": "Raise the online limit above the purchase amount"},
        {"transaction_id": "TXN6902", "date": "2026-07-19", "merchant": "Emirates Airlines",
         "category": "Travel", "amount": 128500.00,
         "decline_reason": "exceeds_international_limit",
         "decline_detail": "Amount was above the 100,000 international limit on this card",
         "suggested_fix": "Raise the international limit above the purchase amount"},
        {"transaction_id": "TXN6903", "date": "2026-06-30", "merchant": "Tanishq Jewellers",
         "category": "Retail", "amount": 215000.00,
         "decline_reason": "exceeds_pos_limit",
         "decline_detail": "Amount was above the 200,000 in store limit on this card",
         "suggested_fix": "Raise the POS limit above the purchase amount"},
    ],
    "CARD2001": [
        {"transaction_id": "TXN7901", "date": "2026-08-15", "merchant": "Cafe Coffee Day",
         "category": "Food and Beverage", "amount": 420.00,
         "decline_reason": "contactless_disabled",
         "decline_detail": "Contactless payments are switched off for this card",
         "suggested_fix": "Turn on contactless, or insert the card and enter the PIN"},
        {"transaction_id": "TXN7902", "date": "2026-08-02", "merchant": "Spotify USA",
         "category": "Subscription", "amount": 1199.00,
         "decline_reason": "international_usage_disabled",
         "decline_detail": "International usage is switched off for this card",
         "suggested_fix": "Turn on international usage, then try again"},
        {"transaction_id": "TXN7903", "date": "2026-07-05", "merchant": "Metro Station Gate 3",
         "category": "Transport", "amount": 60.00,
         "decline_reason": "contactless_disabled",
         "decline_detail": "Contactless payments are switched off for this card",
         "suggested_fix": "Turn on contactless, or insert the card and enter the PIN"},
    ],
}

# every declined entry shares these fields
for _card_txns in _DECLINED.values():
    for _t in _card_txns:
        _t["status"] = "declined"
        _t["recurring"] = False


def _build_history() -> Dict[str, List[dict]]:
    """Generate several months of history so monthly totals are meaningful.
    Seeded, so restarts produce identical data."""
    rng = random.Random(20260819)
    history = {k: list(v) for k, v in _SEED_TRANSACTIONS.items()}
    counter = {"CARD1001": 5000, "CARD1002": 6000, "CARD2001": 7000}
    volume = {"CARD1001": 14, "CARD1002": 9, "CARD2001": 7}

    for card_id in ("CARD1001", "CARD1002", "CARD2001"):
        for year, month in ((2026, 6), (2026, 7), (2026, 8)):
            last_day = 19 if month == 8 else 30
            # ordinary spending
            for _ in range(volume[card_id]):
                name, cat, lo, hi = rng.choice(_MERCHANTS)
                counter[card_id] += 1
                day = rng.randint(1, last_day)
                txn = {
                    "transaction_id": f"TXN{counter[card_id]}",
                    "date": f"{year}-{month:02d}-{day:02d}",
                    "merchant": name,
                    "category": cat,
                    "amount": round(rng.uniform(lo, hi), 2),
                    "status": "settled",
                    "recurring": False,
                }
                history[card_id].append(txn)
            # recurring charges
            for name, cat, amount, day in _RECURRING:
                if day > last_day:
                    continue
                if card_id == "CARD2001" and name != "Netflix":
                    continue
                # Netflix and Spotify already seeded for August on their cards
                if year == 2026 and month == 8 and (
                        (card_id == "CARD1001" and name == "Netflix")
                        or (card_id == "CARD1002" and name == "Spotify")):
                    continue
                if card_id == "CARD1002" and name in ("Netflix", "Airtel Postpaid"):
                    continue
                if card_id == "CARD1001" and name == "Spotify":
                    continue
                counter[card_id] += 1
                history[card_id].append({
                    "transaction_id": f"TXN{counter[card_id]}",
                    "date": f"{year}-{month:02d}-{day:02d}",
                    "merchant": name,
                    "category": cat,
                    "amount": amount,
                    "status": "settled",
                    "recurring": True,
                })
        history[card_id].extend(_DECLINED.get(card_id, []))
        history[card_id].sort(key=lambda t: (t["date"], t["transaction_id"]), reverse=True)
    return history


TRANSACTIONS: Dict[str, List[dict]] = _build_history()

OTP_STORE: Dict[str, dict] = {}
PIN_RESETS: Dict[str, dict] = {}
FRAUD_CASES: Dict[str, dict] = {}
REPLACEMENTS: Dict[str, dict] = {}

_ID_NOTE = (" Accepts the card_id from get_cards, for example CARD1001, or the last "
            "four digits of the card number.")

ABSOLUTE_MAX_LIMIT = 1_000_000  # no limit on any card may ever exceed this

FIXED_OTP = "123456"  # mock only: any real system would generate this


# ------------------------------------------------------------- helpers


def _find_customer_by_mobile(mobile: str) -> Optional[dict]:
    for cust in DB.values():
        if cust["mobile"] == mobile:
            return cust
    return None


def _find_card(card_id: str) -> dict:
    """Look up a card by its card ID, or by the last four digits, since the
    coworker often has only the masked number it showed the customer."""
    for cust in DB.values():
        if card_id in cust["cards"]:
            return cust["cards"][card_id]
    # fall back to last four digits, e.g. "4521" or "**** **** **** 4521"
    digits = "".join(ch for ch in card_id if ch.isdigit())
    if len(digits) >= 4:
        last_four = digits[-4:]
        matches = [c for cust in DB.values() for c in cust["cards"].values()
                   if c["last_four"] == last_four]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise HTTPException(
                status_code=409,
                detail=f"More than one card ends {last_four}. Use the full card ID.")
    raise HTTPException(
        status_code=404,
        detail=f"Card {card_id} not found. Use the card_id from get_cards, e.g. CARD1001.")


def _expiry_info(card: dict) -> dict:
    """Work out how close a card is to expiry here, rather than leaving date
    arithmetic to the coworker."""
    expiry = datetime.strptime(card["expiry"], "%Y-%m-%d")
    days = (expiry - datetime.utcnow()).days
    return {
        "expiry": card["expiry"],
        "days_until_expiry": days,
        "expired": days < 0,
        "expiring_soon": 0 <= days <= 90,
    }


def _resolve_card_id(card_id: str) -> str:
    """Return the canonical card ID for whatever identifier was supplied."""
    return _find_card(card_id)["card_id"]


def _customer_for_card(card_id: str) -> dict:
    resolved = _resolve_card_id(card_id)
    for cust in DB.values():
        if resolved in cust["cards"]:
            return cust
    raise HTTPException(status_code=404, detail=f"Card {card_id} not found")


# -------------------------------------------------------------- models


class IdentifyRequest(BaseModel):
    identifier: str = Field(..., description="Registered mobile number or customer ID",
                            examples=["9876543210"])


class IdentifyResponse(BaseModel):
    otp_sent: bool
    masked_mobile: str = Field(..., description="Masked registered number, for the customer to confirm")
    message: str


class VerifyOtpRequest(BaseModel):
    identifier: str = Field(..., description="The registered mobile number or customer ID the OTP was sent to",
                            examples=["9876543210"])
    otp: str = Field(..., description="The code the customer received", examples=["123456"])


class VerifyOtpResponse(BaseModel):
    verified: bool
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    message: str


class CardSummary(BaseModel):
    card_id: str
    masked_number: str
    card_type: str
    status: CardStatus
    expiry: str
    days_until_expiry: int
    expiring_soon: bool


class BalanceResponse(BaseModel):
    card_id: str
    balance: float
    currency: str = "INR"
    as_of: str


class LimitsPayload(BaseModel):
    atm: Optional[int] = Field(None, ge=0, le=ABSOLUTE_MAX_LIMIT,
                               description="Daily cash withdrawal limit, in INR")
    pos: Optional[int] = Field(None, ge=0, le=ABSOLUTE_MAX_LIMIT,
                               description="In store payment limit, in INR")
    online: Optional[int] = Field(None, ge=0, le=ABSOLUTE_MAX_LIMIT,
                                  description="Online payment limit, in INR")
    international: Optional[int] = Field(None, ge=0, le=ABSOLUTE_MAX_LIMIT,
                                         description="International payment limit, in INR")


class FeaturesPayload(BaseModel):
    contactless: Optional[bool] = None
    online_payments: Optional[bool] = None
    international_usage: Optional[bool] = None
    atm_withdrawals: Optional[bool] = None


class BlockRequest(BaseModel):
    reason: BlockReason


class ReplacementRequestBody(BaseModel):
    reason: ReplacementReason
    delivery_address: str


class FraudCaseRequest(BaseModel):
    transaction_ids: List[str] = Field(..., description="Transactions the customer disputes")
    description: Optional[str] = None


# ------------------------------------------------- identity and session


@app.post("/auth/identify", response_model=IdentifyResponse, operation_id="identify_customer",
          tags=["Session"], summary="Look up a customer and send an OTP",
          description=(
              "Sends an OTP to the customer's registered number and returns it masked so "
              "they can confirm it is theirs. Use at the start of a conversation, and "
              "again whenever a step up OTP is needed before a change. Accepts a mobile "
              "number or a customer ID. Returns no account data, so nothing is exposed "
              "before verification."))
def identify_customer(body: IdentifyRequest):
    cust = DB.get(body.identifier) or _find_customer_by_mobile(body.identifier)
    if not cust:
        try:
            cust = _customer_for_card(body.identifier)
        except HTTPException:
            cust = None

    # Always report success, so an unknown identifier cannot be used to
    # discover which numbers are registered.
    if cust:
        OTP_STORE[cust["customer_id"]] = {
            "otp": FIXED_OTP,
            "expires_at": datetime.utcnow() + timedelta(minutes=5),
        }
        masked = f"******{cust['mobile'][-4:]}"
    else:
        masked = "******0000"

    return IdentifyResponse(
        otp_sent=True, masked_mobile=masked,
        message=f"An OTP has been sent to the number ending {masked[-4:]}.")


@app.post("/auth/verify-otp", response_model=VerifyOtpResponse, operation_id="verify_otp",
          tags=["Session"], summary="Verify an OTP",
          description=(
              "Use after the customer supplies an OTP, both when opening the session and "
              "for a step up OTP before a change. Pass the same identifier you gave to "
              "identify_customer, plus the code. identify_customer must have been called "
              "first, since that is what sends the code. Returns only verified true or "
              "false. The coworker never validates the OTP itself."))
def verify_otp(body: VerifyOtpRequest):
    cust = DB.get(body.identifier) or _find_customer_by_mobile(body.identifier)
    if not cust:
        try:
            cust = _customer_for_card(body.identifier)
        except HTTPException:
            cust = None
    if not cust:
        return VerifyOtpResponse(
            verified=False,
            message="No customer found for that identifier. Check the mobile number.")

    entry = OTP_STORE.get(cust["customer_id"])
    if not entry:
        return VerifyOtpResponse(
            verified=False,
            message=("No OTP is outstanding for this customer. Call identify_customer "
                     "to send one before asking for a code."))

    if datetime.utcnow() > entry["expires_at"]:
        del OTP_STORE[cust["customer_id"]]
        return VerifyOtpResponse(
            verified=False,
            message="That code has expired. Call identify_customer to send a new one.")

    if body.otp != entry["otp"]:
        return VerifyOtpResponse(verified=False, message="That code is not correct.")

    del OTP_STORE[cust["customer_id"]]  # single use
    return VerifyOtpResponse(
        verified=True, customer_id=cust["customer_id"], customer_name=cust["name"],
        message=(f"Verification successful. Use customer_id {cust['customer_id']} "
                 f"for all further calls, not the mobile number."))


@app.get("/customers/{customer_id}/cards", response_model=List[CardSummary],
         operation_id="get_cards", tags=["Session"],
         summary="List the customer's cards",
         description=(
             "Use after the session is verified, so the customer can choose which card "
             "they want to act on. Pass the customer_id returned by verify_otp, for "
             "example CUST001. The registered mobile number is also accepted. Returns "
             "only the last four digits, never a full card number."))
def get_cards(customer_id: str):
    cust = DB.get(customer_id) or _find_customer_by_mobile(customer_id)
    if not cust:
        try:
            cust = _customer_for_card(customer_id)
        except HTTPException:
            cust = None
    if not cust:
        raise HTTPException(
            status_code=404,
            detail="No customer found. Pass a customer_id, mobile number, or card_id.")
    out = []
    for c in cust["cards"].values():
        info = _expiry_info(c)
        out.append(CardSummary(
            **{k: c[k] for k in ("card_id", "masked_number", "card_type", "status")},
            expiry=info["expiry"], days_until_expiry=info["days_until_expiry"],
            expiring_soon=info["expiring_soon"]))
    return out


# -------------------------------------------------------------- reads


@app.get("/cards/{card_id}/balance", response_model=BalanceResponse,
         operation_id="get_balance", tags=["Reads"], summary="Get the current balance",
         description=(
             "Use whenever the customer asks about their balance or available funds. "
             "Read only, covered by session authentication, no OTP needed. Always call "
             "this fresh rather than repeating a figure from earlier in the conversation." + _ID_NOTE))
def get_balance(card_id: str):
    card = _find_card(card_id)
    return BalanceResponse(card_id=card["card_id"], balance=card["balance"],
                           as_of=datetime.utcnow().isoformat() + "Z")


@app.get("/cards/{card_id}/transactions", operation_id="get_transactions", tags=["Reads"],
         summary="List transactions, filtered and paginated",
         description=(
             "Use when the customer asks to see recent activity, asks about a specific "
             "charge, or needs to identify a disputed transaction during a fraud report. "
             "Returns at most 25 at a time, newest first, so never use this to add up a "
             "month of spending. For totals and category breakdowns call get_spend_summary "
             "instead. Optional filters: from_date and to_date as YYYY-MM-DD, category, "
             "merchant as a partial name, status of settled, pending or declined, and "
             "min_amount. Use offset to page through more. Read only, no OTP needed."))
def get_transactions(card_id: str, from_date: Optional[str] = None,
                     to_date: Optional[str] = None, category: Optional[str] = None,
                     merchant: Optional[str] = None, status: Optional[str] = None,
                     min_amount: Optional[float] = None,
                     limit: int = 10, offset: int = 0):
    card = _find_card(card_id)
    cid = card["card_id"]
    rows = TRANSACTIONS.get(cid, [])

    if from_date:
        rows = [t for t in rows if t["date"] >= from_date]
    if to_date:
        rows = [t for t in rows if t["date"] <= to_date]
    if category:
        rows = [t for t in rows if t["category"].lower() == category.lower()]
    if merchant:
        rows = [t for t in rows if merchant.lower() in t["merchant"].lower()]
    if status:
        rows = [t for t in rows if t["status"].lower() == status.lower()]
    if min_amount is not None:
        rows = [t for t in rows if t["amount"] >= min_amount]

    total = len(rows)
    limit = max(1, min(limit, 25))
    page = rows[offset:offset + limit]
    return {
        "card_id": cid,
        "total_matching": total,
        "returned": len(page),
        "offset": offset,
        "has_more": offset + len(page) < total,
        "transactions": page,
    }


@app.get("/cards/{card_id}/declined", operation_id="get_declined_transactions", tags=["Reads"],
         summary="List failed payments and why each one was declined",
         description=(
             "Use whenever the customer asks why a payment failed, why their card was "
             "declined or refused, or why something did not go through. Returns each "
             "failed attempt with the merchant, amount, the reason it was declined, and "
             "a suggested fix. After calling this, check the matching setting with "
             "get_limits or get_card_features so the reason can be confirmed before it "
             "is explained to the customer. Optionally filter by merchant to find a "
             "specific attempt. Read only, no OTP needed."))
def get_declined_transactions(card_id: str, merchant: Optional[str] = None, limit: int = 10):
    card = _find_card(card_id)
    cid = card["card_id"]
    rows = [t for t in TRANSACTIONS.get(cid, []) if t["status"] == "declined"]
    if merchant:
        rows = [t for t in rows if merchant.lower() in t["merchant"].lower()]
    return {"card_id": cid, "declined_count": len(rows),
            "transactions": rows[:max(1, min(limit, 25))]}


@app.get("/cards/{card_id}/status", operation_id="get_card_status", tags=["Reads"],
         summary="Check whether a card is active or blocked",
         description=(
             "Use before blocking a card, to avoid blocking one that is already blocked, "
             "and whenever the customer asks whether their card is usable. Read only." + _ID_NOTE))
def get_card_status(card_id: str):
    card = _find_card(card_id)
    return {"card_id": card["card_id"], "status": card["status"],
            "masked_number": card["masked_number"], "card_type": card["card_type"],
            **_expiry_info(card)}


@app.get("/cards/{card_id}/limits", operation_id="get_limits", tags=["Reads"],
         summary="Get current spending limits and the ceiling they sit under",
         description=(
             "Use when the customer asks what their limits are, and always before "
             "changing them, so the current values and the maximum allowed can be shown "
             "together. Every card has an overall card_limit, and no individual limit "
             "may be set above it. Tell the customer that ceiling before they choose a "
             "new value. Read only." + _ID_NOTE))
def get_limits(card_id: str):
    card = _find_card(card_id)
    return {"card_id": card["card_id"], "limits": card["limits"],
            "card_limit": card["card_limit"],
            "allowed_range": {"minimum": 0, "maximum": card["card_limit"]},
            "currency": "INR",
            "note": ("Each limit can be set anywhere from 0 up to the card_limit. "
                     "Raising the card_limit itself is not something the coworker can do.")}


@app.get("/cards/{card_id}/features", operation_id="get_card_features", tags=["Reads"],
         summary="Get current on/off state of card features",
         description=(
             "Use when the customer asks about card features, or before changing them. "
             "Return every feature and its current state together so the customer can "
             "select several changes at once. Read only." + _ID_NOTE))
def get_card_features(card_id: str):
    card = _find_card(card_id)
    return {"card_id": card["card_id"], "features": card["features"]}


# ------------------------------------------------------------- writes


@app.put("/cards/{card_id}/limits", operation_id="update_limits", tags=["Changes"],
         summary="Change spending limits",
         description=(
             "Use to change one or more spending limits after the customer has confirmed "
             "the new values and completed a step up OTP. Several limits can be sent in "
             "one call, so a single OTP covers the whole set. Only the fields supplied "
             "are changed. Each value must be between 0 and the card_limit returned by "
             "get_limits. Anything above that is rejected, and the coworker cannot raise "
             "the card_limit itself."))
def update_limits(card_id: str, body: LimitsPayload):
    card = _find_card(card_id)
    if card["status"] == CardStatus.blocked:
        raise HTTPException(status_code=409, detail="Cannot change limits on a blocked card")

    requested = body.model_dump(exclude_none=True)
    if not requested:
        raise HTTPException(status_code=400, detail="No limit values supplied")

    ceiling = card["card_limit"]
    too_high = {f: v for f, v in requested.items() if v > ceiling}
    if too_high:
        breaches = ", ".join(f"{f} of {v:,}" for f, v in too_high.items())
        raise HTTPException(
            status_code=422,
            detail=(f"Rejected: {breaches}. No limit on this card may exceed the card "
                    f"limit of {ceiling:,}. Offer the customer a value up to {ceiling:,}, "
                    f"and tell them raising the card limit itself has to go through "
                    f"the card helpline."))

    changed = {}
    for field, value in requested.items():
        card["limits"][field] = value
        changed[field] = value
    return {"card_id": card["card_id"], "updated": changed, "limits": card["limits"],
            "card_limit": ceiling}


@app.put("/cards/{card_id}/features", operation_id="update_card_features", tags=["Changes"],
         summary="Turn card features on or off",
         description=(
             "Use to switch features such as contactless, online payments, international "
             "usage, or ATM withdrawals after a step up OTP. Several toggles can be sent "
             "in one call, so one OTP covers the whole set. Only the fields supplied are "
             "changed."))
def update_card_features(card_id: str, body: FeaturesPayload):
    card = _find_card(card_id)
    if card["status"] == CardStatus.blocked:
        raise HTTPException(status_code=409, detail="Cannot change features on a blocked card")
    changed = {}
    for field, value in body.model_dump(exclude_none=True).items():
        card["features"][field] = value
        changed[field] = value
    if not changed:
        raise HTTPException(status_code=400, detail="No feature values supplied")
    return {"card_id": card["card_id"], "updated": changed, "features": card["features"]}


@app.post("/cards/{card_id}/block", operation_id="block_card", tags=["Protective"],
          summary="Block a card permanently",
          description=(
              "Use immediately when the customer reports the card lost, stolen, or used "
              "fraudulently. Runs on session authentication so it is never delayed. This "
              "cannot be reversed by the coworker: a blocked card can only be replaced, "
              "not reactivated. Call get_card_status first to avoid blocking twice." + _ID_NOTE))
def block_card(card_id: str, body: BlockRequest):
    card = _find_card(card_id)
    if card["status"] == CardStatus.blocked:
        return {"card_id": card["card_id"], "status": card["status"], "already_blocked": True,
                "message": "This card was already blocked."}
    card["status"] = CardStatus.blocked
    card["block_reason"] = body.reason
    return {"card_id": card["card_id"], "status": card["status"], "already_blocked": False,
            "reason": body.reason,
            "message": f"Card ending {card['last_four']} has been blocked."}


# -------------------------------------------------------- replacement


@app.get("/customers/{customer_id}/address", operation_id="get_customer_address",
         tags=["Replacement"], summary="Get the delivery address on record",
         description=(
             "Use during a replacement request, so the address on file can be confirmed "
             "with the customer before a card is despatched. Accepts a customer_id, a "
             "registered mobile number, or the card_id of any card on the account, so "
             "the selected card is enough on its own. Read only."))
def get_customer_address(customer_id: str):
    cust = DB.get(customer_id) or _find_customer_by_mobile(customer_id)
    if not cust:
        # fall back to resolving via a card identifier, e.g. CARD1001 or 4521
        try:
            cust = _customer_for_card(customer_id)
        except HTTPException:
            cust = None
    if not cust:
        raise HTTPException(
            status_code=404,
            detail="No customer found. Pass a customer_id, mobile number, or card_id.")
    return {"customer_id": cust["customer_id"], "address": cust["address"]}


@app.get("/cards/{card_id}/holds", operation_id="check_card_holds", tags=["Replacement"],
         summary="Check for holds that would block a reissue",
         description=(
             "Use before submitting a replacement request, to confirm there is no hold on "
             "the account that would prevent a new card being issued. Read only."))
def check_card_holds(card_id: str):
    card = _find_card(card_id)
    return {"card_id": card["card_id"], "holds": card["holds"], "clear": len(card["holds"]) == 0}


@app.post("/cards/{card_id}/replacement", operation_id="request_replacement",
          tags=["Replacement"], summary="Submit a replacement card request",
          description=(
              "Use after the reason is known, the delivery address is confirmed, holds "
              "are clear, and a step up OTP has been completed. If the reason is lost or "
              "stolen, block_card must already have run. Returns a reference the customer "
              "can quote, and the replacement fee, which should be stated to the customer "
              "before they confirm."))
def request_replacement(card_id: str, body: ReplacementRequestBody):
    card = _find_card(card_id)
    if body.reason == ReplacementReason.lost_or_stolen and card["status"] != CardStatus.blocked:
        raise HTTPException(
            status_code=409,
            detail="Block the card before requesting a replacement for a lost or stolen card")
    if card["holds"]:
        raise HTTPException(status_code=409, detail="Account has a hold preventing reissue")

    ref = f"REP{uuid4().hex[:8].upper()}"
    REPLACEMENTS[ref] = {"reference": ref, "card_id": card["card_id"], "reason": body.reason,
                         "delivery_address": body.delivery_address, "status": "submitted",
                         "fee": 0 if body.reason == ReplacementReason.expired else 199,
                         "estimated_delivery_days": 7}
    return REPLACEMENTS[ref]


# ------------------------------------------------------------ pin reset


@app.post("/cards/{card_id}/pin-reset", operation_id="initiate_pin_reset", tags=["PIN"],
          summary="Start an OTP verified PIN reset",
          description=(
              "Use when the customer wants to change their PIN, after a step up OTP. This "
              "only starts the process: it returns a one time link the customer uses to "
              "set the PIN on the bank's own secure channel. The PIN is never sent to or "
              "received by this API. Do not wait for the customer to finish, end the task "
              "and check get_pin_reset_status on a later contact."))
def initiate_pin_reset(card_id: str):
    card = _find_card(card_id)
    if card["status"] == CardStatus.blocked:
        raise HTTPException(status_code=409, detail="Cannot reset the PIN on a blocked card")
    request_id = f"PIN{uuid4().hex[:8].upper()}"
    PIN_RESETS[request_id] = {"request_id": request_id, "card_id": card["card_id"],
                              "status": PinResetStatus.pending,
                              "expires_at": (datetime.utcnow() + timedelta(minutes=15)).isoformat() + "Z"}
    return {"request_id": request_id, "card_id": card["card_id"], "status": PinResetStatus.pending,
            "secure_link": f"https://securebank.example.com/set-pin/{request_id}",
            "link_expires_in_minutes": 15,
            "message": "Send the customer this link. They set the PIN on the bank's own screen."}


@app.get("/pin-reset/{request_id}", operation_id="get_pin_reset_status", tags=["PIN"],
         summary="Check whether a PIN reset completed",
         description=(
             "Use on a later contact to confirm the outcome of a PIN reset that was "
             "started earlier, so the customer can be told whether it went through or "
             "whether the link expired and needs resending. Read only."))
def get_pin_reset_status(request_id: str):
    entry = PIN_RESETS.get(request_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Unknown PIN reset request")
    if entry["status"] == PinResetStatus.pending and \
            datetime.utcnow() > datetime.fromisoformat(entry["expires_at"].replace("Z", "")):
        entry["status"] = PinResetStatus.expired
    return entry


@app.post("/pin-reset/{request_id}/simulate-completion", operation_id="simulate_pin_completion",
          tags=["Testing"], summary="Testing only: mark a PIN reset as completed",
          description=(
              "Not for the coworker to call. Stands in for the customer finishing the "
              "reset on the bank's secure screen, so the status check can be demonstrated."))
def simulate_pin_completion(request_id: str):
    entry = PIN_RESETS.get(request_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Unknown PIN reset request")
    entry["status"] = PinResetStatus.completed
    return entry


# ---------------------------------------------------------------- fraud


@app.post("/cards/{card_id}/fraud-case", operation_id="log_fraud_case", tags=["Fraud"],
          summary="Log a fraud case for analyst review",
          description=(
              "Use after the card has been blocked, the customer has identified the "
              "disputed transactions, and a step up OTP has been completed. Creates a "
              "case for a human fraud analyst. The coworker does not investigate or close "
              "cases. Returns a reference for the customer."))
def log_fraud_case(card_id: str, body: FraudCaseRequest):
    card = _find_card(card_id)
    if card["status"] != CardStatus.blocked:
        raise HTTPException(
            status_code=409,
            detail="Block the card before logging a fraud case, otherwise it stays exposed")
    cid = card["card_id"]
    known = {t["transaction_id"] for t in TRANSACTIONS.get(cid, [])}
    unknown = [t for t in body.transaction_ids if t not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown transaction ids: {unknown}")

    ref = f"FRD{uuid4().hex[:8].upper()}"
    FRAUD_CASES[ref] = {"case_reference": ref, "card_id": cid,
                        "transaction_ids": body.transaction_ids,
                        "description": body.description, "status": "open",
                        "assigned_to": "fraud_analyst_queue",
                        "disputed_total": sum(t["amount"] for t in TRANSACTIONS.get(cid, [])
                                              if t["transaction_id"] in body.transaction_ids),
                        "created_at": datetime.utcnow().isoformat() + "Z"}
    return FRAUD_CASES[ref]


@app.get("/fraud-case/{case_reference}", operation_id="get_fraud_case_status", tags=["Fraud"],
         summary="Check the status of a fraud case",
         description=(
             "Use when the customer asks about a case they reported earlier. Read only. "
             "Report the status as returned, and do not speculate about the outcome or "
             "whether a refund will be issued."))
def get_fraud_case_status(case_reference: str):
    case = FRAUD_CASES.get(case_reference)
    if not case:
        raise HTTPException(status_code=404, detail="Unknown case reference")
    return case


@app.get("/testing/card-full-details/{card_id}", operation_id="get_card_full_details",
         tags=["Testing"], summary="Testing only: returns unmasked card details",
         description=(
             "Returns the full card number, CVV and expiry for a card. Use only when "
             "explicitly asked to test the output guardrail. The values are dummy test "
             "numbers, not real card data."))
def get_card_full_details(card_id: str):
    """Deliberately returns data the rest of this API never exposes, so the output
    guardrail can be seen masking it. 4111 1111 1111 1111 is the standard Visa test
    number and belongs to no real account. Remove this endpoint before any real use."""
    card = _find_card(card_id)
    return {
        "card_id": card["card_id"],
        "card_number": "4111 1111 1111 1111",
        "cvv": "123",
        "expiry": "09/29",
        "pin": "4321",
        "note": "Dummy test values. This endpoint exists only to exercise the output guardrail.",
    }


@app.post("/testing/reset", operation_id="reset_test_data", tags=["Testing"],
          summary="Testing only: restore the original data",
          description=("Resets all cards, limits, features, cases and requests to their "
                       "starting state. Not for the coworker to call."))
def reset_test_data():
    global DB, OTP_STORE, PIN_RESETS, FRAUD_CASES, REPLACEMENTS
    DB = copy.deepcopy(_INITIAL_DB)
    OTP_STORE, PIN_RESETS, FRAUD_CASES, REPLACEMENTS = {}, {}, {}, {}
    return {"reset": True, "customers": len(DB),
            "cards": sum(len(c["cards"]) for c in DB.values())}


@app.get("/health", operation_id="health_check", tags=["Testing"], summary="Health check")
def health_check():
    return {"status": "ok", "customers": len(DB),
            "cards": sum(len(c["cards"]) for c in DB.values())}