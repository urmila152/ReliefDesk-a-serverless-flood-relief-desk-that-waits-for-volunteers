"""Single HTTP API router (API Gateway HTTP API -> this Lambda)."""
import hmac, json, os, re
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Key

from common import (D, OFFERS, REQ, VOL, cell_of, clean, get_request, haversine_km, now_ms, now_s, reply)

sfn = boto3.client("stepfunctions")
ddbc = boto3.client("dynamodb")
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")
COORDINATOR_KEY = os.environ.get("COORDINATOR_KEY", "")
REQ_T, VOL_T, OFFER_T = (os.environ.get(k, "") for k in ("REQUESTS_TABLE", "VOLUNTEERS_TABLE", "OFFERS_TABLE"))
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
WEB_FILES = {"index.html": "text/html", "desk.html": "text/html", "volunteer.html": "text/html",
             "style.css": "text/css", "app.js": "application/javascript", "config.js": "application/javascript"}
STATUS_ORDER = {"escalated": 0, "open": 1, "assigned": 2, "done": 3, "merged": 4}


def is_coordinator(event):
    given = (event.get("headers") or {}).get("x-coordinator-key", "")
    return bool(COORDINATOR_KEY) and hmac.compare_digest(given, COORDINATOR_KEY)


# ---------- public ----------
def create_request(event):
    try:
        b = json.loads(event.get("body") or "{}")
        text = str(b["text"]).strip()
        lat, lng = float(b["lat"]), float(b["lng"])
    except (KeyError, ValueError, TypeError):
        return reply(400, {"error": "Describe what you need and share a location."})
    if not (3 <= len(text) <= 1000) or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return reply(400, {"error": "Text must be 3-1000 characters and the location must be valid."})
    rid = uuid4().hex[:12]
    item = {
        "request_id": rid, "text": text, "lat": D(lat), "lng": D(lng), "cell": cell_of(lat, lng),
        "name": str(b.get("name", ""))[:60], "phone": re.sub(r"[^\d+ ]", "", str(b.get("phone", "")))[:20],
        "status": "open", "created_at": now_ms(), "skip_llm": bool(b.get("demo_mode")), "ttl": now_s() + 14 * 86400,
    }
    REQ.put_item(Item=item)
    try:
        ex = sfn.start_execution(stateMachineArn=STATE_MACHINE_ARN, name=rid, input=json.dumps({"request_id": rid}))
        REQ.update_item(Key={"request_id": rid}, UpdateExpression="SET execution_arn = :a",
                        ExpressionAttributeValues={":a": ex["executionArn"]})
    except Exception as exc:
        print("start_execution failed:", repr(exc))
        return reply(500, {"error": "Could not start processing. Please try again."})
    return reply(202, {"request_id": rid})


def request_status(rid):
    r = get_request(rid)
    if not r:
        return reply(404, {"error": "Request not found."})
    out = {"request_id": rid, "status": r["status"], "need": r.get("need"), "summary": r.get("summary"),
           "assigned_name": r.get("assigned_name")}
    if r["status"] == "merged":  # show the progress of the request this one was merged into
        p = get_request(r["parent_id"]) or {}
        out.update({"status": "merged", "parent_status": p.get("status"), "assigned_name": p.get("assigned_name")})
    return reply(200, out)


# ---------- coordinator ----------
def list_requests():
    scan = REQ.scan()
    items = scan["Items"]
    while "LastEvaluatedKey" in scan and len(items) < 500:
        scan = REQ.scan(ExclusiveStartKey=scan["LastEvaluatedKey"])
        items += scan["Items"]
    fields = ("request_id", "text", "name", "phone", "lat", "lng", "need", "people", "urgency", "summary", "status",
              "assigned_name", "merged_count", "created_at", "reopen_count", "extraction", "parent_id")
    rows = [{k: i[k] for k in fields if k in i} for i in items]
    rows.sort(key=lambda r: (STATUS_ORDER.get(r["status"], 9), -int(r.get("urgency", 0))))
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return reply(200, {"requests": rows[:200], "counts": counts})


