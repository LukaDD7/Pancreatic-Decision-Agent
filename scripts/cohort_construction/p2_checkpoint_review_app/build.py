"""Build one offline HTML from an assigned, validated doctor-only FHIR Bundle."""
import argparse
import json
from pathlib import Path

ASSETS=Path(__file__).parent/'assets'


def build_reviewer_html(bundle, *, assignment_id, expected_packet_sha256):
    from scripts.cohort_construction.p2_checkpoint_fhir.doctor import packet_from_doctor_bundle
    if not isinstance(assignment_id,str) or not assignment_id.strip():
        raise ValueError('REVIEW_ASSIGNMENT_ID_REQUIRED')
    if not isinstance(expected_packet_sha256,str) or len(expected_packet_sha256)!=64:
        raise ValueError('TRUSTED_PACKET_HASH_REQUIRED')
    packet_from_doctor_bundle(bundle,expected_packet_sha256=expected_packet_sha256)
    assigned=dict(format='checkpoint-review-assignment-0.1.0',assignment_id=assignment_id,
                  expected_packet_sha256=expected_packet_sha256,bundle=bundle)
    embedded=json.dumps(assigned,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    # Only this validated doctor assignment enters the delivered HTML. Never
    # read a researcher manifest and depend on CSS to hide it from a physician.
    for char,escape in [('&','\\u0026'),('<','\\u003c'),('>','\\u003e'),('\u2028','\\u2028'),('\u2029','\\u2029')]:
        embedded=embedded.replace(char,escape)
    html=(ASSETS/'reviewer.html').read_text()
    html=html.replace('/*__REVIEWER_CSS__*/',(ASSETS/'reviewer.css').read_text())
    html=html.replace('/*__REVIEWER_JS__*/',(ASSETS/'reviewer.js').read_text())
    return html.replace('__ASSIGNED_DOCTOR_DATA__',embedded)


def main():
    from scripts.cohort_construction.p2_checkpoint_fhir.common import parse_json_unique
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--assignment-id',required=True)
    parser.add_argument('--expected-packet-sha256',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    html=build_reviewer_html(parse_json_unique(args.bundle.read_text()),assignment_id=args.assignment_id,
                             expected_packet_sha256=args.expected_packet_sha256)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x',encoding='utf-8',newline='') as stream:stream.write(html)
    print(json.dumps(dict(status='CREATED',output=str(args.output.resolve()),doctor_only=True),ensure_ascii=False))


if __name__=='__main__':main()
