#!/usr/bin/env python
##################################################
# Thanks to Eric Deutsch (Expander Agent) for this
# validator of ARS UUID specified TRAPI Responses
#
# Note: the latest version of the code is best
#       run within a poetry shell, i.e.
#
# Usage:
#     poetry install
#     poetry shell
#     cd scripts
#     ./trapi_validator.py --help
#
##################################################
import asyncio
from typing import Dict, Optional
from sys import stderr
from os.path import isfile
from json.decoder import JSONDecodeError

import requests
from requests.exceptions import JSONDecodeError
import json
import argparse

from bmt import Toolkit
from reasoner_validator.validator import TRAPIResponseValidator
from reasoner_validator.trapi import call_trapi
from reasoner_validator.versioning import get_latest_version
from reasoner_validator.biolink import get_biolink_model_toolkit
from reasoner_validator.report import ValidationReporter

ARS_HOSTS = [
    'ars-prod.transltr.io',
    'ars.test.transltr.io',
    'ars.ci.transltr.io',
    'ars-dev.transltr.io',
    'ars.transltr.io'
]


def get_cli_arguments():

    # Parse command line options
    arg_parser = argparse.ArgumentParser(description='TRAPI/Biolink validation of TRAPI Request/Responses.')
    arg_parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='If given, show detailed information about ongoing processing, including validation results ' +
             '(Note that even if this flag is not given, non-empty validation results can still be ' +
             'selectively shown at end of script execution).'
    )
    arg_parser.add_argument(
        '--biolink_version', type=str, nargs='?', default=None,
        help='Biolink Version for validation (if omitted or None, defaults to the '
             'current default version of the Biolink Model Toolkit; ignored when "--json" flag is given).'
    )
    arg_parser.add_argument(
        '--trapi_version', type=str, nargs='?', default=None,
        help='TRAPI Version for validation (if omitted or None, defaults to the ' +
             'current default version the package; ignored when "--json" flag is given)' +
             'Note that the version may be a SemVer release, a Git branch name (e.g. master) or even'
             'a file path (with file extension .yaml) specifying or referencing the target TRAPI schema file.'
    )
    arg_parser.add_argument(
        '-r', '--ars_response_id', type=str, nargs='?', default=None,
        help='The value of this argument can either be an ARS query PK identifier or '
             'a file name to JSON file with a previously run query.  Ignored when --endpoint or --arax_id are given.'
    )
    arg_parser.add_argument(
        '-a', '--arax_id', type=str, nargs='?', default=None,
        help='The value of this argument is an ARAX response identifier.  Ignored when an --endpoint is given.'
    )
    arg_parser.add_argument(
        '-e', '--endpoint', type=str, nargs='?', default=None,
        help="Target TRAPI service endpoint to be directly used for query. Note: the endpoint is the root URL, " +
             "without any path (like /query). This argument overrides the --response_id CLI argument."
    )
    arg_parser.add_argument(
        '-l', '--local_request', type=str, nargs='?', default=None,
        help='Local JSON input text file source of the TRAPI Request. Mandatory when --endpoint CLI argument is given.'
    )
    arg_parser.add_argument(
        '-j', '--json', action='store_true',
        help='If given, dump validation messages in JSON format '
             '(default: dump messages in human readable format).'
    )
    arg_parser.add_argument(
        '--title', type=str, nargs='?', default=None,
        help='User provided string for human readable report output title (title suppressed if None; ' +
             'if a blank string is given then title is autogenerated; ignored when "--json" flag is given).'
    )
    arg_parser.add_argument(
        '-n', '--number_of_identifiers',
        metavar='N', type=int, nargs='?', default=0,
        help='Maximum integer N number of message indexing identifiers to report per validation code; ' +
             'set to zero if all identifiers are to be reported (ignored when "--json" flag is given).'
    )
    arg_parser.add_argument(
        '-m', '--number_of_messages',
        metavar='N', type=int, nargs='?', default=0,
        help='Maximum integer N number of messages to report per indexing identifier; set to zero if all messages ' +
             'are to be reported (default: 0, display all identifiers; ignored when "--json" flag is given).'
    )
    arg_parser.add_argument(
        '-c', '--compact_format', action='store_true',
        help='If given, compress human readable text output by suppressing blank lines '
             '(default: False; ignored when "--json" flag is given).'
    )

    return arg_parser.parse_args()


