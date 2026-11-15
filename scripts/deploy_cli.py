"""Deploy ReliefDesk WITHOUT CloudFormation (for accounts where CloudFormation is blocked).
Creates IAM roles, 3 DynamoDB tables, 3 Lambda functions, the Step Functions workflow and an HTTP API
that also serves the web pages over HTTPS. Safe to re-run: it updates what already exists.

Usage: python scripts/deploy_cli.py --key YOUR_DASHBOARD_PASSWORD [--region ap-south-1] [--offer-timeout 60]"""
import argparse, io, json, pathlib, sys, time, zipfile

import boto3
from botocore.exceptions import ClientError

ROOT = pathlib.Path(__file__).resolve().parent.parent
P = "reliefdesk"
ap = argparse.ArgumentParser()
ap.add_argument("--key", required=True, help="coordinator dashboard password (8+ characters)")
ap.add_argument("--region")
ap.add_argument("--offer-timeout", type=int, default=60)
ap.add_argument("--completion-timeout", type=int, default=2700)
ap.add_argument("--bedrock-model", default="")
args = ap.parse_args()
if len(args.key) < 8:
    sys.exit("--key must be at least 8 characters")

sess = boto3.session.Session(region_name=args.region)
region = sess.region_name or sys.exit("No region set. Pass --region ap-south-1")
acct = sess.client("sts").get_caller_identity()["Account"]
iam, ddb, lam, sfn, apigw = (sess.client(n) for n in ("iam", "dynamodb", "lambda", "stepfunctions", "apigatewayv2"))
CURRENT = ""


def step(msg):
    global CURRENT
    CURRENT = msg
    print("-", msg, flush=True)


def retry(fn, tries=8, wait=5, when=lambda e: True):
    for i in range(tries):
        try:
            return fn()
        except ClientError as e:
            if i == tries - 1 or not when(e):
                raise
            time.sleep(wait)


def ensure_role(name, service, actions_resources):
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}]}
    try:
        arn, created = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"], True
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        arn, created = iam.get_role(RoleName=name)["Role"]["Arn"], False
    policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": a, "Resource": r} for a, r in actions_resources]}
    iam.put_role_policy(RoleName=name, PolicyName="inline", PolicyDocument=json.dumps(policy))
    return arn, created


def ensure_table(name, attrs, key, gsis=(), ttl=False):
    try:
        ddb.describe_table(TableName=name)
        return
    except ddb.exceptions.ResourceNotFoundException:
        pass
    kw = {}
    if gsis:
        kw["GlobalSecondaryIndexes"] = [
            {"IndexName": i, "KeySchema": [{"AttributeName": h, "KeyType": "HASH"}] + ([{"AttributeName": r, "KeyType": "RANGE"}] if r else []),
             "Projection": {"ProjectionType": "ALL"}} for i, h, r in gsis]
    ddb.create_table(TableName=name, BillingMode="PAY_PER_REQUEST", KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                     AttributeDefinitions=[{"AttributeName": a, "AttributeType": t} for a, t in attrs], **kw)
    ddb.get_waiter("table_exists").wait(TableName=name)
    if ttl:
        ddb.update_time_to_live(TableName=name, TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"})


def build_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in (ROOT / "src").glob("*.py"):
            z.write(f, f.name)
        for f in (ROOT / "frontend").iterdir():
            if f.name != "config.js":
                z.write(f, "web/" + f.name)
        z.writestr("web/config.js", 'window.RELIEF_API = "";\n')  # pages and API share one origin
    return buf.getvalue()


def ensure_function(name, handler, env, timeout, role_arn, code):
    cfg = dict(Runtime="python3.11", Role=role_arn, Handler=handler, Timeout=timeout, MemorySize=256,
               Architectures=["arm64"], Environment={"Variables": env})
    try:
        lam.get_function(FunctionName=name)
        lam.update_function_code(FunctionName=name, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=name)
        lam.update_function_configuration(FunctionName=name, **{k: v for k, v in cfg.items() if k != "Architectures"})
    except lam.exceptions.ResourceNotFoundException:
        retry(lambda: lam.create_function(FunctionName=name, Code={"ZipFile": code}, **cfg),
              when=lambda e: e.response["Error"]["Code"] == "InvalidParameterValueException")  # role still propagating
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    return lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]


