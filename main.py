"""
Mock Card Management API
------------------------
Stands in for a real card management system so the Card Operations Co-Worker
can be built and tested before any live backend exists.

Shape of it:
  - identify_customer sends an OTP, verify_otp returns the customer_id, the
    card_id, and the card state together, so one round trip gets everything
    later calls need.
  - One customer has exactly one card.
  - get_card returns state, balance, limits, features, EMI options, PIN
    status, and transactions in a single call.
  - update_card changes limits and features in a single call.
  - A PIN change returns a link on this service. Opening it in a browser is
    what marks the PIN changed; there is no endpoint that lets the Co-Worker
    do it on the customer's behalf.

State is held in memory, so a card blocked earlier really is blocked. It
resets when the process restarts.

Run:  uvicorn main:app --reload --port 8000
Spec: http://localhost:8000/openapi.json
Docs: http://localhost:8000/docs
"""

import copy
import os
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

# Optional API key. Set API_KEY to require an x-api-key header on every
# request. Leave it unset and the API is open, which is fine for a mock.
API_KEY = os.getenv("API_KEY")

# The PIN link is built from the incoming request, so it is correct both
# locally and once deployed. Set PUBLIC_BASE_URL to override.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")

MONTHLY_LIMIT = 100000      # 1 lakh. Every limit sits in 1..this.
BILL_DAY = 28
OTP_MINUTES = 10
PIN_LINK_MINUTES = 15
REPLACEMENT_FEE = 199
EMI_PLANS = {3: 13.0, 6: 14.0, 12: 15.0, 24: 16.0}
FIXED_OTP = "123456"        # mock only

app = FastAPI(
    title="Mock Card Management API",
    version="5.0.0",
    description=(
        "Card servicing for the Card Operations Co-Worker. Identify the customer "
        "by mobile number and verify the OTP to get their customer_id and card_id, "
        "then read or change the card. One customer, one card."
    ),
)


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    open_paths = ("/docs", "/redoc", "/openapi.json", "/health", "/set-pin", "/admin")
    if API_KEY and not request.url.path.startswith(open_paths):
        if request.headers.get("x-api-key") != API_KEY:
            return JSONResponse(status_code=401,
                                content={"detail": "Missing or invalid x-api-key header"})
    return await call_next(request)


# ---------------------------------------------------------------- enums


class CardState(str, Enum):
    active = "active"
    blocked = "blocked"


class BlockReason(str, Enum):
    lost = "lost"
    stolen = "stolen"
    fraud = "fraud"
    damaged = "damaged"


class TransactionState(str, Enum):
    completed = "completed"
    pending = "pending"


class ReplacementReason(str, Enum):
    lost_or_stolen = "lost_or_stolen"
    damaged = "damaged"


# ------------------------------------------------------------- storage
# Four customers, one card each. Card numbers are stored masked only: the
# full PAN does not exist anywhere in this service, so it cannot be returned.


def _day(offset: int) -> str:
    """A date `offset` days before today, so the data never looks stale."""
    return (date.today() - timedelta(days=offset)).isoformat()


CUSTOMERS: Dict[str, dict] = {
    "CUST001": {"customer_id": "CUST001", "name": "Ananya Sharma",
                "mobile": "9876543210", "card_id": "CARD1001",
                "address": "42 Brigade Road, Bengaluru, Karnataka 560001"},
    "CUST002": {"customer_id": "CUST002", "name": "Rohit Menon",
                "mobile": "9123456780", "card_id": "CARD2001",
                "address": "8 Anna Salai, Chennai, Tamil Nadu 600002"},
    "CUST003": {"customer_id": "CUST003", "name": "Priya Nair",
                "mobile": "9988776655", "card_id": "CARD3001",
                "address": "17 Marine Drive, Kochi, Kerala 682031"},
    "CUST004": {"customer_id": "CUST004", "name": "Arjun Reddy",
                "mobile": "9012345678", "card_id": "CARD4001",
                "address": "5 Banjara Hills Road 12, Hyderabad, Telangana 500034"},
}

