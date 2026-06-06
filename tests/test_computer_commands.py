"""Validate the computer tool's xdotool/scrot command construction offline.

We patch _run (capturing argv) and _screenshot_b64, so no X display is needed —
this checks that each action maps to a well-formed command.

    python3 tests/test_computer_commands.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uaa.computer as cu  # noqa: E402

CALLS: list[list[str]] = []
cu._run = lambda cmd, timeout=15.0: (CALLS.append(list(cmd)), (0, ""))[1]
cu._screenshot_b64 = lambda: "FAKEB64"
cu._GEOMETRY = (cu.C.DISPLAY_WIDTH, cu.C.DISPLAY_HEIGHT)  # prime cache so bounds checks don't probe
cu.C.SCREENSHOT_SETTLE_S = 0  # don't sleep between action and screenshot


def run(action_input):
    CALLS.clear()
    content, err = cu.execute(action_input)
    return content, err, (CALLS[0] if CALLS else [])


def test():
    W, H = cu.C.DISPLAY_WIDTH, cu.C.DISPLAY_HEIGHT

    # screenshot uses the capture path (no xdotool action command)
    content, err, cmd = run({"action": "screenshot"})
    assert not err and content[0]["type"] == "image" and content[0]["source"]["data"] == "FAKEB64"
    assert cmd == []

    # left_click -> mousemove --sync x y ; click --repeat 1 1
    _, err, cmd = run({"action": "left_click", "coordinate": [100, 200]})
    assert not err and cmd[:5] == ["xdotool", "mousemove", "--sync", "100", "200"]
    assert cmd[cmd.index("click") + 1:cmd.index("click") + 4] == ["--repeat", "1", "1"]

    # modifiers wrap the click with keydown/keyup
    _, err, cmd = run({"action": "left_click", "coordinate": [10, 10], "text": "ctrl+shift"})
    assert "keydown" in cmd and "ctrl" in cmd and "shift" in cmd and "keyup" in cmd
    assert cmd.index("keydown") < cmd.index("click") < cmd.index("keyup")

    # double / triple click repeat counts
    _, _, cmd = run({"action": "double_click", "coordinate": [5, 5]})
    assert cmd[cmd.index("click") + 1:cmd.index("click") + 3] == ["--repeat", "2"]
    _, _, cmd = run({"action": "triple_click", "coordinate": [5, 5]})
    assert cmd[cmd.index("click") + 1:cmd.index("click") + 3] == ["--repeat", "3"]

    # right / middle map to buttons 3 / 2
    _, _, cmd = run({"action": "right_click", "coordinate": [5, 5]})
    assert cmd[-1] == "3"
    _, _, cmd = run({"action": "middle_click", "coordinate": [5, 5]})
    assert cmd[-1] == "2"

    # type -- text (uses -- to stop option parsing)
    _, err, cmd = run({"action": "type", "text": "-rf hello"})
    assert not err and cmd == ["xdotool", "type", "--delay", "12", "--", "-rf hello"]

    # key combo
    _, err, cmd = run({"action": "key", "text": "ctrl+s"})
    assert not err and cmd == ["xdotool", "key", "--", "ctrl+s"]

    # scroll down -> button 5 repeated; with coord prefix
    _, err, cmd = run({"action": "scroll", "coordinate": [50, 60],
                       "scroll_direction": "down", "scroll_amount": 3})
    assert not err and "mousemove" in cmd
    assert cmd[cmd.index("click") + 1:cmd.index("click") + 3] == ["--repeat", "3"]
    assert cmd[-1] == "5"  # down = button 5

    # scroll right = button 7
    _, _, cmd = run({"action": "scroll", "scroll_direction": "right", "scroll_amount": 1})
    assert "7" in cmd

    # mouse_move
    _, err, cmd = run({"action": "mouse_move", "coordinate": [11, 22]})
    assert not err and cmd == ["xdotool", "mousemove", "--sync", "11", "22"]

    # left_click_drag with start + modifiers
    _, err, cmd = run({"action": "left_click_drag", "start_coordinate": [1, 2],
                       "coordinate": [3, 4], "text": "shift"})
    assert not err
    assert "mousedown" in cmd and "mouseup" in cmd
    assert cmd.index("keydown") < cmd.index("mousedown") and cmd.index("mouseup") < cmd.index("keyup")

    # left_mouse_down / up
    _, _, cmd = run({"action": "left_mouse_down"})
    assert cmd == ["xdotool", "mousedown", "1"]
    _, _, cmd = run({"action": "left_mouse_up"})
    assert cmd == ["xdotool", "mouseup", "1"]

    # hold_key builds keydown ... sleep ... keyup
    _, err, cmd = run({"action": "hold_key", "text": "ctrl", "duration": 1})
    assert not err and "keydown" in cmd and "sleep" in cmd and "keyup" in cmd

    # cursor_position
    _, err, cmd = run({"action": "cursor_position"})
    assert cmd == ["xdotool", "getmouselocation", "--shell"]

    # out of bounds is rejected without issuing a command
    content, err, cmd = run({"action": "left_click", "coordinate": [W, 0]})
    assert err and cmd == [] and content[0]["type"] == "text"

    # unknown action
    content, err, _ = run({"action": "frobnicate"})
    assert err

    print("ok: all computer actions construct correct xdotool/scrot commands")
    print("COMPUTER COMMANDS TEST PASSED")


if __name__ == "__main__":
    test()
