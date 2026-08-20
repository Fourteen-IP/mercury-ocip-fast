"""Tests for the OCI ``Parser`` and the ``OCIType`` base class.

The tests use real generated command classes. They exercise every field
kind the parser knows (scalar, object, list and table) across the four
translation directions: class -> XML, XML -> class, dict -> class and
class -> dict. This keeps the suite close to the wire behaviour the server
gives us, and away from parser internals that can change.
"""

from __future__ import annotations

import pytest

from mercury_ocip_fast.commands.base_command import (
    ErrorResponse,
    OCINil,
    OCITable,
    OCITableRow,
    OCIType,
)
from mercury_ocip_fast.commands.commands import (
    AccessDevice,
    AccessDeviceEndpointAdd,
    AdditionalMessageOptionsMenuKeysModifyEntry,
    AuthenticationRequest,
    AuthenticationResponse,
    AuthenticationVerifyResponse,
    BroadWorksMobilityAlertingMobileNumberReplacementList,
    CallCenterReportAbandonedCallThresholdReplacementList,
    EnterpriseDepartmentKey,
    LoginRequest22V5,
    ProfileAndServiceBusyLampFieldInfo,
    ReplacementDeviceList,
    ServiceInstanceAddProfile,
)
from mercury_ocip_fast.utils.parser import Parser


def busy_lamp_info(table: OCITable) -> ProfileAndServiceBusyLampFieldInfo:
    """Build a table-carrying command with its other required fields set."""
    return ProfileAndServiceBusyLampFieldInfo(
        list_uri="list@example.com",
        enable_call_park_notification=True,
        monitored_user_table=table,
    )


class TestScalarToXml:
    def test_emits_class_name_and_aliased_field(self):
        xml = AuthenticationRequest(user_id="admin").to_xml()
        assert 'xsi:type="AuthenticationRequest"' in xml
        # The field alias turns ``user_id`` into ``userId``.
        assert "<userId>admin</userId>" in xml

    def test_none_fields_are_dropped(self):
        # A login request has a ``userId`` and a ``password``. A field that is
        # None does not go into the body.
        xml = LoginRequest22V5(user_id="admin").to_xml()
        assert "<userId>admin</userId>" in xml
        assert "password" not in xml

    def test_bool_field_is_lowercase_text(self):
        xml = busy_lamp_info(OCITable(col_heading=[])).to_xml()
        assert "<enableCallParkNotification>true</enableCallParkNotification>" in xml


class TestScalarRoundTrip:
    def test_class_to_xml_to_class(self):
        original = AuthenticationRequest(user_id="admin")
        restored = AuthenticationRequest.from_xml(original.to_xml())
        assert restored.user_id == "admin"

    def test_bool_and_int_are_coerced_from_wire_strings(self):
        # xmltodict hands every leaf back as a string. The parser coerces
        # each scalar to the type its field declares.
        resp = AuthenticationVerifyResponse.from_dict(
            {
                "AuthenticationVerifyResponse": {
                    "loginType": "User",
                    "locale": "en_US",
                    "encoding": "UTF-8",
                    "userDomain": "example.com",
                    "isEnterprise": "true",
                    "passwordExpiresDays": "7",
                }
            }
        )
        assert resp.is_enterprise is True
        assert resp.password_expires_days == 7

    def test_unparseable_scalar_is_kept_as_is(self):
        # An empty numeric tag cannot coerce to int, so it stays untouched
        # instead of raising.
        resp = AuthenticationVerifyResponse.from_dict(
            {
                "AuthenticationVerifyResponse": {
                    "loginType": "User",
                    "locale": "en_US",
                    "encoding": "UTF-8",
                    "userDomain": "example.com",
                    "isEnterprise": "false",
                    "passwordExpiresDays": "",
                }
            }
        )
        assert resp.password_expires_days == ""


