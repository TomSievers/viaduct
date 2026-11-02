import logging

import anyio
import pytest
from kipy.errors import ApiError, ConnectionError as KiCadConnectionError
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from viaduct import server
from viaduct.errors import ViaductServer, describe, expected
from viaduct.readability.config import ConfigError
from viaduct.setup_files import ProjectOpen


def failing_server(error):
    srv = ViaductServer("test")

    @srv.tool()
    def fails() -> dict:
        raise error
    return srv


def call(srv, name="fails", arguments=None):
    return anyio.run(srv.call_tool, name, arguments or {})


@pytest.mark.parametrize("error", [ValueError("bad count"), RuntimeError("bad count"), ProjectOpen("bad count"),
                                   ConfigError("bad count"), ApiError("bad count"),
                                   FileNotFoundError("bad count")])
def test_expected_errors_keep_their_own_words(error):
    assert expected(error) and describe(error) == "bad count"


def test_system_errors_lose_their_errno():
    assert describe(FileNotFoundError(2, "No such file or directory", "/w/x")) == "No such file or directory: /w/x"
    assert describe(PermissionError(13, "Permission denied")) == "Permission denied"


def test_not_reaching_kicad_says_what_to_check():
    assert "Preferences > Plugins" in describe(KiCadConnectionError("Failed to connect to KiCad"))


def test_an_error_without_text_is_still_named():
    assert describe(ValueError()) == "ValueError"


def test_a_bug_is_named_as_one():
    message = describe(KeyError("x1"))
    assert "bug" in message and "KeyError" in message and "x1" in message


def test_a_tool_failure_reaches_the_client_as_a_tool_error():
    with pytest.raises(ToolError, match="bad count") as caught:
        call(failing_server(ValueError("bad count")))
    # UnexpectedToolError is the type whose text the library hides
    assert not isinstance(caught.value, UnexpectedToolError)
    assert str(caught.value) == "Error executing tool fails: bad count"
    assert isinstance(caught.value.__cause__, ValueError)


def test_a_bug_reaches_the_client_and_logs_its_traceback(caplog):
    with caplog.at_level(logging.ERROR), pytest.raises(ToolError, match="KeyError") as caught:
        call(failing_server(KeyError("x1")))
    assert not isinstance(caught.value, UnexpectedToolError)
    assert caplog.records[-1].exc_info[0] is KeyError


def test_an_expected_failure_logs_no_traceback(caplog):
    with caplog.at_level(logging.ERROR), pytest.raises(ToolError):
        call(failing_server(ValueError("bad count")))
    assert not caplog.records


def test_arguments_failing_the_schema_are_still_reported():
    srv = ViaductServer("test")

    @srv.tool()
    def takes(count: int) -> dict:
        return {"count": count}
    with pytest.raises(ToolError, match="count"):
        call(srv, "takes", {"count": "many"})


def test_the_real_server_passes_on_its_tools_messages(tmp_path):
    with pytest.raises(ToolError, match="pass exactly one of plan or plan_file"):
        call(server.mcp, "check_plan")
    with pytest.raises(ToolError, match="not a .kicad_pcb"):
        call(server.mcp, "open_headless", {"board": str(tmp_path / "nope.kicad_sch")})
    with pytest.raises(ToolError, match="file not found"):
        call(server.mcp, "read_setup_files", {"board": str(tmp_path / "nope.kicad_pcb")})
