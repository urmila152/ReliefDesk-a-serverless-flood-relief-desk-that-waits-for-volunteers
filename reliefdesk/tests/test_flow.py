"""Local test of the lifecycle logic with moto (DynamoDB) and a mocked Step Functions client.
Run: python tests/test_flow.py"""
import json, os, sys
from unittest import mock

os.environ.update(AWS_DEFAULT_REGION="ap-south-1", AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x",
                  REQUESTS_TABLE="req", VOLUNTEERS_TABLE="vol", OFFERS_TABLE="off", COORDINATOR_KEY="secret-key-1",
                  BEDROCK_MODEL_ID="", STATE_MACHINE_ARN="arn:aws:states:ap-south-1:1:stateMachine:x", OFFER_TIMEOUT="600")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import boto3
from moto import mock_aws


def setup():
    c = boto3.client("dynamodb")
    c.create_table(TableName="req", BillingMode="PAY_PER_REQUEST",
                   AttributeDefinitions=[{"AttributeName": "request_id", "AttributeType": "S"}, {"AttributeName": "cell", "AttributeType": "S"}, {"AttributeName": "created_at", "AttributeType": "N"}],
                   KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
                   GlobalSecondaryIndexes=[{"IndexName": "cell-index", "KeySchema": [{"AttributeName": "cell", "KeyType": "HASH"}, {"AttributeName": "created_at", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}}])
    c.create_table(TableName="vol", BillingMode="PAY_PER_REQUEST", AttributeDefinitions=[{"AttributeName": "volunteer_id", "AttributeType": "S"}], KeySchema=[{"AttributeName": "volunteer_id", "KeyType": "HASH"}])
    c.create_table(TableName="off", BillingMode="PAY_PER_REQUEST",
                   AttributeDefinitions=[{"AttributeName": "offer_id", "AttributeType": "S"}, {"AttributeName": "volunteer_id", "AttributeType": "S"}],
                   KeySchema=[{"AttributeName": "offer_id", "KeyType": "HASH"}],
                   GlobalSecondaryIndexes=[{"IndexName": "volunteer-index", "KeySchema": [{"AttributeName": "volunteer_id", "KeyType": "HASH"}], "Projection": {"ProjectionType": "ALL"}}])


def ev(method, path, body=None, key=None):
    return {"requestContext": {"http": {"method": method}}, "rawPath": path, "body": json.dumps(body) if body else None,
            "headers": {"x-coordinator-key": key} if key else {}}


@mock_aws
def run():
    setup()
    import api, extract, steps
    from common import VOL
    sfn = mock.MagicMock()
    sfn.start_execution.return_value = {"executionArn": "arn:exec"}
    api.sfn = sfn

    for vid, name, lat in (("v-near", "Ravi", 18.5090), ("v-far", "Sana", 18.5500)):
        VOL.put_item(Item={"volunteer_id": vid, "name": name, "lat": str(lat), "lng": "73.9259", "available": True, "skills": ["general"]})
    from decimal import Decimal
    for vid, lat in (("v-near", "18.5090"), ("v-far", "18.5500")):
        VOL.update_item(Key={"volunteer_id": vid}, UpdateExpression="SET lat=:a, lng=:b", ExpressionAttributeValues={":a": Decimal(lat), ":b": Decimal("73.9259")})

    # 1. intake validation + creation
    assert api.handler(ev("POST", "/requests", {"text": "x", "lat": 1, "lng": 1}), None)["statusCode"] == 400
    r1 = json.loads(api.handler(ev("POST", "/requests", {"text": "Family of 4 trapped on roof, one elderly woman, water rising", "lat": 18.5089, "lng": 73.9259, "name": "A", "phone": "99999"}), None)["body"])["request_id"]
    r2 = json.loads(api.handler(ev("POST", "/requests", {"text": "फंसे हैं छत पर 3 लोग बचाओ", "lat": 18.5089, "lng": 73.9259}), None)["body"])["request_id"]

    # 2. extract + dedupe + score (r1 first, then r2 should merge into r1)
    for rid in (r1, r2):
        extract.handler({"request_id": rid}, None)
    assert steps.handler({"action": "dedupe", "request_id": r1}, None)["duplicate"] is False
    assert steps.handler({"action": "dedupe", "request_id": r2}, None)["duplicate"] is True
    steps.handler({"action": "score", "request_id": r1}, None)
    status = json.loads(api.handler(ev("GET", f"/requests/{r2}"), None)["body"])
    assert status["status"] == "merged", status
    rows = json.loads(api.handler(ev("GET", "/requests", key="secret-key-1"), None)["body"])
    assert api.handler(ev("GET", "/requests"), None)["statusCode"] == 401
    top = rows["requests"][0]
    assert top["request_id"] == r1 and top["need"] == "rescue" and top["people"] == 7 and top["urgency"] == 100, top

    # 3. find nearest volunteers, offer, race to accept
    found = steps.handler({"action": "find", "request_id": r1}, None)
    assert [c["volunteer_id"] for c in found["candidates"]] == ["v-near", "v-far"], found
    steps.handler({"action": "offer", "request_id": r1, "candidates": found["candidates"], "idx": 0, "token": "TOKEN-1"}, None)
    offers = json.loads(api.handler(ev("GET", "/volunteers/v-near/offers"), None)["body"])["offers"]
    assert len(offers) == 1 and "phone" not in offers[0]
    oid = offers[0]["offer_id"]
    ok = api.handler(ev("POST", f"/offers/{oid}/respond", {"action": "accept"}), None)
    again = api.handler(ev("POST", f"/offers/{oid}/respond", {"action": "accept"}), None)
    assert ok["statusCode"] == 200 and again["statusCode"] == 409, (ok, again)
    sfn.send_task_success.assert_called_once()
    prof = json.loads(api.handler(ev("GET", "/volunteers/v-near"), None)["body"])
    assert prof["task"]["phone"] == "99999" and prof["available"] is False

    # 4. completion callback, then close frees the volunteer
    steps.handler({"action": "await", "request_id": r1, "token": "TOKEN-2"}, None)
    assert api.handler(ev("POST", f"/requests/{r1}/complete", {"volunteer_id": "v-far", "outcome": "done"}), None)["statusCode"] == 403
    assert api.handler(ev("POST", f"/requests/{r1}/complete", {"volunteer_id": "v-near", "outcome": "done"}), None)["statusCode"] == 200
    steps.handler({"action": "close", "request_id": r1}, None)
    prof = json.loads(api.handler(ev("GET", "/volunteers/v-near"), None)["body"])
    assert prof["available"] is True and prof["task"] is None

    # 5. unreachable path: reopen raises urgency and skips volunteers already tried
    r3 = json.loads(api.handler(ev("POST", "/requests", {"text": "Need insulin and a doctor, 1 person", "lat": 18.5, "lng": 73.93}), None)["body"])["request_id"]
    extract.handler({"request_id": r3}, None)
    steps.handler({"action": "score", "request_id": r3}, None)
    found = steps.handler({"action": "find", "request_id": r3}, None)
    steps.handler({"action": "offer", "request_id": r3, "candidates": found["candidates"], "idx": 0, "token": "T3"}, None)
    oid = json.loads(api.handler(ev("GET", f"/volunteers/{found['candidates'][0]['volunteer_id']}/offers"), None)["body"])["offers"][0]["offer_id"]
    api.handler(ev("POST", f"/offers/{oid}/respond", {"action": "decline"}), None)
    sfn.send_task_failure.assert_called_once()
    re_ = steps.handler({"action": "reopen", "request_id": r3}, None)
    assert re_["reopen_count"] == 1
    found2 = steps.handler({"action": "find", "request_id": r3}, None)
    assert found["candidates"][0]["volunteer_id"] not in [c["volunteer_id"] for c in found2["candidates"]]
    print("ALL CHECKS PASSED")


run()
