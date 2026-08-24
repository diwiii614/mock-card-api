"""
Mock Card Management API
------------------------
Stands in for a real card management system so the Card Operations Co-Worker
can be built and tested before any live backend exists.

Designed for tool selection, not for REST tidiness. A Co-Worker picks a tool
by reading its description, so the surface is deliberately small: eleven
operations, each one an obvious answer to a different customer request.

  - Nothing is readable until send_otp + verify_otp return a session token.
  - Every call after that carries x-session-token.
  - One customer has exactly one card, so no card_id is ever passed.
  - Anything that changes the card needs a second, single use step up token,
    obtained from the same send_otp / verify_otp pair.
  - get_account returns everything readable in one call, so the Co-Worker
    does not have to chain four reads before it can answer.

State is held in memory. POST /admin/reset puts everything back.

Run:  uvicorn main:app --reload --port 8000
Spec: http://localhost:8000/openapi.json
Docs: http://localhost:8000/docs
"""

import copy
import os
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Dict, Optional
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

# Optional API key. Set API_KEY as an environment variable to require an
# x-api-key header on every request. Leave it unset and the API is open,
# which is fine locally but not for a public deployment.
API_KEY = os.getenv("API_KEY")

MONTHLY_LIMIT = 100000      # 1 lakh. Every spending limit lives inside 0..this.
BILL_DAY = 28               # Statement is due on the 28th.
SESSION_MINUTES = 30
OTP_MINUTES = 5
STEP_UP_MINUTES = 5
PIN_LINK_MINUTES = 15
EMI_MIN_AMOUNT = 2500
EMI_PROCESSING_FEE = 199
EMI_PLANS = {3: 13.0, 6: 14.0, 12: 15.0, 24: 16.0}
REPLACEMENT_FEE = 199
FIXED_OTP = "123456"        # mock only: any real system would generate this

