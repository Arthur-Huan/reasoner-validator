"""Testing Validation Report methods"""
from typing import Dict, Set

from reasoner_validator.report import ValidationReporter


def test_messages():

    # Loading and checking a first reporter
    reporter1 = ValidationReporter(prefix="First Validation Report")
    assert not reporter1.has_messages()
    reporter1.info("this is information.")
    assert reporter1.has_messages()
    assert reporter1.has_information()
    assert not reporter1.has_warnings()
    assert not reporter1.has_errors()
    reporter1.warning("this is a warning?")
    assert reporter1.has_warnings()
    reporter1.error("this is an error!")
    assert reporter1.has_messages()

    # Testing merging of messages from a second reporter
    reporter2 = ValidationReporter(prefix="Second Validation Report")
    reporter2.info("this is more information.")
    reporter2.warning("this is another warning?")
    reporter2.error("this is a second error!")
    reporter1.merge(reporter2)

    # No prefix...
    reporter3 = ValidationReporter()
    reporter3.error("Ka Boom!")
    reporter1.merge(reporter3)

    # testing addition a few raw batch messages
    new_messages: Dict[str, Set[str]] = {
            "information": {"Hello Dolly...", "Well,... hello Dolly"},
            "warnings": {"Warning, Will Robinson, warning!"},
            "errors": {"Dave, this can only be due to human error!"}
    }
    reporter1.add_messages(new_messages)

    # Verify what we have
    messages: Dict[str, Set[str]] = reporter1.get_messages()
    assert "First Validation Report: INFO - this is information." in messages["information"]
    assert "Second Validation Report: INFO - this is more information." in messages["information"]
    assert "Hello Dolly..." in messages["information"]
    assert "Well,... hello Dolly" in messages["information"]
    assert "First Validation Report: WARNING - this is a warning?" in messages["warnings"]
    assert "Second Validation Report: WARNING - this is another warning?" in messages["warnings"]
    assert "Warning, Will Robinson, warning!" in messages["warnings"]
    assert "First Validation Report: ERROR - this is an error!" in messages["errors"]
    assert "Second Validation Report: ERROR - this is a second error!" in messages["errors"]
    assert "Dave, this can only be due to human error!" in messages["errors"]
    assert "ERROR - Ka Boom!" in messages["errors"]
