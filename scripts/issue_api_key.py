"""Issue a RugBuster Solana API key (beta: every plan is free).

    DATABASE_URL=... python scripts/issue_api_key.py --label "Name or company" --plan starter

The key is printed once. Only its sha256 is stored, so a lost key cannot be
recovered: issue a new one and revoke the old with
    UPDATE score_api_keys SET active = FALSE WHERE label = '...';
"""
import argparse
import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import quota  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", required=True, help="who the key is for")
    parser.add_argument("--plan", required=True, choices=sorted(quota.PLANS))
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set")
    key = quota.issue_key(lambda: psycopg2.connect(url), args.label, args.plan)
    print(f"{args.plan} key for {args.label} ({quota.PLANS[args.plan]} checks a day):")
    print(key)


if __name__ == "__main__":
    main()