CARDS: Dict[str, dict] = {
    "CARD1001": {
        "card_id": "CARD1001", "customer_id": "CUST001",
        "masked_number": "**** **** **** 4521", "last_four": "4521",
        "card_type": "Visa Credit", "state": CardState.active,
        "limits": {"atm": 25000, "pos": 100000, "online": 50000, "international": 20000},
        "features": {"contactless": True, "online_payments": True,
                     "international_usage": False, "atm_withdrawals": True},
        "pin_changed": False, "pin_changed_at": None,
        "transactions": [
            # Two charges from the same unrecognised merchant, one still
            # pending: the case the dispute and ticket flow is built around.
            {"transaction_id": "TXN9001", "date": _day(1),
             "merchant": "Blue Tokai Coffee", "amount": 480.00,
             "state": TransactionState.completed},
            {"transaction_id": "TXN9002", "date": _day(2), "merchant": "Netflix",
             "amount": 649.00, "state": TransactionState.completed},
            {"transaction_id": "TXN9003", "date": _day(2),
             "merchant": "UNKNOWN MERCHANT 44821", "amount": 18999.00,
             "state": TransactionState.pending},
            {"transaction_id": "TXN9004", "date": _day(3),
             "merchant": "UNKNOWN MERCHANT 44821", "amount": 4500.00,
             "state": TransactionState.completed},
            {"transaction_id": "TXN9005", "date": _day(4),
             "merchant": "More Supermarket", "amount": 2340.50,
             "state": TransactionState.completed},
            {"transaction_id": "TXN9006", "date": _day(6), "merchant": "Indian Oil",
             "amount": 3000.00, "state": TransactionState.completed},
            {"transaction_id": "TXN9007", "date": _day(7), "merchant": "MakeMyTrip",
             "amount": 12800.00, "state": TransactionState.completed},
            {"transaction_id": "TXN9008", "date": _day(9), "merchant": "Amazon India",
             "amount": 5499.00, "state": TransactionState.completed},
        ],
    },
    "CARD2001": {
        "card_id": "CARD2001", "customer_id": "CUST002",
        "masked_number": "**** **** **** 7710", "last_four": "7710",
        "card_type": "Visa Credit", "state": CardState.active,
        "limits": {"atm": 20000, "pos": 75000, "online": 75000, "international": 50000},
        "features": {"contactless": False, "online_payments": True,
                     "international_usage": False, "atm_withdrawals": True},
        "pin_changed": False, "pin_changed_at": None,
        "transactions": [
            {"transaction_id": "TXN9201", "date": _day(1), "merchant": "Swiggy",
             "amount": 720.00, "state": TransactionState.completed},
            {"transaction_id": "TXN9202", "date": _day(3),
             "merchant": "UNKNOWN MERCHANT 71204", "amount": 9200.00,
             "state": TransactionState.pending},
            {"transaction_id": "TXN9203", "date": _day(5),
             "merchant": "BESCOM Electricity", "amount": 2150.00,
             "state": TransactionState.completed},
            {"transaction_id": "TXN9204", "date": _day(8), "merchant": "Croma",
             "amount": 18400.00, "state": TransactionState.completed},
            {"transaction_id": "TXN9205", "date": _day(11), "merchant": "IRCTC",
             "amount": 1985.00, "state": TransactionState.completed},
        ],
    },
    "CARD3001": {
        # Light user: small spend, low limits.
        "card_id": "CARD3001", "customer_id": "CUST003",
        "masked_number": "**** **** **** 3390", "last_four": "3390",
        "card_type": "Mastercard Credit", "state": CardState.active,
        "limits": {"atm": 10000, "pos": 50000, "online": 40000, "international": 5000},
        "features": {"contactless": True, "online_payments": True,
                     "international_usage": False, "atm_withdrawals": False},
        "pin_changed": False, "pin_changed_at": None,
        "transactions": [
            {"transaction_id": "TXN9301", "date": _day(2),
             "merchant": "Third Wave Coffee", "amount": 390.00,
             "state": TransactionState.completed},
            {"transaction_id": "TXN9302", "date": _day(4),
             "merchant": "Apollo Pharmacy", "amount": 845.50,
             "state": TransactionState.completed},
            {"transaction_id": "TXN9303", "date": _day(8), "merchant": "Uber",
             "amount": 260.00, "state": TransactionState.pending},
        ],
    },
    "CARD4001": {
        # Heavy user, close to the monthly cap: for "am I near my limit".
        "card_id": "CARD4001", "customer_id": "CUST004",
        "masked_number": "**** **** **** 6642", "last_four": "6642",
        "card_type": "Visa Credit", "state": CardState.active,
        "limits": {"atm": 30000, "pos": 100000, "online": 100000, "international": 75000},
        "features": {"contactless": True, "online_payments": True,
                     "international_usage": True, "atm_withdrawals": True},
        "pin_changed": False, "pin_changed_at": None,
        "transactions": [
            {"transaction_id": "TXN9401", "date": _day(1), "merchant": "Taj Krishna",
             "amount": 24500.00, "state": TransactionState.completed},
            {"transaction_id": "TXN9402", "date": _day(2), "merchant": "Apple India",
             "amount": 31900.00, "state": TransactionState.completed},
            {"transaction_id": "TXN9403", "date": _day(4), "merchant": "Indigo Airlines",
             "amount": 18750.00, "state": TransactionState.pending},
            {"transaction_id": "TXN9404", "date": _day(6), "merchant": "Zomato",
             "amount": 1240.00, "state": TransactionState.completed},
            {"transaction_id": "TXN9405", "date": _day(9), "merchant": "Nykaa",
             "amount": 8615.00, "state": TransactionState.completed},
            {"transaction_id": "TXN9406", "date": _day(12), "merchant": "Shell",
             "amount": 4200.00, "state": TransactionState.completed},
        ],
    },
}