def trace(rid):
    r = get_request(rid)
    if not r or "execution_arn" not in r:
        return reply(404, {"error": "No execution for this request."})
    arn = r["execution_arn"]
    status = sfn.describe_execution(executionArn=arn)["status"]
    steps = []
    for ev in sfn.get_execution_history(executionArn=arn, maxResults=250)["events"]:
        if "stateEnteredEventDetails" in ev:
            steps.append({"name": ev["stateEnteredEventDetails"]["name"], "at": int(ev["timestamp"].timestamp() * 1000)})
    return reply(200, {"status": status, "steps": steps})


# ---------- volunteers ----------
def volunteer_profile(vid):
    v = VOL.get_item(Key={"volunteer_id": vid}).get("Item")
    if not v:
        return reply(404, {"error": "Unknown volunteer link."})
    out = {"name": v.get("name"), "available": bool(v.get("available")), "task": None}
    if v.get("current_request"):
        r = get_request(v["current_request"])
        if r and r["status"] == "assigned" and r.get("assigned_to") == vid:
            out["task"] = {k: r.get(k) for k in ("request_id", "need", "people", "summary", "text", "name", "phone", "lat", "lng")}
    return reply(200, out)


def volunteer_offers(vid):
    v = VOL.get_item(Key={"volunteer_id": vid}).get("Item")
    if not v:
        return reply(404, {"error": "Unknown volunteer link."})
    rows = []
    for o in OFFERS.query(IndexName="volunteer-index", KeyConditionExpression=Key("volunteer_id").eq(vid))["Items"]:
        if o["status"] != "pending" or int(o["expires_at"]) <= now_s():
            continue
        r = get_request(o["request_id"])
        if not r or r["status"] != "open":
            continue
        rows.append({"offer_id": o["offer_id"], "need": r.get("need"), "people": r.get("people"), "summary": r.get("summary"),
                     "urgency": r.get("urgency"), "expires_at": o["expires_at"],
                     "distance_km": round(haversine_km(r["lat"], r["lng"], v["lat"], v["lng"]), 1)})
    rows.sort(key=lambda x: -int(x.get("urgency") or 0))
    return reply(200, {"offers": rows})


