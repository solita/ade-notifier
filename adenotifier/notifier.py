import requests
import logging
import re
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from .manifest import Manifest
from typing import List, Set, Dict, Tuple, Optional
from tenacity import (
    retry,
    stop_after_attempt,
    retry_if_exception_type,
    wait_exponential,
    before_sleep_log,
)

import sys

logging.basicConfig(stream=sys.stderr, level=logging.DEBUG)
logger = logging.getLogger(__name__)


# Use tenacity decorator to retry on RequestException, just in case there are network issues.
@retry(
    wait=wait_exponential(multiplier=1, min=4, max=10),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(requests.exceptions.RequestException),
    before_sleep=before_sleep_log(logger, logging.DEBUG),
)
def search_manifests(
    source_system_name: str,
    source_entity_name: str,
    base_url: str,
    notify_api_key: str,
    notify_api_key_secret: str,
    state: str,
):
    """Searches manifests from ADE Notify API.

    Args:
        source_system_name (str): Source system name defined in ADE source entity.
        source_entity_name (str): ADE source entity name.
        state (str): Manifest state, supported values: "OPEN", "NOTIFIED", "FAILED", "ARCHIVED".
        base_url (str): ADE Notify API base url, e.g. https://external-api.{environment}.datahub.{tenant}.saas.agiledataengine.com:443/notify-api.
        notify_api_key (str): ADE Notify API key.
        notify_api_key_secret (str): ADE Notify API key secret.

    Returns:
        List [str] of manifest ids.

    """

    session = requests.Session()
    session.auth = (notify_api_key, notify_api_key_secret)
    session.headers.update({"Content-Type": "application/json"})
    session.mount(
        "https://",
        HTTPAdapter(
            max_retries=Retry(
                total=3,
                status_forcelist=[401, 404, 429, 500, 502, 503, 504],
                backoff_factor=2,
                allowed_methods=None,
                raise_on_status=True,
            )
        ),
    )  # HTTP request retry settings.
    request_url = "{0}/tenants/local/installations/local/environments/local/source-systems/{1}/source-entities/{2}/manifests".format(
        base_url, source_system_name, source_entity_name
    )

    if state != "":
        response = session.get(request_url + "?state={0}".format(state.upper()))
    else:
        response = session.get(request_url)
    response.raise_for_status()

    if response.status_code == 200:
        manifests = response.json()
        if manifests != []:
            # Ordering manifests by created time
            manifests = sorted(manifests, key=lambda i: i["created"])

    return manifests


def parse_batch(file_url: str, regexp: str):
    """Parses batch number from given file url with given regular expression.

    Args:
        file_url (str): Source file url.
        regexp (str): Regular expression for finding the batch number from the source file url.
            Supports capturing groups, which are concatenated before casting to integer.

    Returns:
        Int batch number.

    """

    batch = ""
    result = re.search(regexp, file_url)

    for group in result.groups():
        batch += group

    return int(batch)


