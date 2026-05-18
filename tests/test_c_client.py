"""End-to-end tests for the C calc_client binary.

Same shape as test_python_client.py: drive the client as a subprocess,
feed stdin, assert on stdout/stderr/return code. The server-side
fixture is `running_server` (the Python server, since the wire format
is identical and we've already tested the C server separately) — this
exercises the C client end-to-end against a real server.

If anything in this file fails but the Python equivalent passes, the
issue is C-client-specific (struct layout, getopt parsing, line input
buffer, etc.) rather than protocol- or server-side.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

CLIENT_BIN = Path(__file__).resolve().parent.parent / "build" / "calc_client_c"


def _run_client(socket_path: str, stdin_text: str, timeout: float = 5.0
                ) -> subprocess.CompletedProcess:
    """Spawn calc_client_c with the given socket and stdin, return result."""
    if not CLIENT_BIN.exists():
        pytest.fail(f"{CLIENT_BIN} not built - run scripts/build_module.sh")
    return subprocess.run(
        [str(CLIENT_BIN), "--socket", str(socket_path)],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# --------------------------------------------------------------------------
# UI shape
# --------------------------------------------------------------------------

def test_menu_lists_all_four_ops_and_exit(running_server):
    """Confirm every menu entry from the spec sample is printed."""
    result = _run_client(running_server, "5\n")
    assert result.returncode == 0, result.stderr
    for line in [
        "(1) Add 2 numbers",
        "(2) Subtract 2 numbers",
        "(3) Multiply 2 numbers",
        "(4) Divide 2 numbers",
        "(5) Exit",
    ]:
        assert line in result.stdout, f"missing menu line: {line!r}"


def test_choosing_exit_returns_zero(running_server):
    """Menu option 5 exits cleanly with code 0."""
    result = _run_client(running_server, "5\n")
    assert result.returncode == 0


def test_eof_at_menu_exits_cleanly(running_server):
    """Ctrl-D (empty stdin) ends the loop with code 0 - no segfault, no crash."""
    result = _run_client(running_server, "")
    assert result.returncode == 0


# --------------------------------------------------------------------------
# Happy-path round trips
# --------------------------------------------------------------------------

@pytest.mark.parametrize("choice, a, b, expected", [
    (1, 42, 37,  79),     # ADD
    (2, 10,  3,   7),     # SUB
    (3, -6,  7, -42),     # MUL
    (4, 100, 4,  25),     # DIV
])
def test_each_op_round_trip(running_server, choice, a, b, expected):
    """Pick from menu, enter operands, see 'Result is N!' for each op."""
    stdin = f"{choice}\n{a}\n{b}\n5\n"
    result = _run_client(running_server, stdin)
    assert result.returncode == 0, result.stderr
    assert f"Result is {expected}!" in result.stdout


def test_ux_lines_match_sample(running_server):
    """The four sample-output lines appear in order around one calc."""
    result = _run_client(running_server, "1\n42\n37\n5\n")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    idx = 0
    for needle in [
        "Sending request...",
        "Request OKAY...",
        "Receiving response...",
        "Result is 79!",
    ]:
        found = out.find(needle, idx)
        assert found >= 0, f"missing {needle!r} after position {idx}\n{out}"
        idx = found + len(needle)


# --------------------------------------------------------------------------
# Error paths
# --------------------------------------------------------------------------

def test_divide_by_zero_prints_friendly_error(running_server):
    """STATUS_DIV_ZERO from the kernel is translated to a human message.

    The C client's status_text() table maps kernel statuses to strings;
    this test pins that mapping to "division by zero".
    """
    result = _run_client(running_server, "4\n7\n0\n5\n")
    assert result.returncode == 0, result.stderr
    assert "division by zero" in result.stdout
    assert "Result is" not in result.stdout


def test_out_of_range_command_reprompts(running_server):
    """Picking a menu number outside 1..5 prints a hint and re-shows menu."""
    result = _run_client(running_server, "9\n5\n")
    assert result.returncode == 0, result.stderr
    assert "pick 1.." in result.stdout
    assert result.stdout.count("(1) Add 2 numbers") >= 2


def test_non_numeric_command_reprompts(running_server):
    """Typing a non-number for the menu doesn't crash - just re-prompts."""
    result = _run_client(running_server, "abc\n5\n")
    assert result.returncode == 0, result.stderr
    assert "not a number" in result.stdout


def test_non_integer_operand_reprompts(running_server):
    """Bad operand input ('xyz' for an integer) re-prompts the menu."""
    result = _run_client(running_server, "1\nxyz\n5\n")
    assert result.returncode == 0, result.stderr
    assert "is not an integer" in result.stdout


def test_server_not_reachable_exits_two(tmp_path):
    """If the server socket doesn't exist, the client must fail with code 2.

    Doesn't take the running_server fixture - we deliberately want NO
    server listening at the path we hand it.
    """
    if not CLIENT_BIN.exists():
        pytest.fail(f"{CLIENT_BIN} not built - run scripts/build_module.sh")
    bogus = tmp_path / "nope.sock"
    result = subprocess.run(
        [str(CLIENT_BIN), "--socket", str(bogus)],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2
    assert "cannot reach" in result.stderr


# --------------------------------------------------------------------------
# Cross-server interop (C client against C server)
# --------------------------------------------------------------------------

def test_c_client_against_c_server(running_c_server):
    """C client + C server: the pure-C path end-to-end.

    Every other test in this file uses the Python server as the backing
    socket. This one swaps in the C server so the entire pipeline —
    client subprocess, C struct on the wire, C server subprocess, kernel
    chardev — is exercised without any Python in the middle.
    """
    result = _run_client(running_c_server, "1\n100\n23\n5\n")
    assert result.returncode == 0, result.stderr
    assert "Result is 123!" in result.stdout
