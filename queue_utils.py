import os
import json

QUEUE_FILE = os.path.join(os.getcwd(), "output", "print_queue.json")

def add_to_queue(order):
    queue = []
    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            try:
                queue = json.load(f)
            except Exception:
                queue = []
    queue.append(order)
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)

def get_queue():
    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception:
                return []
    return []
