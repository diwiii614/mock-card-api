"""
Mock Card Management API
------------------------
Stands in for a real card management system so the Card Operations Coworker
can be built and tested before any live backend exists.

Shape of the API:
  - Nothing is readable until identify + verify_otp return a session token.
  - Every call after that carries x-session-token.
  - One customer has exactly one card, so no card_id is ever passed:
    the session determines the card.
  - Anything that changes the card needs a second, single use step up token.

State is held in memory, so blocking a card really does change its status
within a session. POST /admin/reset puts everything back.

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
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

# Optional API key. Set API_KEY as an environment variable to require an
# x-api-key header on every request. Leave it unset and the API is open,
# which is fine locally but not for a public deployment.
API_KEY = os.getenv("API_KEY")

MONTHLY_LIMIT = 100000      # 1 lakh. Every spending limit lives inside 0..this.
BILL_DAY = 28               # Statement is due on the 28th.
SESSION_MINUTES = 30
PIN_LINK_MINUTES = 15       # How long a PIN change link stays usable.
EMI_MIN_AMOUNT = 2500       # Banks will not convert a trivial bill to EMI.
EMI_PROCESSING_FEE = 199    # One time, charged on the next statement.

# Tenure to annual rate, in the range Indian card issuers actually quote for
# balance conversion. Longer tenure, higher rate.
EMI_PLANS = {3: 13.0, 6: 14.0, 12: 15.0, 24: 16.0}
OTP_MINUTES = 5
STEP_UP_MINUTES = 5
FIXED_OTP = "123456"        # mock only: any real system would generate this

app = FastAPI(
    title="Mock Card Management API",
    version="2.0.0",
    description=(
        "Card servicing operations for the Card Operations Coworker. "
        "Identify the customer, verify an OTP, and use the returned session "
        "token on every other call. Covers usage against the monthly limit, "
        "billing, transactions, limits, feature toggles, blocking, "
        "replacement, PIN reset, and support tickets."
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


class PinResetStatus(str, Enum):
    pending = "pending"
    completed = "completed"
    expired = "expired"


class TicketStatus(str, Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"


# ------------------------------------------------------------- storage
# Two fake customers, one card each. Card numbers are stored masked only:
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
EMI_PLANS_TAKEN: Dict[str, dict] = {}
TICKETS: Dict[str, dict] = {}


# ------------------------------------------------------------- helpers


def _now() -> datetime:
    return datetime.utcnow()


def _find_customer_by_identifier(identifier: str) -> Optional[dict]:
    if identifier in DB:
        return DB[identifier]
    for cust in DB.values():
        if cust["mobile"] == identifier:
            return cust
    return None


def get_session(
    x_session_token: Optional[str] = Header(None, description="Session token from verify_otp"),
) -> dict:
    """Every endpoint below the auth section depends on this. It resolves the
    token to a customer, so no endpoint takes a customer_id or card_id."""
    entry = SESSIONS.get(x_session_token) if x_session_token else None
    if not entry:
        raise HTTPException(
            status_code=401,
            detail="No valid session. Run identify_customer and verify_otp first.")
    if _now() > entry["expires_at"]:
        del SESSIONS[x_session_token]
        raise HTTPException(status_code=401,
                            detail="Session expired. Identify the customer again.")
    return DB[entry["customer_id"]]


def get_step_up_session(
    x_session_token: Optional[str] = Header(None, description="Session token from verify_otp"),
    x_step_up_token: Optional[str] = Header(None,
                                            description="Single use token from verify_step_up"),
) -> dict:
    """For anything that changes the card. Consumes the step up token, so a
    fresh one is needed for each change."""
    cust = get_session(x_session_token)
    tok = STEP_UP_TOKENS.get(x_step_up_token) if x_step_up_token else None
    if not tok or tok["customer_id"] != cust["customer_id"]:
        raise HTTPException(
            status_code=401,
            detail=("This action needs step up verification. "
                    "Run request_step_up and verify_step_up."))
    if _now() > tok["expires_at"]:
        del STEP_UP_TOKENS[x_step_up_token]
        raise HTTPException(status_code=401,
                            detail="Step up verification expired. Request a new one.")
    del STEP_UP_TOKENS[x_step_up_token]  # single use
    return cust


def _cycle() -> dict:
    """The statement cycle ends on BILL_DAY. Anything after that day rolls
    into the next cycle."""
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


class IdentifyRequest(BaseModel):
    identifier: str = Field(..., description="Registered mobile number or customer ID",
                            examples=["9876543210"])


class IdentifyResponse(BaseModel):
    otp_sent: bool
    masked_mobile: str = Field(...,
                               description="Masked registered number, for the customer to confirm")
    challenge_id: str
    message: str


class VerifyOtpRequest(BaseModel):
    challenge_id: str
    otp: str = Field(..., examples=["123456"])


class VerifyOtpResponse(BaseModel):
    verified: bool
    session_token: Optional[str] = Field(
        None, description="Send as x-session-token on every other call")
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    expires_in_minutes: Optional[int] = None
    message: str


class StepUpResponse(BaseModel):
    otp_sent: bool
    challenge_id: str
    message: str


class VerifyStepUpResponse(BaseModel):
    verified: bool
    step_up_token: Optional[str] = Field(
        None, description="Send as x-step-up-token on the next change. Single use.")
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


class BlockRequest(BaseModel):
    reason: BlockReason


class ReplacementRequestBody(BaseModel):
    delivery_address: str = Field(..., description="Confirmed with the customer first")


class EmiConversionRequest(BaseModel):
    tenure_months: int = Field(..., description="One of the tenures from get_emi_options",
                               examples=[6])


class TicketRequest(BaseModel):
    name: str = Field(..., examples=["Ananya Sharma"])
    address: str = Field(..., examples=["42 Brigade Road, Bengaluru, Karnataka 560001"])
    subject: Optional[str] = Field(None, description="What the ticket is about")


# ------------------------------------------------- identity and session


@app.post("/auth/identify", response_model=IdentifyResponse, operation_id="identify_customer",
          tags=["Session"], summary="Look up a customer and send an OTP",
          description=(
              "Use at the very start of a conversation, once the customer has given a "
              "registered mobile number or customer ID. Sends an OTP and returns the "
              "number masked so the customer can confirm it is theirs. Returns no "
              "account data, so nothing is exposed before verification."))
def identify_customer(body: IdentifyRequest):
    cust = _find_customer_by_identifier(body.identifier)
    challenge_id = f"CHL{uuid4().hex[:10].upper()}"

    # Always report success, so an unknown identifier cannot be used to
    # discover which numbers are registered.
    OTP_STORE[challenge_id] = {
        "customer_id": cust["customer_id"] if cust else None,
        "purpose": "login",
        "otp": FIXED_OTP,
        "expires_at": _now() + timedelta(minutes=OTP_MINUTES),
    }
    masked = f"******{cust['mobile'][-4:]}" if cust else "******0000"
    return IdentifyResponse(
        otp_sent=True, masked_mobile=masked, challenge_id=challenge_id,
        message=f"An OTP has been sent to the number ending {masked[-4:]}.")


@app.post("/auth/verify-otp", response_model=VerifyOtpResponse, operation_id="verify_otp",
          tags=["Session"], summary="Verify the OTP and open a session",
          description=(
              "Use after the customer supplies the OTP they received. On success it "
              "returns a session token, which must be sent as x-session-token on every "
              "other call. Nothing else in this API works without it. The coworker never "
              "validates the OTP itself."))
def verify_otp(body: VerifyOtpRequest):
    entry = OTP_STORE.get(body.challenge_id)
    if not entry or entry["purpose"] != "login":
        raise HTTPException(status_code=404, detail="Unknown or already used challenge")
    if _now() > entry["expires_at"]:
        return VerifyOtpResponse(verified=False,
                                 message="This OTP has expired. Request a new one.")
    if body.otp != entry["otp"] or entry["customer_id"] is None:
        return VerifyOtpResponse(verified=False, message="That OTP is not correct.")

    cust = DB[entry["customer_id"]]
    del OTP_STORE[body.challenge_id]  # single use
    token = f"SESS{uuid4().hex.upper()}"
    SESSIONS[token] = {"customer_id": cust["customer_id"],
                       "expires_at": _now() + timedelta(minutes=SESSION_MINUTES)}
    return VerifyOtpResponse(
        verified=True, session_token=token, customer_id=cust["customer_id"],
        customer_name=cust["name"], expires_in_minutes=SESSION_MINUTES,
        message="Verification successful. Use this session token on every other call.")


@app.post("/auth/step-up", response_model=StepUpResponse, operation_id="request_step_up",
          tags=["Session"], summary="Send a second OTP before a change",
          description=(
              "Use before changing limits, changing features, or blocking the card. "
              "Sends a fresh OTP to the registered number. Reads never need this."))
def request_step_up(cust: dict = Depends(get_session)):
    challenge_id = f"CHL{uuid4().hex[:10].upper()}"
    OTP_STORE[challenge_id] = {"customer_id": cust["customer_id"], "purpose": "step_up",
                               "otp": FIXED_OTP,
                               "expires_at": _now() + timedelta(minutes=OTP_MINUTES)}
    return StepUpResponse(
        otp_sent=True, challenge_id=challenge_id,
        message=f"A verification OTP has been sent to the number ending {cust['mobile'][-4:]}.")


@app.post("/auth/verify-step-up", response_model=VerifyStepUpResponse,
          operation_id="verify_step_up", tags=["Session"],
          summary="Verify the step up OTP and get a change token",
          description=(
              "Use after the customer supplies the step up OTP. Returns a step up token "
              "to send as x-step-up-token on the next change. The token is single use, "
              "so batch related changes into one call rather than asking for several "
              "OTPs."))
def verify_step_up(body: VerifyOtpRequest, cust: dict = Depends(get_session)):
    entry = OTP_STORE.get(body.challenge_id)
    if not entry or entry["purpose"] != "step_up":
        raise HTTPException(status_code=404, detail="Unknown or already used challenge")
    if _now() > entry["expires_at"]:
        return VerifyStepUpResponse(verified=False,
                                    message="This OTP has expired. Request a new one.")
    if body.otp != entry["otp"] or entry["customer_id"] != cust["customer_id"]:
        return VerifyStepUpResponse(verified=False, message="That OTP is not correct.")

    del OTP_STORE[body.challenge_id]
    token = f"STEP{uuid4().hex.upper()}"
    STEP_UP_TOKENS[token] = {"customer_id": cust["customer_id"],
                             "expires_at": _now() + timedelta(minutes=STEP_UP_MINUTES)}
    return VerifyStepUpResponse(verified=True, step_up_token=token,
                                expires_in_minutes=STEP_UP_MINUTES,
                                message="Verified. This token covers one change.")


# -------------------------------------------------------------- reads


@app.get("/card", operation_id="get_card", tags=["Reads"],
         summary="Get the card and whether it is usable",
         description=(
             "Use to see which card the session belongs to and whether it is active or "
             "blocked. Call before blocking, to avoid blocking a card that is already "
             "blocked. Returns the last four digits only, never a full card number."))
def get_card(cust: dict = Depends(get_session)):
    card = cust["card"]
    return {"card_id": card["card_id"], "masked_number": card["masked_number"],
            "card_type": card["card_type"], "status": card["status"],
            "customer_name": cust["name"]}


@app.get("/card/usage", operation_id="get_usage", tags=["Reads"],
         summary="Get spend this cycle against the monthly limit",
         description=(
             "Use whenever the customer asks how much they have spent, how much is left, "
             "or whether they are near their limit. Counts completed and pending "
             "transactions in the current cycle; cancelled ones do not count. Read only. "
             "Always call this fresh rather than repeating a figure from earlier."))
def get_usage(cust: dict = Depends(get_session)):
    card = cust["card"]
    used = _usage(card)
    cyc = _cycle()
    return {"card_id": card["card_id"], "currency": "INR",
            "monthly_limit": MONTHLY_LIMIT, "used": used,
            "available": round(MONTHLY_LIMIT - used, 2),
            "percent_used": round(used / MONTHLY_LIMIT * 100, 1),
            "cycle_start": cyc["start"].isoformat(), "cycle_end": cyc["end"].isoformat()}


@app.get("/card/bill", operation_id="get_bill", tags=["Reads"],
         summary="Get the bill amount and due date",
         description=(
             "Use when the customer asks when their bill is due, how much is due, or "
             "what the minimum payment is. The bill is due on the 28th of the cycle. "
             "Read only."))
def get_bill(cust: dict = Depends(get_session)):
    card = cust["card"]
    amount = _usage(card)
    cyc = _cycle()
    return {"card_id": card["card_id"], "currency": "INR",
            "statement_amount": amount,
            "minimum_due": round(amount * 0.05, 2),
            "bill_due_date": cyc["end"].isoformat(),
            "days_until_due": (cyc["end"] - date.today()).days,
            "cycle_start": cyc["start"].isoformat(), "cycle_end": cyc["end"].isoformat(),
            "status": "unpaid"}


@app.get("/card/emi-options", operation_id="get_emi_options", tags=["EMI"],
         summary="List EMI plans available on the current bill",
         description=(
             "Use when the customer says the bill is too large to pay at once, or asks "
             "about instalments. Returns the tenures available and the annual interest "
             "rate on each, plus the one time processing fee. Read only, nothing is "
             "committed. Read the options out so the customer can pick a tenure."))
def get_emi_options(cust: dict = Depends(get_session)):
    card = cust["card"]
    amount = _usage(card)
    existing = EMI_PLANS_TAKEN.get(card["card_id"])
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"This bill is already on an EMI plan, reference {existing['reference']}")
    if amount < EMI_MIN_AMOUNT:
        raise HTTPException(
            status_code=409,
            detail=f"A bill of at least {EMI_MIN_AMOUNT} is needed to convert to EMI")
    return {"card_id": card["card_id"], "currency": "INR",
            "bill_amount": amount,
            "processing_fee": EMI_PROCESSING_FEE,
            "options": [{"tenure_months": m, "interest_rate_annual_percent": r}
                        for m, r in sorted(EMI_PLANS.items())]}


@app.post("/card/emi", operation_id="convert_bill_to_emi", tags=["EMI"],
          summary="Convert the current bill to EMI",
          description=(
              "Use after the customer has picked a tenure from get_emi_options and "
              "confirmed the rate and the processing fee. Needs step up verification. "
              "Returns a reference for the customer."))
def convert_bill_to_emi(body: EmiConversionRequest, cust: dict = Depends(get_step_up_session)):
    card = cust["card"]
    if body.tenure_months not in EMI_PLANS:
        raise HTTPException(status_code=400,
                            detail=f"Tenure must be one of {sorted(EMI_PLANS)} months")
    if card["card_id"] in EMI_PLANS_TAKEN:
        raise HTTPException(status_code=409, detail="This bill is already on an EMI plan")
    amount = _usage(card)
    if amount < EMI_MIN_AMOUNT:
        raise HTTPException(
            status_code=409,
            detail=f"A bill of at least {EMI_MIN_AMOUNT} is needed to convert to EMI")

    ref = f"EMI{uuid4().hex[:8].upper()}"
    EMI_PLANS_TAKEN[card["card_id"]] = {
        "reference": ref, "card_id": card["card_id"], "currency": "INR",
        "principal": amount, "tenure_months": body.tenure_months,
        "interest_rate_annual_percent": EMI_PLANS[body.tenure_months],
        "processing_fee": EMI_PROCESSING_FEE, "status": "active",
        "first_instalment_date": _cycle()["end"].isoformat()}
    return {**EMI_PLANS_TAKEN[card["card_id"]],
            "message": (f"Bill of {amount} converted to {body.tenure_months} monthly "
                        f"instalments at {EMI_PLANS[body.tenure_months]}%. Reference {ref}.")}


@app.get("/card/transactions", operation_id="get_transactions", tags=["Reads"],
         summary="List recent transactions",
         description=(
             "Use when the customer asks to see recent activity or asks about a specific "
             "charge. Returns date, merchant, category, amount, and status. Pass status "
             "to narrow to completed, pending, or cancelled. Read only."))
def get_transactions(status: Optional[TxnStatus] = None, cust: dict = Depends(get_session)):
    card = cust["card"]
    txns = card["transactions"]
    if status:
        txns = [t for t in txns if t["status"] == status]
    return {"card_id": card["card_id"], "count": len(txns), "transactions": txns}


@app.get("/card/limits", operation_id="get_limits", tags=["Reads"],
         summary="Get current spending limits",
         description=(
             "Use when the customer asks what their limits are, and before changing them "
             "so the current values can be shown alongside the new ones. Every limit "
             "sits between 0 and the monthly limit. Read only."))
def get_limits(cust: dict = Depends(get_session)):
    card = cust["card"]
    return {"card_id": card["card_id"], "currency": "INR", "limits": card["limits"],
            "min_limit": 0, "max_limit": MONTHLY_LIMIT}


@app.get("/card/features", operation_id="get_card_features", tags=["Reads"],
         summary="Get current on/off state of card features",
         description=(
             "Use when the customer asks about card features, or before changing them. "
             "Returns every feature and its current state together, so the customer can "
             "select several changes at once. Read only."))
def get_card_features(cust: dict = Depends(get_session)):
    card = cust["card"]
    return {"card_id": card["card_id"], "features": card["features"]}


@app.get("/card/address", operation_id="get_customer_address", tags=["Reads"],
         summary="Get the delivery address on record",
         description=(
             "Use during a replacement request or when raising a ticket, so the address "
             "on file can be confirmed with the customer. Read only."))
def get_customer_address(cust: dict = Depends(get_session)):
    return {"customer_id": cust["customer_id"], "name": cust["name"],
            "address": cust["address"]}


# ------------------------------------------------------------- changes
# Everything here needs x-step-up-token as well as x-session-token.


@app.put("/card/limits", operation_id="update_limits", tags=["Changes"],
         summary="Change spending limits",
         description=(
             "Use to change one or more spending limits after the customer has confirmed "
             "the new values and completed step up verification. Each value must be "
             "between 0 and the monthly limit. Several limits can be sent in one call, "
             "so one step up token covers the whole set. Only the fields supplied are "
             "changed."))
def update_limits(body: LimitsPayload, cust: dict = Depends(get_step_up_session)):
    card = cust["card"]
    if card["status"] == CardStatus.blocked:
        raise HTTPException(status_code=409, detail="Cannot change limits on a blocked card")
    changed = {}
    for field, value in body.model_dump(exclude_none=True).items():
        card["limits"][field] = value
        changed[field] = value
    if not changed:
        raise HTTPException(status_code=400, detail="No limit values supplied")
    return {"card_id": card["card_id"], "updated": changed, "limits": card["limits"]}


@app.put("/card/features", operation_id="update_card_features", tags=["Changes"],
         summary="Turn card features on or off",
         description=(
             "Use to switch features such as contactless, online payments, international "
             "usage, or ATM withdrawals after step up verification. Several toggles can "
             "be sent in one call, so one step up token covers the whole set. Only the "
             "fields supplied are changed."))
def update_card_features(body: FeaturesPayload, cust: dict = Depends(get_step_up_session)):
    card = cust["card"]
    if card["status"] == CardStatus.blocked:
        raise HTTPException(status_code=409, detail="Cannot change features on a blocked card")
    changed = {}
    for field, value in body.model_dump(exclude_none=True).items():
        card["features"][field] = value
        changed[field] = value
    if not changed:
        raise HTTPException(status_code=400, detail="No feature values supplied")
    return {"card_id": card["card_id"], "updated": changed, "features": card["features"]}


@app.post("/card/block", operation_id="block_card", tags=["Changes"],
          summary="Block the card permanently",
          description=(
              "Use when the customer reports the card lost, stolen, or used "
              "fraudulently, after step up verification. This cannot be reversed by the "
              "coworker: a blocked card can only be replaced, not reactivated. Call "
              "get_card first to avoid blocking a card that is already blocked."))
def block_card(body: BlockRequest, cust: dict = Depends(get_step_up_session)):
    card = cust["card"]
    if card["status"] == CardStatus.blocked:
        return {"card_id": card["card_id"], "status": card["status"], "already_blocked": True,
                "message": "This card was already blocked."}
    card["status"] = CardStatus.blocked
    card["block_reason"] = body.reason
    return {"card_id": card["card_id"], "status": card["status"], "already_blocked": False,
            "reason": body.reason,
            "message": f"Card ending {card['last_four']} has been blocked."}


# -------------------------------------------------- replacement and PIN


@app.post("/card/replacement", operation_id="request_replacement", tags=["Servicing"],
          summary="Request a replacement card",
          description=(
              "Use once the customer has confirmed the delivery address. Returns a "
              "reference the customer can quote, and the replacement fee, which should "
              "be stated to the customer before they confirm."))
def request_replacement(body: ReplacementRequestBody, cust: dict = Depends(get_session)):
    card = cust["card"]
    ref = f"REP{uuid4().hex[:8].upper()}"
    REPLACEMENTS[ref] = {"reference": ref, "card_id": card["card_id"],
                         "delivery_address": body.delivery_address,
                         "status": "submitted", "fee": 199, "currency": "INR",
                         "estimated_delivery_days": 7}
    return REPLACEMENTS[ref]


@app.post("/card/pin-reset", operation_id="initiate_pin_reset", tags=["PIN"],
          summary="Generate a PIN change link",
          description=(
              "Use when the customer wants to change their PIN. This only starts the "
              "process: it returns a one time link the customer opens themselves to set "
              "the PIN. The PIN is never sent to or received by this API. Do not wait "
              "for the customer to finish, give them the link and check "
              "get_pin_reset_status later."))
def initiate_pin_reset(request: Request, cust: dict = Depends(get_session)):
    card = cust["card"]
    if card["status"] == CardStatus.blocked:
        raise HTTPException(status_code=409, detail="Cannot reset the PIN on a blocked card")
    request_id = f"PIN{uuid4().hex[:8].upper()}"
    PIN_RESETS[request_id] = {"request_id": request_id, "card_id": card["card_id"],
                              "customer_id": cust["customer_id"], "pin_changed": False,
                              "changed_at": None,
                              "expires_at": _now() + timedelta(minutes=PIN_LINK_MINUTES)}
    base = str(request.base_url).rstrip("/")
    return {"request_id": request_id, "card_id": card["card_id"],
            "secure_link": f"{base}/set-pin/{request_id}",
            "link_expires_in_minutes": PIN_LINK_MINUTES,
            "pin_changed": False,
            "message": "Send the customer this link. They set the PIN on the secure screen."}


@app.get("/card/pin-reset/{request_id}", operation_id="get_pin_reset_status", tags=["PIN"],
         summary="Check whether the PIN was actually changed",
         description=(
             "Use to confirm the outcome of a PIN reset started earlier. Returns "
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
    return {"request_id": request_id, "card_id": entry["card_id"],
            "pin_changed": entry["pin_changed"], "status": status,
            "changed_at": entry["changed_at"],
            "expires_at": entry["expires_at"].isoformat() + "Z"}


@app.get("/set-pin/{request_id}", include_in_schema=False)
def set_pin_page(request_id: str):
    """The customer facing screen the secure link points at.

    Deliberately kept out of the OpenAPI spec, so it is not extractable as a
    tool: the coworker cannot mark a PIN change as done on the customer's
    behalf. Opening it in a browser is what flips pin_changed to true. No PIN
    is accepted or stored here, since no PIN exists anywhere in this service.
    """
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
    return HTMLResponse(_pin_page(
        "PIN changed",
        f"The PIN for card ending {DB[entry['customer_id']]['card']['last_four']} has been "
        f"updated. You can close this page."))


def _pin_page(heading: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{heading}</title></head>"
            f"<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;"
            f"line-height:1.5'><h1>{heading}</h1><p>{body}</p></body></html>")


# ------------------------------------------------------------- tickets


@app.post("/tickets", operation_id="raise_ticket", tags=["Tickets"],
          summary="Raise a support ticket",
          description=(
              "Use when the customer reports something the coworker cannot resolve "
              "itself, such as a disputed or unrecognised transaction, or a complaint. "
              "Confirm the name and address with the customer first. Returns a ticket "
              "number to give to the customer."))
def raise_ticket(body: TicketRequest, cust: dict = Depends(get_session)):
    number = f"TKT{uuid4().hex[:8].upper()}"
    TICKETS[number] = {"ticket_number": number, "customer_id": cust["customer_id"],
                       "name": body.name, "address": body.address,
                       "subject": body.subject, "status": TicketStatus.open,
                       "assigned_to": "support_queue",
                       "created_at": _now().isoformat() + "Z"}
    return {**TICKETS[number],
            "message": f"Ticket {number} has been raised. Quote this number on any follow up."}


@app.get("/tickets/{ticket_number}", operation_id="get_ticket", tags=["Tickets"],
         summary="Check the status of a ticket",
         description=(
             "Use when the customer asks about a ticket they raised earlier. Report the "
             "status as returned, and do not speculate about the outcome. Read only."))
def get_ticket(ticket_number: str, cust: dict = Depends(get_session)):
    ticket = TICKETS.get(ticket_number)
    if not ticket or ticket["customer_id"] != cust["customer_id"]:
        raise HTTPException(status_code=404, detail="Unknown ticket number")
    return ticket


# ------------------------------------------------------------- testing


@app.post("/admin/reset", operation_id="reset_mock_data", tags=["Testing"],
          summary="Testing only: reset all mock data",
          description=(
              "Not for the coworker to call. Restores cards, limits, features, and "
              "transactions to their starting state and clears every session, ticket, "
              "replacement, and PIN reset."))
def reset_mock_data():
    global DB
    DB = copy.deepcopy(SEED)
    for store in (SESSIONS, OTP_STORE, STEP_UP_TOKENS, PIN_RESETS, REPLACEMENTS,
                  TICKETS, EMI_PLANS_TAKEN):
        store.clear()
    return {"reset": True, "customers": len(DB),
            "message": "Mock data restored to its starting state."}


@app.get("/health", operation_id="health_check", tags=["Testing"], summary="Health check")
def health_check():
    return {"status": "ok", "customers": len(DB), "active_sessions": len(SESSIONS)}