def add_to_manifest(
    file_url: str,
    source: object,
    base_url: str,
    notify_api_key: str,
    notify_api_key_secret: str,
):
    """Utilizes Manifest class and other functions to add the given file_url to a manifest for the given configured data source.

    Args:
        file_url (str): Source file url.
        source (object): Data source configuration JSON object. See notifier documentation for format & required attributes.
        base_url (str): ADE Notify API base url, e.g. https://external-api.{environment}.datahub.{tenant}.saas.agiledataengine.com:443/notify-api.
        notify_api_key (str): ADE Notify API key.
        notify_api_key_secret (str): ADE Notify API key secret.

    Returns:
        Manifest object.

    """

    # Set single_file_manifest based on configuration
    if "single_file_manifest" in source["attributes"]:
        if source["attributes"]["single_file_manifest"]:
            single_file_manifest = True
        else:
            single_file_manifest = False
    else:
        single_file_manifest = False

    open_manifest_ids = []

    # Search open manifests for data source if not single_file_manifest
    if not single_file_manifest:
        open_manifests = search_manifests(
            source_system_name=source["attributes"]["ade_source_system"],
            source_entity_name=source["attributes"]["ade_source_entity"],
            base_url=base_url,
            notify_api_key=notify_api_key,
            notify_api_key_secret=notify_api_key_secret,
            state="OPEN",
        )

        for open_manifest_id in open_manifests:
            open_manifest_ids.append(open_manifest_id["id"])

        logging.info("Open manifests: {0}".format(open_manifest_ids))

    # Initialize a manifest object with mandatory attributes.
    manifest = Manifest(
        base_url=base_url,
        source_system_name=source["attributes"]["ade_source_system"],
        source_entity_name=source["attributes"]["ade_source_entity"],
        format=source["manifest_parameters"]["format"],
        notify_api_key=notify_api_key,
        notify_api_key_secret=notify_api_key_secret,
    )

    # Set optional manifest attributes if configured in data source.
    if "columns" in source["manifest_parameters"]:
        manifest.columns = source["manifest_parameters"]["columns"]
    if "compression" in source["manifest_parameters"]:
        manifest.compression = source["manifest_parameters"]["compression"]
    if "delim" in source["manifest_parameters"]:
        manifest.delim = source["manifest_parameters"]["delim"]
    if "fullscanned" in source["manifest_parameters"]:
        manifest.fullscanned = source["manifest_parameters"]["fullscanned"]
    if "skiph" in source["manifest_parameters"]:
        manifest.skiph = source["manifest_parameters"]["skiph"]

    if open_manifest_ids == []:
        # Create a new manifest if open manifests are not found.
        manifest.create()
        logging.info("Manifest created: {0}".format(manifest.id))
    else:
        if "max_files_in_manifest" in source["attributes"]:
            manifest.fetch_manifest(open_manifest_ids[-1])

            manifest.fetch_manifest_entries()
            manifest_entries = manifest.manifest_entries

            if len(manifest_entries) >= source["attributes"]["max_files_in_manifest"]:
                logging.info("Max files in manifest reached. Creating a new manifest")
                # Create a new manifest if current manifest has already reached max files limit
                manifest.create()
                logging.info("Manifest created: {0}".format(manifest.id))
            else:
                # Use latest existing manifest if open manifests are found and max_files_in_manifest not reached.
                manifest.fetch_manifest(open_manifest_ids[-1])
                logging.info("Using open manifest: {0}".format(manifest.id))
        else:
            # Use latest existing manifest if open manifests are found.
            manifest.fetch_manifest(open_manifest_ids[-1])
            logging.info("Using open manifest: {0}".format(manifest.id))

    if (
        "path_replace" in source["attributes"]
        and "path_replace_with" in source["attributes"]
    ):
        # Modify manifest entry file url if configured.
        entry_path = file_url.replace(
            source["attributes"]["path_replace"],
            source["attributes"]["path_replace_with"],
        )
    else:
        entry_path = file_url

    if "batch_from_file_path_regex" in source["attributes"]:
        # Parse entry specific batch number from file name if configured.
        try:
            batch = parse_batch(
                file_url, source["attributes"]["batch_from_file_path_regex"]
            )
            logging.info("Batch: {0}".format(batch))
        except Exception as e:
            batch = None
            logging.warning("Batch parsing failed:\n{0}".format(e))
    else:
        batch = None

    # Add entry to manifest.
    try:
        manifest.add_entry(entry_path, batch)
    except Exception as e:
        # Retry with a new manifest if e.g. an uncontrolled parallel execution has closed the manifest
        logging.warning(
            "Adding entry to manifest failed, retrying with a new manifest."
        )
        manifest.create()
        logging.info("Manifest created: {0}".format(manifest.id))
        manifest.add_entry(entry_path, batch)

    logging.info("Added entry: {0}".format(entry_path))

    # Notify manifest if single_file_manifest = true
    if single_file_manifest:
        logging.info("Single_file_manifest set as true, notifying.")
        manifest.notify()
        logging.info("Notified manifest: {0}.".format(manifest.id))

    return manifest


