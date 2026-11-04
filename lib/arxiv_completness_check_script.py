import logging
import os
import time
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
]

TODAY = date.today()
if TODAY.weekday() == 0:
    DEFAULT_FROM_DATE = TODAY - timedelta(days=3)
else:
    DEFAULT_FROM_DATE = TODAY - timedelta(days=1)

HEP_API_URL = "https://inspirehep.net/api/literature"

BACKOFFICE_SEARCH_URL = "https://inspirehep.net/backoffice/literature/search"
BACKOFFICE_MAX_HITS = 100
BACKOFFICE_INDEX = "inspire-backoffice-backend-hep-workflows"


class LiteratureSearch(Search):
    connection = connections.create_connection(
        hosts=[f"https://{os.environ['OPENSEARCH_INSPIRE_HOST']}/os"],
        timeout=30,
        http_auth=(
            os.environ["OPENSEARCH_INSPIRE_USER"],
            os.environ["OPENSEARCH_INSPIRE_PASSWORD"],
        ),
        ca_certs=os.environ.get(
            "CERTIFICATE_PATH",
            "/home/errbot/certs/CERN_Root_Certification_Authority_2.pem",
        ),
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


def backoffice_check(eprints, from_date=None, to_date=None):
    """Return {eprint: [{"id": ..., "status": ...}, ...]} for Backoffice workflows.

    If ``from_date``/``to_date`` are given, only workflows acquired in that
    interval are considered, otherwise any workflow matches. Eprints without
    workflows are not included.
    """
    found_eprints = {}
    search = LiteratureSearch(index=BACKOFFICE_INDEX)
    source_fields = ["id", "status", "data.arxiv_eprints.value"]
    for eprint in eprints:
        query = Q("term", data__arxiv_eprints__value=eprint)
        if from_date is not None and to_date is not None:
            query &= Q(
                "range",
                data__acquisition_source__datetime={
                    "gte": from_date,
                    "lte": to_date,
                },
            )
        result = (
            search.query(query)
            .params(size=BACKOFFICE_MAX_HITS, _source=source_fields)
            .execute()
        )
        hits = [
            {"id": hit._source["id"], "status": hit._source["status"]}
            for hit in result["hits"]["hits"]
        ]
        if hits:
            found_eprints[eprint] = hits
    return found_eprints


def _fetch_inspire_record_by_api(eprint):
    request_payload = {"q": f"arxiv:{eprint}"}
    request = requests.get(HEP_API_URL, params=request_payload)
    if request.json()["hits"]["hits"]:
        inspire_control_number_for_eprint = request.json()["hits"]["hits"][0][
            "metadata"
        ]["control_number"]
        return inspire_control_number_for_eprint


def _format_eprint_list(eprints, link_to_backoffice=False):
    lines = []
    for eprint in sorted(eprints):
        if link_to_backoffice:
            lines.append(f"* [{eprint}]({BACKOFFICE_SEARCH_URL}?q={eprint})")
        else:
            lines.append(f"* {eprint}")
    return "\n".join(lines)


def prepare_message(
    arxiv_eprints,
    backoffice_eprints,
    backoffice_eprints_outside_interval,
    inspire_eprints,
    check_start_time,
    from_date,
    to_date,
):
    in_interval_eprint_set = set(backoffice_eprints)
    # The two dicts are disjoint: the outside-interval check only covers
    # eprints that were not found in the interval.
    all_backoffice_eprints = {
        **backoffice_eprints_outside_interval,
        **backoffice_eprints,
    }
    backoffice_eprint_set = set(all_backoffice_eprints)

    # The gap is measured against the interval: every eprint published by
    # arXiv in the interval should have been received in it as well.
    gap = arxiv_eprints - in_interval_eprint_set
    # The lists below are checked against the Backoffice overall.
    not_in_backoffice = arxiv_eprints - backoffice_eprint_set
    not_in_inspire = arxiv_eprints - inspire_eprints
    missing_everywhere = not_in_backoffice & not_in_inspire
    stuck_in_backoffice = not_in_inspire & backoffice_eprint_set

    summary = (
        f"**ArXiv Harvest Check** started at {check_start_time}.\n"
        "Summary:\n"
        f"* **{len(arxiv_eprints)}** eprints published by arXiv"
        f" from {from_date} to {to_date}, out of which **{len(inspire_eprints)}** are in INSPIRE \n"
        f"* **{len(in_interval_eprint_set)}** received in the Backoffice"
        f" in this interval ({len(backoffice_eprint_set)} total"
        " in the Backoffice)\n"
    )

    sections = []

    if gap:
        sections.append(
            f":warning: **Gap of {len(gap)} eprints** between arXiv and the"
            " Backoffice: they were published by arXiv but not received in"
            f" the Backoffice from {from_date} to {to_date}."
        )
    else:
        sections.append(
            ":check: All eprints published by arXiv were received in the"
            " Backoffice in this interval."
        )

    if missing_everywhere:
        sections.append(
            f":exclamation: **{len(missing_everywhere)} eprints neither in the"
            " Backoffice nor in INSPIRE:**\n" + _format_eprint_list(missing_everywhere)
        )

    if stuck_in_backoffice:
        sections.append(
            f"**{len(stuck_in_backoffice)} eprints not in INSPIRE but present"
            " in the Backoffice** (awaiting curation, in error, running etc):\n"
            + _format_eprint_list(stuck_in_backoffice, link_to_backoffice=True)
        )

    if not not_in_inspire:
        sections.append("All eprints are on INSPIRE! :confetti:")

    return "\n\n".join([summary, *sections])


def completeness_check(from_date, to_date):
    check_start_date = datetime.now()
    eprints = fetch_arxiv_eprints(from_date, to_date)
    eprints_on_backoffice = backoffice_check(eprints, from_date, to_date)
    eprints_not_in_interval = eprints - set(eprints_on_backoffice)
    eprints_on_backoffice_outside_interval = backoffice_check(eprints_not_in_interval)
    eprints_on_inspire = inspire_check(eprints)
    message = prepare_message(
        eprints,
        eprints_on_backoffice,
        eprints_on_backoffice_outside_interval,
        eprints_on_inspire,
        check_start_date,
        from_date,
        to_date,
    )
    return message


if __name__ == "__main__":
    completeness_check(DEFAULT_FROM_DATE, TODAY)
