#!/usr/bin/env python3
import json
import sys

from src.whatsapp import add_to_group, create_group

people = [
    {"name": "Toby", "phone": "07837370669"},
    {"name": "Archie", "phone": "+447398727993"},
    {"name": "Josh 2 LGS", "phone": ""},
    {"name": "Kiki", "phone": ""},
    {"name": "Gianluca", "phone": ""},
    {"name": "Marwan", "phone": ""},
    {"name": "Tinie", "phone": ""},
    {"name": "Matty 343 HIGH WYCOMBE", "phone": ""},
    {"name": "Hannah LGS", "phone": ""},
]
result = create_group(title="LGS #4 (Bucks)", people=people, phone_id="toby")
print("CREATE", json.dumps(result, ensure_ascii=False), flush=True)
if result.get("ok"):
    sys.exit(0)
added = add_to_group(group="LGS #4 (Bucks)", people=people, phone_id="toby")
print("ADD", json.dumps(added, ensure_ascii=False), flush=True)
sys.exit(0 if added.get("ok") else 1)
