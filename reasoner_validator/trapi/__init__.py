"""TRAPI Validation Functions."""
from json import dumps
from typing import Optional, Dict, List
from os.path import isfile
import copy
from functools import lru_cache

import jsonschema
import requests

from yaml import load, CLoader as Loader

from reasoner_validator import (
    TRAPI_1_4_0_BETA_SEMVER,
    TRAPI_1_4_0_BETA3_SEMVER,
    TRAPI_1_4_0_BETA4_SEMVER,
    TRAPI_1_4_0_SEMVER
)
from reasoner_validator.report import ValidationReporter
from reasoner_validator.trapi.mapping import check_node_edge_mappings
from reasoner_validator.versioning import (
    SemVer,
    SemVerError,
    get_latest_version,
    GIT_ORG,
    GIT_REPO
)

import logging
logger = logging.getLogger(__name__)

# For testing, set TRAPI API query POST timeouts to 10 minutes == 600 seconds
DEFAULT_TRAPI_POST_TIMEOUT = 600.0


class TRAPIAccessError(RuntimeError):
    pass


@lru_cache()
def _load_schema(schema_version: str) -> Dict:
    """
    Load schema from GitHub version or directly from a local schema file.
    :param schema_version: either a GitHub 'v' prefixed SemVer version of a
           TRAPI schema or a file name (path) from which the TRAPI schema may be read in.
    :return: Dict, schema components
    """
    spec: Dict
    if schema_version.lower().endswith(".yaml"):
        # treat as a candidate TRAPI schema file path or name (the latter, assumed local)
        if not isfile(schema_version):
            raise TRAPIAccessError(f"Candidate TRAPI schema file '{schema_version}' does not exist!")
        with open(schema_version, "r") as schema_file:
            spec = load(schema_file, Loader=Loader)
        if spec is None:
            raise TRAPIAccessError(f"Candidate TRAPI schema file '{schema_version}' could not be retrieved!")
    else:
        result = requests.get(
            f"https://raw.githubusercontent.com/{GIT_ORG}/{GIT_REPO}/{schema_version}/TranslatorReasonerAPI.yaml"
        )
        schema_text: str = result.text
        spec = load(schema_text, Loader=Loader)

    components = spec["components"]["schemas"]
    for component, schema in components.items():
        openapi_to_jsonschema(schema, version=schema_version)
    schemas = dict()
    for component in components:
        # build json schema against which we validate
        subcomponents = copy.deepcopy(components)
        schema = subcomponents.pop(component)
        schema["components"] = {"schemas": subcomponents}
        schemas[component] = schema
    return schemas


def load_schema(target: str):
    """
    Load schema from GitHub release or branch, or from a locally specified YAML schema file.
    :param target: release semver, schema file path (with '.yaml' file extension)
                    or a git branch name, all referencing a target TRAPI schema.
    :return: loaded TRAPI schema
    """
    mapped_release = get_latest_version(target)
    if mapped_release:
        schema_version = mapped_release
    else:
        err_msg: str = f"No TRAPI version {target}"
        logger.error(err_msg)
        raise ValueError(err_msg)

    return _load_schema(schema_version)


def _output(json, flat=False):
    return dumps(json, sort_keys=False, indent=None if flat else 4)


async def call_trapi(url: str, trapi_message):
    """
    Given an url and a TRAPI message, post the message
    to the url and return the status and json response.

    :param url:
    :param trapi_message:
    :return:
    """
    query_url = f'{url}/query'

    # print(f"\ncall_trapi({query_url}):\n\t{dumps(trapi_message, sort_keys=False, indent=4)}", file=stderr, flush=True)

    try:
        response = requests.post(query_url, json=trapi_message, timeout=DEFAULT_TRAPI_POST_TIMEOUT)
    except requests.Timeout:
        # fake response object
        logger.error(
            f"call_trapi(\n\turl: '{url}',\n\ttrapi_message: '{_output(trapi_message)}') - Request POST TimeOut?"
        )
        response = requests.Response()
        response.status_code = 408
    except requests.RequestException as re:
        # perhaps another unexpected Request failure?
        logger.error(
            f"call_trapi(\n\turl: '{url}',\n\ttrapi_message: '{_output(trapi_message)}') - "
            f"Request POST exception: {str(re)}"
        )
        response = requests.Response()
        response.status_code = 408

    response_json = None
    if response.status_code == 200:
        try:
            response_json = response.json()
        except Exception as exc:
            logger.error(f"call_trapi({query_url}) JSON access error: {str(exc)}")

    return {'status_code': response.status_code, 'response_json': response_json}


