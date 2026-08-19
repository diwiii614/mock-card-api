"""
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

import os
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
                "balance": 24500.75,
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
                "balance": 8200.00,
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
                "balance": 15750.40,
                "limits": {"atm": 20000, "pos": 150000, "online": 75000,
                           "international": 50000},
                "features": {"contactless": False, "online_payments": True,
                             "international_usage": False, "atm_withdrawals": True},
                "holds": [],
            },
        },
    },
}

TRANSACTIONS: Dict[str, List[dict]] = {
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

OTP_STORE: Dict[str, dict] = {}
PIN_RESETS: Dict[str, dict] = {}
FRAUD_CASES: Dict[str, dict] = {}
REPLACEMENTS: Dict[str, dict] = {}

FIXED_OTP = "123456"  # mock only: any real system would generate this


# ------------------------------------------------------------- helpers


def _find_customer_by_mobile(mobile: str) -> Optional[dict]:
    for cust in DB.values():
        if cust["mobile"] == mobile:
            return cust
    return None


def _find_card(card_id: str) -> dict:
    for cust in DB.values():
        if card_id in cust["cards"]:
            return cust["cards"][card_id]
    raise HTTPException(status_code=404, detail=f"Card {card_id} not found")


def _customer_for_card(card_id: str) -> dict:
    for cust in DB.values():
        if card_id in cust["cards"]:
            return cust
    raise HTTPException(status_code=404, detail=f"Card {card_id} not found")


# -------------------------------------------------------------- models


class IdentifyRequest(BaseModel):
    identifier: str = Field(..., description="Registered mobile number or customer ID",
                            examples=["9876543210"])


class IdentifyResponse(BaseModel):
    otp_sent: bool
    masked_mobile: str = Field(..., description="Masked registered number, for the customer to confirm")
    challenge_id: str
    message: str


class VerifyOtpRequest(BaseModel):
    challenge_id: str
    otp: str = Field(..., examples=["123456"])


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


class BalanceResponse(BaseModel):
    card_id: str
    balance: float
    currency: str = "INR"
    as_of: str


class LimitsPayload(BaseModel):
    atm: Optional[int] = None
    pos: Optional[int] = None
    online: Optional[int] = None
    international: Optional[int] = None


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
              "Use at the very start of a conversation, once the customer has given a "
              "registered mobile number or customer ID. Sends an OTP to the registered "
              "number and returns it masked so the customer can confirm it is theirs. "
              "Returns no account data, so nothing is exposed before verification."))
def identify_customer(body: IdentifyRequest):
    cust = DB.get(body.identifier) or _find_customer_by_mobile(body.identifier)
    challenge_id = f"CHL{uuid4().hex[:10].upper()}"

    # Always report success, so an unknown identifier cannot be used to
    # discover which numbers are registered.
    if cust:
        OTP_STORE[challenge_id] = {
            "customer_id": cust["customer_id"],
            "otp": FIXED_OTP,
            "expires_at": datetime.utcnow() + timedelta(minutes=5),
        }
        masked = f"******{cust['mobile'][-4:]}"
    else:
        OTP_STORE[challenge_id] = {"customer_id": None, "otp": FIXED_OTP,
                                   "expires_at": datetime.utcnow() + timedelta(minutes=5)}
        masked = "******0000"

    return IdentifyResponse(
        otp_sent=True, masked_mobile=masked, challenge_id=challenge_id,
        message=f"An OTP has been sent to the number ending {masked[-4:]}.")


@app.post("/auth/verify-otp", response_model=VerifyOtpResponse, operation_id="verify_otp",
          tags=["Session"], summary="Verify the OTP and open the session",
          description=(
              "Use after the customer supplies the OTP they received. Returns only a "
              "verified true or false plus the customer identity. The coworker never "
              "validates the OTP itself. On success the session is verified and account "
              "data may then be fetched."))
def verify_otp(body: VerifyOtpRequest):
    entry = OTP_STORE.get(body.challenge_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Unknown or expired challenge")
    if datetime.utcnow() > entry["expires_at"]:
        return VerifyOtpResponse(verified=False, message="This OTP has expired. Request a new one.")
    if body.otp != entry["otp"] or entry["customer_id"] is None:
        return VerifyOtpResponse(verified=False, message="That OTP is not correct.")

    cust = DB[entry["customer_id"]]
    del OTP_STORE[body.challenge_id]  # single use
    return VerifyOtpResponse(verified=True, customer_id=cust["customer_id"],
                             customer_name=cust["name"], message="Verification successful.")


@app.get("/customers/{customer_id}/cards", response_model=List[CardSummary],
         operation_id="get_cards", tags=["Session"],
         summary="List the customer's cards",
         description=(
             "Use after the session is verified, so the customer can choose which card "
             "they want to act on. Returns only the last four digits, never a full card "
             "number."))
def get_cards(customer_id: str):
    cust = DB.get(customer_id)
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")
    return [CardSummary(**{k: c[k] for k in ("card_id", "masked_number", "card_type", "status")})
            for c in cust["cards"].values()]


# -------------------------------------------------------------- reads


@app.get("/cards/{card_id}/balance", response_model=BalanceResponse,
         operation_id="get_balance", tags=["Reads"], summary="Get the current balance",
         description=(
             "Use whenever the customer asks about their balance or available funds. "
             "Read only, covered by session authentication, no OTP needed. Always call "
             "this fresh rather than repeating a figure from earlier in the conversation."))
def get_balance(card_id: str):
    card = _find_card(card_id)
    return BalanceResponse(card_id=card_id, balance=card["balance"],
                           as_of=datetime.utcnow().isoformat() + "Z")


@app.get("/cards/{card_id}/transactions", operation_id="get_transactions", tags=["Reads"],
         summary="List recent transactions",
         description=(
             "Use when the customer asks to see recent activity, asks about a specific "
             "charge, or needs to identify a disputed transaction during a fraud report. "
             "Returns merchant, category, amount, and whether the charge is recurring or "
             "still pending. Read only, no OTP needed."))
def get_transactions(card_id: str, limit: int = 10):
    _find_card(card_id)
    return {"card_id": card_id, "transactions": TRANSACTIONS.get(card_id, [])[:limit]}


@app.get("/cards/{card_id}/status", operation_id="get_card_status", tags=["Reads"],
         summary="Check whether a card is active or blocked",
         description=(
             "Use before blocking a card, to avoid blocking one that is already blocked, "
             "and whenever the customer asks whether their card is usable. Read only."))
def get_card_status(card_id: str):
    card = _find_card(card_id)
    return {"card_id": card_id, "status": card["status"],
            "masked_number": card["masked_number"]}


@app.get("/cards/{card_id}/limits", operation_id="get_limits", tags=["Reads"],
         summary="Get current spending limits",
         description=(
             "Use when the customer asks what their limits are, and before changing them "
             "so the current values can be shown alongside the new ones. Read only."))
def get_limits(card_id: str):
    card = _find_card(card_id)
    return {"card_id": card_id, "limits": card["limits"], "currency": "INR"}


@app.get("/cards/{card_id}/features", operation_id="get_card_features", tags=["Reads"],
         summary="Get current on/off state of card features",
         description=(
             "Use when the customer asks about card features, or before changing them. "
             "Return every feature and its current state together so the customer can "
             "select several changes at once. Read only."))
def get_card_features(card_id: str):
    card = _find_card(card_id)
    return {"card_id": card_id, "features": card["features"]}


# ------------------------------------------------------------- writes


@app.put("/cards/{card_id}/limits", operation_id="update_limits", tags=["Changes"],
         summary="Change spending limits",
         description=(
             "Use to change one or more spending limits after the customer has confirmed "
             "the new values and completed a step up OTP. Several limits can be sent in "
             "one call, so a single OTP covers the whole set. Only the fields supplied "
             "are changed."))
def update_limits(card_id: str, body: LimitsPayload):
    card = _find_card(card_id)
    if card["status"] == CardStatus.blocked:
        raise HTTPException(status_code=409, detail="Cannot change limits on a blocked card")
    changed = {}
    for field, value in body.model_dump(exclude_none=True).items():
        card["limits"][field] = value
        changed[field] = value
    if not changed:
        raise HTTPException(status_code=400, detail="No limit values supplied")
    return {"card_id": card_id, "updated": changed, "limits": card["limits"]}


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
    return {"card_id": card_id, "updated": changed, "features": card["features"]}


@app.post("/cards/{card_id}/block", operation_id="block_card", tags=["Protective"],
          summary="Block a card permanently",
          description=(
              "Use immediately when the customer reports the card lost, stolen, or used "
              "fraudulently. Runs on session authentication so it is never delayed. This "
              "cannot be reversed by the coworker: a blocked card can only be replaced, "
              "not reactivated. Call get_card_status first to avoid blocking twice."))
def block_card(card_id: str, body: BlockRequest):
    card = _find_card(card_id)
    if card["status"] == CardStatus.blocked:
        return {"card_id": card_id, "status": card["status"], "already_blocked": True,
                "message": "This card was already blocked."}
    card["status"] = CardStatus.blocked
    card["block_reason"] = body.reason
    return {"card_id": card_id, "status": card["status"], "already_blocked": False,
            "reason": body.reason,
            "message": f"Card ending {card['last_four']} has been blocked."}


# -------------------------------------------------------- replacement


@app.get("/customers/{customer_id}/address", operation_id="get_customer_address",
         tags=["Replacement"], summary="Get the delivery address on record",
         description=(
             "Use during a replacement request, so the address on file can be confirmed "
             "with the customer before a card is despatched. Read only."))
def get_customer_address(customer_id: str):
    cust = DB.get(customer_id)
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")
    return {"customer_id": customer_id, "address": cust["address"]}


@app.get("/cards/{card_id}/holds", operation_id="check_card_holds", tags=["Replacement"],
         summary="Check for holds that would block a reissue",
         description=(
             "Use before submitting a replacement request, to confirm there is no hold on "
             "the account that would prevent a new card being issued. Read only."))
def check_card_holds(card_id: str):
    card = _find_card(card_id)
    return {"card_id": card_id, "holds": card["holds"], "clear": len(card["holds"]) == 0}


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
    REPLACEMENTS[ref] = {"reference": ref, "card_id": card_id, "reason": body.reason,
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
    PIN_RESETS[request_id] = {"request_id": request_id, "card_id": card_id,
                              "status": PinResetStatus.pending,
                              "expires_at": (datetime.utcnow() + timedelta(minutes=15)).isoformat() + "Z"}
    return {"request_id": request_id, "card_id": card_id, "status": PinResetStatus.pending,
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
    known = {t["transaction_id"] for t in TRANSACTIONS.get(card_id, [])}
    unknown = [t for t in body.transaction_ids if t not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown transaction ids: {unknown}")

    ref = f"FRD{uuid4().hex[:8].upper()}"
    FRAUD_CASES[ref] = {"case_reference": ref, "card_id": card_id,
                        "transaction_ids": body.transaction_ids,
                        "description": body.description, "status": "open",
                        "assigned_to": "fraud_analyst_queue",
                        "disputed_total": sum(t["amount"] for t in TRANSACTIONS.get(card_id, [])
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


@app.get("/health", operation_id="health_check", tags=["Testing"], summary="Health check")
def health_check():
    return {"status": "ok", "customers": len(DB),
            "cards": sum(len(c["cards"]) for c in DB.values())}