def respond_offer(oid, event):
    try:
        action = json.loads(event.get("body") or "{}")["action"]
    except (KeyError, ValueError):
        action = None
    if action not in ("accept", "decline"):
        return reply(400, {"error": "action must be accept or decline"})
    o = OFFERS.get_item(Key={"offer_id": oid}).get("Item")
    if not o:
        return reply(404, {"error": "Offer not found."})
    token, rid, vid = o["task_token"], o["request_id"], o["volunteer_id"]

    if action == "decline":
        try:
            OFFERS.update_item(Key={"offer_id": oid}, UpdateExpression="SET #s = :d", ConditionExpression="#s = :p",
                               ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":d": "declined", ":p": "pending"})
            sfn.send_task_failure(taskToken=token, error="Declined", cause="Volunteer declined")
        except Exception as exc:
            print("decline ignored:", repr(exc))
        return reply(200, {"ok": True})

    name = (VOL.get_item(Key={"volunteer_id": vid}).get("Item") or {}).get("name", "Volunteer")
    S = lambda s: {"S": s}  # noqa: E731
    try:
        # One atomic claim: request still open + volunteer still free + offer still pending and unexpired.
        ddbc.transact_write_items(TransactItems=[
            {"Update": {"TableName": REQ_T, "Key": {"request_id": S(rid)},
                        "UpdateExpression": "SET #s = :a, assigned_to = :v, assigned_name = :n",
                        "ConditionExpression": "#s = :o", "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {":a": S("assigned"), ":o": S("open"), ":v": S(vid), ":n": S(name)}}},
            {"Update": {"TableName": VOL_T, "Key": {"volunteer_id": S(vid)},
                        "UpdateExpression": "SET available = :f, current_request = :r",
                        "ConditionExpression": "available = :t",
                        "ExpressionAttributeValues": {":f": {"BOOL": False}, ":t": {"BOOL": True}, ":r": S(rid)}}},
            {"Update": {"TableName": OFFER_T, "Key": {"offer_id": S(oid)},
                        "UpdateExpression": "SET #s = :acc", "ConditionExpression": "#s = :p AND expires_at > :now",
                        "ExpressionAttributeNames": {"#s": "status"},
                        "ExpressionAttributeValues": {":acc": S("accepted"), ":p": S("pending"), ":now": {"N": str(now_s())}}}},
        ])
    except ddbc.exceptions.TransactionCanceledException:
        return reply(409, {"error": "This request was already taken, or your offer expired."})
    try:
        sfn.send_task_success(taskToken=token, output=json.dumps({"volunteer_id": vid}))
    except Exception as exc:  # workflow already moved on: undo the claim
        print("send_task_success failed, rolling back:", repr(exc))
        REQ.update_item(Key={"request_id": rid}, UpdateExpression="SET #s = :o REMOVE assigned_to, assigned_name",
                        ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":o": "open"})
        VOL.update_item(Key={"volunteer_id": vid}, UpdateExpression="SET available = :t REMOVE current_request",
                        ExpressionAttributeValues={":t": True})
        return reply(409, {"error": "This offer is no longer active."})
    return reply(200, {"ok": True})


def complete_request(rid, event):
    try:
        b = json.loads(event.get("body") or "{}")
        vid, outcome = b["volunteer_id"], b["outcome"]
    except (KeyError, ValueError):
        return reply(400, {"error": "volunteer_id and outcome are required."})
    if outcome not in ("done", "unreachable"):
        return reply(400, {"error": "outcome must be done or unreachable"})
    r = get_request(rid)
    if not r or r["status"] != "assigned" or r.get("assigned_to") != vid:
        return reply(403, {"error": "This request is not assigned to you."})
    token = r.get("completion_token")
    if not token:
        return reply(409, {"error": "Still starting up. Try again in a moment."})
    try:
        sfn.send_task_success(taskToken=token, output=json.dumps({"outcome": outcome}))
    except Exception as exc:
        print("complete failed:", repr(exc))
        return reply(409, {"error": "Already updated."})
    return reply(200, {"ok": True})


ROUTES = [
    ("POST", r"^/requests$", lambda m, e: create_request(e), False),
    ("GET", r"^/requests$", lambda m, e: list_requests(), True),
    ("GET", r"^/requests/([\w-]+)$", lambda m, e: request_status(m.group(1)), False),
    ("GET", r"^/requests/([\w-]+)/trace$", lambda m, e: trace(m.group(1)), True),
    ("POST", r"^/requests/([\w-]+)/complete$", lambda m, e: complete_request(m.group(1), e), False),
    ("GET", r"^/volunteers/([\w-]+)$", lambda m, e: volunteer_profile(m.group(1)), False),
    ("GET", r"^/volunteers/([\w-]+)/offers$", lambda m, e: volunteer_offers(m.group(1)), False),
    ("POST", r"^/offers/([\w-]+)/respond$", lambda m, e: respond_offer(m.group(1), e), False),
]


def static(path):
    name = "index.html" if path == "/" else path.lstrip("/")
    f = os.path.join(WEB, name)
    if name not in WEB_FILES or not os.path.isfile(f):
        return None
    with open(f, encoding="utf-8") as fh:
        return {"statusCode": 200, "headers": {"content-type": WEB_FILES[name] + "; charset=utf-8", "cache-control": "no-cache"}, "body": fh.read()}


def handler(event, _ctx):
    method, path = event["requestContext"]["http"]["method"], event["rawPath"].rstrip("/") or "/"
    for m, pattern, fn, needs_key in ROUTES:
        match = re.match(pattern, path)
        if m == method and match:
            if needs_key and not is_coordinator(event):
                return reply(401, {"error": "Coordinator key required."})
            try:
                return fn(match, event)
            except Exception as exc:
                print("unhandled:", repr(exc))
                return reply(500, {"error": "Something went wrong."})
    if method == "GET":
        page = static(path)
        if page:
            return page
    return reply(404, {"error": "Not found."})