# Snapshot taken at import, before anything can mutate the live dicts.
# reset_mock_data restores from this.
SEED_CUSTOMERS = copy.deepcopy(CUSTOMERS)
SEED_CARDS = copy.deepcopy(CARDS)

OTP_STORE: Dict[str, dict] = {}
PIN_RESETS: Dict[str, dict] = {}
REPLACEMENTS: Dict[str, dict] = {}
TICKETS: Dict[str, dict] = {}


# ------------------------------------------------------------- helpers


def _now() -> datetime:
    return datetime.utcnow()


def _card(card_id: str) -> dict:
    card = CARDS.get((card_id or "").strip().upper())
    if not card:
        raise HTTPException(
            status_code=404,
            detail=("Unknown card_id. Use the card_id returned by verify_otp, or "
                    "identify the customer again."))
    return card


def _customer(customer_id: str) -> dict:
    cust = CUSTOMERS.get((customer_id or "").strip().upper())
    if not cust:
        raise HTTPException(
            status_code=404,
            detail="Unknown customer_id. Use the customer_id returned by verify_otp.")
    return cust


def _cycle() -> dict:
    """The statement cycle ends on BILL_DAY."""
    today = date.today()
    if today.day <= BILL_DAY:
        end = today.replace(day=BILL_DAY)
    else:
        nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        end = nxt.replace(day=BILL_DAY)
    start = (end.replace(day=1) - timedelta(days=1)).replace(day=BILL_DAY) + timedelta(days=1)
    return {"start": start, "end": end}


def _used(card: dict) -> float:
    """Spend this cycle. Completed and pending both count, because a pending
    charge has still consumed the customer's available limit."""
    cyc = _cycle()
    total = sum(t["amount"] for t in card["transactions"]
                if cyc["start"].isoformat() <= t["date"] <= cyc["end"].isoformat())
    return round(total, 2)


def _base_url(request: Request) -> str:
    return (PUBLIC_BASE_URL or str(request.base_url)).rstrip("/")


# -------------------------------------------------------------- models


class IdentifyRequest(BaseModel):
    identifier: str = Field(..., description="Registered mobile number or customer ID",
                            examples=["9876543210"])

    @field_validator("identifier", mode="before")
    @classmethod
    def _as_text(cls, v):
        """A model reading `"9876543210"` will often emit it as a JSON number.
        Accept that rather than answering with a validation error."""
        return None if v is None else str(v).strip()


