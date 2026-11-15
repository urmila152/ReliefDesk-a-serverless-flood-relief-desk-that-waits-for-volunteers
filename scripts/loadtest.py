"""Fire fictional requests at the API to show scale-up.  Usage: python scripts/loadtest.py API_URL [count] [concurrency]
Sets demo_mode so the language model is skipped and the test costs almost nothing."""
import json, random, statistics, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

api = sys.argv[1].rstrip("/")
n = int(sys.argv[2]) if len(sys.argv) > 2 else 200
workers = int(sys.argv[3]) if len(sys.argv) > 3 else 20
AREAS = [(18.5089, 73.9259), (18.5074, 73.8077), (18.4575, 73.8677), (18.5580, 73.8075), (18.6298, 73.7997)]
TEXTS = ["{n} people trapped on the roof, water rising", "Need insulin and a doctor for an elderly man", "No food since yesterday, {n} children",
         "No drinking water for {n} families", "बचाओ, {n} लोग फंसे हैं", "आम्हाला निवारा हवा आहे, {n} जण", "Pregnant woman needs ambulance"]


def one(i):
    la, ln = random.choice(AREAS)
    body = json.dumps({"text": random.choice(TEXTS).format(n=random.randint(2, 8)), "lat": la, "lng": ln, "demo_mode": True}).encode()
    for attempt in range(4):
        t = time.time()
        try:
            req = urllib.request.Request(api + "/requests", body, {"content-type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
            return time.time() - t
        except Exception:
            time.sleep(0.5 * (attempt + 1))  # throttled (429): back off and retry
    return None


start = time.time()
with ThreadPoolExecutor(workers) as ex:
    lat = list(ex.map(one, range(n)))
ok = [x for x in lat if x is not None]
print(f"{len(ok)}/{n} accepted in {time.time() - start:.1f}s | median {statistics.median(ok) * 1000:.0f} ms | p95 {sorted(ok)[int(len(ok) * .95) - 1] * 1000:.0f} ms")
