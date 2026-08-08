from jsonschema import Draft7Validator

CARD_SCHEMA = {
    "type": "object",
    "required": ["cards"],
    "additionalProperties": False,
    "properties": {
        "cards": {
            "type": "array",
            "minItems": 0,
            "maxItems": 20,
            "items": {
                "type": "object",
                "required": ["question", "answer"],
                "additionalProperties": False,
                "properties": {
                    "question": {"type": "string", "minLength": 3, "maxLength": 500},
                    "answer": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "tags": {
                        "type": "array",
                        "maxItems": 10,
                        "items": {"type": "string", "maxLength": 40},
                    },
                },
            },
        }
    },
}

_validator = Draft7Validator(CARD_SCHEMA)


def validate(obj):
    """Return (ok, error_message). obj must be a dict already parsed from JSON."""
    errors = sorted(_validator.iter_errors(obj), key=lambda e: e.path)
    if errors:
        return False, "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:3])
    return True, ""