class IdentifyResponse(BaseModel):
    otp_sent: bool
    masked_mobile: str = Field(..., description="For the customer to confirm it is theirs")
    challenge_id: str
    message: str


class VerifyOtpRequest(BaseModel):
    challenge_id: str
    otp: str = Field(..., examples=["123456"])

    @field_validator("challenge_id", "otp", mode="before")
    @classmethod
    def _as_text(cls, v):
        """Same coercion, plus the spacing a customer uses reading a code back."""
        return None if v is None else str(v).replace(" ", "").replace("-", "").strip()


class VerifyOtpResponse(BaseModel):
    verified: bool
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    card_id: Optional[str] = Field(None, description="Use this on every card call")
    card_state: Optional[CardState] = None
    masked_number: Optional[str] = None
    message: str


class LimitsPayload(BaseModel):
    atm: Optional[int] = Field(None, gt=0, le=MONTHLY_LIMIT)
    pos: Optional[int] = Field(None, gt=0, le=MONTHLY_LIMIT)
    online: Optional[int] = Field(None, gt=0, le=MONTHLY_LIMIT)
    international: Optional[int] = Field(None, gt=0, le=MONTHLY_LIMIT)


class FeaturesPayload(BaseModel):
    contactless: Optional[bool] = None
    online_payments: Optional[bool] = None
    international_usage: Optional[bool] = None
    atm_withdrawals: Optional[bool] = None


class UpdateCardRequest(BaseModel):
    limits: Optional[LimitsPayload] = Field(
        None, description="Only the limits supplied are changed")
    features: Optional[FeaturesPayload] = Field(
        None, description="Only the features supplied are changed")


class BlockRequest(BaseModel):
    reason: BlockReason


class ReplacementRequestBody(BaseModel):
    reason: ReplacementReason
    delivery_address: str = Field(..., description="Confirmed with the customer first")


class TicketRequest(BaseModel):
    name: str = Field(..., examples=["Ananya Sharma"])
    address: str = Field(..., examples=["42 Brigade Road, Bengaluru, Karnataka 560001"])
    issue: str = Field(..., description="What the customer is reporting",
                       examples=["Does not recognise TXN9003 for 18,999"])


# ------------------------------------------------------------ identity


@app.post("/auth/identify", response_model=IdentifyResponse,
          operation_id="identify_customer", tags=["Session"],
          summary="Look up a customer and send an OTP",
          description=(
              "Use at the very start of a conversation, once the customer has given a "
              "registered mobile number or customer ID. Sends an OTP and returns the "
              "number masked so the customer can confirm it is theirs. Returns no "
              "account data, so nothing is exposed before verification. Pass the "
              "challenge_id to verify_otp."))
def identify_customer(body: IdentifyRequest):
    cust = CUSTOMERS.get(body.identifier.upper()) or next(
        (c for c in CUSTOMERS.values() if c["mobile"] == body.identifier), None)
    challenge_id = f"CHL{uuid4().hex[:10].upper()}"
    # Always report success, so an unknown identifier cannot be used to work
    # out which numbers are registered.
    OTP_STORE[challenge_id] = {"customer_id": cust["customer_id"] if cust else None,
                               "otp": FIXED_OTP,
                               "expires_at": _now() + timedelta(minutes=OTP_MINUTES)}
    masked = f"******{cust['mobile'][-4:]}" if cust else "******0000"
    return IdentifyResponse(otp_sent=True, masked_mobile=masked, challenge_id=challenge_id,
                            message=f"An OTP has been sent to the number ending {masked[-4:]}.")


@app.post("/auth/verify-otp", response_model=VerifyOtpResponse, operation_id="verify_otp",
          tags=["Session"], summary="Verify the OTP and get the customer's card",
          description=(
              "Use after the customer supplies the OTP they received. On success it "
              "returns the customer_id, the card_id, and whether the card is active or "
              "blocked, so no further lookup is needed to start work. Keep the card_id "
              "for the rest of the conversation. Do not share any account information "
              "until this returns verified true."))
