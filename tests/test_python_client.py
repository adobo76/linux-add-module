"""End-to-end tests for client/calc_client.py.

The client is interactive (reads from stdin, writes to stdout), so we
drive it as a subprocess: feed a prepared stdin string and assert on
the captured stdout/stderr. The `running_server` fixture (defined in
conftest.py) gives us a real server backed by /dev/calc_dev for each
test, so what we're really verifying is the full
client→socket→server→chardev→back round-trip exactly as a human user
would experience it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

CLIENT_SCRIPT = Path(__file__).resolve().parent.parent / "client" / "calc_client.py"


def _run_client(socket_path: str, stdin_text: str, timeout: float = 5.0
                ) -> subprocess.CompletedProcess:
    """Spawn calc_client with the given socket and stdin, return its result."""
    return subprocess.run(
        [sys.executable, str(CLIENT_SCRIPT), "--socket", str(socket_path)],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# --------------------------------------------------------------------------
# UI shape
# --------------------------------------------------------------------------

def test_menu_lists_all_four_ops_and_exit(running_server):
    """Confirm every menu entry the spec asks for is printed.

    `5\\n` chooses Exit immediately so we don't need to feed operands.
    """
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
    """Menu option 5 (Exit) exits cleanly with code 0."""
    result = _run_client(running_server, "5\n")
    assert result.returncode == 0


def test_eof_at_menu_exits_cleanly(running_server):
    """Ctrl-D (no stdin at all) ends the loop with code 0 rather than crashing."""
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
    """One case per op: pick from menu, enter operands, read 'Result is N!'."""
    stdin = f"{choice}\n{a}\n{b}\n5\n"
    result = _run_client(running_server, stdin)
    assert result.returncode == 0, result.stderr
    assert f"Result is {expected}!" in result.stdout


def test_ux_lines_match_sample(running_server):
    """The four sample-output lines appear in order around one calc."""
    result = _run_client(running_server, "1\n42\n37\n5\n")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    # Each line must appear, in the order the spec sample shows.
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


def test_multiple_requests_in_one_session(running_server):
    """A single client session can issue several calcs back-to-back."""
    stdin = "1\n10\n20\n3\n5\n6\n2\n100\n50\n5\n"
    result = _run_client(running_server, stdin)
    assert result.returncode == 0, result.stderr
    assert "Result is 30!"  in result.stdout      # ADD 10 + 20
    assert "Result is 30!"  in result.stdout      # MUL 5 * 6 (yes, same number)
    assert "Result is 50!"  in result.stdout      # SUB 100 - 50


# --------------------------------------------------------------------------
# Error paths the client must handle gracefully
# --------------------------------------------------------------------------

def test_divide_by_zero_prints_friendly_error(running_server):
    """The kernel's STATUS_DIV_ZERO is translated to a human message.

    The client must NOT print 'Result is ...' for an error response;
    instead it prints the mapped status text.
    """
    result = _run_client(running_server, "4\n7\n0\n5\n")
    assert result.returncode == 0, result.stderr
    assert "division by zero" in result.stdout
    assert "Result is" not in result.stdout


def test_out_of_range_command_reprompts(running_server):
    """Picking a menu number outside 1..5 prints a hint and re-shows the menu."""
    result = _run_client(running_server, "9\n5\n")
    assert result.returncode == 0, result.stderr
    assert "pick 1.." in result.stdout
    # Menu must show up again after the rejection (twice total: initial + after error).
    assert result.stdout.count("(1) Add 2 numbers") >= 2


def test_non_numeric_command_reprompts(running_server):
    """Typing a non-number for the menu doesn't crash."""
    result = _run_client(running_server, "abc\n5\n")
    assert result.returncode == 0, result.stderr
    assert "not a number" in result.stdout


def test_non_integer_operand_reprompts(running_server):
    """Typing 'xyz' as an operand re-prompts (returns to menu)."""
    result = _run_client(running_server, "1\nxyz\n5\n")
    assert result.returncode == 0, result.stderr
    assert "is not an integer" in result.stdout


def test_server_not_reachable_exits_two(tmp_path):
    """If the server socket doesn't exist, the client must fail with code 2.

    Doesn't use the running_server fixture - we deliberately want NO
    server listening at the path we pass.
    """
    bogus = tmp_path / "nope.sock"
    result = subprocess.run(
        [sys.executable, str(CLIENT_SCRIPT), "--socket", str(bogus)],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2
    assert "cannot reach" in result.stderr
