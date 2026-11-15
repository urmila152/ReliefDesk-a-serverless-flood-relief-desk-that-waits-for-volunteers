"""Stop running executions and wipe requests/offers; free all volunteers (use before a demo).  Usage: python scripts/reset.py [stack-name]"""
import boto3

from _stack import outputs, stack_name

out = outputs(stack_name())
sfn, ddb = boto3.client("stepfunctions", region_name=out.get("Region")), boto3.resource("dynamodb", region_name=out.get("Region"))
stopped = 0
for page in sfn.get_paginator("list_executions").paginate(stateMachineArn=out["StateMachineArn"], statusFilter="RUNNING"):
    for e in page["executions"]:
        sfn.stop_execution(executionArn=e["executionArn"], cause="demo reset")
        stopped += 1
for name, key in ((out["RequestsTableName"], "request_id"), (out["OffersTableName"], "offer_id")):
    t = ddb.Table(name)
    with t.batch_writer() as bw:
        for page in t.meta.client.get_paginator("scan").paginate(TableName=name, ProjectionExpression=key):
            for i in page["Items"]:
                bw.delete_item(Key={key: i[key]["S"] if isinstance(i[key], dict) else i[key]})
v = ddb.Table(out["VolunteersTableName"])
for i in v.scan()["Items"]:
    v.update_item(Key={"volunteer_id": i["volunteer_id"]}, UpdateExpression="SET available = :t REMOVE current_request", ExpressionAttributeValues={":t": True})
print(f"stopped {stopped} executions, cleared requests and offers, freed volunteers")