def fix_nullable(schema) -> None:
    """Fix nullable schema."""
    if "oneOf" in schema:
        schema["oneOf"].append({"type": "null"})
        return
    if "anyOf" in schema:
        schema["anyOf"].append({"type": "null"})
        return
    schema["oneOf"] = [
        {
            key: schema.pop(key)
            for key in list(schema.keys())
        },
        {"type": "null"},
    ]


def map_semver(version: str):
    mapped_semver: Optional[SemVer]
    try:
        mapped_semver = SemVer.from_string(version)
    except SemVerError as sve:
        # if we cannot map the version, then it may simply
        # be a non-versioned branch of the schemata
        logger.error(str(sve))
        mapped_semver = None
    return mapped_semver


def patch_schema(tag: str, schema: Dict, version: str):
    # temporary patch for small TRAPI schema bugs
    mapped_semver: Optional[SemVer] = map_semver(version)
    if (
            mapped_semver and
            (TRAPI_1_4_0_SEMVER >= mapped_semver >= TRAPI_1_4_0_BETA3_SEMVER)
    ):
        if tag == "auxiliary_graphs" and "oneOf" in schema:
            # TODO: very short term workaround for problematic
            #       TRAPI 1.4.0 'auxiliary_graphs' value schema
            schema["type"] = "object"
            value_types: List[str] = schema.pop("oneOf")
            schema["additionalProperties"] = value_types[0]


def openapi_to_jsonschema(schema, version: str) -> None:
    """
    Convert OpenAPI schema to JSON schema.
    :param schema: Dict, in-memory representation of the OpenAPI schema to be validated.
    :param version: str, TRAPI version against which the schema is currently being validated.
    :return:
    """
    mapped_semver: Optional[SemVer] = map_semver(version)

    # we'll only tweak mapped schemata and
    # such releases that are prior to TRAPI 1.4.0-beta
    if (
            mapped_semver and
            not (TRAPI_1_4_0_BETA4_SEMVER >= mapped_semver >= TRAPI_1_4_0_BETA_SEMVER)
    ) and "allOf" in schema:
        # 2022 September 1 hacky patch to rewrite 'allOf'
        # tagged schemata, in TRAPI 1.3.0 or earlier, to 'oneOf'
        schema["oneOf"] = schema.pop("allOf")

    if schema.get("type", None) == "object":
        for tag, prop in schema.get("properties", dict()).items():
            patch_schema(tag, prop, version)
            openapi_to_jsonschema(prop, version=version)

    elif schema.get("type", None) == "array":
        openapi_to_jsonschema(schema.get("items", dict()), version=version)

    if schema.pop("nullable", False):
        fix_nullable(schema)


