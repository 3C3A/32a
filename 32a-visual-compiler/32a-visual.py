#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
32a Visual Compiler
- Tkinter-based visual block-flow editor and assembler for 16g / 32a.
- Standard library only.
- The text source is authoritative; the block canvas is a live visualisation.
- The assembler/compiler is embedded; no separate 32aasm.py or 16gasm.py is used.
- Every logical arrow receives its own random high-contrast color, stable during redraws.

Assembly syntax:
    ADD 12, 34
    JUMP 0, 3             # raw relative inputs
    JUMP -> loop          # visual absolute target / label shortcut
    CALL @subroutine      # visual absolute target / label shortcut
    loop:
    ...

Binary format (runtime-compatible):
    OP(1) + INPUT1(2, big-endian) + INPUT2(2, big-endian)
    0x26 separator between records only; no trailing separator.
"""
from __future__ import annotations

import re
import colorsys
import random
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SEPARATOR = 0x26
MAX_U16 = 0xFFFF

OPCODES = {
    "PASS": 0x00, "ADD": 0x01, "SUB": 0x02, "MUL": 0x03,
    "DIV": 0x04, "MOD": 0x05, "VGA": 0x06, "SHFL": 0x07,
    "AND": 0x08, "OR": 0x09, "XOR": 0x0A, "SHL": 0x0B,
    "SHR": 0x0C, "BLEND": 0x0D, "BRIGHT": 0x0E, "CMP": 0x0F,
    "TEXT": 0x10, "IF_EQ": 0x11, "IF_NE": 0x12, "IF_GT": 0x13,
    "IF_LT": 0x14, "IF_GE": 0x15, "IF_LE": 0x16, "JUMP": 0x17,
    "DLOAD": 0x18, "DSTORE": 0x19, "LOAD": 0x1A, "STORE": 0x1B,
    "WAIT": 0x1C, "HALT": 0x1D, "CALL": 0x1E, "RET": 0x1F,
}
NAMES_BY_OPCODE = {v: k for k, v in OPCODES.items()}
GPU_OPS = {name for name, code in OPCODES.items() if code < 0x10}
IF_OPS = {"IF_EQ", "IF_NE", "IF_GT", "IF_LT", "IF_GE", "IF_LE"}
CONTROL_OPS = IF_OPS | {"JUMP", "CALL", "RET", "HALT"}
FLOW_SENSITIVE_OPS = IF_OPS | {"JUMP", "CALL", "RET"}
RESULT_OPS = {
    "PASS", "ADD", "SUB", "MUL", "DIV", "MOD", "AND", "OR", "XOR",
    "SHL", "SHR", "BLEND", "BRIGHT", "CMP", "LOAD", "DLOAD",
}

OP_DESCRIPTIONS = {
    "PASS": "INPUT1을 결과로 전달합니다.",
    "ADD": "두 16비트 입력을 더합니다. 결과는 하위 16비트입니다.",
    "SUB": "INPUT1 - INPUT2. 언더플로는 unsigned 16비트로 래핑됩니다.",
    "MUL": "두 값을 곱하고 하위 16비트만 사용합니다.",
    "DIV": "unsigned 나눗셈의 몫입니다. 0으로 나누면 결과 0과 경고입니다.",
    "MOD": "unsigned 나눗셈의 나머지입니다. 0으로 나누면 결과 0과 경고입니다.",
    "VGA": "INPUT1=(Y<<8)|X, INPUT2=RGB565. 논리 픽셀 하나를 기록합니다.",
    "SHFL": "다음 비-SHFL 명령의 입력을 과거 결과 이력으로 연결합니다. 0은 원래 입력입니다.",
    "AND": "비트 단위 AND 연산입니다.", "OR": "비트 단위 OR 연산입니다.",
    "XOR": "비트 단위 XOR 연산입니다.", "SHL": "INPUT1을 INPUT2만큼 왼쪽으로 이동합니다.",
    "SHR": "INPUT1을 INPUT2만큼 논리 오른쪽 이동합니다.",
    "BLEND": "두 RGB565 색상을 채널별 50:50으로 혼합합니다.",
    "BRIGHT": "RGB565 색상을 INPUT2(0~255) 밝기 계수로 조절합니다.",
    "CMP": "unsigned 비교 결과: 작음=0, 같음=1, 큼=2.",
    "TEXT": "3×5 논리 픽셀 문자 출력. A=(문자Y<<8)|문자X, B=(팔레트<<8)|문자코드.",
    "IF_EQ": "INPUT1 == INPUT2이면 바로 다음 명령 하나를 무효화합니다.",
    "IF_NE": "INPUT1 != INPUT2이면 바로 다음 명령 하나를 무효화합니다.",
    "IF_GT": "INPUT1 > INPUT2이면 바로 다음 명령 하나를 무효화합니다.",
    "IF_LT": "INPUT1 < INPUT2이면 바로 다음 명령 하나를 무효화합니다.",
    "IF_GE": "INPUT1 >= INPUT2이면 바로 다음 명령 하나를 무효화합니다.",
    "IF_LE": "INPUT1 <= INPUT2이면 바로 다음 명령 하나를 무효화합니다.",
    "JUMP": "다음 위치 기준 상대 이동: 다음PC = PC + 1 + INPUT1 - INPUT2.",
    "DLOAD": "연결된 .32d 파일의 INPUT1 워드 주소에서 16비트 값을 읽습니다.",
    "DSTORE": "연결된 .32d 파일의 INPUT1 워드 주소에 INPUT2를 씁니다.",
    "LOAD": "RAM[INPUT1]의 16비트 값을 읽습니다.",
    "STORE": "RAM[INPUT1]에 INPUT2를 기록합니다.",
    "WAIT": "INPUT1 밀리초 동안 프로그램 실행을 일시 중지합니다.",
    "HALT": "현재 프로그램 실행을 정상 종료합니다.",
    "CALL": "복귀 주소 PC+1을 저장하고 JUMP와 같은 상대 이동 규칙으로 호출합니다.",
    "RET": "CALL 전용 복귀 주소 스택에서 복귀합니다.",
}

@dataclass
class Instruction:
    mnemonic: str
    opcode: int
    a: int
    b: int
    line_number: int
    source_line: str
    target_index: Optional[int] = None
    target_label: Optional[str] = None
    symbolic_target: bool = False

@dataclass
class Link:
    source_index: int
    target_index: int
    input_name: str
    history_index: int
    shfl_index: int
    uncertain: bool

class ArrowColorRegistry:
    """Assign a bright random color to each logical arrow and keep it stable on redraw."""

    def __init__(self):
        self._colors: dict[tuple, str] = {}
        self._hues: dict[tuple, float] = {}
        self._rng = random.SystemRandom()

    def color_for(self, key: tuple) -> str:
        cached = self._colors.get(key)
        if cached is not None:
            return cached

        # Keep newly generated hues reasonably separated from existing arrows.
        # The minimum separation decreases as the diagram becomes denser.
        min_separation = max(0.018, min(0.075, 0.50 / (len(self._hues) + 1)))
        used_hues = tuple(self._hues.values())
        hue = self._rng.random()
        for _ in range(128):
            candidate = self._rng.random()
            if all(min(abs(candidate - old), 1.0 - abs(candidate - old)) >= min_separation
                   for old in used_hues):
                hue = candidate
                break

        # High saturation and full value keep arrows visible on the dark canvas.
        red, green, blue = colorsys.hsv_to_rgb(hue, 0.68, 1.0)
        color = "#{:02X}{:02X}{:02X}".format(
            round(red * 255), round(green * 255), round(blue * 255)
        )
        self._hues[key] = hue
        self._colors[key] = color
        return color


class AssemblyError(Exception):
    def __init__(self, message: str, line: Optional[int] = None):
        super().__init__(message)
        self.line = line
    def __str__(self) -> str:
        return f"Line {self.line}: {super().__str__()}" if self.line else super().__str__()


def split_comment(line: str) -> tuple[str, str]:
    positions = [p for p in (line.find("#"), line.find(";"), line.find("//")) if p >= 0]
    if not positions:
        return line, ""
    pos = min(positions)
    return line[:pos], line[pos:]


def parse_number(token: str, line_number: int) -> int:
    token = token.strip().replace("_", "")
    try:
        if not token:
            raise ValueError
        if token.lower().startswith(("0x", "0b", "0o")):
            value = int(token, 0)
        elif re.fullmatch(r"[0-9]+", token):
            value = int(token, 10)
        else:
            raise ValueError
    except ValueError:
        raise AssemblyError(f"잘못된 숫자 {token!r}. 10진수, 0x, 0b, 0o 형식을 사용하세요.", line_number)
    if not 0 <= value <= MAX_U16:
        raise AssemblyError(f"{token!r}은 16비트 unsigned 범위(0~65535)를 벗어났습니다.", line_number)
    return value


def parse_operand_pair(text: str, line_number: int) -> tuple[int, int]:
    text = text.strip()
    if not text:
        return 0, 0
    if "," in text:
        parts = text.split(",")
        if len(parts) != 2 or not all(p.strip() for p in parts):
            raise AssemblyError("피연산자는 '값1, 값2' 형식이어야 합니다.", line_number)
    else:
        parts = text.split()
        if len(parts) == 1:
            parts.append("0")
        if len(parts) != 2:
            raise AssemblyError("피연산자는 최대 두 개까지 지정할 수 있습니다.", line_number)
    return parse_number(parts[0], line_number), parse_number(parts[1], line_number)


def parse_source(source: str) -> list[Instruction]:
    """Parse assembly source. Labels may be used as JUMP ->label / CALL @label."""
    labels: dict[str, int] = {}
    pending: list[dict] = []

    for line_number, raw in enumerate(source.splitlines(), 1):
        code, _comment = split_comment(raw)
        rest = code.strip()
        if not rest:
            continue
        while True:
            label_match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", rest)
            if not label_match:
                break
            label = label_match.group(1).upper()
            if label in labels:
                raise AssemblyError(f"중복 라벨 {label!r}입니다.", line_number)
            labels[label] = len(pending)
            rest = rest[label_match.end():].strip()
            if not rest:
                break
        if not rest:
            continue
        match = re.match(r"^([A-Za-z][A-Za-z0-9_]*)(?:\s+(.*))?$", rest)
        if not match:
            raise AssemblyError("명령 형식이 올바르지 않습니다. 예: ADD 10, 20", line_number)
        mnemonic = match.group(1).upper()
        if mnemonic not in OPCODES:
            raise AssemblyError(f"알 수 없는 OP {mnemonic!r}입니다.", line_number)
        operand_text = (match.group(2) or "").strip()
        item = {"mnemonic": mnemonic, "line": line_number, "raw": raw,
                "operand_text": operand_text, "target": None, "symbolic": False}

        # Convenient absolute target syntax: JUMP ->label, JUMP @12, CALL @routine.
        if mnemonic in {"JUMP", "CALL"} and operand_text:
            target_match = re.fullmatch(r"(?:->|@)\s*([A-Za-z_][A-Za-z0-9_]*|[0-9]+|0[xX][0-9A-Fa-f]+|0[bB][01]+|0[oO][0-7]+)", operand_text)
            plain_label = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", operand_text)
            if target_match or plain_label:
                token = target_match.group(1) if target_match else operand_text
                item["target"] = token
                item["symbolic"] = True
                pending.append(item)
                continue
        item["a"], item["b"] = parse_operand_pair(operand_text, line_number)
        pending.append(item)

    if not pending:
        return []

    result: list[Instruction] = []
    count = len(pending)
    for index, item in enumerate(pending):
        mnemonic = item["mnemonic"]
        a = item.get("a", 0)
        b = item.get("b", 0)
        target_index: Optional[int] = None
        target_label: Optional[str] = None
        symbolic = bool(item["symbolic"])
        if symbolic:
            token = item["target"]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token) and not token.lower().startswith(("0x", "0b", "0o")):
                target_label = token.upper()
                if target_label not in labels:
                    raise AssemblyError(f"정의되지 않은 라벨 {token!r}입니다.", item["line"])
                target_index = labels[target_label]
            else:
                target_index = parse_number(token, item["line"])
            if target_index > count or (mnemonic == "CALL" and target_index >= count):
                raise AssemblyError(f"{mnemonic} 대상 블럭 {target_index}이 프로그램 범위를 벗어났습니다.", item["line"])
            delta = target_index - (index + 1)
            a = max(delta, 0)
            b = max(-delta, 0)
        elif mnemonic in {"JUMP", "CALL"}:
            target_index = index + 1 + a - b
            if target_index < 0 or target_index > count or (mnemonic == "CALL" and target_index >= count):
                raise AssemblyError(f"{mnemonic}의 계산된 대상 블럭 {target_index}이 프로그램 범위를 벗어났습니다.", item["line"])
        result.append(Instruction(mnemonic, OPCODES[mnemonic], a, b,
                                  item["line"], item["raw"], target_index,
                                  target_label, symbolic))
    return result


def encode_program(instructions: list[Instruction], target_format: str) -> bytes:
    target_format = target_format.lower()
    if target_format not in {"16g", "32a"}:
        raise AssemblyError("출력 형식은 16g 또는 32a여야 합니다.")
    if not instructions:
        raise AssemblyError("컴파일할 명령어가 없습니다.")
    records = bytearray()
    for index, ins in enumerate(instructions):
        if target_format == "16g" and ins.opcode >= 0x10:
            raise AssemblyError(f".16g는 GPU OP만 지원합니다. {ins.mnemonic}은 CPU OP입니다.", ins.line_number)
        if index:
            records.append(SEPARATOR)
        records.extend((ins.opcode, (ins.a >> 8) & 0xFF, ins.a & 0xFF,
                        (ins.b >> 8) & 0xFF, ins.b & 0xFF))
    return bytes(records)


def decode_program(data: bytes, target_format: str) -> list[tuple[int, int, int]]:
    """Runtime-style fixed-record decoder used by self-tests."""
    max_op = 0x0F if target_format.lower() == "16g" else 0x1F
    out: list[tuple[int, int, int]] = []
    pos = 0
    while pos < len(data):
        if len(data) - pos < 5:
            raise AssemblyError("명령 레코드가 5바이트보다 짧습니다.")
        op = data[pos]
        a = (data[pos + 1] << 8) | data[pos + 2]
        b = (data[pos + 3] << 8) | data[pos + 4]
        if op > max_op:
            raise AssemblyError(f"잘못된 OP 0x{op:02X}입니다.")
        out.append((op, a, b))
        pos += 5
        if pos < len(data):
            if data[pos] != SEPARATOR:
                raise AssemblyError("명령 사이에 0x26 구분자가 없습니다.")
            pos += 1
            if pos == len(data):
                raise AssemblyError("파일 끝에 불필요한 구분자가 있습니다.")
    return out


def find_shfl_links(instructions: list[Instruction]) -> tuple[list[Link], list[tuple[int, str]]]:
    """Predict SHFL source blocks assuming linear order; control flow can alter runtime history."""
    links: list[Link] = []
    unresolved: list[tuple[int, str]] = []
    history: list[int] = []
    pending: Optional[tuple[int, int, int]] = None
    # Control flow encountered before a consumer can change which results exist in history.
    control_seen = False
    for index, ins in enumerate(instructions):
        if ins.mnemonic == "SHFL":
            pending = (index, ins.a, ins.b)  # latest SHFL replaces unused setting
            continue
        if pending is not None:
            shfl_index, a_index, b_index = pending
            for input_name, history_index in (("A", a_index), ("B", b_index)):
                if history_index == 0:
                    continue
                src_pos = len(history) - history_index
                if src_pos < 0:
                    unresolved.append((index, f"{input_name}: 이력 #{history_index}가 아직 없음"))
                else:
                    links.append(Link(history[src_pos], index, input_name,
                                      history_index, shfl_index, control_seen))
            pending = None
        if ins.mnemonic in RESULT_OPS:
            history.append(index)
        if ins.mnemonic in FLOW_SENSITIVE_OPS:
            control_seen = True
    return links, unresolved


def instruction_summary(ins: Instruction, index: int, count: int) -> str:
    m, a, b = ins.mnemonic, ins.a, ins.b
    if m == "VGA":
        return f"X={a & 255}, Y={a >> 8}, RGB565=0x{b:04X}"
    if m == "TEXT":
        return f"문자 X={a & 255}, Y={a >> 8}, 팔레트={b >> 8}, 코드=0x{b & 255:02X}"
    if m == "SHFL":
        return f"다음 명령 입력 이력: A=#{a if a else '원본'}, B=#{b if b else '원본'}"
    if m in IF_OPS:
        return f"조건 참이면 블럭 {index + 1} 무효화 → {min(index + 2, count)}로 이동"
    if m in {"JUMP", "CALL"}:
        return f"상대 이동 A={a}, B={b} → 블럭 {ins.target_index}"
    if m in {"DLOAD", "LOAD"}:
        return f"주소 0x{a:04X}에서 워드 읽기"
    if m in {"DSTORE", "STORE"}:
        return f"주소 0x{a:04X}에 0x{b:04X} 쓰기"
    if m == "WAIT":
        return f"{a} ms 대기"
    if m in {"HALT", "RET"}:
        return "입력 없음" if a == 0 and b == 0 else f"입력값: {a}, {b} (실행 시 무시)"
    return f"INPUT1=0x{a:04X} ({a}), INPUT2=0x{b:04X} ({b})"


class VisualCompilerApp(tk.Tk):
    # Canvas rows use fixed world coordinates, independent of the visible viewport.
    ROW_BASE = 58
    ROW_HEIGHT = 62
    BLOCK_HALF_HEIGHT = 20
    CANVAS_MIN_WIDTH = 470

    BG = "#10151d"
    PANEL = "#192230"
    PANEL_2 = "#202c3d"
    TEXT = "#e7edf5"
    MUTED = "#91a0b5"
    BLUE = "#4ca6ff"
    GREEN = "#47c78a"
    ORANGE = "#f2ad55"
    RED = "#ff6b73"
    PURPLE = "#b895ff"

    def __init__(self):
        super().__init__()
        self.title("32a Visual Compiler")
        self.geometry("1450x900")
        self.minsize(1050, 680)
        self.configure(bg=self.BG)
        self.current_source_path: Optional[Path] = None
        self.current_output_path: Optional[Path] = None
        self.instructions: list[Instruction] = []
        self.selected_index: Optional[int] = None
        self.parse_error: Optional[AssemblyError] = None
        self._syncing_editor = False
        self._parse_after_id: Optional[str] = None
        self._drag_source: Optional[int] = None
        self._drag_line_id: Optional[int] = None
        self._last_valid_instructions: list[Instruction] = []
        self._draw_after_id: Optional[str] = None
        self._pending_stale = False
        self._drawn_selected_index: Optional[int] = None
        self._drawn_stale = False
        self._line_number_count = 0
        self._cached_links: list[Link] = []
        self._cached_unresolved: list[tuple[int, str]] = []
        self._cached_flow_ops: list[tuple[int, Instruction]] = []
        self.arrow_colors = ArrowColorRegistry()
        self._build_style()
        self._build_ui()
        self._set_editor_text(
            "# 32a Visual Compiler demo\n"
            "# JUMP/CALL은 ->블럭번호 또는 ->라벨로 목적지를 직접 지정할 수 있습니다.\n"
            "# SHFL 화살표는 직선 실행 순서를 기준으로 한 예상 연결입니다.\n"
            "\n"
            "start:\n"
            "ADD 10, 20\n"
            "SHFL 1, 0\n"
            "ADD 5, 0\n"
            "TEXT 0x0000, 0xFF41\n"
            "WAIT 250\n"
            "HALT\n"
        )
        self._parse_and_refresh()

    def _build_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=self.BG)
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("TLabel", background=self.BG, foreground=self.TEXT,
                        font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background=self.PANEL, foreground=self.MUTED,
                        font=("Segoe UI", 9))
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT,
                        font=("Segoe UI Semibold", 16))
        style.configure("TButton", background=self.PANEL_2, foreground=self.TEXT,
                        bordercolor="#344256", lightcolor=self.PANEL_2,
                        darkcolor=self.PANEL_2, focuscolor=self.BLUE,
                        font=("Segoe UI", 9), padding=(9, 5))
        style.map("TButton",
                  background=[("pressed", "#26374b"), ("active", "#2b3b50")],
                  foreground=[("disabled", self.MUTED), ("!disabled", self.TEXT)])
        style.configure("Accent.TButton", background="#2367a8", foreground="white",
                        bordercolor="#367db3", lightcolor="#2367a8",
                        darkcolor="#2367a8", focuscolor=self.BLUE,
                        padding=(12, 6))
        style.map("Accent.TButton",
                  background=[("pressed", "#1b5288"), ("active", "#2f83d2")],
                  foreground=[("!disabled", "white")])
        style.configure("TCombobox", fieldbackground=self.PANEL_2,
                        background=self.PANEL_2, foreground=self.TEXT,
                        arrowcolor=self.TEXT, bordercolor="#344256",
                        lightcolor=self.PANEL_2, darkcolor=self.PANEL_2,
                        padding=4)
        style.map("TCombobox",
                  fieldbackground=[("readonly", self.PANEL_2), ("focus", self.PANEL_2)],
                  foreground=[("readonly", self.TEXT)],
                  background=[("readonly", self.PANEL_2)])
        style.configure("Horizontal.TPanedwindow", background=self.BG)
        style.configure("Vertical.TScrollbar", background=self.PANEL_2,
                        troughcolor=self.BG, arrowcolor=self.TEXT)
        style.configure("Horizontal.TScrollbar", background=self.PANEL_2,
                        troughcolor=self.BG, arrowcolor=self.TEXT)

    def _build_ui(self):
        # Main toolbar: actions are grouped so the editor keeps more vertical space.
        top = tk.Frame(self, bg=self.BG, padx=14, pady=9)
        top.pack(fill="x")
        brand = tk.Frame(top, bg=self.BG)
        brand.pack(side="left")
        tk.Label(brand, text="32a", bg=self.BG, fg=self.BLUE,
                 font=("Segoe UI Semibold", 22)).pack(side="left")
        tk.Label(brand, text="VISUAL COMPILER", bg=self.BG, fg=self.TEXT,
                 font=("Segoe UI Semibold", 11)).pack(side="left", padx=(8, 16), pady=(5, 0))

        actions = tk.Frame(top, bg=self.BG)
        ttk.Button(actions, text="새 코드", command=self.new_source).pack(side="left", padx=2)
        ttk.Button(actions, text="소스 열기", command=self.open_source).pack(side="left", padx=2)
        ttk.Button(actions, text="코드 내보내기", command=self.export_source).pack(side="left", padx=2)
        ttk.Button(actions, text="컴파일", style="Accent.TButton",
                   command=self.compile_file).pack(side="left", padx=(8, 5))
        tk.Label(actions, text="출력", bg=self.BG, fg=self.MUTED,
                 font=("Segoe UI", 9)).pack(side="left", padx=(8, 4))
        self.format_var = tk.StringVar(value="32a")
        self.format_combo = ttk.Combobox(
            actions, textvariable=self.format_var, values=("32a", "16g"),
            width=6, state="readonly")
        self.format_combo.pack(side="left")
        self.format_combo.bind("<<ComboboxSelected>>", self._on_format_changed)

        self.status_label = tk.Label(top, text="준비", bg=self.BG, fg=self.GREEN,
                                     font=("Segoe UI", 9), anchor="e")
        self.status_label.pack(side="right", padx=(8, 2))
        actions.pack(side="left", fill="x", expand=True)

        body = ttk.Panedwindow(self, orient="horizontal", style="Horizontal.TPanedwindow")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        left = ttk.Frame(body, style="Panel.TFrame", padding=8)
        right = ttk.Frame(body, style="Panel.TFrame", padding=8)
        body.add(left, weight=5)
        body.add(right, weight=5)

        # Left: block workspace.
        block_toolbar = tk.Frame(left, bg=self.PANEL)
        block_toolbar.pack(fill="x", pady=(0, 7))
        tk.Label(block_toolbar, text="BLOCK FLOW", bg=self.PANEL, fg=self.TEXT,
                 font=("Segoe UI Semibold", 11)).pack(side="left", padx=(4, 9))
        self.op_var = tk.StringVar(value="ADD")
        self.op_combo = ttk.Combobox(
            block_toolbar, textvariable=self.op_var,
            values=tuple(OPCODES.keys()), width=13, state="readonly")
        self.op_combo.pack(side="left", padx=3)
        ttk.Button(block_toolbar, text="＋ 블럭", command=self.add_block).pack(side="left", padx=3)
        ttk.Button(block_toolbar, text="삭제", command=self.delete_block).pack(side="left", padx=3)
        ttk.Button(block_toolbar, text="↑", width=3,
                   command=lambda: self.move_block(-1)).pack(side="left", padx=(8, 2))
        ttk.Button(block_toolbar, text="↓", width=3,
                   command=lambda: self.move_block(1)).pack(side="left", padx=2)

        canvas_wrap = tk.Frame(left, bg=self.PANEL)
        canvas_wrap.pack(fill="both", expand=True)
        canvas_wrap.grid_rowconfigure(0, weight=1)
        canvas_wrap.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            canvas_wrap, bg="#111a27", highlightthickness=0,
            xscrollincrement=24, yscrollincrement=self.ROW_HEIGHT,
            borderwidth=0, takefocus=1)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas_vscroll = tk.Scrollbar(
            canvas_wrap, orient="vertical", command=self._canvas_yview,
            bg=self.PANEL_2, activebackground="#344a63", troughcolor="#0e1621",
            highlightbackground=self.PANEL, highlightcolor=self.PANEL,
            relief="flat", borderwidth=0, width=12)
        self.canvas_vscroll.grid(row=0, column=1, sticky="ns")
        self.canvas_hscroll = tk.Scrollbar(
            canvas_wrap, orient="horizontal", command=self._canvas_xview,
            bg=self.PANEL_2, activebackground="#344a63", troughcolor="#0e1621",
            highlightbackground=self.PANEL, highlightcolor=self.PANEL,
            relief="flat", borderwidth=0, width=12)
        self.canvas_hscroll.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(
            xscrollcommand=self.canvas_hscroll.set,
            yscrollcommand=self.canvas_vscroll.set)
        self.canvas.bind("<ButtonPress-1>", self.on_canvas_press)
        self.canvas.bind("<B1-Motion>", self.on_canvas_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_release)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind("<Shift-MouseWheel>", self.on_horizontal_mousewheel)
        self.canvas.bind("<Up>", lambda _e: self._canvas_yview("scroll", -1, "units"))
        self.canvas.bind("<Down>", lambda _e: self._canvas_yview("scroll", 1, "units"))

        details = tk.Frame(left, bg="#151e2b", padx=12, pady=9)
        details.pack(fill="x", pady=(8, 0))
        detail_head = tk.Frame(details, bg="#151e2b")
        detail_head.pack(fill="x")
        tk.Label(detail_head, text="예상 크기 / 세부 정보", bg="#151e2b",
                 fg=self.TEXT, font=("Segoe UI Semibold", 10)).pack(side="left")
        self.size_label = tk.Label(
            detail_head, text="0 blocks · 0 bytes", bg="#151e2b", fg=self.GREEN,
            font=("Consolas", 9, "bold"), anchor="e")
        self.size_label.pack(side="right", padx=(8, 0))
        self.detail_text = tk.Label(
            details, text="블럭을 선택하면 세부 정보가 여기에 표시됩니다.",
            bg="#151e2b", fg=self.MUTED, justify="left", anchor="w",
            font=("Segoe UI", 9), wraplength=650)
        self.detail_text.pack(fill="x", pady=(7, 0))
        self.link_summary = tk.Label(
            details, text="화살표: JUMP/CALL 대상 · SHFL 결과 이력 예상 연결",
            bg="#151e2b", fg="#8ea4bd", justify="left", anchor="w",
            font=("Segoe UI", 8))
        self.link_summary.pack(fill="x", pady=(5, 0))
        details.bind("<Configure>", self._on_details_configure)

        # Right: source editor. Both axes have independent scrollbars.
        editor_header = tk.Frame(right, bg=self.PANEL)
        editor_header.pack(fill="x", pady=(0, 6))
        tk.Label(editor_header, text="ASSEMBLY SOURCE", bg=self.PANEL,
                 fg=self.TEXT, font=("Segoe UI Semibold", 11)).pack(side="left")
        tk.Label(editor_header, text="직접 편집 · 자동 미리보기", bg=self.PANEL,
                 fg=self.MUTED, font=("Segoe UI", 8)).pack(side="right")

        text_wrap = tk.Frame(right, bg=self.PANEL_2)
        text_wrap.pack(fill="both", expand=True)
        text_wrap.grid_rowconfigure(0, weight=1)
        text_wrap.grid_columnconfigure(1, weight=1)
        self.line_numbers = tk.Text(
            text_wrap, width=5, padx=7, pady=9, state="disabled",
            bg="#182230", fg="#6f8299", insertbackground=self.TEXT,
            relief="flat", font=("Consolas", 10), wrap="none", takefocus=0,
            cursor="arrow", borderwidth=0)
        self.line_numbers.grid(row=0, column=0, sticky="ns")
        self.editor = tk.Text(
            text_wrap, undo=True, padx=10, pady=9,
            bg="#111a27", fg="#dbe7f5", insertbackground="white",
            selectbackground="#245b8a", relief="flat", borderwidth=0,
            font=("Consolas", 10), wrap="none", tabs=(32,),
            maxundo=-1, autoseparators=True)
        self.editor.grid(row=0, column=1, sticky="nsew")
        self.editor_scroll = tk.Scrollbar(
            text_wrap, orient="vertical", command=self._editor_yview,
            bg=self.PANEL_2, activebackground="#344a63", troughcolor="#0e1621",
            highlightbackground=self.PANEL, highlightcolor=self.PANEL,
            relief="flat", borderwidth=0, width=12)
        self.editor_scroll.grid(row=0, column=2, sticky="ns")
        self.editor_xscroll = tk.Scrollbar(
            text_wrap, orient="horizontal", command=self.editor.xview,
            bg=self.PANEL_2, activebackground="#344a63", troughcolor="#0e1621",
            highlightbackground=self.PANEL, highlightcolor=self.PANEL,
            relief="flat", borderwidth=0, width=12)
        self.editor_xscroll.grid(row=1, column=1, sticky="ew")
        self.editor.configure(
            yscrollcommand=self._editor_yscroll_set,
            xscrollcommand=self.editor_xscroll.set)
        self.editor.bind("<<Modified>>", self.on_editor_modified)
        self.editor.bind("<MouseWheel>", lambda _e: self.after_idle(self.update_line_numbers))
        self.bind_all("<Control-Shift-S>", lambda _e: self.export_source())
        self.bind_all("<Control-Return>", lambda _e: self.compile_file())

        self.bottom_editor = tk.Label(
            right,
            text="JUMP ->블럭번호 / CALL @라벨 · 블럭의 왼쪽 여백에서 화살표를 끌어 목적지를 지정할 수 있습니다.\nSHFL 연결은 직선 코드 순서 기준의 예상이며, 분기·반복 실행에서는 실제 이력과 달라질 수 있습니다.",
            bg=self.PANEL, fg=self.MUTED, justify="left", anchor="w",
            font=("Segoe UI", 8), wraplength=650)
        self.bottom_editor.pack(fill="x", pady=(7, 0))
        self.update_line_numbers()

    def _on_format_changed(self, _event=None):
        parsed = self._last_valid_instructions if self.parse_error else self.instructions
        self._update_details(parsed, str(self.parse_error) if self.parse_error else None)

    def _on_details_configure(self, event):
        wrap = max(260, event.width - 24)
        self.detail_text.configure(wraplength=wrap)

    def _editor_yscroll_set(self, first, last):
        self.editor_scroll.set(first, last)
        self.line_numbers.yview_moveto(float(first))

    def _editor_yview(self, *args):
        self.editor.yview(*args)
        self.line_numbers.yview(*args)

    def update_line_numbers(self):
        # Rebuild the gutter only when the number of lines changes.
        line_count = int(self.editor.index("end-1c").split(".")[0])
        if line_count != self._line_number_count:
            numbers = "\n".join(str(i) for i in range(1, line_count + 1))
            self.line_numbers.configure(state="normal")
            self.line_numbers.delete("1.0", "end")
            self.line_numbers.insert("1.0", numbers)
            self.line_numbers.configure(state="disabled")
            self._line_number_count = line_count
        self.line_numbers.yview_moveto(self.editor.yview()[0])

    def _set_editor_text(self, text: str):
        self._syncing_editor = True
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", text)
        self.editor.edit_modified(False)
        self._syncing_editor = False
        self.update_line_numbers()

    def on_editor_modified(self, _event=None):
        if self._syncing_editor:
            self.editor.edit_modified(False)
            return
        if self.editor.edit_modified():
            self.editor.edit_modified(False)
            self.update_line_numbers()
            if self._parse_after_id:
                self.after_cancel(self._parse_after_id)
            self._parse_after_id = self.after(180, self._parse_and_refresh)

    def _parse_and_refresh(self):
        self._parse_after_id = None
        source = self.editor.get("1.0", "end-1c")
        try:
            parsed = parse_source(source)
            self.instructions = parsed
            self._last_valid_instructions = parsed
            self._cache_graph_data(parsed)
            self.parse_error = None
            if self.selected_index is not None and self.selected_index >= len(parsed):
                self.selected_index = len(parsed) - 1 if parsed else None
            self.status_label.configure(text=f"정상 · {len(parsed)} blocks", fg=self.GREEN)
            self._update_details(parsed)
            self.request_draw(stale=False)
        except AssemblyError as exc:
            self.parse_error = exc
            self.status_label.configure(text=f"코드 오류 · {exc}", fg=self.RED)
            self._update_details(self._last_valid_instructions, error=str(exc))
            # Preserve the last valid preview while clearly marking it as stale.
            self.request_draw(stale=True)

    def _update_details(self, parsed=None, error: Optional[str] = None):
        if parsed is None:
            parsed = self._last_valid_instructions if self.parse_error else self.instructions
        count = len(parsed)
        fmt = self.format_var.get().lower()
        size = max(0, count * 5 + max(0, count - 1))
        text = f"{count} blocks  ·  예상 바이너리 크기 {size:,} bytes  ·  레코드 5B + 구분자 1B"
        if fmt == "16g":
            cpu_count = sum(1 for ins in parsed if ins.opcode >= 0x10)
            if cpu_count:
                text += f"  ·  CPU OP {cpu_count}개 포함(16g 컴파일 불가)"
            else:
                text += "  ·  GPU OP만 사용 가능"
        self.size_label.configure(text=text)
        if error:
            self.detail_text.configure(text=f"컴파일/미리보기 오류: {error}", fg=self.RED)
            return
        if self.selected_index is None or not (0 <= self.selected_index < len(parsed)):
            self.detail_text.configure(
                text="블럭을 선택하면 입력값, OP 설명, 목적지와 SHFL 연결 정보를 볼 수 있습니다.",
                fg=self.MUTED)
            return
        ins = parsed[self.selected_index]
        summary = (
            f"블럭 {self.selected_index:03d}  |  {ins.mnemonic}  |  OP=0x{ins.opcode:02X} ({ins.opcode:05b})\n"
            f"INPUT1=0x{ins.a:04X} ({ins.a})    INPUT2=0x{ins.b:04X} ({ins.b})\n"
            f"{OP_DESCRIPTIONS[ins.mnemonic]}\n"
            f"세부: {instruction_summary(ins, self.selected_index, len(parsed))}  ·  소스 {ins.line_number}행")
        self.detail_text.configure(text=summary, fg=self.TEXT)

    def _cache_graph_data(self, instructions: list[Instruction]):
        self._cached_links, self._cached_unresolved = find_shfl_links(instructions)
        self._cached_flow_ops = [
            (index, ins) for index, ins in enumerate(instructions)
            if ins.mnemonic in {"JUMP", "CALL"} or ins.mnemonic in IF_OPS
        ]

    def request_draw(self, stale: bool = False, delay: int = 45):
        self._pending_stale = stale
        if self._draw_after_id:
            self.after_cancel(self._draw_after_id)
        self._draw_after_id = self.after(delay, self.draw_blocks)

    def _on_canvas_configure(self, _event=None):
        # Debounce rapid resize events; one final render is enough.
        self.request_draw(stale=self.parse_error is not None, delay=90)

    def _canvas_yview(self, *args):
        self.canvas.yview(*args)
        self.request_draw(stale=self.parse_error is not None, delay=25)

    def _canvas_xview(self, *args):
        self.canvas.xview(*args)
        self.request_draw(stale=self.parse_error is not None, delay=25)

    def on_horizontal_mousewheel(self, event):
        self.canvas.xview_scroll(int(-event.delta / 120), "units")
        self.request_draw(stale=self.parse_error is not None, delay=25)
        return "break"

    def _refresh_block_selection(self):
        # Only two rectangle items need changing when selection moves.
        if self._drawn_selected_index is not None:
            old = self._drawn_selected_index
            old_is_gpu = (old < len(self._last_valid_instructions) and
                          self._last_valid_instructions[old].opcode < 0x10)
            old_edge = "#596575" if self._drawn_stale else ("#347db3" if old_is_gpu else "#3a9a71")
            self.canvas.itemconfigure(f"block_rect_{old}", outline=old_edge, width=1)
        new = self.selected_index
        if new is not None:
            new_is_gpu = (new < len(self._last_valid_instructions) and
                          self._last_valid_instructions[new].opcode < 0x10)
            if new_is_gpu or (new < len(self._last_valid_instructions)):
                self.canvas.itemconfigure(f"block_rect_{new}", outline=self.BLUE, width=2)
        self._drawn_selected_index = new

    def _canvas_row_y(self, index: int) -> int:
        return self.ROW_BASE + index * self.ROW_HEIGHT

    def _canvas_row_at_y(self, y: float) -> int:
        return int((y - (self.ROW_BASE - self.ROW_HEIGHT / 2)) // self.ROW_HEIGHT)

    def draw_blocks(self):
        self._draw_after_id = None
        if not hasattr(self, "canvas"):
            return
        stale = self._pending_stale
        instructions = self._last_valid_instructions if stale else self.instructions
        visible_width = max(self.canvas.winfo_width(), 1)
        width = max(visible_width, self.CANVAS_MIN_WIDTH)
        height = max(150, 96 + max(1, len(instructions)) * self.ROW_HEIGHT)

        # Keep the user's viewport while rebuilding the visible section.
        try:
            x_fraction = self.canvas.xview()[0]
            y_fraction = self.canvas.yview()[0]
        except tk.TclError:
            x_fraction = y_fraction = 0.0

        self.canvas.configure(scrollregion=(0, 0, width, height))
        self.canvas.xview_moveto(x_fraction)
        self.canvas.yview_moveto(y_fraction)
        if width <= visible_width + 1:
            self.canvas_hscroll.grid_remove()
        else:
            self.canvas_hscroll.grid()
        self.canvas.delete("all")
        left_x = min(156, max(112, int(visible_width * 0.25)))
        right_x = max(left_x + 300, width - 140)

        self.canvas.create_text(
            16, 16, anchor="w", text="PROGRAM FLOW  /  위에서 아래로 기본 실행",
            fill="#8196ad", font=("Segoe UI", 8, "bold"), tags=("header",))
        if stale:
            self.canvas.create_text(
                width - 14, 16, anchor="e", text="STALE PREVIEW · 코드 오류",
                fill=self.RED, font=("Segoe UI", 8, "bold"), tags=("header",))

        if not instructions:
            self.canvas.create_text(
                width // 2, 120, text="＋ 블럭을 추가하거나 오른쪽에 코드를 입력하세요",
                fill="#71849b", font=("Segoe UI", 11))
            self.canvas.configure(scrollregion=(0, 0, width, height))
            self._drawn_selected_index = None
            self._drawn_stale = stale
            return

        # Render only rows inside (and just around) the viewport. Large files stay responsive.
        view_top = self.canvas.canvasy(0)
        view_bottom = self.canvas.canvasy(max(1, self.canvas.winfo_height()))
        first = max(0, int((view_top - (self.ROW_BASE + self.ROW_HEIGHT / 2)) // self.ROW_HEIGHT) - 1)
        last = min(len(instructions) - 1,
                   int((view_bottom - (self.ROW_BASE - self.ROW_HEIGHT / 2)) // self.ROW_HEIGHT) + 1)
        if last < first:
            first, last = 0, min(len(instructions) - 1, 10)
        visible_indices = range(first, last + 1)

        # Arrows are rendered only when their vertical span intersects the viewport.
        links = self._cached_links
        unresolved = self._cached_unresolved
        for i, ins in self._cached_flow_ops:
            y = self._canvas_row_y(i)
            if ins.mnemonic in {"JUMP", "CALL"} and ins.target_index is not None:
                target = ins.target_index
                if 0 <= target <= len(instructions):
                    ty = self._canvas_row_y(target)
                    if min(y, ty) <= view_bottom + self.ROW_HEIGHT and max(y, ty) >= view_top - self.ROW_HEIGHT:
                        lane_x = 25 + (i % 8) * 15
                        dash = (5, 3) if ins.mnemonic == "CALL" else None
                        color = self.arrow_colors.color_for(("flow", i, target, ins.mnemonic))
                        self.canvas.create_line(
                            left_x, y, lane_x, y, lane_x, ty, left_x, ty,
                            fill=color, width=2, dash=dash, arrow="last",
                            arrowshape=(8, 10, 4), tags=("flow_arrow",))
                        self.canvas.create_text(
                            lane_x + 4, (y + ty) / 2, anchor="w",
                            text=f"{ins.mnemonic} → {target:03d}", fill=color,
                            font=("Segoe UI", 7, "bold"), tags=("flow_arrow",))
            elif ins.mnemonic in IF_OPS and i + 2 <= len(instructions):
                target = i + 2
                ty = self._canvas_row_y(target)
                if min(y, ty) <= view_bottom + self.ROW_HEIGHT and max(y, ty) >= view_top - self.ROW_HEIGHT:
                    lane_x = 154
                    color = self.arrow_colors.color_for(("if-skip", i, target))
                    self.canvas.create_line(
                        lane_x, y, lane_x, ty, fill=color, width=1,
                        dash=(2, 4), arrow="last", arrowshape=(6, 8, 3),
                        tags=("if_arrow",))
                    self.canvas.create_text(
                        lane_x - 4, (y + ty) / 2, anchor="e", text="TRUE: skip",
                        fill=color, font=("Segoe UI", 7), tags=("if_arrow",))

        for n, link in enumerate(links):
            sy = self._canvas_row_y(link.source_index)
            ty = self._canvas_row_y(link.target_index)
            if min(sy, ty) > view_bottom + self.ROW_HEIGHT or max(sy, ty) < view_top - self.ROW_HEIGHT:
                continue
            lane_x = width - 22 - (n % 8) * 13
            color = self.arrow_colors.color_for((
                "shfl", link.shfl_index, link.source_index, link.target_index,
                link.input_name, link.history_index))
            self.canvas.create_line(
                right_x, sy, lane_x, sy, lane_x, ty, right_x, ty,
                fill=color, width=2 if not link.uncertain else 1.5,
                dash=(5, 3) if link.uncertain else None,
                arrow="last", arrowshape=(8, 10, 4), tags=("shfl_arrow",))
            label = f"SHFL {link.input_name}←#{link.history_index}"
            if link.uncertain:
                label += " ?"
            self.canvas.create_text(
                lane_x - 3, (sy + ty) / 2, anchor="e", text=label,
                fill=color, font=("Segoe UI", 7, "bold"), tags=("shfl_arrow",))

        for target_idx, message in unresolved:
            if first <= target_idx <= last:
                y = self._canvas_row_y(target_idx)
                self.canvas.create_text(
                    right_x + 4, y + 18, anchor="w", text="⚠ " + message,
                    fill=self.RED, font=("Segoe UI", 7), tags=("warning",))

        for i in visible_indices:
            ins = instructions[i]
            cy = self._canvas_row_y(i)
            y0, y1 = cy - self.BLOCK_HALF_HEIGHT, cy + self.BLOCK_HALF_HEIGHT
            selected = (i == self.selected_index)
            is_gpu = ins.opcode < 0x10
            fill = "#183c5c" if is_gpu else "#244c3e"
            if stale:
                fill = "#303947"
            edge = self.BLUE if selected else (
                "#596575" if stale else ("#347db3" if is_gpu else "#3a9a71"))
            block_tag = f"block_{i}"
            self.canvas.create_text(
                20, cy, anchor="w", text=f"{i:03d}", fill="#95a9bf",
                font=("Consolas", 9, "bold"), tags=(block_tag, "block"))
            self.canvas.create_rectangle(
                left_x, y0, right_x, y1, fill=fill, outline=edge,
                width=2 if selected else 1,
                tags=(block_tag, f"block_rect_{i}", "block"))
            # A slim category stripe makes CPU/GPU blocks distinguishable at a glance.
            self.canvas.create_rectangle(
                left_x + 1, y0 + 1, left_x + 4, y1 - 1,
                fill=("#4ca6ff" if is_gpu else "#47c78a") if not stale else "#778394",
                outline="", tags=(block_tag, "block"))
            self.canvas.create_text(
                left_x + 14, cy, anchor="w", text=ins.mnemonic,
                fill="#ffffff", font=("Segoe UI Semibold", 10), tags=(block_tag, "block"))
            self.canvas.create_text(
                left_x + 130, cy, anchor="w",
                text=f"A 0x{ins.a:04X}    B 0x{ins.b:04X}",
                fill="#d4e1ef", font=("Consolas", 9), tags=(block_tag, "block"))
            summary = instruction_summary(ins, i, len(instructions))
            max_chars = max(0, int((right_x - left_x - 330) / 6.1))
            if max_chars >= 14 and len(summary) > max_chars:
                summary = summary[:max_chars - 1] + "…"
            elif max_chars < 14:
                summary = ""
            if summary:
                self.canvas.create_text(
                    right_x - 10, cy, anchor="e", text=summary,
                    fill="#a9bdcf", font=("Segoe UI", 8), tags=(block_tag, "block"))
            if ins.mnemonic in {"JUMP", "CALL"}:
                self.canvas.create_oval(
                    left_x - 6, cy - 6, left_x + 6, cy + 6,
                    fill=self.ORANGE if ins.mnemonic == "CALL" else self.BLUE,
                    outline="#eaf3ff", width=1, tags=(block_tag, "block"))
            if ins.mnemonic == "SHFL":
                self.canvas.create_oval(
                    right_x - 5, cy - 5, right_x + 5, cy + 5,
                    fill=self.PURPLE, outline="#f2eaff", tags=(block_tag, "block"))

        self.canvas.configure(scrollregion=(0, 0, width, height))
        self.canvas.xview_moveto(x_fraction)
        self.canvas.yview_moveto(y_fraction)
        self._drawn_selected_index = self.selected_index
        self._drawn_stale = stale

    def on_canvas_press(self, event):
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        row = self._canvas_row_at_y(y)
        if 0 <= row < len(self.instructions):
            self.select_block(row)
            ins = self.instructions[row]
            # Drag starts only from the dedicated left gutter on a JUMP/CALL block.
            if x < 174 and ins.mnemonic in {"JUMP", "CALL"}:
                self._drag_source = row
                self.canvas.configure(cursor="crosshair")
                self._drag_line_id = self.canvas.create_line(150, self._canvas_row_y(row), x, y,
                    fill=self.ORANGE, width=2, dash=(4, 2), tags=("drag_line",))

    def on_canvas_motion(self, event):
        if self._drag_source is None or self._drag_line_id is None:
            return
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        sy = self._canvas_row_y(self._drag_source)
        self.canvas.coords(self._drag_line_id, 150, sy, x, y)

    def on_canvas_release(self, event):
        if self._drag_source is None:
            return
        source = self._drag_source
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        target = self._canvas_row_at_y(y)
        self.canvas.delete("drag_line")
        self._drag_source = None
        self._drag_line_id = None
        self.canvas.configure(cursor="")
        if 0 <= target < len(self.instructions) or (target == len(self.instructions) and self.instructions[source].mnemonic == "JUMP"):
            self._set_control_target(source, target)
        else:
            self.status_label.configure(text="목표 블럭 위에 놓아 주세요.", fg=self.ORANGE)

    def _set_control_target(self, source_index: int, target_index: int):
        ins = self.instructions[source_index]
        delta = target_index - (source_index + 1)
        a, b = max(delta, 0), max(-delta, 0)
        if a > MAX_U16 or b > MAX_U16:
            messagebox.showerror("대상 오류", "상대 이동 거리가 16비트 범위를 벗어났습니다.")
            return
        self._replace_instruction_line(ins, a, b)
        self.selected_index = source_index
        self._parse_and_refresh()
        self.status_label.configure(text=f"{ins.mnemonic} → 블럭 {target_index:03d} 연결", fg=self.GREEN)

    def _replace_instruction_line(self, ins: Instruction, a: int, b: int):
        line_index = ins.line_number
        raw = self.editor.get(f"{line_index}.0", f"{line_index}.end")
        code, comment = split_comment(raw)
        m = re.match(r"^(\s*)(?:([A-Za-z_][A-Za-z0-9_]*:\s*)?)(JUMP|CALL)\b", code, re.IGNORECASE)
        if not m:
            return
        prefix = m.group(1) + (m.group(2) or "")
        new_line = f"{prefix}{ins.mnemonic} {a}, {b}"
        if comment:
            new_line += "  " + comment
        self._syncing_editor = True
        self.editor.delete(f"{line_index}.0", f"{line_index}.end")
        self.editor.insert(f"{line_index}.0", new_line)
        self.editor.edit_modified(False)
        self._syncing_editor = False
        self.update_line_numbers()

    def select_block(self, index: int):
        self.selected_index = index
        parsed = self._last_valid_instructions if self.parse_error else self.instructions
        self._update_details(parsed, str(self.parse_error) if self.parse_error else None)
        self._refresh_block_selection()
        if 0 <= index < len(parsed):
            line = parsed[index].line_number
            # Programmatic selection (for example, adding a block at EOF) should
            # bring the corresponding canvas row into view automatically.
            canvas_height = max(1, self.canvas.winfo_height())
            total_height = max(150, 96 + max(1, len(parsed)) * self.ROW_HEIGHT)
            self.canvas.configure(scrollregion=(
                0, 0, max(self.canvas.winfo_width(), self.CANVAS_MIN_WIDTH), total_height))
            row_y = self._canvas_row_y(index)
            view_top = self.canvas.canvasy(0)
            view_bottom = view_top + canvas_height
            if row_y < view_top + 24 or row_y > view_bottom - 24:
                desired_top = max(0, row_y - canvas_height * 0.45)
                self.canvas.yview_moveto(min(1.0, desired_top / total_height))
                self.request_draw(stale=self.parse_error is not None, delay=25)
            self.editor.tag_remove("selected_line", "1.0", "end")
            self.editor.tag_configure("selected_line", background="#1e3044")
            self.editor.tag_add("selected_line", f"{line}.0", f"{line}.end")
            self.editor.see(f"{line}.0")
        else:
            self.editor.tag_remove("selected_line", "1.0", "end")

    def add_block(self):
        mnemonic = self.op_var.get()
        if mnemonic not in OPCODES:
            return
        lines = self.editor.get("1.0", "end-1c").splitlines()
        new_line = f"{mnemonic} 0, 0"
        if self.selected_index is not None and self.selected_index < len(self.instructions):
            insert_at = self.instructions[self.selected_index].line_number
            lines.insert(insert_at, new_line)  # insert immediately after selected source line
        else:
            lines.append(new_line)
        self._set_editor_text("\n".join(lines) + "\n")
        self._parse_and_refresh()
        if self.selected_index is None:
            self.selected_index = max(0, len(self.instructions) - 1)
        else:
            self.selected_index = min(self.selected_index + 1, len(self.instructions) - 1)
        self.select_block(self.selected_index)
        self.status_label.configure(text=f"{mnemonic} 블럭 추가", fg=self.GREEN)

    def delete_block(self):
        if self.selected_index is None or not (0 <= self.selected_index < len(self.instructions)):
            return
        ins = self.instructions[self.selected_index]
        lines = self.editor.get("1.0", "end-1c").splitlines()
        if 1 <= ins.line_number <= len(lines):
            del lines[ins.line_number - 1]
        self._set_editor_text("\n".join(lines) + ("\n" if lines else ""))
        remaining = len(self.instructions) - 1
        self.selected_index = min(self.selected_index, remaining - 1) if remaining > 0 else None
        self._parse_and_refresh()
        self.select_block(self.selected_index) if self.selected_index is not None else None

    def move_block(self, direction: int):
        if self.selected_index is None or not (0 <= self.selected_index < len(self.instructions)):
            return
        other = self.selected_index + direction
        if not (0 <= other < len(self.instructions)):
            return
        lines = self.editor.get("1.0", "end-1c").splitlines()
        first = self.instructions[self.selected_index].line_number - 1
        second = self.instructions[other].line_number - 1
        # Preserve comments and labels on the instruction line by swapping full source lines.
        lines[first], lines[second] = lines[second], lines[first]
        self._set_editor_text("\n".join(lines) + "\n")
        self.selected_index = other
        self._parse_and_refresh()
        self.select_block(other)
        self.status_label.configure(text="블럭 순서 변경 · 점프/라벨 목적지를 확인하세요", fg=self.ORANGE)

    def new_source(self):
        if not messagebox.askyesno("새 코드", "현재 편집 내용을 지우고 새 코드를 시작할까요?"):
            return
        self.current_source_path = None
        self.current_output_path = None
        self.selected_index = None
        self._set_editor_text("# New 32a program\n")
        self._parse_and_refresh()

    def open_source(self):
        path = filedialog.askopenfilename(title="어셈블리 소스 열기",
            filetypes=[("Assembly source", "*.asm *.s *.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            messagebox.showerror("열기 실패", str(exc))
            return
        self.current_source_path = Path(path)
        self.selected_index = None
        self._set_editor_text(text)
        self._parse_and_refresh()
        self.status_label.configure(text=f"소스 열기: {Path(path).name}", fg=self.GREEN)

    def export_source(self):
        path = filedialog.asksaveasfilename(title="어셈블리 코드 내보내기",
            defaultextension=".asm", initialfile=(self.current_source_path.stem + ".asm" if self.current_source_path else "program.asm"),
            filetypes=[("Assembly source", "*.asm"), ("Text file", "*.txt")])
        if not path:
            return
        try:
            Path(path).write_text(self.editor.get("1.0", "end-1c") + "\n", encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("내보내기 실패", str(exc))
            return
        self.status_label.configure(text=f"코드 내보내기 완료: {Path(path).name}", fg=self.GREEN)

    def compile_file(self):
        if self._parse_after_id:
            self.after_cancel(self._parse_after_id)
            self._parse_after_id = None
        source = self.editor.get("1.0", "end-1c")
        try:
            instructions = parse_source(source)
            self.instructions = instructions
            self._last_valid_instructions = instructions
            if self.selected_index is not None and self.selected_index >= len(instructions):
                self.selected_index = len(instructions) - 1 if instructions else None
            self.parse_error = None
            self._cache_graph_data(instructions)
            self._update_details(instructions)
            self.request_draw(stale=False)
            fmt = self.format_var.get().lower()
            data = encode_program(instructions, fmt)
            # Verify the exact output with the same fixed-record parsing scheme as 16g/32a runtime.
            decoded = decode_program(data, fmt)
            if len(decoded) != len(instructions):
                raise AssemblyError("내부 검증에서 명령어 개수가 일치하지 않습니다.")
        except AssemblyError as exc:
            messagebox.showerror("컴파일 오류", str(exc))
            self.status_label.configure(text=f"컴파일 실패 · {exc}", fg=self.RED)
            self._parse_and_refresh()
            return
        ext = ".16g" if fmt == "16g" else ".32a"
        default_name = (self.current_source_path.stem if self.current_source_path else "program") + ext
        path = filedialog.asksaveasfilename(title=f"{fmt} 바이너리 컴파일",
            defaultextension=ext, initialfile=default_name,
            filetypes=[(f"{fmt} binary", f"*{ext}"), ("All files", "*.*")])
        if not path:
            return
        output_path = Path(path)
        if output_path.suffix.lower() != ext:
            output_path = output_path.with_suffix(ext)
            path = str(output_path)
        try:
            output_path.write_bytes(data)
        except OSError as exc:
            messagebox.showerror("저장 실패", str(exc))
            return
        self.current_output_path = Path(path)
        self.status_label.configure(text=f"컴파일 완료 · {len(instructions)} blocks · {len(data)} bytes", fg=self.GREEN)
        messagebox.showinfo("컴파일 완료",
            f"파일이 생성되었습니다.\n\n출력: {path}\n형식: .{fmt}\n명령어: {len(instructions)}개\n크기: {len(data):,} bytes")

    def on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")
        self.request_draw(stale=self.parse_error is not None, delay=25)
        return "break"


def run_self_tests():
    # Each logical arrow gets a random high-contrast color, stable across redraws.
    palette = ArrowColorRegistry()
    first_color = palette.color_for(("shfl", 1, 0, 2, "A", 1))
    assert first_color.startswith("#") and len(first_color) == 7
    assert palette.color_for(("shfl", 1, 0, 2, "A", 1)) == first_color
    second_color = palette.color_for(("flow", 3, 0, "JUMP"))
    assert second_color != first_color

    sample = """\nstart:\nADD 10, 20\nSHFL 1, 0\nADD 5, 0\nJUMP ->start\n"""
    ins = parse_source(sample)
    assert len(ins) == 4
    assert (ins[3].a, ins[3].b, ins[3].target_index) == (0, 4, 0)
    binary = encode_program(ins, "32a")
    assert len(binary) == 6 * len(ins) - 1
    assert decode_program(binary, "32a") == [(x.opcode, x.a, x.b) for x in ins]
    links, unresolved = find_shfl_links(ins)
    assert len(links) == 1 and links[0].source_index == 0 and links[0].target_index == 2
    assert not unresolved
    try:
        encode_program(ins, "16g")
    except AssemblyError:
        pass
    else:
        raise AssertionError(".16g must reject CPU OP")
    # Separator byte inside operands is valid because the decoder reads fixed 5-byte records.
    edge = parse_source("PASS 0x2600, 0x0026\n")
    edge_bin = encode_program(edge, "16g")
    assert decode_program(edge_bin, "16g") == [(0, 0x2600, 0x26)]


if __name__ == "__main__":
    run_self_tests()
    app = VisualCompilerApp()
    app.mainloop()
