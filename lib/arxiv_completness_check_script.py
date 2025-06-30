import logging
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests
from opensearchpy import Q, Search
from opensearchpy.connection import connections
from sickle import Sickle
from sickle.oaiexceptions import NoRecordsMatch

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CORE_CATEGORIES = [
    "physics:hep-lat",
    "physics:hep-ex",
    "physics:hep-ph",
    "physics:hep-th",
    "physics:quant-ph",
]

TODAY = date.today()
if TODAY.weekday() == 0:
    DEFAULT_FROM_DATE = TODAY - timedelta(days=3)
else:
    DEFAULT_FROM_DATE = TODAY - timedelta(days=1)

HEP_API_URL = "https://inspirehep.net/api/literature"
NEW_LINE_SYMBOL = "\n "


class LiteratureSearch(Search):
    connection = connections.create_connection(
        hosts=[f"https://{os.environ['OPENSEARCH_INSPIRE_HOST']}/os"],
        timeout=30,
        http_auth=(
            os.environ["OPENSEARCH_INSPIRE_USER"],
            os.environ["OPENSEARCH_INSPIRE_PASSWORD"],
        ),
        ca_certs="/home/errbot/certs/CERN_Root_Certification_Authority_2.pem",
    )

    def __init__(self, index, **kwargs):
        super().__init__(
            using=kwargs.get("using", LiteratureSearch.connection),
            index=index,
        )


def _get_identifier_value_from_arxiv_identifier(arxiv_identifier):
    return arxiv_identifier.identifier.split(":")[2]


def fetch_arxiv_eprints(from_date, to_date):
    logger.info("Fetching new records from arXiv")
    sickle = Sickle("https://oaipmh.arxiv.org/oai")
    oaiargs = {"metadataPrefix": "oai_dc", "from": from_date, "until": to_date}

    eprints = set()
    for category in CORE_CATEGORIES:
        try:
            identifiers_found = sickle.ListIdentifiers(set=category, **oaiargs)
            identifiers_set = {
                _get_identifier_value_from_arxiv_identifier(identifier)
                for identifier in identifiers_found
            }
            eprints.update(identifiers_set)
            time.sleep(10)
        except NoRecordsMatch:
            continue

    logger.info(f"Fetched {str(len(eprints))} eprints")
    return eprints


def inspire_check(eprints):
    found_eprints = set()
    for eprint in eprints:
        found_control_number = _fetch_inspire_record_by_api(eprint)
        if found_control_number:
            found_eprints.add(eprint)
    return found_eprints


def holdingpen_check(eprints, from_date, to_date):
    found_eprints = defaultdict(list)
    search = LiteratureSearch(index="holdingpen-hep")
    source_fields = ["id", "_workflow.status", "metadata.arxiv_eprints.value"]
    for eprint in eprints:
        query = Q("match", metadata__arxiv_eprints__value=eprint) & Q(
            "range",
            metadata__acquisition_source__datetime={
                "gt": from_date,
                "lte": to_date,
            },
        )
        result = search.query(query).params(size=1, _source=source_fields).execute()
        if result["hits"]["hits"]:
            result = result["hits"]["hits"][0]
            status = result._source["_workflow"]["status"]
            found_eprints[status].append(eprint)
    return found_eprints


def _fetch_inspire_record_by_api(eprint):
    request_payload = {"q": eprint}
    request = requests.get(HEP_API_URL, params=request_payload)
    if request.json()["hits"]["hits"]:
        inspire_control_number_for_eprint = request.json()["hits"]["hits"][0][
            "metadata"
        ]["control_number"]
        return inspire_control_number_for_eprint


def prepare_message(
    arxiv_eprints,
    holdingpen_eprints,
    inspire_eprints,
    check_start_time,
    from_date,
    to_date,
):
    workflows_in_error_state = holdingpen_eprints.get("ERROR", [])
    workflows_in_halted_state = holdingpen_eprints.get("HALTED", [])
    workflows_in_waiting_state = holdingpen_eprints.get("WAITING", [])
    workflows_in_initial_state = holdingpen_eprints.get("INITIAL", [])
    workflows_in_completed_state = holdingpen_eprints.get("COMPLETED", [])

    number_of_holdingpen_matches = sum(
        [len(value) for value in holdingpen_eprints.values()]
    )
    missing_articles = arxiv_eprints - inspire_eprints
    missing_article_info = (
        f"""
:exclamation: Missing records for the following eprints:
{NEW_LINE_SYMBOL.join(
    ['* ' + emprint_number for emprint_number in missing_articles])}"""
        if missing_articles
        else "All eprints are on INSPIRE! :confetti:"
    )

    message = (
        f"""
**ArXiv Harvest Check** started at {check_start_time}.
Summary:\n"""
        + f"* **{len(arxiv_eprints)}** eprints published by"
        f" ArXiv from {from_date} to {to_date},"
        f" **{number_of_holdingpen_matches}**"
        f" received in total, **{len(inspire_eprints)}** in INSPIRE."
        + f"""

{missing_article_info}

Matched eprints on Holdingpen:
- **{len(workflows_in_completed_state)}** corresponding workflows were completed,
- **{len(workflows_in_error_state)}** corresponding  workflows in Error state,
- **{len(workflows_in_halted_state)}** corresponding workflows in Halted state,
- **{len(workflows_in_waiting_state)}** corresponding workflows in Waiting state,
- **{len(workflows_in_initial_state)}** corresponding workflows in Initial state.
    """
    )
    return message


def completeness_check(from_date, to_date):
    check_start_date = datetime.now()
    eprints = fetch_arxiv_eprints(from_date, to_date)
    eprints_on_holdingpen = holdingpen_check(eprints, from_date, to_date)
    eprints_on_inspire = inspire_check(eprints)
    message = prepare_message(
        eprints,
        eprints_on_holdingpen,
        eprints_on_inspire,
        check_start_date,
        from_date,
        to_date,
    )
    return message


if __name__ == "__main__":
    completeness_check(DEFAULT_FROM_DATE, TODAY)
