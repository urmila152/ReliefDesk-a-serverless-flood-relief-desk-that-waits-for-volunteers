"""Add fictional volunteers around the demo areas.  Usage: python scripts/seed.py [stack-name]"""
import secrets
from decimal import Decimal

import boto3

from _stack import outputs, stack_name

out = outputs(stack_name())
table = boto3.resource("dynamodb", region_name=out.get("Region")).Table(out["VolunteersTableName"])
PEOPLE = [  # name, lat, lng, skills
    ("Ravi Patil", 18.5100, 73.9250, ["general"]), ("Sana Sheikh", 18.5060, 73.9300, ["medical", "general"]),
    ("Omkar Jadhav", 18.5090, 73.9200, ["rescue"]), ("Neha Kulkarni", 18.5080, 73.8100, ["general"]),
    ("Imran Khan", 18.5050, 73.8050, ["food", "water", "shelter"]), ("Aditi Deshmukh", 18.4600, 73.8650, ["general"]),
    ("Vikram More", 18.5560, 73.8090, ["medical", "rescue"]), ("Pooja Gaikwad", 18.6280, 73.8020, ["general"]),
]
print("Volunteer links (one per person):")
for name, lat, lng, skills in PEOPLE:
    vid = f"{name.split()[0].lower()}-{secrets.token_hex(3)}"
    table.put_item(Item={"volunteer_id": vid, "name": name, "lat": Decimal(str(lat)), "lng": Decimal(str(lng)), "available": True, "skills": skills})
    print(f"  {name:16} {out['SiteUrl']}/volunteer.html?v={vid}")
