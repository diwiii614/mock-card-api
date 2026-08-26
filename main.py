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
import json
import os
from collections import deque
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

# Optional API key. Set API_KEY to require an x-api-key header on every
# request. Leave it unset and the API is open, which is fine for a mock.
API_KEY = os.getenv("API_KEY")

# The PIN link is built from the incoming request, so it is correct both
# locally and once deployed. Set PUBLIC_BASE_URL to override.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")

# Sessions are enforced by default: an endpoint will not serve a customer's
# data until that customer has verified an OTP. Set ENFORCE_SESSION=false to
# turn it off while poking at the API with curl.
ENFORCE_SESSION = os.getenv("ENFORCE_SESSION", "true").lower() != "false"
VERIFICATION_MINUTES = 15        # how long a verified session stays usable
OTP_ATTEMPTS = 3            # wrong codes allowed before the session is burned

MONTHLY_LIMIT = 100000      # 1 lakh. Every limit sits in 1..this.
BILL_DAY = 28
OTP_MINUTES = 10            # how long an unverified session can still be verified
PIN_LINK_MINUTES = 15
REPLACEMENT_FEE = 199
EMI_PLANS = {3: 13.0, 6: 14.0, 12: 15.0, 24: 16.0}
FIXED_OTP = "123456"        # mock only

app = FastAPI(
    title="Mock Card Management API",
    version="5.0.0",
    description=(
        "Card servicing for the Card Operations Co-Worker. Identify the customer "
        "by mobile number and verify the OTP: that returns their customer_id and "
        "card_id and unlocks their data for 30 minutes. Every other endpoint "
        "returns 401 until it has. One customer, one card."
    ),
)


# The last REQUEST_LOG_SIZE calls, with the exact arguments as received.
# Read it at GET /admin/requests. This exists so you can see what an agent
# platform actually sent, rather than inferring it from an error message.
REQUEST_LOG_SIZE = 20
REQUEST_LOG: "deque[dict]" = deque(maxlen=REQUEST_LOG_SIZE)


@app.middleware("http")
async def log_request(request: Request, call_next):
    body_text = ""
    if request.method in ("POST", "PATCH", "PUT"):
        raw = await request.body()
        body_text = raw.decode("utf-8", "replace")[:2000]

        # Put the body back, or the endpoint reads an empty stream.
        async def _receive():
            return {"type": "http.request", "body": raw, "more_body": False}
        request._receive = _receive

    response = await call_next(request)

    if not request.url.path.startswith("/admin"):
        try:
            parsed = json.loads(body_text) if body_text else None
        except json.JSONDecodeError:
            parsed = body_text or None
        # Every header, so it can be seen whether the calling platform
        # forwards a stable per-conversation identifier of its own. If it
        # does, the verification register can be keyed on that instead, and
        # the model stops carrying an id altogether.
        headers = {k.lower(): v[:200] for k, v in request.headers.items()
                   if k.lower() not in ("authorization", "cookie", "x-api-key")}
        interesting = {k: v for k, v in headers.items()
                       if k != "user-agent" and any(
                           w in k for w in ("session", "conversation", "thread", "trace",
                                            "correlation", "request-id", "chat", "run",
                                            "agent", "neo", "flow", "invocation"))}
        REQUEST_LOG.append({
            "at": datetime.utcnow().isoformat() + "Z",
            "method": request.method,
            "path": request.url.path,
            "query": dict(request.query_params),
            "body": parsed,
            "status": response.status_code,
            "possible_session_headers": interesting or None,
            "headers": headers,
        })
    return response


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


