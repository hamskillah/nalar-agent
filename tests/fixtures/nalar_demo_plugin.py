"""A user-level plugin fixture for the test suite."""


def tools():
    return [{
        "name": "demo_lookup",
        "description": "Look up a demo value.",
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {"key": {"type": "string"}},
                       "required": ["key"]},
        "handler": lambda args: {"key": args.get("key")},
    }]