def verify_otp(body: VerifyOtpRequest):
    entry = OTP_STORE.get(body.challenge_id)
    if not entry:
        return VerifyOtpResponse(
            verified=False,
            message=("That code is no longer valid, it may already have been used. "
                     "Call identify_customer again and use the new challenge_id."))
    if _now() > entry["expires_at"]:
        del OTP_STORE[body.challenge_id]
        return VerifyOtpResponse(
            verified=False,
            message=("This code has expired. Call identify_customer again and use the "
                     "new challenge_id."))
    if body.otp != entry["otp"] or entry["customer_id"] is None:
        return VerifyOtpResponse(
            verified=False,
            message=("That code is not correct. Ask the customer to check it and try "
                     "again, or call identify_customer to send a new one."))

    del OTP_STORE[body.challenge_id]  # single use
    cust = CUSTOMERS[entry["customer_id"]]
    card = CARDS[cust["card_id"]]
    return VerifyOtpResponse(
        verified=True, customer_id=cust["customer_id"], customer_name=cust["name"],
        card_id=card["card_id"], card_state=card["state"],
        masked_number=card["masked_number"],
        message=(f"Verification successful. {cust['name']} holds card "
                 f"{card['masked_number']}, currently {card['state']}."))


@app.get("/customers/{customer_id}/address", operation_id="get_customer_address",
         tags=["Reads"], summary="Get the address on record",
         description=(
             "Use when arranging a replacement card or raising a ticket, so the address "
             "on file can be confirmed with the customer. Read only."))
def get_customer_address(customer_id: str):
    cust = _customer(customer_id)
    return {"customer_id": cust["customer_id"], "name": cust["name"],
            "address": cust["address"]}


# ---------------------------------------------------------------- card


@app.get("/cards/{card_id}", operation_id="get_card", tags=["Reads"],
         summary="Get everything about the card in one call",
         description=(
             "Use whenever the customer asks about their card, their spending, how much "
             "is left, their limits, their card features, EMI options, whether their PIN "
             "was changed, or recent transactions. Returns all of it together, so there "
             "is no need to chain several reads. Read only. Call it again after any "
             "change rather than repeating figures from earlier in the conversation."))
def get_card(card_id: str):
    card = _card(card_id)
    used = _used(card)
    cyc = _cycle()
    return {
        "card_id": card["card_id"], "customer_id": card["customer_id"],
        "masked_number": card["masked_number"], "card_type": card["card_type"],
        "state": card["state"],
        "balance": {"currency": "INR", "monthly_limit": MONTHLY_LIMIT, "used": used,
                    "available": round(MONTHLY_LIMIT - used, 2),
                    "percent_used": round(used / MONTHLY_LIMIT * 100, 1),
                    "cycle_start": cyc["start"].isoformat(),
                    "cycle_end": cyc["end"].isoformat()},
        "limits": {**card["limits"], "min_limit": 1, "max_limit": MONTHLY_LIMIT},
        "features": card["features"],
        "emi_options": [{"tenure_months": m, "interest_rate_annual_percent": r}
                        for m, r in sorted(EMI_PLANS.items())],
        "pin": {"pin_changed": card["pin_changed"],
                "pin_changed_at": card["pin_changed_at"]},
        "transactions": card["transactions"],
    }


@app.patch("/cards/{card_id}", operation_id="update_card", tags=["Changes"],
           summary="Change spending limits and card features",
           description=(
               "Use to change limits, switch features such as contactless, online "
               "payments, international usage, or ATM withdrawals, or both at once, "
               "after the customer has confirmed the new values. Every limit must be "
               "greater than 0 and no more than the monthly limit. Send all the changes "
               "the customer asked for in one call. Only the fields supplied change."))
