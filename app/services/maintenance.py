"""Operational maintenance tasks for the trading database.

Usage:
    python -m app.services.maintenance --purge-stale-validations
"""
import argparse
import logging

from app.database import init_db, purge_strategy_validation_before_scheme
from app.validation import VALIDATION_SCHEME_VERSION

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("maintenance")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--purge-stale-validations", action="store_true",
        help="Delete strategy validation records from older scheme versions")
    args = parser.parse_args()
    if not args.purge_stale_validations:
        parser.print_help()
        return
    init_db()
    deleted = purge_strategy_validation_before_scheme(VALIDATION_SCHEME_VERSION)
    log.info("Deleted %s validation records from schemes older than %s",
             deleted, VALIDATION_SCHEME_VERSION)


if __name__ == "__main__":
    main()
