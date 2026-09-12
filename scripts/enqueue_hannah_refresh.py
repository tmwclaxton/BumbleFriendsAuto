from src.phone_queue import enqueue, queue_snapshot

print("queue", [(j["id"], j["kind"], j.get("name"), j["status"]) for j in queue_snapshot()])
print(enqueue("refresh", "Hannah", phone_id="toby"))