def update_card(card_id: str, body: UpdateCardRequest):
    card = _card(card_id)
    if card["state"] == CardState.blocked:
        raise HTTPException(status_code=409, detail="Cannot change a blocked card")
    changed = {}
    if body.limits:
        for field, value in body.limits.model_dump(exclude_none=True).items():
            card["limits"][field] = value
            changed[field] = value
    if body.features:
        for field, value in body.features.model_dump(exclude_none=True).items():
            card["features"][field] = value
            changed[field] = value
    if not changed:
        raise HTTPException(status_code=400, detail="No limits or features supplied")
    return {"card_id": card["card_id"], "updated": changed,
            "limits": card["limits"], "features": card["features"]}


@app.post("/cards/{card_id}/block", operation_id="block_card", tags=["Changes"],
          summary="Block the card permanently",
          description=(
              "Use when the customer reports the card lost, stolen, or used "
              "fraudulently. This cannot be reversed: a blocked card can only be "
              "replaced, not reactivated, so tell the customer that and get an explicit "
              "yes before calling this. Check the card state first to avoid blocking a "
              "card that is already blocked."))
def block_card(card_id: str, body: BlockRequest):
    card = _card(card_id)
    if card["state"] == CardState.blocked:
        return {"card_id": card["card_id"], "state": card["state"], "already_blocked": True,
                "message": "This card was already blocked."}
    card["state"] = CardState.blocked
    card["block_reason"] = body.reason
    return {"card_id": card["card_id"], "state": card["state"], "already_blocked": False,
            "reason": body.reason,
            "message": f"Card ending {card['last_four']} has been blocked."}


@app.post("/cards/{card_id}/replacement", operation_id="request_replacement",
          tags=["Servicing"], summary="Request a replacement card",
          description=(
              "Use once the reason is known and the customer has confirmed the delivery "
              "address. If the card was lost or stolen it must be blocked first. Returns "
              "a reference the customer can quote, and the fee, which should be stated "
              "before they confirm."))
def request_replacement(card_id: str, body: ReplacementRequestBody):
    card = _card(card_id)
    if body.reason == ReplacementReason.lost_or_stolen and card["state"] != CardState.blocked:
        raise HTTPException(
            status_code=409,
            detail="Block the card before requesting a replacement for a lost or stolen card")
    ref = f"REP{uuid4().hex[:8].upper()}"
    REPLACEMENTS[ref] = {"reference": ref, "card_id": card["card_id"], "reason": body.reason,
                         "delivery_address": body.delivery_address, "state": "submitted",
                         "fee": REPLACEMENT_FEE, "currency": "INR",
                         "estimated_delivery_days": 7}
    return REPLACEMENTS[ref]


@app.post("/cards/{card_id}/pin-reset", operation_id="initiate_pin_reset",
          tags=["Servicing"], summary="Send the customer a PIN change link",
          description=(
              "Use when the customer wants to change their PIN. Returns a one time link "
              "the customer opens themselves; the PIN is never sent to or received by "
              "this API. Give the customer the link and move on. To check whether they "
              "finished, call get_card and read the pin block: pin_changed turns true "
              "once they have opened the link."))
def initiate_pin_reset(card_id: str, request: Request):
    card = _card(card_id)
    if card["state"] == CardState.blocked:
        raise HTTPException(status_code=409, detail="Cannot reset the PIN on a blocked card")
    request_id = f"PIN{uuid4().hex[:8].upper()}"
    PIN_RESETS[request_id] = {"request_id": request_id, "card_id": card["card_id"],
                              "expires_at": _now() + timedelta(minutes=PIN_LINK_MINUTES)}
    return {"request_id": request_id, "card_id": card["card_id"],
            "pin_change_link": f"{_base_url(request)}/set-pin/{request_id}",
            "link_expires_in_minutes": PIN_LINK_MINUTES,
            "pin_changed": card["pin_changed"],
            "message": ("Send the customer this link. Once they open it, pin_changed in "
                        "get_card turns true.")}


@app.post("/tickets", operation_id="raise_ticket", tags=["Servicing"],
          summary="Raise a support ticket",
          description=(
              "Use when the customer reports something that cannot be resolved here, "
              "such as a transaction they do not recognise, or a complaint. Confirm the "
              "name and address with the customer first. Returns a reference number to "
              "give them. Do not speculate about the outcome or promise a refund."))