app = FastAPI(
    title="Mock Card Management API",
    version="3.0.0",
    description=(
        "Card servicing for the Card Operations Co-Worker. Send an OTP, verify "
        "it for a session token, then use that token on every other call. "
        "Covers the account snapshot, transactions, limits and features, "
        "blocking, EMI, replacement, PIN change, and support tickets."
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


class TxnStatus(str, Enum):
    completed = "completed"
    pending = "pending"
    cancelled = "cancelled"


class OtpPurpose(str, Enum):
    login = "login"
    step_up = "step_up"


class PinResetStatus(str, Enum):
    pending = "pending"
    completed = "completed"
    expired = "expired"


# ------------------------------------------------------------- storage
# Three fake customers, one card each. Card numbers are stored masked only:
# the full PAN does not exist anywhere in this service, so it can never be
# returned.


def _day(offset: int) -> str:
    """A date `offset` days before today, so the data never looks stale."""
    return (date.today() - timedelta(days=offset)).isoformat()


SEED: Dict[str, dict] = {
    "CUST001": {
        "customer_id": "CUST001",
        "name": "Ananya Sharma",
        "mobile": "9876543210",
        "address": "42 Brigade Road, Bengaluru, Karnataka 560001",
        "card": {
            "card_id": "CARD1001",
            "masked_number": "**** **** **** 4521",
            "last_four": "4521",
            "card_type": "Visa Credit",
            "status": CardStatus.active,
            "limits": {"atm": 25000, "pos": 100000, "online": 50000,
                       "international": 0},
            "features": {"contactless": True, "online_payments": True,
                         "international_usage": False, "atm_withdrawals": True},
            "transactions": [
                {"transaction_id": "TXN9001", "date": _day(1),
                 "merchant": "Blue Tokai Coffee", "category": "Food and Beverage",
                 "amount": 480.00, "status": TxnStatus.completed},
                {"transaction_id": "TXN9002", "date": _day(2),
                 "merchant": "Netflix", "category": "Subscription",
                 "amount": 649.00, "status": TxnStatus.completed},
                {"transaction_id": "TXN9003", "date": _day(2),
                 "merchant": "UNKNOWN MERCHANT 44821", "category": "Unclassified",
                 "amount": 18999.00, "status": TxnStatus.pending},
                {"transaction_id": "TXN9004", "date": _day(3),
                 "merchant": "UNKNOWN MERCHANT 44821", "category": "Unclassified",
                 "amount": 4500.00, "status": TxnStatus.completed},
                {"transaction_id": "TXN9005", "date": _day(4),
                 "merchant": "More Supermarket", "category": "Groceries",
                 "amount": 2340.50, "status": TxnStatus.completed},
                {"transaction_id": "TXN9006", "date": _day(6),
                 "merchant": "Indian Oil", "category": "Fuel",
                 "amount": 3000.00, "status": TxnStatus.completed},
                {"transaction_id": "TXN9007", "date": _day(7),
                 "merchant": "MakeMyTrip", "category": "Travel",
                 "amount": 12800.00, "status": TxnStatus.cancelled},
                {"transaction_id": "TXN9008", "date": _day(9),
                 "merchant": "Amazon India", "category": "Retail",
                 "amount": 5499.00, "status": TxnStatus.completed},
            ],
        },
    },
    "CUST002": {
        "customer_id": "CUST002",
        "name": "Rohit Menon",
        "mobile": "9123456780",
        "address": "8 Anna Salai, Chennai, Tamil Nadu 600002",
        "card": {
            "card_id": "CARD2001",
            "masked_number": "**** **** **** 7710",
            "last_four": "7710",
            "card_type": "Visa Credit",
            "status": CardStatus.active,
            "limits": {"atm": 20000, "pos": 75000, "online": 75000,
                       "international": 50000},
            "features": {"contactless": False, "online_payments": True,
                         "international_usage": False, "atm_withdrawals": True},
            "transactions": [
                {"transaction_id": "TXN9201", "date": _day(1),
                 "merchant": "Swiggy", "category": "Food and Beverage",
                 "amount": 720.00, "status": TxnStatus.completed},
                {"transaction_id": "TXN9202", "date": _day(3),
                 "merchant": "UNKNOWN MERCHANT 71204", "category": "Unclassified",
                 "amount": 9200.00, "status": TxnStatus.pending},
                {"transaction_id": "TXN9203", "date": _day(5),
                 "merchant": "BESCOM Electricity", "category": "Utilities",
                 "amount": 2150.00, "status": TxnStatus.completed},
            ],
        },
    },
    "CUST003": {
        "customer_id": "CUST003",
        "name": "Priya Nair",
        "mobile": "9988776655",
        "address": "17 Marine Drive, Kochi, Kerala 682031",
        "card": {
            "card_id": "CARD3001",
            "masked_number": "**** **** **** 3390",
            "last_four": "3390",
            "card_type": "Mastercard Credit",
            "status": CardStatus.active,
            "limits": {"atm": 10000, "pos": 50000, "online": 40000,
                       "international": 0},
            "features": {"contactless": True, "online_payments": True,
                         "international_usage": False, "atm_withdrawals": False},
            "transactions": [
                {"transaction_id": "TXN9301", "date": _day(2),
                 "merchant": "Third Wave Coffee", "category": "Food and Beverage",
                 "amount": 390.00, "status": TxnStatus.completed},
                {"transaction_id": "TXN9302", "date": _day(4),
                 "merchant": "Apollo Pharmacy", "category": "Healthcare",
                 "amount": 845.50, "status": TxnStatus.completed},
                {"transaction_id": "TXN9303", "date": _day(8),
                 "merchant": "Uber", "category": "Travel",
                 "amount": 260.00, "status": TxnStatus.cancelled},
            ],
        },
    },
}

DB: Dict[str, dict] = copy.deepcopy(SEED)

SESSIONS: Dict[str, dict] = {}
OTP_STORE: Dict[str, dict] = {}
STEP_UP_TOKENS: Dict[str, dict] = {}
PIN_RESETS: Dict[str, dict] = {}
REPLACEMENTS: Dict[str, dict] = {}
TICKETS: Dict[str, dict] = {}
EMI_TAKEN: Dict[str, dict] = {}


# ------------------------------------------------------------- helpers


def _now() -> datetime:
    return datetime.utcnow()


def get_session(
    x_session_token: Optional[str] = Header(None, description="Session token from verify_otp"),
) -> dict:
    """Resolves the token to a customer, so no endpoint takes a customer_id
    or a card_id."""
    entry = SESSIONS.get(x_session_token) if x_session_token else None
    if not entry:
        raise HTTPException(status_code=401,
                            detail="No valid session. Run send_otp and verify_otp first.")
    if _now() > entry["expires_at"]:
        del SESSIONS[x_session_token]
        raise HTTPException(status_code=401,
                            detail="Session expired. Identify the customer again.")
    return DB[entry["customer_id"]]


def get_step_up_session(
    x_session_token: Optional[str] = Header(None, description="Session token from verify_otp"),
    x_step_up_token: Optional[str] = Header(None,
                                            description="Single use token from verify_otp"),
) -> dict:
    """For anything that changes the card. Consumes the step up token, so a
    fresh one is needed for each change."""
    cust = get_session(x_session_token)
    tok = STEP_UP_TOKENS.get(x_step_up_token) if x_step_up_token else None
    if not tok or tok["customer_id"] != cust["customer_id"]:
        raise HTTPException(
            status_code=401,
            detail=("This action needs step up verification. Call send_otp with the "
                    "session token, then verify_otp, and pass the step up token."))
    if _now() > tok["expires_at"]:
        del STEP_UP_TOKENS[x_step_up_token]
        raise HTTPException(status_code=401,
                            detail="Step up verification expired. Request a new OTP.")
    del STEP_UP_TOKENS[x_step_up_token]  # single use
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


def _usage(card: dict) -> float:
    """Completed and pending spend counts. Cancelled does not."""
    cyc = _cycle()
    total = 0.0
    for t in card["transactions"]:
        if t["status"] == TxnStatus.cancelled:
            continue
        if cyc["start"].isoformat() <= t["date"] <= cyc["end"].isoformat():
            total += t["amount"]
    return round(total, 2)


# -------------------------------------------------------------- models


class SendOtpRequest(BaseModel):
    identifier: Optional[str] = Field(
        None, description="Registered mobile number or customer ID. Required to start a "
                          "session, omit when stepping up inside an open session.",
        examples=["9876543210"])


class SendOtpResponse(BaseModel):
    otp_sent: bool
    purpose: OtpPurpose
    masked_mobile: str
    challenge_id: str
    message: str


class VerifyOtpRequest(BaseModel):
    challenge_id: str
    otp: str = Field(..., examples=["123456"])


class VerifyOtpResponse(BaseModel):
    verified: bool
    purpose: Optional[OtpPurpose] = None
    session_token: Optional[str] = Field(
        None, description="Send as x-session-token on every other call")
    step_up_token: Optional[str] = Field(
        None, description="Send as x-step-up-token on one change")
    customer_name: Optional[str] = None
    expires_in_minutes: Optional[int] = None
    message: str


class LimitsPayload(BaseModel):
    atm: Optional[int] = Field(None, ge=0, le=MONTHLY_LIMIT)
    pos: Optional[int] = Field(None, ge=0, le=MONTHLY_LIMIT)
    online: Optional[int] = Field(None, ge=0, le=MONTHLY_LIMIT)
    international: Optional[int] = Field(None, ge=0, le=MONTHLY_LIMIT)


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


class EmiRequest(BaseModel):
    tenure_months: int = Field(..., description="One of the tenures offered by get_account",
                               examples=[6])


class ReplacementRequest(BaseModel):
    delivery_address: str = Field(..., description="Confirmed with the customer first")


class TicketRequest(BaseModel):
    name: str = Field(..., examples=["Ananya Sharma"])
    address: str = Field(..., examples=["42 Brigade Road, Bengaluru, Karnataka 560001"])
    subject: Optional[str] = Field(None, description="What the ticket is about")


# ----------------------------------------------------------------- auth


@app.post("/auth/otp/send", response_model=SendOtpResponse, operation_id="send_otp",
          tags=["Session"], summary="Send an OTP to the registered mobile",
          description=(
              "Two uses. At the start of a conversation, pass the customer's registered "
              "mobile number or customer ID to identify them; no account data is "
              "returned, so nothing is exposed before verification. Inside an open "
              "session, pass the session token and no identifier to get a step up OTP "
              "before changing limits, changing features, blocking the card, or "
              "converting the bill to EMI. Pass the returned challenge_id to verify_otp."))
def send_otp(body: SendOtpRequest,
             x_session_token: Optional[str] = Header(None,
                                                     description="Session token, for step up only")):
    session = SESSIONS.get(x_session_token) if x_session_token else None
    challenge_id = f"CHL{uuid4().hex[:10].upper()}"

    if session and not body.identifier:
        cust = DB[session["customer_id"]]
        OTP_STORE[challenge_id] = {"customer_id": cust["customer_id"],
                                   "purpose": OtpPurpose.step_up, "otp": FIXED_OTP,
                                   "expires_at": _now() + timedelta(minutes=OTP_MINUTES)}
        return SendOtpResponse(
            otp_sent=True, purpose=OtpPurpose.step_up,
            masked_mobile=f"******{cust['mobile'][-4:]}", challenge_id=challenge_id,
            message=("A verification OTP has been sent to the number ending "
                     f"{cust['mobile'][-4:]}."))

    if not body.identifier:
        raise HTTPException(status_code=400,
                            detail="Supply the customer's mobile number or customer ID")

    cust = DB.get(body.identifier) or next(
        (c for c in DB.values() if c["mobile"] == body.identifier), None)
    # Always report success, so an unknown identifier cannot be used to
    # discover which numbers are registered.
    OTP_STORE[challenge_id] = {"customer_id": cust["customer_id"] if cust else None,
                               "purpose": OtpPurpose.login, "otp": FIXED_OTP,
                               "expires_at": _now() + timedelta(minutes=OTP_MINUTES)}
    masked = f"******{cust['mobile'][-4:]}" if cust else "******0000"
    return SendOtpResponse(otp_sent=True, purpose=OtpPurpose.login, masked_mobile=masked,
                           challenge_id=challenge_id,
                           message=f"An OTP has been sent to the number ending {masked[-4:]}.")


@app.post("/auth/otp/verify", response_model=VerifyOtpResponse, operation_id="verify_otp",
          tags=["Session"], summary="Verify an OTP and get a token",
          description=(
              "Use after the customer supplies the OTP they received. If the challenge "
              "was for identification it returns a session_token, which must be sent as "
              "x-session-token on every other call. If it was for step up it returns a "
              "step_up_token, single use, sent as x-step-up-token on one change. The "
              "Co-Worker never validates the OTP itself."))
def verify_otp(body: VerifyOtpRequest):
    entry = OTP_STORE.get(body.challenge_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Unknown or already used challenge")
    if _now() > entry["expires_at"]:
        return VerifyOtpResponse(verified=False,
                                 message="This OTP has expired. Send a new one.")
    if body.otp != entry["otp"] or entry["customer_id"] is None:
        return VerifyOtpResponse(verified=False, message="That OTP is not correct.")

    del OTP_STORE[body.challenge_id]  # single use
    cust = DB[entry["customer_id"]]

    if entry["purpose"] == OtpPurpose.step_up:
        token = f"STEP{uuid4().hex.upper()}"
        STEP_UP_TOKENS[token] = {"customer_id": cust["customer_id"],
                                 "expires_at": _now() + timedelta(minutes=STEP_UP_MINUTES)}
        return VerifyOtpResponse(
            verified=True, purpose=OtpPurpose.step_up, step_up_token=token,
            customer_name=cust["name"], expires_in_minutes=STEP_UP_MINUTES,
            message="Verified. This token covers one change.")

    token = f"SESS{uuid4().hex.upper()}"
    SESSIONS[token] = {"customer_id": cust["customer_id"],
                       "expires_at": _now() + timedelta(minutes=SESSION_MINUTES)}
    return VerifyOtpResponse(
        verified=True, purpose=OtpPurpose.login, session_token=token,
        customer_name=cust["name"], expires_in_minutes=SESSION_MINUTES,
        message="Verification successful. Use this session token on every other call.")


# ---------------------------------------------------------------- reads


@app.get("/account", operation_id="get_account", tags=["Reads"],
         summary="Get the whole account in one call",
         description=(
             "Use as the first call after verification, and whenever the customer asks "
             "about their card, spending, bill, due date, limits, features, EMI options, "
             "address, or recent transactions. Returns all of it together, so there is "
             "no need to chain several reads. Pass transaction_status to narrow the "
             "transaction list to completed, pending, or cancelled; leave it off to see "
             "everything. Read only. Call it again after any change rather than "
             "repeating figures from earlier in the conversation."))
def get_account(transaction_status: Optional[TxnStatus] = None,
                cust: dict = Depends(get_session)):
    card = cust["card"]
    used = _usage(card)
    cyc = _cycle()
    active_plan = EMI_TAKEN.get(card["card_id"])
    txns = card["transactions"]
    if transaction_status:
        txns = [t for t in txns if t["status"] == transaction_status]
    return {
        "customer": {"customer_id": cust["customer_id"], "name": cust["name"],
                     "masked_mobile": f"******{cust['mobile'][-4:]}",
                     "address": cust["address"]},
        "card": {"card_id": card["card_id"], "masked_number": card["masked_number"],
                 "card_type": card["card_type"], "status": card["status"]},
        "usage": {"currency": "INR", "monthly_limit": MONTHLY_LIMIT, "used": used,
                  "available": round(MONTHLY_LIMIT - used, 2),
                  "percent_used": round(used / MONTHLY_LIMIT * 100, 1),
                  "cycle_start": cyc["start"].isoformat(),
                  "cycle_end": cyc["end"].isoformat()},
        "bill": {"statement_amount": used, "minimum_due": round(used * 0.05, 2),
                 "due_date": cyc["end"].isoformat(),
                 "days_until_due": (cyc["end"] - date.today()).days, "status": "unpaid"},
        "emi": {"eligible": used >= EMI_MIN_AMOUNT and not active_plan,
                "minimum_bill_for_emi": EMI_MIN_AMOUNT,
                "processing_fee": EMI_PROCESSING_FEE,
                "options": [{"tenure_months": m, "interest_rate_annual_percent": r}
                            for m, r in sorted(EMI_PLANS.items())],
                "active_plan": active_plan},
        "limits": {**card["limits"], "min_limit": 0, "max_limit": MONTHLY_LIMIT},
        "features": card["features"],
        "transactions": {"count": len(txns), "filtered_by": transaction_status,
                         "items": txns},
    }




# -------------------------------------------------------------- changes
# Everything here needs x-step-up-token as well as x-session-token.


@app.patch("/card", operation_id="update_card", tags=["Changes"],
           summary="Change spending limits and card features",
           description=(
               "Use to change limits, switch features such as contactless, online "
               "payments, international usage, or ATM withdrawals, or both at once, "
               "after the customer has confirmed the new values and completed step up "
               "verification. Every limit must be between 0 and the monthly limit. Send "
               "all the changes the customer asked for in one call, so a single step up "
               "token covers them. Only the fields supplied are changed."))
def update_card(body: UpdateCardRequest, cust: dict = Depends(get_step_up_session)):
    card = cust["card"]
    if card["status"] == CardStatus.blocked:
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
    return {"updated": changed, "limits": card["limits"], "features": card["features"]}


@app.post("/card/block", operation_id="block_card", tags=["Changes"],
          summary="Block the card permanently",
          description=(
              "Use when the customer reports the card lost, stolen, or used "
              "fraudulently, after step up verification. This cannot be reversed by the "
              "Co-Worker: a blocked card can only be replaced, not reactivated. Check "
              "the card status from get_account first to avoid blocking a card that is "
              "already blocked."))
def block_card(body: BlockRequest, cust: dict = Depends(get_step_up_session)):
    card = cust["card"]
    if card["status"] == CardStatus.blocked:
        return {"status": card["status"], "already_blocked": True,
                "message": "This card was already blocked."}
    card["status"] = CardStatus.blocked
    card["block_reason"] = body.reason
    return {"status": card["status"], "already_blocked": False, "reason": body.reason,
            "message": f"Card ending {card['last_four']} has been blocked."}


@app.post("/card/emi", operation_id="convert_bill_to_emi", tags=["Changes"],
          summary="Convert the current bill to EMI",
          description=(
              "Use when the customer says the bill is too large to pay at once and has "
              "picked a tenure from the options in get_account, confirmed the interest "
              "rate and the processing fee, and completed step up verification. One "
              "active plan per card. Returns a reference for the customer."))
def convert_bill_to_emi(body: EmiRequest, cust: dict = Depends(get_step_up_session)):
    card = cust["card"]
    if body.tenure_months not in EMI_PLANS:
        raise HTTPException(status_code=400,
                            detail=f"Tenure must be one of {sorted(EMI_PLANS)} months")
    if card["card_id"] in EMI_TAKEN:
        raise HTTPException(status_code=409, detail="This bill is already on an EMI plan")
    amount = _usage(card)
    if amount < EMI_MIN_AMOUNT:
        raise HTTPException(
            status_code=409,
            detail=f"A bill of at least {EMI_MIN_AMOUNT} is needed to convert to EMI")

    ref = f"EMI{uuid4().hex[:8].upper()}"
    EMI_TAKEN[card["card_id"]] = {
        "reference": ref, "currency": "INR", "principal": amount,
        "tenure_months": body.tenure_months,
        "interest_rate_annual_percent": EMI_PLANS[body.tenure_months],
        "processing_fee": EMI_PROCESSING_FEE, "status": "active",
        "first_instalment_date": _cycle()["end"].isoformat()}
    return {**EMI_TAKEN[card["card_id"]],
            "message": (f"Bill of {amount} converted to {body.tenure_months} monthly "
                        f"instalments at {EMI_PLANS[body.tenure_months]}%. Reference {ref}.")}


# ------------------------------------------------------------ servicing


@app.post("/card/replacement", operation_id="request_replacement", tags=["Servicing"],
          summary="Request a replacement card",
          description=(
              "Use once the customer has confirmed the delivery address, which is in "
              "get_account. Returns a reference the customer can quote, and the "
              "replacement fee, which should be stated before they confirm."))
def request_replacement(body: ReplacementRequest, cust: dict = Depends(get_session)):
    ref = f"REP{uuid4().hex[:8].upper()}"
    REPLACEMENTS[ref] = {"reference": ref, "card_id": cust["card"]["card_id"],
                         "delivery_address": body.delivery_address, "status": "submitted",
                         "fee": REPLACEMENT_FEE, "currency": "INR",
                         "estimated_delivery_days": 7}
    return REPLACEMENTS[ref]


@app.post("/card/pin-reset", operation_id="initiate_pin_reset", tags=["Servicing"],
          summary="Generate a PIN change link",
          description=(
              "Use when the customer wants to change their PIN. Returns a one time link "
              "the customer opens themselves. The PIN is never sent to or received by "
              "this API. Give the customer the link, do not wait for them to finish, and "
              "check get_pin_reset_status when they say they are done."))
def initiate_pin_reset(request: Request, cust: dict = Depends(get_session)):
    card = cust["card"]
    if card["status"] == CardStatus.blocked:
        raise HTTPException(status_code=409, detail="Cannot reset the PIN on a blocked card")
    request_id = f"PIN{uuid4().hex[:8].upper()}"
    PIN_RESETS[request_id] = {"request_id": request_id, "customer_id": cust["customer_id"],
                              "pin_changed": False, "changed_at": None,
                              "expires_at": _now() + timedelta(minutes=PIN_LINK_MINUTES)}
    base = str(request.base_url).rstrip("/")
    return {"request_id": request_id, "secure_link": f"{base}/set-pin/{request_id}",
            "link_expires_in_minutes": PIN_LINK_MINUTES, "pin_changed": False,
            "message": "Send the customer this link. They set the PIN on the secure screen."}


@app.get("/card/pin-reset/{request_id}", operation_id="get_pin_reset_status",
         tags=["Servicing"], summary="Check whether the PIN was actually changed",
         description=(
             "Use to confirm the outcome of a PIN change started earlier. Returns "
             "pin_changed false until the customer opens the link and completes it, so "
             "the customer can be told whether it went through or whether the link "
             "expired and needs resending. Read only."))
def get_pin_reset_status(request_id: str, cust: dict = Depends(get_session)):
    entry = PIN_RESETS.get(request_id)
    if not entry or entry["customer_id"] != cust["customer_id"]:
        raise HTTPException(status_code=404, detail="Unknown PIN reset request")
    if entry["pin_changed"]:
        status = PinResetStatus.completed
    elif _now() > entry["expires_at"]:
        status = PinResetStatus.expired
    else:
        status = PinResetStatus.pending
    return {"request_id": request_id, "pin_changed": entry["pin_changed"],
            "status": status, "changed_at": entry["changed_at"]}


@app.post("/tickets", operation_id="raise_ticket", tags=["Servicing"],
          summary="Raise a support ticket",
          description=(
              "Use when the customer reports something the Co-Worker cannot resolve "
              "itself, such as a disputed or unrecognised transaction, or a complaint. "
              "Confirm the name and address with the customer first, both are in "
              "get_account. Returns a ticket number to give to the customer. Do not "
              "speculate about the outcome or promise a refund."))
def raise_ticket(body: TicketRequest, cust: dict = Depends(get_session)):
    number = f"TKT{uuid4().hex[:8].upper()}"
    TICKETS[number] = {"ticket_number": number, "customer_id": cust["customer_id"],
                       "name": body.name, "address": body.address, "subject": body.subject,
                       "status": "open", "assigned_to": "support_queue",
                       "created_at": _now().isoformat() + "Z"}
    return {**TICKETS[number],
            "message": f"Ticket {number} has been raised. Quote this number on any follow up."}


# ------------------------------------------------------- testing tools
# reset_mock_data and health_check are in the spec, tagged Testing. The PIN
# link below is not: it is the customer's own screen, and keeping it out of
# the document means the Co-Worker has no tool that can complete a PIN
# change on the customer's behalf.


@app.get("/set-pin/{request_id}", include_in_schema=False)
def set_pin_page(request_id: str):
    """The customer facing screen the PIN link points at. Opening it is what
    flips pin_changed to true. No PIN is accepted or stored, since no PIN
    exists anywhere in this service."""
    entry = PIN_RESETS.get(request_id)
    if not entry:
        return HTMLResponse(_pin_page("Link not recognised",
                                      "This PIN change link is not valid."), status_code=404)
    if not entry["pin_changed"] and _now() > entry["expires_at"]:
        return HTMLResponse(_pin_page("Link expired",
                                      "Ask us to send a new PIN change link."), status_code=410)
    if not entry["pin_changed"]:
        entry["pin_changed"] = True
        entry["changed_at"] = _now().isoformat() + "Z"
    last_four = DB[entry["customer_id"]]["card"]["last_four"]
    return HTMLResponse(_pin_page(
        "PIN changed",
        f"The PIN for the card ending {last_four} has been updated. You can close this page."))


def _pin_page(heading: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{heading}</title>"
            f"</head><body style='font-family:system-ui;max-width:32rem;margin:4rem auto;"
            f"line-height:1.5'><h1>{heading}</h1><p>{body}</p></body></html>")


@app.post("/admin/reset", operation_id="reset_mock_data", tags=["Testing"],
          summary="Testing only: restore all mock data",
          description=(
              "Testing utility, not part of any customer conversation. Restores cards, "
              "limits, features, transactions, and EMI plans to their starting state and "
              "clears every session, ticket, replacement, and PIN reset. Never call this "
              "while helping a customer: it ends their session and undoes changes they "
              "have already been told were made. Only call it when the person explicitly "
              "asks to reset the test data."))
def reset_mock_data():
    global DB
    DB = copy.deepcopy(SEED)
    for store in (SESSIONS, OTP_STORE, STEP_UP_TOKENS, PIN_RESETS, REPLACEMENTS,
                  TICKETS, EMI_TAKEN):
        store.clear()
    return {"reset": True, "customers": len(DB),
            "message": "Mock data restored to its starting state."}


@app.get("/health", operation_id="health_check", tags=["Testing"],
         summary="Check the service is up",
         description=(
             "Returns the service status and how many customers and open sessions exist. "
             "Needs no session token. Use it to confirm the connection is working, for "
             "example after an error, or to wake a sleeping instance before a "
             "conversation. It returns no customer data."))
def health_check():
    return {"status": "ok", "customers": len(DB), "active_sessions": len(SESSIONS)}


# ---------------------------------------------------- spec for the tool
# FastAPI emits OpenAPI 3.1, where an optional field becomes
# anyOf [type, null]. Older spec parsers handle that badly, and an auth
# header marked "required: false" invites the model to omit it and then
# guess at the 401. Both are fixed here, in the document only: the runtime
# still returns a clean 401 rather than a validation error.


def _simplify(node):
    """Collapse 3.1 nullable unions and example lists into 3.0 equivalents."""
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
    schema = get_openapi(title=app.title, version=app.version,
                         description=app.description, routes=app.routes)
    schema = _simplify(schema)
    for path in schema["paths"].values():
        for op in path.values():
            # send_otp is the one place the session header really is optional:
            # it is absent when identifying, present when stepping up.
            if op.get("operationId") == "send_otp":
                continue
            for param in op.get("parameters", []):
                if param.get("in") == "header":
                    param["required"] = True
    schema["openapi"] = "3.0.3"
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