class TRAPISchemaValidator(ValidationReporter):
    """
    TRAPI Validator is a wrapper class for validating
    conformance of JSON messages to the Translator Reasoner API.
    """
    def __init__(
            self,
            prefix: Optional[str] = None,
            trapi_version: Optional[str] = None,
            strict_validation: Optional[bool] = None
    ):
        """
        TRAPI Validator constructor.

        :param prefix: str named context of the TRAPISchemaValidator, used as a prefix in validation messages.
        :param trapi_version: str, version of component to validate against
        :param strict_validation: Optional[bool] = None, if True, some tests validate as 'error';  False, simply issues
                                  'info' message; A value of 'None' uses the default value for specific graph contexts.

        """
        self.default_trapi: bool = False
        if trapi_version is None:
            self.default_trapi = True
        self.trapi_version = get_latest_version(trapi_version) \
            if trapi_version else get_latest_version(self.DEFAULT_TRAPI_VERSION)

        logger.info(f"\nTRAPISchemaValidator set to TRAPI Version: '{self.trapi_version}'")

        ValidationReporter.__init__(
            self,
            prefix=prefix if prefix is not None else "TRAPI Validation",
            strict_validation=strict_validation
        )

    def get_trapi_version(self) -> str:
        """
        :return: str, TRAPI (SemVer) version currently targeted by the TRAPISchemaValidator.
        """
        return self.trapi_version

    def reset_trapi_version(self, version: str):
        """
        Reset TRAPI version tracked by the TRAPISchemaValidator.
        :param version: new version
        :return: None
        """
        self.trapi_version = version

    def minimum_required_trapi_version(self, version: str) -> bool:
        """
        :param version: simple 'major.minor.patch' TRAPI schema release SemVer
        :return: True if current version is equal to, or newer than, a targeted 'minimum_version'
        """
        try:
            current: SemVer = SemVer.from_string(self.trapi_version)
            target: SemVer = SemVer.from_string(version)
            return current >= target
        except SemVerError as sve:
            logger.error(f"minimum_required_trapi_version() error: {str(sve)}")
            return False

    def validate(self, instance, component):
        """Validate instance against schema.

        Parameters
        ----------
        instance
            dict, instance to validate
        component : str
            TRAPI subschema to validate (e.g. 'Query', 'QueryGraph', 'KnowledgeGraph', 'Result'; Default: 'Query')

        Raises
        ------
        `ValidationError
            <https://python-jsonschema.readthedocs.io/en/latest/errors/#jsonschema.exceptions.ValidationError>`_
            If the instance is invalid.

        Examples
        --------
        >>> TRAPISchemaValidator(trapi_version="1.3.0").validate({"message": {}}, "QGraph")

        """
        schema = load_schema(self.trapi_version)[component]
        # print("instance", instance)
        jsonschema.validate(instance, schema)

    def is_valid_trapi_query(self, instance, component: str = "Query"):
        """Make sure that the Message is a syntactically valid TRAPI Query JSON object.

        Parameters
        ----------
        instance:
            Dict, instance to validate
        component:
            str, TRAPI subschema to validate (e.g. 'Query', 'QueryGraph', 'KnowledgeGraph', 'Result'; Default: 'Query')

        Returns
        -------
        Validation ("information", "warning" and "error") messages are returned within the host TRAPIValidator instance.

        Examples
        --------
        >>> TRAPISchemaValidator(trapi_version="1.3.0").is_valid_trapi_query({"message": {}}, component="Query")
        """
        try:
            self.validate(
                instance=instance,
                component=component
            )
        except jsonschema.ValidationError as e:
            if len(e.message) <= 160:
                reason = e.message
            else:
                reason = e.message[0:49] + " "*5 + "... " + " "*5 + e.message[-100:-1]
            self.report(
                code="critical.trapi.validation",
                identifier=self.trapi_version,
                component=component,
                reason=reason
            )

    def merge(self, reporter):
        """
        Merge all messages and metadata from a second TRAPISchemaValidator,
        into the calling TRAPISchemaValidator instance.

        :param reporter: second TRAPISchemaValidator
        """
        ValidationReporter.merge(self, reporter)

        # First come, first serve... We only overwrite
        # empty versions in the parent reporter
        if isinstance(reporter, TRAPISchemaValidator) and not self.get_trapi_version():
            self.reset_trapi_version(reporter.get_trapi_version())

    def to_dict(self) -> Dict:
        """
        Export TRAPISchemaValidator contents as a Python dictionary
        (including TRAPI version and parent class dictionary content)
        :return: Dict
        """
        dictionary = ValidationReporter.to_dict(self)
        dictionary["trapi_version"] = self.get_trapi_version()
        return dictionary

    def report_header(self, title: Optional[str], compact_format: bool) -> str:
        header: str = ValidationReporter.report_header(self, title, compact_format)
        header += f" validating against TRAPI version " \
                  f"'{str(self.get_trapi_version() if self.get_trapi_version() is not None else 'Default')}'"
        return header


def check_trapi_validity(instance, trapi_version: str, component: str = "Query") -> TRAPISchemaValidator:
    """
    Checks schema compliance of a Query component against a given TRAPI version.

    Parameters
    ----------
    instance:
        Dict, of format {"message": {}}
    component:
        str, TRAPI subschema to validate (e.g. 'Query', 'QueryGraph', 'KnowledgeGraph', 'Result'; Default: 'Query')
    trapi_version:
        str, version of component to validate against

    Returns
    -------
    ValidationReporter catalog of "information", "warnings" or "errors" indexed messages (maybe empty)
    """
    trapi_validator = TRAPISchemaValidator(trapi_version=trapi_version)
    trapi_validator.is_valid_trapi_query(instance, component=component)
    return trapi_validator
