"""Write config.js with the deployed API URL and upload the site to S3.  Usage: python scripts/publish_frontend.py [stack-name]"""
import mimetypes, pathlib

import boto3

from _stack import outputs, stack_name

out = outputs(stack_name())
site = pathlib.Path(__file__).resolve().parent.parent / "frontend"
(site / "config.js").write_text(f'window.RELIEF_API = "{out["ApiUrl"]}";\n')
s3 = boto3.client("s3")
for f in sorted(site.iterdir()):
    ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
    s3.upload_file(str(f), out["SiteBucketName"], f.name, ExtraArgs={"ContentType": ctype + ("; charset=utf-8" if ctype.startswith("text") or "javascript" in ctype else "")})
    print("uploaded", f.name)
print("\nIntake page:      ", out["SiteUrl"])
print("Coordinator desk: ", out["SiteUrl"] + "/desk.html")
