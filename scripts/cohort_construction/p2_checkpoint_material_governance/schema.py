"""Provider-neutral schema entry; no model is invoked by importing this file."""
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from scripts.cohort_construction.p2_checkpoint_packet.compiler import _validate
def validate_schema(name, value):
    if name == 'MaterialReview':
        from .review import MATERIAL_REVIEW_SCHEMA
        Draft202012Validator(MATERIAL_REVIEW_SCHEMA).validate(value)
        return
    schema = json.loads((Path(__file__).with_name('schemas') / f'{name}.schema.json').read_text(encoding='utf-8'))
    Draft202012Validator(schema).validate(value)
    _validate(value, schema, schema, name)