# Global variable for TRAPI Response targeted for validation
trapi_response: Optional[Dict] = None


async def direct_trapi_request(
        endpoint: str,
        trapi_request_filepath: str,
        validator: TRAPIResponseValidator,
        verbose: bool
):
    global trapi_response

    # Attempt loading of the candidate TRAPI Request JSON file
    if verbose:
        print(f"Loading TRAPI Request JSON file from '{trapi_request_filepath}'")

    if isfile(trapi_request_filepath):
        trapi_request: Optional[Dict] = None
        try:
            with open(trapi_request_filepath) as infile:
                trapi_request = json.load(infile)
        except IOError as e:
            print(e, file=stderr)
        except (JSONDecodeError, TypeError) as e:
            print(f"TRAPI Request JSON file loading reported an error: {e}", file=stderr)
            trapi_request = None

        if trapi_request is not None:
            validator.is_valid_trapi_query(instance=trapi_request, component="Query")
            if validator.has_errors():
                print(
                    f"Request JSON is not strictly compliant with TRAPI release " +
                    f"{trapi_request}? TRAPI query will not be attempted!")
            else:
                # Submit the candidate JSON file to the endpoint
                if verbose:
                    print(f"Submitting TRAPI Response file to endpoint '{endpoint}'")

            # Make the TRAPI call to the Case targeted
            # endpoint with specified TRAPI request
            result = await call_trapi(endpoint, trapi_request)

            # Was the web service (HTTP) call successful?
            status_code: int = result['status_code']
            if status_code != 200:
                validator.report("error.trapi.response.unexpected_http_code", identifier=status_code)
            else:
                trapi_response = result['response_json']


def retrieve_trapi_response(host_url: str, response_id: str):
    try:
        response_content = requests.get(
            f"{host_url}{response_id}",
            headers={'accept': 'application/json'}
        )
        if response_content:
            status_code = response_content.status_code
            if status_code == 200:
                print(f"...Result returned from '{host_url}'!")
        else:
            status_code = 404

    except Exception as e:
        print(f"Remote host {host_url} unavailable: Connection attempt to {host_url} triggered an exception: {e}")
        response_content = None
        status_code = 404

    return status_code, response_content


def retrieve_ars_result(response_id: str, verbose: bool):

    global trapi_response

    if verbose:
        print(f"Trying to retrieve ARS Response UUID '{response_id}'...")
        
    response_content: Optional = None
    status_code: int = 404
    
    for ars_host in ARS_HOSTS:
        if verbose:
            print(f"\n...from {ars_host}", end=None)

        status_code, response_content = retrieve_trapi_response(
            host_url=f"https://{ars_host}/ars/api/messages/",
            response_id=response_id
        )
        if status_code != 200:
            continue

    if status_code != 200:
        print(f"Unsuccessful HTTP status code '{status_code}' reported for ARS PK '{response_id}'?")
        return

    # Unpack the response content into a dict
    try:
        response_dict = response_content.json()
    except Exception as e:
        print(f"Cannot decode ARS PK '{response_id}' to a Translator Response, exception: {e}")
        return

    if 'fields' in response_dict:
        if 'actor' in response_dict['fields'] and str(response_dict['fields']['actor']) == '9':
            print("The supplied response id is a collection id. Please supply the UUID for a response")
        elif 'data' in response_dict['fields']:
            print(f"Validating ARS PK '{response_id}' TRAPI Response result...")
            trapi_response = response_dict['fields']['data']
        else:
            print("ARS response dictionary is missing 'fields.data'?")
    else:
        print("ARS response dictionary is missing 'fields'?")