class TestObjectField:
    def test_nested_object_becomes_nested_element(self):
        obj = AccessDeviceEndpointAdd(
            access_device=AccessDevice(device_level="Group", device_name="dev1"),
            line_port="lp1",
            port_number=5,
        )
        xml = obj.to_xml()
        assert (
            "<accessDevice><deviceLevel>Group</deviceLevel>"
            "<deviceName>dev1</deviceName></accessDevice>" in xml
        )

    def test_object_round_trip_rebuilds_the_child_instance(self):
        obj = AccessDeviceEndpointAdd(
            access_device=AccessDevice(device_level="Group", device_name="dev1"),
            line_port="lp1",
            port_number=5,
        )
        restored = AccessDeviceEndpointAdd.from_xml(obj.to_xml())
        assert isinstance(restored.access_device, AccessDevice)
        assert restored.access_device.device_name == "dev1"
        assert restored.port_number == 5


class TestListField:
    def test_scalar_list_repeats_the_element(self):
        obj = BroadWorksMobilityAlertingMobileNumberReplacementList(
            mobile_number=["+1", "+2"]
        )
        xml = obj.to_xml()
        assert xml.count("<mobileNumber>") == 2
        restored = BroadWorksMobilityAlertingMobileNumberReplacementList.from_xml(xml)
        assert restored.mobile_number == ["+1", "+2"]

    def test_single_element_list_round_trips_as_a_list(self):
        # xmltodict collapses a lone repeated element to a bare value. The
        # parser must still hand back a list, with the element coerced.
        obj = CallCenterReportAbandonedCallThresholdReplacementList(
            abandoned_call_threshold_seconds=[30]
        )
        restored = CallCenterReportAbandonedCallThresholdReplacementList.from_xml(
            obj.to_xml()
        )
        assert restored.abandoned_call_threshold_seconds == [30]

    def test_list_of_objects_round_trips(self):
        obj = ReplacementDeviceList(
            device=[
                AccessDevice(device_level="Group", device_name="a"),
                AccessDevice(device_level="System", device_name="b"),
            ]
        )
        restored = ReplacementDeviceList.from_xml(obj.to_xml())
        assert [d.device_name for d in restored.device] == ["a", "b"]
        assert all(isinstance(d, AccessDevice) for d in restored.device)


class TestTableField:
    def test_table_encodes_headings_and_rows(self):
        table = OCITable(
            col_heading=["User Id", "Last Name"],
            row=[OCITableRow(col=["a@x.com", "Smith"])],
        )
        xml = busy_lamp_info(table).to_xml()
        assert "<colHeading>User Id</colHeading>" in xml
        assert "<colHeading>Last Name</colHeading>" in xml
        assert "<row><col>a@x.com</col><col>Smith</col></row>" in xml

    def test_table_round_trips_to_an_ocitable(self):
        table = OCITable(
            col_heading=["User Id", "Last Name"],
            row=[
                OCITableRow(col=["a@x.com", "Smith"]),
                OCITableRow(col=["b@x.com", "Jones"]),
            ],
        )
        restored = ProfileAndServiceBusyLampFieldInfo.from_xml(
            busy_lamp_info(table).to_xml()
        )
        result = restored.monitored_user_table
        assert isinstance(result, OCITable)
        assert result.col_heading == ["User Id", "Last Name"]
        assert [r.col for r in result.row] == [
            ["a@x.com", "Smith"],
            ["b@x.com", "Jones"],
        ]


class TestPolymorphicSubtype:
    def test_concrete_subtype_survives_round_trip(self):
        # ``department`` is declared as the abstract ``DepartmentKey``. A
        # concrete subtype is tagged with ``xsi:type`` and must decode back
        # to that subtype with its own fields, not the base.
        profile = ServiceInstanceAddProfile(
            name="Front Desk",
            calling_line_id_last_name="Desk",
            calling_line_id_first_name="Front",
            department=EnterpriseDepartmentKey(
                service_provider_id="sp1", name="Sales"
            ),
        )
        xml = profile.to_xml()
        assert 'xsi:type="EnterpriseDepartmentKey"' in xml

        restored = ServiceInstanceAddProfile.from_xml(xml)
        assert isinstance(restored.department, EnterpriseDepartmentKey)
        assert restored.department.service_provider_id == "sp1"
        assert restored.department.name == "Sales"


