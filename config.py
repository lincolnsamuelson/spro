import json
import os


def load_config(path: str = None) -> dict:
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(path) as f:
        return json.load(f)
