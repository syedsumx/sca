"""Deterministic demo data: ~50 customers, ~500 support tickets, and per-customer policy tags."""

import logging
import random
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from tether.db.models import Customer, PolicyTagRow, SupportTicket

log = logging.getLogger(__name__)

FIRST_NAMES = [
    "Ada", "Grace", "Alan", "Linus", "Margaret", "Dennis", "Barbara", "Ken", "Frances", "Tim",
    "Radia", "Guido", "Katherine", "John", "Hedy", "Edsger", "Sophie", "Bjarne", "Annie", "Donald",
    "Shafi", "Leslie", "Jean", "Niklaus", "Mary", "Yukihiro", "Karen", "Brian", "Lynn", "Vint",
]  # fmt: skip
LAST_NAMES = [
    "Lovelace", "Hopper", "Turing", "Torvalds", "Hamilton", "Ritchie", "Liskov", "Thompson",
    "Allen", "Berners-Lee", "Perlman", "van Rossum", "Johnson", "McCarthy", "Lamarr", "Dijkstra",
    "Wilson", "Stroustrup", "Easley", "Knuth", "Goldwasser", "Lamport", "Bartik", "Wirth",
    "Keller", "Matsumoto", "Jones", "Kernighan", "Conway", "Cerf",
]  # fmt: skip
STREETS = ["Maple Ave", "Oak St", "Harbor Rd", "Elm Ct", "Cedar Ln", "Pine Blvd", "Birch Way"]
CITIES = ["Springfield", "Riverton", "Lakeside", "Fairview", "Greenville", "Madison", "Clinton"]
PRODUCTS = ["Aurora router", "Nimbus headset", "Quasar laptop", "Pulse smartwatch", "Echo speaker"]

TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "billing": [
        ("Double charge on invoice",
         "Hi, this is {name}. I was charged twice for order {order} on my card ending {card}. "
         "Please refund the duplicate payment. You can reach me at {phone} or {email}."),
        ("Invoice address is wrong",
         "My invoice for order {order} shows the wrong billing address. It should be {address}. "
         "Thanks, {name} ({email})."),
    ],
    "shipping": [
        ("Package not delivered",
         "Order {order} for the {product} still has not arrived at {address}. Tracking has not "
         "updated in five days. Please call {name} on {phone}."),
        ("Damaged on arrival",
         "The {product} from order {order} arrived with a cracked case. I would like a "
         "replacement shipped to {address}. Regards, {name}."),
    ],
    "account": [
        ("Cannot reset password",
         "I'm {name} and the password reset email never reaches {email}. I have checked the spam "
         "folder. My phone for verification is {phone}."),
        ("Update my contact details",
         "Please update my phone number to {phone} and confirm my address is {address}. "
         "Account email: {email}. - {name}"),
    ],
    "technical": [
        ("Device keeps rebooting",
         "My {product} reboots every few minutes since the last firmware update. Serial {serial}. "
         "{name}, {email}."),
        ("Bluetooth pairing fails",
         "The {product} will not pair with my phone. I tried resetting it twice. Contact me at "
         "{email}. Thanks, {name}."),
    ],
    "refund": [
        ("Refund request",
         "I'd like to return the {product} from order {order} and get a refund to my card ending "
         "{card}. Pick-up address: {address}. {name}, {phone}."),
        ("Refund still pending",
         "My refund for order {order} was approved two weeks ago but has not shown up on the "
         "card ending {card}. Please check. {name} ({email})."),
    ],
}  # fmt: skip

# Customers whose policy differs from the default (granted / internal / +2 years).
REVOKED_CONSENT = {"cust-013", "cust-042"}
PENDING_CONSENT = {"cust-021"}
CONFIDENTIAL_LICENSE = {"cust-005", "cust-017", "cust-033"}
EXPIRED_RETENTION = {"cust-044"}


@dataclass(frozen=True)
class SeedResult:
    customers: int
    tickets: int


def customer_id(n: int) -> str:
    return f"cust-{n:03d}"


def seed(
    session: Session, n_customers: int = 50, n_tickets: int = 500, rng_seed: int = 42
) -> SeedResult:
    """Replace the demo source data with a deterministic dataset."""
    rng = random.Random(rng_seed)
    session.execute(delete(SupportTicket))
    session.execute(delete(Customer))
    session.execute(delete(PolicyTagRow))

    today = date.today()
    customers: list[Customer] = []
    for n in range(1, n_customers + 1):
        first = FIRST_NAMES[(n - 1) % len(FIRST_NAMES)]
        last = LAST_NAMES[(n * 7) % len(LAST_NAMES)]
        cid = customer_id(n)
        handle = f"{first}.{last}".lower().replace(" ", "").replace("-", "")
        customers.append(
            Customer(
                id=cid,
                name=f"{first} {last}",
                email=f"{handle}{n}@example.com",
                phone=f"+1-555-{rng.randint(100, 999)}-{rng.randint(1000, 9999)}",
                address=f"{rng.randint(1, 999)} {rng.choice(STREETS)}, {rng.choice(CITIES)}",
            )
        )
        session.add(
            PolicyTagRow(
                subject_id=cid,
                consent_status=(
                    "revoked" if cid in REVOKED_CONSENT
                    else "pending" if cid in PENDING_CONSENT
                    else "granted"
                ),
                license="confidential" if cid in CONFIDENTIAL_LICENSE else "internal",
                retention_until=(
                    today - timedelta(days=1) if cid in EXPIRED_RETENTION
                    else today + timedelta(days=730)
                ),
            )
        )  # fmt: skip
    session.add_all(customers)
    session.flush()

    # Every customer gets at least one ticket; the rest are spread randomly.
    owners = [c for c in customers] + [
        rng.choice(customers) for _ in range(n_tickets - len(customers))
    ]
    categories = sorted(TEMPLATES)
    for i, cust in enumerate(owners, start=1):
        category = rng.choice(categories)
        title, body = rng.choice(TEMPLATES[category])
        session.add(
            SupportTicket(
                id=f"tkt-{i:04d}",
                customer_id=cust.id,
                category=category,
                title=title,
                body=body.format(
                    name=cust.name,
                    email=cust.email,
                    phone=cust.phone,
                    address=cust.address,
                    order=f"ORD-{rng.randint(10000, 99999)}",
                    card=f"{rng.randint(1000, 9999)}",
                    product=rng.choice(PRODUCTS),
                    serial=f"SN{rng.randint(100000, 999999)}",
                ),
            )
        )
    session.flush()
    log.info("seeded %d customers and %d tickets", len(customers), len(owners))
    return SeedResult(customers=len(customers), tickets=len(owners))