class TestNillable:
    def test_ocinil_becomes_a_nil_attribute(self):
        obj = AdditionalMessageOptionsMenuKeysModifyEntry(save_message=OCINil())
        xml = obj.to_xml()
        assert 'C:nil="true"' in xml
        assert "saveMessage" in xml


class TestToDictFromXml:
    def test_reads_command_and_lifts_xsi_type(self):
        xml = (
            "<BroadsoftDocument xmlns:xsi="
            '"http://www.w3.org/2001/XMLSchema-instance">'
            '<command xsi:type="AuthenticationResponse">'
            "<userId>admin</userId><nonce>12345</nonce>"
            "</command></BroadsoftDocument>"
        )
        result = Parser.to_dict_from_xml(xml)
        command = result["command"]
        assert command["userId"] == "admin"
        assert command["nonce"] == "12345"
        # The parser lifts ``xsi:type`` into a Clark-notation attribute key.
        xsi_key = "{http://www.w3.org/2001/XMLSchema-instance}type"
        assert command["attributes"][xsi_key] == "AuthenticationResponse"

    def test_non_string_input_returns_empty(self):
        assert Parser.to_dict_from_xml(None) == {}  # type: ignore[arg-type]


class TestDictConversions:
    def test_to_dict_from_class_uses_aliases(self):
        assert Parser.to_dict_from_class(AuthenticationRequest(user_id="a")) == {
            "userId": "a"
        }

    def test_to_dict_from_class_can_wrap_in_class_name(self):
        wrapped = Parser.to_dict_from_class(
            AuthenticationRequest(user_id="a"), wrap_in_class_name=True
        )
        assert wrapped == {"AuthenticationRequest": {"userId": "a"}}

    def test_to_xml_from_dict_matches_the_class_path(self):
        xml = Parser.to_xml_from_dict({"userId": "admin"}, AuthenticationRequest)
        assert xml == AuthenticationRequest(user_id="admin").to_xml()

    def test_class_name_wrapper_key_is_optional(self):
        # A bare dict, with no class-name wrapper, still decodes.
        resp = AuthenticationResponse.from_dict(
            {"userId": "admin", "nonce": "n", "passwordAlgorithm": "MD5"}
        )
        assert resp.user_id == "admin"

    def test_non_dict_source_raises_type_error(self):
        with pytest.raises(TypeError):
            Parser.to_class_from_dict([], AuthenticationRequest)  # type: ignore[arg-type]


class TestErrorResponse:
    def test_from_dict_reads_alias_fields(self):
        payload = {
            "attributes": {
                "{http://www.w3.org/2001/XMLSchema-instance}type": "ErrorResponse"
            },
            "summary": "Login failed",
            "errorCode": 4001,
        }
        err = ErrorResponse.from_dict(payload)
        assert err.summary == "Login failed"
        assert err.error_code == 4001


class TestOCITypeInit:
    def test_unknown_field_raises(self):
        with pytest.raises(ValueError):
            OCIType(not_a_field="x")

    def test_field_aliases_for_dataclass(self):
        aliases = AuthenticationRequest(user_id="a").get_field_aliases()
        assert aliases["user_id"] == "userId"

    def test_field_aliases_empty_for_plain_type(self):
        assert OCIType().get_field_aliases() == {}


class TestOCITable:
    def test_to_dict_maps_headings_to_rows(self):
        table = OCITable(
            col_heading=["User Id", "Last Name"],
            row=[
                OCITableRow(col=["a@x.com", "Smith"]),
                OCITableRow(col=["b@x.com", "Jones"]),
            ],
        )
        assert table.to_dict() == [
            {"user_id": "a@x.com", "last_name": "Smith"},
            {"user_id": "b@x.com", "last_name": "Jones"},
        ]
