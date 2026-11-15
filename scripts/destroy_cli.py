"""Remove everything deploy_cli.py created.  Usage: python scripts/destroy_cli.py"""
import json, pathlib

import boto3

ROOT = pathlib.Path(__file__).resolve().parent.parent
out = json.loads((ROOT / "deploy_outputs.json").read_text())
r, P = out["Region"], "reliefdesk"
s = boto3.session.Session(region_name=r)
sfn, lam, ddb, iam, apigw = (s.client(n) for n in ("stepfunctions", "lambda", "dynamodb", "iam", "apigatewayv2"))


def quiet(fn, **kw):
    try:
        fn(**kw)
    except Exception as e:
        print("skip:", str(e)[:100])


for a in apigw.get_apis()["Items"]:
    if a["Name"] == f"{P}-api":
        quiet(apigw.delete_api, ApiId=a["ApiId"])
for e in sfn.list_executions(stateMachineArn=out["StateMachineArn"], statusFilter="RUNNING")["executions"]:
    quiet(sfn.stop_execution, executionArn=e["executionArn"])
quiet(sfn.delete_state_machine, stateMachineArn=out["StateMachineArn"])
for fn in ("extract", "steps", "api"):
    quiet(lam.delete_function, FunctionName=f"{P}-{fn}")
for t in ("requests", "volunteers", "offers"):
    quiet(ddb.delete_table, TableName=f"{P}-{t}")
for role in ("lambda-role", "sfn-role"):
    quiet(iam.delete_role_policy, RoleName=f"{P}-{role}", PolicyName="inline")
    quiet(iam.delete_role, RoleName=f"{P}-{role}")
(ROOT / "deploy_outputs.json").unlink()
print("removed")