def retrieve_arax_result(response_id: str):

    global trapi_response

    status_code, response_content = retrieve_trapi_response(
        host_url="https://arax.ncats.io/devED/api/arax/v1.4/response/",
        response_id=response_id
    )
    if status_code == 200:
        # Unpack the response content into a dict
        try:
            trapi_response = response_content.json()
        except Exception as e:
            print(f"Cannot decode ARAX Response ID '{response_id}' to a Translator Response, exception: {e}")
    else:
        print(f"Unsuccessful HTTP status code '{status_code}' reported for ARAX Response ID '{response_id}'?")


def validation_report(validator: TRAPIResponseValidator, args):

    def prompt_user(msg: str):
        text = input(f"{msg} (Type 'Y' or 'Yes' to show): ")
        text = text.upper()
        if text == "YES" or text == "Y":
            return True
        else:
            return False

    show_messages: bool = False
    if validator.has_critical() or validator.has_errors() or validator.has_warnings():
        show_messages = prompt_user("Validation errors and/or warnings were reported")
    elif validator.has_information():
        show_messages = prompt_user("No validation errors or warnings, but some information was reported")

    if show_messages or args.verbose:
        if args.json:
            print(json.dumps(validator.get_messages(), sort_keys=True, indent=2))
        else:
            validator.dump(
                title=args.title,
                id_rows=args.number_of_identifiers,
                msg_rows=args.number_of_messages,
                compact_format=args.compact_format
            )


def main():

    global trapi_response

    args = get_cli_arguments()

    # Override the TRAPI release here if specified
    resolved_trapi_version: Optional[str] = None
    if args.trapi_version:
        resolved_trapi_version = get_latest_version(args.trapi_version)

    # Explicitly resolve the Biolink Model version to be used
    resolved_biolink_version: Optional[str] = None
    if args.biolink_version:
        bmt: Toolkit = get_biolink_model_toolkit(biolink_version=args.biolink_version)
        resolved_biolink_version: str = bmt.get_model_version()

    # Perform a validation on it
    validator = TRAPIResponseValidator(
        trapi_version=resolved_trapi_version,
        biolink_version=resolved_biolink_version
    )
    if args.verbose:
        print(
            f"Validating against TRAPI '{validator.get_trapi_version()}' and " +
            f"Biolink Model version '{validator.get_biolink_version()}'"
        )

    # Retrieve the TRAPI JSON Response result to be validated
    if args.endpoint:
        if args.local_request:
            # The internal TRAPI call is a coroutine
            # from which the results in a global variable
            asyncio.run(
                direct_trapi_request(
                    endpoint=args.endpoint,
                    trapi_request_filepath=args.local_request,
                    validator=validator,
                    verbose=args.verbose
                )
            )
            if validator.has_messages():
                # Report detected TRAPI Request JSON problems
                validation_report(validator, args)

        else:
            print("Need to specific a --local_request JSON input text file (path) argument for your TRAPI endpoint!")

    elif args.arax_id:
        retrieve_arax_result(args.arax_id)
    
    elif args.ars_response_id:
        if isfile(args.ars_response_id):
            # The response identifier can just be a local file...
            with open(args.ars_response_id) as infile:
                trapi_response = json.load(infile)
        else:
            # ... unless, it is an ARS PK
            retrieve_ars_result(response_id=args.ars_response_id, verbose=args.verbose)

    else:
        print("Need to specify either an --endpoint/--local_request or a --response_id input argument to proceed!")

    if not trapi_response:
        print("TRAPI Response JSON is unavailable for validation?")
        return

    # OK, we have something to validate here...
    validator.check_compliance_of_trapi_response(response=trapi_response)

    # Print out the outcome of the main validation of the TRAPI Response
    validation_report(validator, args)


if __name__ == "__main__":
    main()