class TicketState(str, Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"


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

# verification_id -> the session record. A session is created unverified by
# identify_customer and becomes verified by verify_otp. The Co-Worker passes
# verification_id on every later call; the secret it stands for (in production, the
# bank's own token) never leaves this dict.
VERIFICATIONS: Dict[str, dict] = {}
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


def _session(verification_id: str) -> dict:
    """Resolve a verification_id to a verified session, or refuse with a message
    that names the fix. Every endpoint below the auth pair calls this."""
    if not ENFORCE_SESSION:
        return {"customer_id": None, "bypass": True}
    given = (verification_id or "").strip()
    if not given:
        raise HTTPException(
            status_code=401,
            detail=("No verification_id was supplied. Call identify_customer, then "
                    "verify_otp, and pass the resulting verification_id on this call. "
                    "If your tool definition has no field for it, it is out of date: "
                    "reload the OpenAPI spec from /openapi.json."))
    entry = VERIFICATIONS.get(given)
    if not entry:
        if not given.startswith("SES"):
            raise HTTPException(
                status_code=401,
                detail=(f"'{given[:40]}' was not issued by this API. A verification_id "
                        f"always starts with SES and comes from identify_customer. Do "
                        f"not supply an id from anywhere else."))
        raise HTTPException(
            status_code=401,
            detail=("Unknown verification_id. Call identify_customer to start a session, "
                    "then verify_otp."))
    if entry.get("expired"):
        raise HTTPException(
            status_code=401,
            detail=(f"This session expired after {VERIFICATION_MINUTES} minutes. Call "
                    f"identify_customer again and have the customer verify a new OTP."))
    if not entry["verified"]:
        raise HTTPException(
            status_code=401,
            detail=("This session has not been verified yet. Ask the customer for the "
                    "OTP and call verify_otp with this verification_id."))
    if _now() > entry["expires_at"]:
        entry["verified"] = False
        entry["expired"] = True
        raise HTTPException(
            status_code=401,
            detail=(f"This session expired after {VERIFICATION_MINUTES} minutes. Call "
                    f"identify_customer again and have the customer verify a new OTP."))
    return entry


def _session_card(verification_id: str, card_id: str) -> dict:
    """The session authorises, the card_id identifies. A card that belongs to
    someone else is refused even with a valid session."""
    card = _card(card_id)
    entry = _session(verification_id)
    if entry.get("bypass"):
        return card
    if card["customer_id"] != entry["customer_id"]:
        raise HTTPException(
            status_code=403,
            detail="That card does not belong to the customer verified in this session.")
    return card


def _session_customer(verification_id: str) -> dict:
    entry = _session(verification_id)
    if entry.get("bypass"):
        return None
    return CUSTOMERS[entry["customer_id"]]


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
    verification_id: str = Field(
        ..., description=("Issued by this API. Always starts with SES. Pass it to "
                          "verify_otp, then on every other call. Never invent this "
                          "value or reuse one from another system."),
        examples=["SESF05CA0B9443D"])
    otp_sent: bool
    masked_mobile: str = Field(..., description="For the customer to confirm it is theirs")
    otp_valid_for_minutes: int
    message: str


class VerifyOtpRequest(BaseModel):
    verification_id: Optional[str] = Field(
        None, description=("The exact value returned by identify_customer, starting "
                           "with SES. Never invent it."),
        examples=["SESF05CA0B9443D"])
    # Accepted only so that a caller working from an out of date tool
    # definition still succeeds instead of failing validation. Not advertised.
    challenge_id: Optional[str] = Field(None, exclude=True)

    @model_validator(mode="after")
    def _coalesce(self):
        if not self.verification_id and self.challenge_id:
            self.verification_id = self.challenge_id
        if not self.verification_id:
            raise ValueError("verification_id is required, from identify_customer")
        return self
    otp: str = Field(..., examples=["123456"])

    @field_validator("otp", mode="before")
    @classmethod
    def _clean_otp(cls, v):
        """Coerce a JSON number to text, and strip the spacing or dashes a
        customer uses when reading a code back: "123 456" and "123-456"."""
        return None if v is None else str(v).replace(" ", "").replace("-", "").strip()

    @field_validator("verification_id", mode="before")
    @classmethod
    def _clean_id(cls, v):
        """Trim only. Never strip dashes here: a foreign id must survive intact
        so it can be reported back accurately in the error."""
        return None if v is None else str(v).strip()


class VerifyOtpResponse(BaseModel):
    verified: bool
    verification_id: str
    attempts_remaining: Optional[int] = None
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    card_id: Optional[str] = Field(None, description="Use this on every card call")
    card_state: Optional[CardState] = None
    masked_number: Optional[str] = None
    verification_valid_for_minutes: Optional[int] = Field(
        None, description="How long this session unlocks the customer's data for")
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


def _coerce_scalar(v):
    """'true' -> True, '30000' -> 30000. Agents frequently send both as text."""
    if isinstance(v, str):
        t = v.strip().strip('"\'')
        if t.lower() in ("true", "yes", "on", "1"):
            return True
        if t.lower() in ("false", "no", "off", "0"):
            return False
        if t.lstrip("-").isdigit():
            return int(t)
        return t
    return v


def _as_object(value, valid_keys):
    """Accept a nested object, a JSON string, or a loose 'a:1, b:true' string.

    An agent platform will often flatten a nested tool argument into text
    before sending it, and which encoding it picks varies. Rejecting those
    with a 422 tells the model nothing useful, so they are parsed instead.
    """
    if value is None or isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return {k: _coerce_scalar(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            pass
        # "contactless:true" or "atm=30000, online=60000"
        out = {}
        for part in text.replace(";", ",").split(","):
            if not part.strip():
                continue
            sep = ":" if ":" in part else ("=" if "=" in part else None)
            if not sep:
                continue
            k, _, v = part.partition(sep)
            k = k.strip().strip('"\'').lower()
            if k in valid_keys:
                out[k] = _coerce_scalar(v)
        return out or None
    return value


class UpdateCardRequest(BaseModel):
    model_config = {"extra": "allow"}   # so flattened top level fields survive

    verification_id: str = Field(
        ..., description=("The verified verification_id from verify_otp, starting with "
                          "SES. Never invent it."),
        examples=["SESF05CA0B9443D"])
    limits: Optional[LimitsPayload] = Field(
        None, description="Only the limits supplied are changed")
    features: Optional[FeaturesPayload] = Field(
        None, description="Only the features supplied are changed")

    @model_validator(mode="before")
    @classmethod
    def _accept_any_shape(cls, data):
        """limits and features may arrive as objects, as JSON strings, or
        flattened to the top level. All three are accepted."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        limit_keys = {"atm", "pos", "online", "international"}
        feature_keys = {"contactless", "online_payments", "international_usage",
                        "atm_withdrawals"}
        data["limits"] = _as_object(data.get("limits"), limit_keys)
        data["features"] = _as_object(data.get("features"), feature_keys)

        # update_card {"atm": 30000, "contactless": false} with no nesting
        loose_limits = {k: _coerce_scalar(v) for k, v in data.items() if k in limit_keys}
        loose_features = {k: _coerce_scalar(v) for k, v in data.items()
                          if k in feature_keys}
        if loose_limits:
            data["limits"] = {**(data.get("limits") or {}), **loose_limits}
        if loose_features:
            data["features"] = {**(data.get("features") or {}), **loose_features}
        return data


class BlockRequest(BaseModel):
    verification_id: str = Field(
        ..., description=("The verified verification_id from verify_otp, starting with "
                          "SES. Never invent it."),
        examples=["SESF05CA0B9443D"])
    reason: BlockReason


class ReplacementRequestBody(BaseModel):
    verification_id: str = Field(
        ..., description=("The verified verification_id from verify_otp, starting with "
                          "SES. Never invent it."),
        examples=["SESF05CA0B9443D"])
    reason: ReplacementReason
    delivery_address: str = Field(..., description="Confirmed with the customer first")


class PinResetRequest(BaseModel):
    verification_id: str = Field(
        ..., description=("The verified verification_id from verify_otp, starting with "
                          "SES. Never invent it."),
        examples=["SESF05CA0B9443D"])


class TicketRequest(BaseModel):
    verification_id: str = Field(
        ..., description=("The verified verification_id from verify_otp, starting with "
                          "SES. Never invent it."),
        examples=["SESF05CA0B9443D"])
    card_id: str = Field(..., description="The verified customer's card",
                         examples=["CARD1001"])
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
              "account data, so nothing is exposed before verification. Returns a "
              "verification_id starting with SES: pass that exact value to verify_otp, "
              "and then on every other call for the rest of the conversation."))
def identify_customer(body: IdentifyRequest):
    cust = CUSTOMERS.get(body.identifier.upper()) or next(
        (c for c in CUSTOMERS.values() if c["mobile"] == body.identifier), None)
    verification_id = f"SES{uuid4().hex[:12].upper()}"
    # The session is created unverified. It unlocks nothing until verify_otp
    # succeeds. An unknown identifier still gets one, so a caller cannot work
    # out which numbers are registered by watching which requests fail.
    VERIFICATIONS[verification_id] = {
        "verification_id": verification_id,
        "customer_id": cust["customer_id"] if cust else None,
        "verified": False,
        "otp": FIXED_OTP,
        "attempts_left": OTP_ATTEMPTS,
        "otp_expires_at": _now() + timedelta(minutes=OTP_MINUTES),
        "expires_at": _now() + timedelta(minutes=OTP_MINUTES),
    }
    masked = f"******{cust['mobile'][-4:]}" if cust else "******0000"
    return IdentifyResponse(
        verification_id=verification_id, otp_sent=True, masked_mobile=masked,
        otp_valid_for_minutes=OTP_MINUTES,
        message=(f"An OTP has been sent to the number ending {masked[-4:]}. "
                 f"Ask the customer to read it back, then call verify_otp with "
                 f"this verification_id."))


@app.post("/auth/verify-otp", response_model=VerifyOtpResponse, operation_id="verify_otp",
          tags=["Session"], summary="Verify the OTP and get the customer's card",
          description=(
              "Use after the customer supplies the OTP they received. On success it "
              "returns the customer_id, the card_id, and whether the card is active or "
              "blocked, so no further lookup is needed to start work, and it unlocks "
              "that customer's data for 30 minutes. Keep the card_id for the rest of "
              "the conversation. Every other endpoint returns 401 until this has "
              "succeeded, so there is no way to read or change anything first."))
def verify_otp(body: VerifyOtpRequest):
    entry = VERIFICATIONS.get(body.verification_id)
    if not entry:
        given = body.verification_id
        if not given.startswith("SES"):
            return VerifyOtpResponse(
                verified=False, verification_id=given,
                message=(f"'{given[:40]}' was not issued by this API. A verification_id "
                         f"always starts with SES and comes from identify_customer. "
                         f"Call identify_customer and use the value it returns."))
        return VerifyOtpResponse(
            verified=False, verification_id=given,
            message="Unknown verification_id. Call identify_customer to start a new session.")

    if entry["verified"]:
        cust = CUSTOMERS[entry["customer_id"]]
        card = CARDS[cust["card_id"]]
        return VerifyOtpResponse(
            verified=True, verification_id=entry["verification_id"], customer_id=cust["customer_id"],
            customer_name=cust["name"], card_id=card["card_id"], card_state=card["state"],
            masked_number=card["masked_number"],
            verification_valid_for_minutes=VERIFICATION_MINUTES,
            message="This session is already verified.")

    if _now() > entry["otp_expires_at"]:
        del VERIFICATIONS[body.verification_id]
        return VerifyOtpResponse(
            verified=False, verification_id=body.verification_id,
            message=("This code has expired. Call identify_customer again to send a "
                     "new one."))

    if body.otp != entry["otp"] or entry["customer_id"] is None:
        entry["attempts_left"] -= 1
        if entry["attempts_left"] <= 0:
            del VERIFICATIONS[body.verification_id]
            return VerifyOtpResponse(
                verified=False, verification_id=body.verification_id, attempts_remaining=0,
                message=("Too many incorrect codes. This session is closed. Call "
                         "identify_customer to start again, or offer the customer a "
                         "ticket if they cannot receive the code."))
        return VerifyOtpResponse(
            verified=False, verification_id=body.verification_id,
            attempts_remaining=entry["attempts_left"],
            message=(f"That code is not correct. Ask the customer to check it and read "
                     f"it back again. {entry['attempts_left']} attempts remaining."))

    # Verified. The session now unlocks this customer's data, and only theirs.
    entry["verified"] = True
    entry["verified_at"] = _now()
    entry["expires_at"] = _now() + timedelta(minutes=VERIFICATION_MINUTES)
    cust = CUSTOMERS[entry["customer_id"]]
    card = CARDS[cust["card_id"]]
    return VerifyOtpResponse(
        verified=True, verification_id=entry["verification_id"], customer_id=cust["customer_id"],
        customer_name=cust["name"], card_id=card["card_id"], card_state=card["state"],
        masked_number=card["masked_number"], verification_valid_for_minutes=VERIFICATION_MINUTES,
        message=(f"Verified. {cust['name']} holds card {card['masked_number']}, "
                 f"currently {card['state'].value}. This session is good for "
                 f"{VERIFICATION_MINUTES} minutes."))


@app.get("/customers/{customer_id}/address", operation_id="get_customer_address",
         tags=["Reads"], summary="Get the address on record",
         description=(
             "Use when arranging a replacement card or raising a ticket, so the address "
             "on file can be confirmed with the customer. Read only."))
def get_customer_address(customer_id: str,
                         verification_id: str = Query(
                             ..., description="A verified session from verify_otp")):
    cust = CUSTOMERS.get((customer_id or "").strip().upper())
    if not cust:
        raise HTTPException(
            status_code=404,
            detail="Unknown customer_id. Use the customer_id returned by verify_otp.")
    entry = _session(verification_id)
    if not entry.get("bypass") and cust["customer_id"] != entry["customer_id"]:
        raise HTTPException(
            status_code=403,
            detail="That customer is not the one verified in this session.")
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
def get_card(card_id: str, verification_id: Optional[str] = Query(
                   None, description=("The verified verification_id from verify_otp, "
                                      "starting with SES. Required. Never invent it."),
                   examples=["SESF05CA0B9443D"])):
    card = _session_card(verification_id, card_id)
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
    card = _session_card(body.verification_id, card_id)
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
    card = _session_card(body.verification_id, card_id)
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
    card = _session_card(body.verification_id, card_id)
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
def initiate_pin_reset(card_id: str, body: PinResetRequest, request: Request):
    card = _session_card(body.verification_id, card_id)
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
              "name and address with the customer first, both are in get_card and "
              "get_customer_address. Returns a reference number to give them. Do not "
              "speculate about the outcome or promise a refund."))
def raise_ticket(body: TicketRequest):
    card = _session_card(body.verification_id, body.card_id)
    ref = f"TKT{uuid4().hex[:8].upper()}"
    now = _now().isoformat() + "Z"
    TICKETS[ref] = {"reference": ref, "customer_id": card["customer_id"],
                    "card_id": card["card_id"], "name": body.name,
                    "address": body.address, "issue": body.issue,
                    "state": TicketState.open, "assigned_to": "support_queue",
                    "created_at": now, "updated_at": now,
                    "history": [{"at": now, "state": TicketState.open,
                                 "note": "Raised by the Card Operations Co-Worker"}]}
    return {**TICKETS[ref],
            "message": f"Ticket {ref} has been raised. Quote this number on any follow up."}


@app.get("/tickets", operation_id="list_tickets", tags=["Servicing"],
         summary="List this customer's tickets",
         description=(
             "Use when the customer asks what has been raised for them, or before "
             "raising a new ticket so the same issue is not logged twice. Returns every "
             "ticket for the verified customer, newest first. Read only."))
def list_tickets(verification_id: Optional[str] = Query(
                   None, description=("The verified verification_id from verify_otp, "
                                      "starting with SES. Required. Never invent it."),
                   examples=["SESF05CA0B9443D"])):
    entry = _session(verification_id)
    mine = [t for t in TICKETS.values()
            if entry.get("bypass") or t["customer_id"] == entry["customer_id"]]
    mine.sort(key=lambda t: t["created_at"], reverse=True)
    return {"count": len(mine),
            "tickets": [{k: v for k, v in t.items() if k != "history"} for t in mine]}


@app.get("/tickets/{reference}", operation_id="get_ticket", tags=["Servicing"],
         summary="Check the status of a ticket",
         description=(
             "Use when the customer quotes a ticket number and asks where it has got "
             "to. Returns the current state and the history of what has happened to it. "
             "Report the state as returned and do not speculate about the outcome. "
             "Read only."))
def get_ticket(reference: str,
               verification_id: Optional[str] = Query(
                   None, description=("The verified verification_id from verify_otp, "
                                      "starting with SES. Required. Never invent it."),
                   examples=["SESF05CA0B9443D"])):
    entry = _session(verification_id)
    ticket = TICKETS.get((reference or "").strip().upper())
    if not ticket or not (entry.get("bypass")
                          or ticket["customer_id"] == entry["customer_id"]):
        raise HTTPException(status_code=404,
                            detail="No ticket with that reference for this customer.")
    return ticket


@app.get("/health", operation_id="health_check", tags=["Testing"],
         summary="Check the service is up",
         description=(
             "Returns the service status and how many customers exist. Use it to confirm "
             "the connection works, or to wake a sleeping instance. Returns no customer "
             "data."))
def health_check():
    verified = sum(1 for x in VERIFICATIONS.values() if x.get("verified"))
    return {"state": "ok", "customers": len(CUSTOMERS), "cards": len(CARDS),
            "session_enforcement": ENFORCE_SESSION,
            "sessions": {"total": len(VERIFICATIONS), "verified": verified},
            "open_tickets": sum(1 for t in TICKETS.values() if t["state"] == "open")}


@app.get("/admin/requests", include_in_schema=False)
def recent_requests():
    """The last 20 calls with their exact arguments, newest first. Not a tool.

    Use this when an agent is failing and you cannot tell why: it shows the
    verification_id it actually sent, not the one you assume it sent. A value
    that is not in the VERIFICATIONS list below was never issued by this
    service, which means it was either invented by the model or injected by
    the platform.
    """
    # Anything that looks like a stable per-conversation id, gathered across
    # every logged call. A value that repeats across several calls from the
    # same conversation is a candidate to key the register on.
    candidates: Dict[str, list] = {}
    for r in REQUEST_LOG:
        for k, v in (r.get("possible_session_headers") or {}).items():
            candidates.setdefault(k, [])
            if v not in candidates[k]:
                candidates[k].append(v)
    return {
        "count": len(REQUEST_LOG),
        "issued_verification_ids": list(VERIFICATIONS),
        "session_header_candidates": candidates or
            "none seen yet: no header resembling a conversation id has arrived",
        "requests": list(reversed(REQUEST_LOG)),
    }


@app.get("/admin/tickets", include_in_schema=False)
def all_tickets():
    """Every ticket across every customer, for watching the queue during a
    demo. Not a tool: the Co-Worker must only ever see its own customer's."""
    return {"count": len(TICKETS), "tickets": list(TICKETS.values())}


@app.patch("/admin/tickets/{reference}", include_in_schema=False)
def update_ticket_state(reference: str, state: TicketState, note: str = ""):
    """Stand in for a human support agent moving a ticket along, so
    get_ticket has something to report. Not a tool, for the same reason
    simulate_pin_completion was not one: the Co-Worker must not be able to
    resolve its own tickets.

        curl -X PATCH '.../admin/tickets/TKT1234ABCD?state=resolved&note=Refunded'
    """
    ticket = TICKETS.get((reference or "").strip().upper())
    if not ticket:
        raise HTTPException(status_code=404, detail="Unknown ticket reference")
    now = _now().isoformat() + "Z"
    ticket["state"] = state
    ticket["updated_at"] = now
    ticket["history"].append({"at": now, "state": state,
                              "note": note or f"State changed to {state}"})
    return ticket


@app.patch("/admin/expire/{verification_id}", include_in_schema=False)
def expire_session(verification_id: str):
    """Force a session to look expired, so the 15 minute path can be tested
    without waiting 15 minutes. Not a tool."""
    entry = VERIFICATIONS.get(verification_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Unknown verification_id")
    entry["expires_at"] = _now() - timedelta(seconds=1)
    return {"verification_id": verification_id, "expired": True}


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
    for store in (VERIFICATIONS, PIN_RESETS, REPLACEMENTS, TICKETS):
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
    # challenge_id is accepted at runtime only, so a caller working from an
    # out of date tool definition still succeeds. It must not appear in the
    # document: advertising it would invite the very mistake it absorbs.
    vreq = schema.get("components", {}).get("schemas", {}).get("VerifyOtpRequest", {})
    vreq.get("properties", {}).pop("challenge_id", None)

    schema["openapi"] = "3.0.3"
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
