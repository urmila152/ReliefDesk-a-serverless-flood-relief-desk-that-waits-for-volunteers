# ReliefDesk

Serverless relief-request desk for floods and disasters. People ask for help in plain language (English, Hindi, Marathi); a Step Functions workflow understands the request, merges duplicates, scores urgency, offers it to the nearest free volunteer, **pauses until they answer**, and escalates if nobody does. Prototype with fictional data, not an emergency service.

## How it works

```
Browser (S3 + CloudFront, HTTPS) --> API Gateway (HTTP API) --> ApiFn (Lambda) --> DynamoDB
                                                                   |  StartExecution
                                                                   v
 Extract (Bedrock or rules) -> Dedupe -> Score -> FindVolunteers -> OfferToVolunteer (waits for callback token, 10 min)
        -> AwaitCompletion (waits for "done" or "could not reach", 45 min) -> Close | Reopen (urgency +10, next volunteer) -> Escalate
```

- **Callback tokens:** the offer step stores a task token in DynamoDB and returns. The execution costs almost nothing while it waits. Accept or decline calls `SendTaskSuccess` or `SendTaskFailure`.
- **Safe accept:** one DynamoDB transaction checks that the request is still open, the volunteer is still free and the offer has not expired, so two volunteers can never both take the same request.
- **Fallbacks:** if Bedrock is unavailable or not configured, keyword rules still extract need, people and vulnerability. Volunteers see offers on their own page even without Telegram.
- **Scoring is rules, not the model:** the LLM only reads the text and writes a one-line summary, so urgency stays predictable.

## Deploy (about 15 minutes)

Prerequisites: AWS CLI configured, [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html), Python 3.11+. Docker is not needed.

```
sam build
sam deploy --guided --stack-name reliefdesk
```

Answer the prompts: pick a region, set `CoordinatorKey` (your dashboard password, 8+ characters), and leave the rest at the defaults for now. Allow SAM to create roles. Then:

```
pip install boto3
python scripts/publish_frontend.py reliefdesk    # writes config.js, uploads the site, prints the URLs
python scripts/seed.py reliefdesk                # 8 fictional volunteers, prints each one's link
```

**Optional: Bedrock extraction.** Enable a model in the Bedrock console (Model access), find its model or inference-profile ID for your region, then redeploy with `--parameter-overrides BedrockModelId=<id>`. Leave it empty to use rules only.

**Optional: Telegram alerts.** Create a bot with @BotFather, pass `TelegramBotToken=<token>`, and add `telegram_chat_id` to a volunteer item in DynamoDB after they press Start on the bot.

## Deploy without CloudFormation (if `sam deploy` is blocked on your account)

Some accounts, such as free-plan accounts, deny CloudFormation. `scripts/deploy_cli.py` creates the same system directly through the AWS APIs: IAM roles, 3 DynamoDB tables, 3 Lambda functions, the Step Functions workflow and an HTTP API. The API also serves the web pages over HTTPS, so there is no S3 or CloudFront in this flavor. It is safe to re-run.

```
pip install boto3
python scripts/deploy_cli.py --key YOUR_DASHBOARD_PASSWORD --region ap-south-1 --offer-timeout 60
python scripts/seed.py
```

If one of the services is restricted on your account, the script stops at that step and prints a `BLOCKED at step ...` message. Remove everything later with `python scripts/destroy_cli.py`.

## Demo script (2 minutes)

1. Open each volunteer link on a phone or in separate tabs, and the coordinator desk on your laptop.
2. Submit a request on the intake page ("4 people trapped on the roof, one elderly woman"). Watch it appear on the desk, ranked by urgency.
3. Submit a second one in the same area: it merges into the first and raises its urgency.
4. Click the request on the desk. The right panel shows the real Step Functions states, with the current one pulsing at "Offer sent, waiting for reply".
5. Accept on the nearest volunteer's page. The run resumes and moves to "Volunteer on task". Open the Step Functions console for the execution graph screenshot.
6. Decline or ignore an offer to show it move to the next volunteer. Press "Could not reach them" to show the reopen with raised urgency.

For a faster demo set `OfferTimeoutSeconds=60` when deploying.

## Prove the scaling

```
python scripts/loadtest.py <ApiUrl> 300 25
python scripts/reset.py reliefdesk     # clears the demo data afterwards
```

The load test sets `demo_mode`, which skips the model, so it costs almost nothing. Check the Lambda concurrency graph in CloudWatch afterwards: it rises during the burst and falls back to zero. The HTTP API is throttled at 50 requests/second (burst 100) as a credit guard.

## Run the local test

```
pip install "moto[dynamodb]" boto3
python tests/test_flow.py
```

Covers intake, extraction, duplicate merge, scoring, nearest-volunteer ranking, offers, the accept race, completion, decline, and reopen.

## Teardown

```
aws s3 rm s3://<SiteBucketName> --recursive
sam delete --stack-name reliefdesk
```

## Known limits (say these in the post)

- Duplicate detection uses a ~1 km grid, so two requests across a cell boundary will not merge.
- The coordinator uses one shared key and volunteers use an unguessable link: fine for a prototype, not for production. A real deployment needs proper authentication and partners (NGOs, disaster authorities).
- Dashboard reads scan the table, which is fine at demo scale. Use a status index beyond a few thousand requests.
- Escalated requests need a human coordinator. There is no automatic retry once the volunteer list is exhausted.
