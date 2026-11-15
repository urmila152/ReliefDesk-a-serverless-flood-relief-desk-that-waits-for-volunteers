import json, math, os, time
from decimal import Decimal

import boto3

_ddb = boto3.resource("dynamodb")
REQ = _ddb.Table(os.environ.get("REQUESTS_TABLE", "requests"))
VOL = _ddb.Table(os.environ.get("VOLUNTEERS_TABLE", "volunteers"))
OFFERS = _ddb.Table(os.environ.get("OFFERS_TABLE", "offers"))

NEEDS = ["rescue", "medical", "shelter", "water", "food", "other"]
BASE_URGENCY = {"rescue": 90, "medical": 75, "shelter": 50, "water": 45, "food": 40, "other": 30}


def now_ms():
    return int(time.time() * 1000)


def now_s():
    return int(time.time())


def D(x):
    return Decimal(str(x))


def clean(o):
    """Make DynamoDB output JSON-safe (Decimal -> int/float, set -> list)."""
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, (set, frozenset)):
        return sorted(clean(i) for i in o)
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [clean(i) for i in o]
    return o


def haversine_km(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = (float(v) for v in (lat1, lng1, lat2, lng2))
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(a))


def cell_of(lat, lng):
    """~1 km grid cell used to find nearby duplicate requests."""
    return f"{round(float(lat), 2):.2f}:{round(float(lng), 2):.2f}"


def compute_urgency(item):
    need = item.get("need", "other")
    score = BASE_URGENCY.get(need, 30)
    score += min(int(item.get("people", 1)), 20) * 1.5
    score += 10 if item.get("vulnerable") else 0
    score += 5 * int(item.get("merged_count", 0))
    score += int(item.get("urgency_bonus", 0))
    return int(min(100, round(score)))


def get_request(rid):
    return REQ.get_item(Key={"request_id": rid}).get("Item")


def reply(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(clean(body)),
    }