def raise_ticket(body: TicketRequest):
    ref = f"TKT{uuid4().hex[:8].upper()}"
    TICKETS[ref] = {"reference": ref, "name": body.name, "address": body.address,
                    "issue": body.issue, "state": "open", "assigned_to": "support_queue",
                    "created_at": _now().isoformat() + "Z"}
    return {**TICKETS[ref],
            "message": f"Ticket {ref} has been raised. Quote this number on any follow up."}


@app.get("/health", operation_id="health_check", tags=["Testing"],
         summary="Check the service is up",
         description=(
             "Returns the service status and how many customers exist. Use it to confirm "
             "the connection works, or to wake a sleeping instance. Returns no customer "
             "data."))
def health_check():
    return {"state": "ok", "customers": len(CUSTOMERS), "cards": len(CARDS)}


@app.post("/admin/reset", include_in_schema=False)
def reset_mock_data():
    """Restore the starting state: card states, limits, features, transactions,
    and pin_changed flags, and clear every OTP challenge, ticket, replacement,
    and PIN link.

    Deliberately excluded from the OpenAPI document, for the same reason the
    PIN screen is. If the Co-Worker could see it, it could wipe a customer's
    session mid conversation and undo changes it had already confirmed.
    Call it with curl, or open it from the docs page.
    """
    # Restored in place, so anything holding a reference stays valid.
    CUSTOMERS.clear()
    CUSTOMERS.update(copy.deepcopy(SEED_CUSTOMERS))
    CARDS.clear()
    CARDS.update(copy.deepcopy(SEED_CARDS))
    for store in (OTP_STORE, PIN_RESETS, REPLACEMENTS, TICKETS):
        store.clear()
    return {"reset": True, "customers": len(CUSTOMERS), "cards": len(CARDS),
            "message": "Mock data restored to its starting state."}


# ----------------------------------------- not a tool: the PIN screen
# Deliberately excluded from the OpenAPI document. The Co-Worker never sees
# it, so it has no way to mark a PIN as changed on the customer's behalf.
# Only the customer opening the link does that.


@app.get("/set-pin/{request_id}", include_in_schema=False)
def set_pin_page(request_id: str):
    entry = PIN_RESETS.get(request_id)
    if not entry:
        return HTMLResponse(_page("Link not recognised",
                                  "This PIN change link is not valid."), status_code=404)
    card = CARDS[entry["card_id"]]
    if _now() > entry["expires_at"] and not card["pin_changed"]:
        return HTMLResponse(_page("Link expired",
                                  "Ask us to send a new PIN change link."), status_code=410)
    # No PIN is accepted or stored here. No PIN exists in this service at all.
    card["pin_changed"] = True
    card["pin_changed_at"] = _now().isoformat() + "Z"
    return HTMLResponse(_page(
        "PIN changed",
        f"The PIN for the card ending {card['last_four']} has been updated. "
        f"You can close this page."))


def _page(heading: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{heading}</title>"
            f"</head><body style='font-family:system-ui;max-width:32rem;margin:4rem auto;"
            f"line-height:1.6;padding:0 1rem'><h1>{heading}</h1><p>{body}</p></body></html>")


# ---------------------------------------------------- spec for the tool
# FastAPI emits OpenAPI 3.1, where an optional field becomes anyOf
# [type, null]. Older spec parsers handle that badly, so those unions are
# collapsed and the document is labelled 3.0.3. The runtime is unaffected.


def _simplify(node):
    if isinstance(node, list):
        return [_simplify(n) for n in node]
    if not isinstance(node, dict):
        return node
    if "anyOf" in node:
        variants = [v for v in node["anyOf"] if v.get("type") != "null"]
        if len(variants) == 1:
            merged = {k: v for k, v in node.items() if k != "anyOf"}
            merged.update(variants[0])
            node = merged
    if isinstance(node.get("examples"), list) and node["examples"]:
        node["example"] = node.pop("examples")[0]
    return {k: _simplify(v) for k, v in node.items()}


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = _simplify(get_openapi(title=app.title, version=app.version,
                                   description=app.description, routes=app.routes))
    schema["openapi"] = "3.0.3"
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