def main():
    tbl = f"arn:aws:dynamodb:{region}:{acct}:table/{P}-*"
    ddb_actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query",
                   "dynamodb:Scan", "dynamodb:BatchWriteItem", "dynamodb:ConditionCheckItem"]
    step("IAM roles")
    lam_role, c1 = ensure_role(f"{P}-lambda-role", "lambda.amazonaws.com", [
        (["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "*"),
        (ddb_actions, [tbl, tbl + "/index/*"]),
        (["states:StartExecution"], f"arn:aws:states:{region}:{acct}:stateMachine:{P}-*"),
        (["states:SendTaskSuccess", "states:SendTaskFailure"], "*"),
        (["states:GetExecutionHistory", "states:DescribeExecution"], f"arn:aws:states:{region}:{acct}:execution:{P}-*:*"),
        (["bedrock:InvokeModel"], "*")])
    sfn_role, c2 = ensure_role(f"{P}-sfn-role", "states.amazonaws.com", [
        (["lambda:InvokeFunction"], f"arn:aws:lambda:{region}:{acct}:function:{P}-*")])
    if c1 or c2:
        time.sleep(12)  # new roles take a few seconds to become usable

    step("DynamoDB tables")
    ensure_table(f"{P}-requests", [("request_id", "S"), ("cell", "S"), ("created_at", "N")], "request_id", [("cell-index", "cell", "created_at")], ttl=True)
    ensure_table(f"{P}-volunteers", [("volunteer_id", "S")], "volunteer_id")
    ensure_table(f"{P}-offers", [("offer_id", "S"), ("volunteer_id", "S")], "offer_id", [("volunteer-index", "volunteer_id", None)], ttl=True)

    step("HTTP API (reserving the address)")
    api = next((a for a in apigw.get_apis()["Items"] if a["Name"] == f"{P}-api"), None) or apigw.create_api(
        Name=f"{P}-api", ProtocolType="HTTP",
        CorsConfiguration={"AllowOrigins": ["*"], "AllowHeaders": ["content-type", "x-coordinator-key"], "AllowMethods": ["GET", "POST", "OPTIONS"]})
    api_id, endpoint = api["ApiId"], api["ApiEndpoint"]

    env = {"REQUESTS_TABLE": f"{P}-requests", "VOLUNTEERS_TABLE": f"{P}-volunteers", "OFFERS_TABLE": f"{P}-offers",
           "COORDINATOR_KEY": args.key, "BEDROCK_MODEL_ID": args.bedrock_model, "TELEGRAM_BOT_TOKEN": "",
           "OFFER_TIMEOUT": str(args.offer_timeout), "SITE_URL": endpoint}
    code = build_zip()
    step("Lambda functions (extract, steps)")
    extract_arn = ensure_function(f"{P}-extract", "extract.handler", env, 30, lam_role, code)
    steps_arn = ensure_function(f"{P}-steps", "steps.handler", env, 20, lam_role, code)

    step("Step Functions workflow")
    definition = (ROOT / "statemachine" / "relief.asl.json").read_text()
    for k, v in {"ExtractFnArn": extract_arn, "StepFnArn": steps_arn, "OfferTimeout": args.offer_timeout, "CompletionTimeout": args.completion_timeout}.items():
        definition = definition.replace("${" + k + "}", str(v))
    json.loads(definition)
    sm = next((m for m in sfn.list_state_machines()["stateMachines"] if m["name"] == f"{P}-workflow"), None)
    if sm:
        sm_arn = sm["stateMachineArn"]
        sfn.update_state_machine(stateMachineArn=sm_arn, definition=definition, roleArn=sfn_role)
    else:
        sm_arn = retry(lambda: sfn.create_state_machine(name=f"{P}-workflow", definition=definition, roleArn=sfn_role, type="STANDARD")["stateMachineArn"],
                       tries=5, wait=6)

    step("Lambda function (api)")
    api_arn = ensure_function(f"{P}-api", "api.handler", {**env, "STATE_MACHINE_ARN": sm_arn}, 20, lam_role, code)

    step("API Gateway routes and stage")
    integ = (apigw.get_integrations(ApiId=api_id)["Items"] or [None])[0] or apigw.create_integration(
        ApiId=api_id, IntegrationType="AWS_PROXY", IntegrationUri=api_arn, PayloadFormatVersion="2.0")
    if not any(r["RouteKey"] == "$default" for r in apigw.get_routes(ApiId=api_id)["Items"]):
        apigw.create_route(ApiId=api_id, RouteKey="$default", Target=f"integrations/{integ['IntegrationId']}")
    throttle = {"ThrottlingBurstLimit": 100, "ThrottlingRateLimit": 50}  # also protects your credits
    if any(s["StageName"] == "$default" for s in apigw.get_stages(ApiId=api_id)["Items"]):
        apigw.update_stage(ApiId=api_id, StageName="$default", DefaultRouteSettings=throttle)
    else:
        apigw.create_stage(ApiId=api_id, StageName="$default", AutoDeploy=True, DefaultRouteSettings=throttle)
    try:
        lam.add_permission(FunctionName=f"{P}-api", StatementId="apigw-invoke", Action="lambda:InvokeFunction",
                           Principal="apigateway.amazonaws.com", SourceArn=f"arn:aws:execute-api:{region}:{acct}:{api_id}/*/*")
    except lam.exceptions.ResourceConflictException:
        pass

    out = {"SiteUrl": endpoint, "ApiUrl": endpoint, "Region": region, "StateMachineArn": sm_arn,
           "RequestsTableName": f"{P}-requests", "VolunteersTableName": f"{P}-volunteers", "OffersTableName": f"{P}-offers"}
    (ROOT / "deploy_outputs.json").write_text(json.dumps(out, indent=2))
    print(f"\nDeployed in {region}.\n  Request page:     {endpoint}/\n  Coordinator desk: {endpoint}/desk.html  (key = what you passed to --key)\n"
          "Next: python scripts/seed.py")


try:
    main()
except ClientError as e:
    msg = str(e)
    if "AccessDenied" in msg or "explicit deny" in msg or "not authorized" in msg:
        sys.exit(f"\nBLOCKED at step '{CURRENT}':\n{msg}\n\nThis service looks restricted on your account. Send me this message.")
    sys.exit(f"\nFAILED at step '{CURRENT}':\n{msg}")