def add_multiple_entries_to_manifest(
    entries: List[dict],
    source: object,
    base_url: str,
    notify_api_key: str,
    notify_api_key_secret: str,
    batch: int = None,
):
    """Utilizes Manifest class and other functions to add the given file_url to a manifest for the given configured data source.

    Args:
        entries (list): Source file url.
        source (object): Data source configuration JSON object. See notifier documentation for format & required attributes.
        base_url (str): ADE Notify API base url, e.g. https://external-api.{environment}.datahub.{tenant}.saas.agiledataengine.com:443/notify-api.
        notify_api_key (str): ADE Notify API key.
        notify_api_key_secret (str): ADE Notify API key secret.
        batch (int): Optional manifest-level batch id.

    Returns:
        Manifest object.

    """

    # Initialize a manifest object with mandatory attributes.
    manifest = Manifest(
        base_url=base_url,
        source_system_name=source["attributes"]["ade_source_system"],
        source_entity_name=source["attributes"]["ade_source_entity"],
        format=source["manifest_parameters"]["format"],
        notify_api_key=notify_api_key,
        notify_api_key_secret=notify_api_key_secret,
    )

    # Set optional manifest attributes if configured in data source.
    if "columns" in source["manifest_parameters"]:
        manifest.columns = source["manifest_parameters"]["columns"]
    if "compression" in source["manifest_parameters"]:
        manifest.compression = source["manifest_parameters"]["compression"]
    if "delim" in source["manifest_parameters"]:
        manifest.delim = source["manifest_parameters"]["delim"]
    if "fullscanned" in source["manifest_parameters"]:
        manifest.fullscanned = source["manifest_parameters"]["fullscanned"]
    if "skiph" in source["manifest_parameters"]:
        manifest.skiph = source["manifest_parameters"]["skiph"]

    # Setting manifest-level batch if needed
    if batch is not None:
        manifest.batch = batch

    # Create a new manifest.
    manifest.create()
    logging.info("Manifest created: {0}".format(manifest.id))

    if (
        "path_replace" in source["attributes"]
        and "path_replace_with" in source["attributes"]
    ):
        # Modify manifest entry file url if configured.
        for entry in entries:
            entry["sourceFile"] = entry["sourceFile"].replace(
                source["attributes"]["path_replace"],
                source["attributes"]["path_replace_with"],
            )

    if "batch_from_file_path_regex" in source["attributes"]:
        # Parse entry specific batch number from file name if configured.
        try:
            for entry_batch in entries:
                entry_batch["batch"] = parse_batch(
                    entry["sourceFile"],
                    source["attributes"]["batch_from_file_path_regex"],
                )
                logging.info("Batch: {0}".format(batch))
        except Exception as e:
            batch = None
            logging.warning("Batch parsing failed:\n{0}".format(e))
    else:
        batch = None

    # Add entry to manifest.
    manifest.add_entries(entries)
    logging.info("Added entries: {0}".format(entries))

    manifest.notify(manifest.id)

    return manifest


def notify_manifests(
    source: object, base_url: str, notify_api_key: str, notify_api_key_secret: str
):
    """Utilizes Manifest class and other functions to notify all open manifests for the given configured data source.

    Args:
        source (object): Data source configuration JSON object. See notifier documentation for format & required attributes.
        base_url (str): ADE Notify API base url, e.g. https://external-api.{environment}.datahub.{tenant}.saas.agiledataengine.com:443/notify-api.
        notify_api_key (str): ADE Notify API key.
        notify_api_key_secret (str): ADE Notify API key secret.

    Returns:
        Array of Manifest objects.

    """

    # Search open manifests for data source.
    open_manifests = search_manifests(
        source_system_name=source["attributes"]["ade_source_system"],
        source_entity_name=source["attributes"]["ade_source_entity"],
        base_url=base_url,
        notify_api_key=notify_api_key,
        notify_api_key_secret=notify_api_key_secret,
        state="OPEN",
    )
    open_manifest_ids = []

    for open_manifest_id in open_manifests:
        open_manifest_ids.append(open_manifest_id["id"])

    # Initialize a manifest object with mandatory attributes.
    manifest = Manifest(
        base_url=base_url,
        source_system_name=source["attributes"]["ade_source_system"],
        source_entity_name=source["attributes"]["ade_source_entity"],
        format=source["manifest_parameters"]["format"],
        notify_api_key=notify_api_key,
        notify_api_key_secret=notify_api_key_secret,
    )

    manifests = []

    if open_manifest_ids == []:
        # Warning if open manifests not found.
        logging.warning(
            "Open manifests for source {0} not found when attempting to notify.".format(
                source["id"]
            )
        )
    else:
        # Notify all open manifests for data source.
        for manifest_id in open_manifest_ids:
            manifest.fetch_manifest(manifest_id)
            manifest.notify()
            logging.info("Notified manifest: {0}.".format(manifest.id))
            manifests.append(manifest)

    return manifests
