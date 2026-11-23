import json
import sys

for line in sys.stdin:
    try:
        request = json.loads(line)
    except ValueError:
        continue
    if request.get("method") != "initialize":
        continue
    sys.stdout.write("[" * 20_000 + "]" * 20_000 + "\n")
    sys.stdout.flush()
    break
