"""Byte-level tests for the input decoder (no tty needed; uses feed())."""

from __future__ import annotations

from zpotify.term.events import Key, Mouse, Paste
from zpotify.term.input import InputReader


def r() -> InputReader:
    return InputReader()


def test_utf8_multibyte() -> None:
    events = r().feed("é".encode("utf-8"))
    assert events == [Key(char="é")]


def test_utf8_emoji_four_bytes() -> None:
    events = r().feed("😀".encode("utf-8"))
    assert events == [Key(char="😀")]


def test_plain_ascii_and_enter_tab_space() -> None:
    events = r().feed(b"a\r\t ")
    assert events == [
        Key(char="a"),
        Key(name="enter"),
        Key(name="tab"),
        Key(name="space", char=" "),
    ]


def test_arrows() -> None:
    events = r().feed(b"\x1b[A\x1b[B\x1b[C\x1b[D")
    names = [e.name for e in events]
    assert names == ["up", "down", "right", "left"]


def test_ctrl_arrow_modifier() -> None:
    (event,) = r().feed(b"\x1b[1;5C")
    assert event == Key(name="right", ctrl=True)


def test_shift_alt_arrow_modifiers() -> None:
    (shift,) = r().feed(b"\x1b[1;2A")
    (alt,) = r().feed(b"\x1b[1;3B")
    assert shift == Key(name="up", shift=True)
    assert alt == Key(name="down", alt=True)


def test_ctrl_letter() -> None:
    (event,) = r().feed(b"\x01")
    assert event == Key(char="a", ctrl=True)


def test_alt_x() -> None:
    (event,) = r().feed(b"\x1bx")
    assert event == Key(char="x", alt=True)


def test_backspace_and_delete() -> None:
    events = r().feed(b"\x7f\x08\x1b[3~")
    assert events == [
        Key(name="backspace"),
        Key(name="backspace"),
        Key(name="delete"),
    ]


def test_function_keys_ss3_and_tilde() -> None:
    events = r().feed(b"\x1bOP\x1b[15~")
    assert [e.name for e in events] == ["f1", "f5"]


def test_home_end_variants() -> None:
    events = r().feed(b"\x1b[H\x1b[F\x1b[1~\x1b[4~")
    assert [e.name for e in events] == ["home", "end", "home", "end"]


def test_mouse_press() -> None:
    (event,) = r().feed(b"\x1b[<0;10;5M")
    assert event == Mouse(x=9, y=4, kind="press", button=1)


def test_mouse_release() -> None:
    (event,) = r().feed(b"\x1b[<0;10;5m")
    assert event == Mouse(x=9, y=4, kind="release", button=1)


def test_mouse_drag() -> None:
    (event,) = r().feed(b"\x1b[<32;3;3M")  # left button held + motion
    assert event == Mouse(x=2, y=2, kind="drag", button=1)


def test_mouse_move_no_button() -> None:
    (event,) = r().feed(b"\x1b[<35;3;3M")  # motion, no button (low bits 3)
    assert event == Mouse(x=2, y=2, kind="move", button=0)


def test_mouse_scroll() -> None:
    up, down = r().feed(b"\x1b[<64;1;1M\x1b[<65;1;1M")
    assert up == Mouse(x=0, y=0, kind="scroll_up", button=0)
    assert down == Mouse(x=0, y=0, kind="scroll_down", button=0)


def test_mouse_ctrl_shift_modifiers() -> None:
    (event,) = r().feed(b"\x1b[<20;2;2M")  # 16 (ctrl) + 4 (shift) + 0 (left)
    assert event == Mouse(x=1, y=1, kind="press", button=1, ctrl=True, shift=True)


def test_bracketed_paste() -> None:
    (event,) = r().feed(b"\x1b[200~hello world\x1b[201~")
    assert event == Paste(text="hello world")


def test_bracketed_paste_incomplete_then_completed() -> None:
    reader = r()
    assert reader.feed(b"\x1b[200~par") == []
    (event,) = reader.feed(b"tial\x1b[201~")
    assert event == Paste(text="partial")


def test_split_escape_across_feeds() -> None:
    reader = r()
    assert reader.feed(b"\x1b") == []
    assert reader.pending_escape is True
    (event,) = reader.feed(b"[C")
    assert event == Key(name="right")
    assert reader.pending_escape is False


def test_lone_esc_via_flush() -> None:
    reader = r()
    assert reader.feed(b"\x1b") == []
    assert reader.pending_escape is True
    assert reader.flush_escape() == [Key(name="esc")]
    assert reader.pending_escape is False


def test_split_multibyte_across_feeds() -> None:
    reader = r()
    data = "😀".encode("utf-8")
    assert reader.feed(data[:2]) == []
    (event,) = reader.feed(data[2:])
    assert event == Key(char="😀")


def test_backtab() -> None:
    (event,) = r().feed(b"\x1b[Z")
    assert event == Key(name="backtab")
