"""Strict adapter for Dify grouped aggregator objects; gate remains unchanged."""
from evidence_gate import main as verified_gate


def main(query, status_code, body):
    for value in (status_code, body):
        if isinstance(value, dict) and set(value) != {"output"}:
            return verified_gate(query, None, None)
    if isinstance(status_code, dict):
        status_code = status_code["output"]
    if isinstance(body, dict):
        body = body["output"]
    return verified_gate(query, status_code, body)
