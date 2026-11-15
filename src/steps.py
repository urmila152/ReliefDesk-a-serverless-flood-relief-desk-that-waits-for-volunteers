"""Workflow task handlers. Step Functions invokes this one function with an `action`."""
import json, os, urllib.request
from uuid import uuid4

from boto3.dynamodb.conditions import Attr, Key

from common import (OFFERS, REQ, VOL, clean, compute_urgency, get_request, haversine_km, now_ms, now_s)

OFFER_TIMEOUT = int(os.environ.get("OFFER_TIMEOUT", "600"))
MAX_RADIUS_KM = 50
MAX_CANDIDATES = 5


def dedupe(e):
    """Merge into an EARLIER open request with the same need in the same ~1 km cell (last 2 h)."""
    rid = e["request_id"]
    me = get_request(rid)
    q = REQ.query(
        IndexName="cell-index",
        KeyConditionExpression=Key("cell").eq(me["cell"]) & Key("created_at").between(now_ms() - 2 * 3600 * 1000, int(me["created_at"]) - 1),
        FilterExpression=Attr("need").eq(me["need"]) & Attr("status").is_in(["open", "assigned"]),
    )
    parents = sorted(q.get("Items", []), key=lambda i: i["created_at"])
    if not parents:
        return {"request_id": rid, "duplicate": False}
    parent = parents[0]
    updated = REQ.update_item(
        Key={"request_id": parent["request_id"]},
        UpdateExpression="ADD merged_count :one, people :n",
        ExpressionAttributeValues={":one": 1, ":n": int(me.get("people", 1))},
        ReturnValues="ALL_NEW",
    )["Attributes"]
    REQ.update_item(Key={"request_id": parent["request_id"]}, UpdateExpression="SET urgency = :u",
                    ExpressionAttributeValues={":u": compute_urgency(updated)})
    REQ.update_item(Key={"request_id": rid}, UpdateExpression="SET #s = :m, parent_id = :p",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":m": "merged", ":p": parent["request_id"]})
    return {"request_id": rid, "duplicate": True}


def score(e):
    rid = e["request_id"]
    REQ.update_item(Key={"request_id": rid}, UpdateExpression="SET urgency = :u",
                    ExpressionAttributeValues={":u": compute_urgency(get_request(rid))})
    return {"request_id": rid}


def find(e):
    rid = e["request_id"]
    r = get_request(rid)
    tried = set(r.get("tried", set()))
    scan = VOL.scan(FilterExpression=Attr("available").eq(True))
    vols = scan["Items"]
    while "LastEvaluatedKey" in scan:
        scan = VOL.scan(FilterExpression=Attr("available").eq(True), ExclusiveStartKey=scan["LastEvaluatedKey"])
        vols += scan["Items"]
    out = []
    for v in vols:
        skills = set(v.get("skills", []))
        if v["volunteer_id"] in tried or (skills and "general" not in skills and r["need"] not in skills):
            continue
        d = haversine_km(r["lat"], r["lng"], v["lat"], v["lng"])
        if d <= MAX_RADIUS_KM:
            out.append({"volunteer_id": v["volunteer_id"], "name": v.get("name", "Volunteer"), "distance_km": round(d, 1)})
    out.sort(key=lambda c: c["distance_km"])
    out = out[:MAX_CANDIDATES]
    return {"request_id": rid, "candidates": out, "count": len(out), "idx": 0}


def notify(volunteer, text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), volunteer.get("telegram_chat_id")
    if not (token and chat):
        return
    try:
        body = json.dumps({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", body, {"content-type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as exc:  # alerts are best effort; the volunteer page is the source of truth
        print("telegram failed:", repr(exc))


def offer(e):
    """Create an offer, store the callback token, and return WITHOUT completing the task:
    Step Functions now waits (at no compute cost) until a volunteer responds or the timeout hits."""
    rid, cand = e["request_id"], e["candidates"][int(e["idx"])]
    r, v = get_request(rid), VOL.get_item(Key={"volunteer_id": cand["volunteer_id"]})["Item"]
    offer_id = uuid4().hex
    OFFERS.put_item(Item={
        "offer_id": offer_id, "request_id": rid, "volunteer_id": v["volunteer_id"], "task_token": e["token"],
        "status": "pending", "created_at": now_s(), "expires_at": now_s() + OFFER_TIMEOUT - 5, "ttl": now_s() + 86400,
    })
    REQ.update_item(Key={"request_id": rid}, UpdateExpression="ADD tried :t",
                    ExpressionAttributeValues={":t": {v["volunteer_id"]}})
    link = f"{os.environ.get('SITE_URL', '')}/volunteer.html?v={v['volunteer_id']}"
    notify(v, f"Relief request ({r['need']}, {int(r['people'])} people, {cand['distance_km']} km away): {r['summary']}\nRespond: {link}")


def await_completion(e):
    REQ.update_item(Key={"request_id": e["request_id"]}, UpdateExpression="SET completion_token = :t",
                    ExpressionAttributeValues={":t": e["token"]})


def _free_volunteer(vid):
    if vid:
        VOL.update_item(Key={"volunteer_id": vid}, UpdateExpression="SET available = :t REMOVE current_request",
                        ExpressionAttributeValues={":t": True})


def close(e):
    rid = e["request_id"]
    r = REQ.update_item(
        Key={"request_id": rid}, UpdateExpression="SET #s = :d, completed_at = :t REMOVE completion_token",
        ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":d": "done", ":t": now_ms()},
        ReturnValues="ALL_NEW")["Attributes"]
    _free_volunteer(r.get("assigned_to"))
    return {"request_id": rid}


def reopen(e):
    """Volunteer could not reach the person, or went silent: free them and offer to the next one, more urgent."""
    rid = e["request_id"]
    r = REQ.update_item(
        Key={"request_id": rid},
        UpdateExpression="SET #s = :o ADD reopen_count :one, urgency_bonus :b REMOVE assigned_to, assigned_name, completion_token",
        ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":o": "open", ":one": 1, ":b": 10},
        ReturnValues="ALL_NEW")["Attributes"]
    REQ.update_item(Key={"request_id": rid}, UpdateExpression="SET urgency = :u",
                    ExpressionAttributeValues={":u": compute_urgency(r)})
    # the volunteer who just failed is the last one in `tried`; free whoever still holds this request
    for v in VOL.scan(FilterExpression=Attr("current_request").eq(rid))["Items"]:
        _free_volunteer(v["volunteer_id"])
    return {"request_id": rid, "reopen_count": int(r["reopen_count"])}


def escalate(e):
    rid = e["request_id"]
    REQ.update_item(Key={"request_id": rid}, UpdateExpression="SET #s = :x, escalated_at = :t",
                    ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":x": "escalated", ":t": now_ms()})
    return {"request_id": rid}


ACTIONS = {"dedupe": dedupe, "score": score, "find": find, "offer": offer, "await": await_completion,
           "close": close, "reopen": reopen, "escalate": escalate}


def handler(event, _ctx):
    return clean(ACTIONS[event["action"]](event))
