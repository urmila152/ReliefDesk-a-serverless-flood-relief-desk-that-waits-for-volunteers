import json, pathlib, sys

import boto3


def outputs(stack="reliefdesk"):
    f = pathlib.Path(__file__).resolve().parent.parent / "deploy_outputs.json"
    if f.exists():  # written by deploy_cli.py (deployment without CloudFormation)
        return json.loads(f.read_text())
    cf = boto3.client("cloudformation")
    return {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName=stack)["Stacks"][0]["Outputs"]}


def stack_name():
    return sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else "reliefdesk"
