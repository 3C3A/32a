// 32a - 32-operation APU for Windows
// MinGW-w64 build (GUI subsystem; WinMain is intentionally used, not wmain):
//   g++ 32a.cpp -o 32a.exe -std=c++17 -O2 -Wall -Wextra -Wpedantic -static -static-libgcc -static-libstdc++ -mwindows -lshell32 -ladvapi32 -luser32 -lgdi32
// .32a record: [OP][A high][A low][B high][B low], with 0x26 between records.
// Named pipe: \\.\pipe\32a, request 5 bytes, response 4 bytes.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0601
#endif
#ifndef WINVER
#define WINVER 0x0601
#endif
#include <windows.h>
#include <shellapi.h>
#include <sddl.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <bitset>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cwchar>
#include <cwctype>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <limits>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {

constexpr int kLogicalWidth = 256;
constexpr int kLogicalHeight = 256;
constexpr int kScale = 2;
constexpr int kWindowSize = kLogicalWidth * kScale;
constexpr size_t kPixelCount = static_cast<size_t>(kLogicalWidth) * kLogicalHeight;
constexpr size_t kHistoryCapacity = 65536;
constexpr size_t kRamWords = 65536;
constexpr size_t kMaxProgramBytes = 128u * 1024u * 1024u;
constexpr size_t kInstructionWindowSize = 256;
constexpr size_t kAutoRegisterCount = 256;
constexpr size_t kCallStackCapacity = 256;
constexpr wchar_t kWindowClass[] = L"32a_APU_Window_Class";
constexpr wchar_t kWindowTitle[] = L"32a APU";
constexpr wchar_t kPipePath[] = L"\\\\.\\pipe\\32a";
constexpr wchar_t kPipeMutexName[] = L"Local\\32a_PipeServer_Mutex";

// Result kinds used by the named-pipe protocol.
constexpr uint8_t kKindNumeric = 0x00;
constexpr uint8_t kKindColor = 0x01;
constexpr uint8_t kKindScreen = 0x02;
constexpr uint8_t kKindControl = 0x03;

// Internal status bits.
constexpr uint8_t kStatusDivZero = 0x01;
constexpr uint8_t kStatusBadHistory = 0x02;
constexpr uint8_t kStatusInvalid = 0x04;
constexpr uint8_t kStatusInternal = 0x08;
constexpr uint8_t kStatusDiskIo = 0x10;
constexpr uint8_t kStatusPc = 0x20;
constexpr uint8_t kStatusStack = 0x40;

struct Instruction {
    uint8_t op = 0;
    uint16_t a = 0;
    uint16_t b = 0;
};

struct ProgramState {
    std::vector<uint32_t> returnStack;
    bool halted = false;
};

struct ExecutionResult {
    uint8_t status = 0;
    uint8_t kind = kKindControl;
    uint16_t value = 0;
    bool printNumeric = false;
    bool fatal = false;
    bool hasNextPc = false;
    uint32_t nextPc = 0;
    DWORD waitMilliseconds = 0;
};

std::atomic<bool> g_running{true};
std::atomic<bool> g_hexOutput{false};
std::atomic<HWND> g_window{nullptr};
std::mutex g_outputMutex;

bool IsValidHandle(HANDLE h) {
    return h != nullptr && h != INVALID_HANDLE_VALUE;
}

bool WriteStandard(bool toStderr, const std::string& text) {
    std::lock_guard<std::mutex> lock(g_outputMutex);
    HANDLE h = GetStdHandle(toStderr ? STD_ERROR_HANDLE : STD_OUTPUT_HANDLE);
    if (!IsValidHandle(h)) return false;
    size_t done = 0;
    while (done < text.size()) {
        const DWORD amount = static_cast<DWORD>(std::min<size_t>(text.size() - done, 1u << 20));
        DWORD written = 0;
        if (!WriteFile(h, text.data() + done, amount, &written, nullptr) || written == 0) return false;
        done += written;
    }
    return true;
}

void PrintError(const std::string& message, bool popupIfNoStderr = false) {
    const std::string line = message + "\r\n";
    if (!WriteStandard(true, line) && popupIfNoStderr) {
        const int needed = MultiByteToWideChar(CP_UTF8, 0, message.c_str(), -1, nullptr, 0);
        if (needed > 0) {
            std::vector<wchar_t> wide(static_cast<size_t>(needed));
            MultiByteToWideChar(CP_UTF8, 0, message.c_str(), -1, wide.data(), needed);
            MessageBoxW(nullptr, wide.data(), L"32a", MB_OK | MB_ICONERROR);
        } else {
            MessageBoxA(nullptr, message.c_str(), "32a", MB_OK | MB_ICONERROR);
        }
    }
}

std::string FormatValue(uint16_t value) {
    std::ostringstream out;
    if (g_hexOutput.load()) {
        out << std::uppercase << std::hex << std::setfill('0') << std::setw(4)
            << static_cast<unsigned int>(value);
    } else {
        for (int bit = 15; bit >= 0; --bit) out << ((value >> bit) & 1u);
    }
    out << "\r\n";
    return out.str();
}

void PrintDiagnostics(const ExecutionResult& result) {
    if (result.status & kStatusDivZero) PrintError("32a: DIV/MOD by zero; result forced to zero.");
    if (result.status & kStatusBadHistory) PrintError("32a: SHFL history index unavailable; affected input replaced with zero.");
    if (result.status & kStatusInvalid) PrintError("32a: invalid opcode or command context.");
    if (result.status & kStatusInternal) PrintError("32a: internal processing error.");
    if (result.status & kStatusDiskIo) PrintError("32a: disk data file I/O error.");
    if (result.status & kStatusPc) PrintError("32a: program counter target is outside the program.");
    if (result.status & kStatusStack) PrintError("32a: CALL/RET return-stack overflow or underflow.");
}

std::string TrimAscii(std::string s) {
    auto isSpace = [](unsigned char c) { return std::isspace(c) != 0; };
    while (!s.empty() && isSpace(static_cast<unsigned char>(s.front()))) s.erase(s.begin());
    while (!s.empty() && isSpace(static_cast<unsigned char>(s.back()))) s.pop_back();
    return s;
}

std::string UpperAscii(std::string s) {
    for (char& c : s) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    return s;
}

std::wstring Utf8ToWide(const std::string& text) {
    if (text.empty()) return {};
    int needed = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, text.data(),
                                     static_cast<int>(text.size()), nullptr, 0);
    UINT codePage = CP_UTF8;
    DWORD flags = MB_ERR_INVALID_CHARS;
    if (needed <= 0) {
        codePage = CP_ACP;
        flags = 0;
        needed = MultiByteToWideChar(codePage, flags, text.data(), static_cast<int>(text.size()), nullptr, 0);
        if (needed <= 0) throw std::runtime_error("cannot convert text to a Windows path");
    }
    std::wstring result(static_cast<size_t>(needed), L'\0');
    if (!MultiByteToWideChar(codePage, flags, text.data(), static_cast<int>(text.size()), result.data(), needed))
        throw std::runtime_error("cannot convert text to a Windows path");
    return result;
}

bool HasExtension(const std::wstring& path, const wchar_t* extension) {
    const size_t n = std::wcslen(extension);
    if (path.size() < n) return false;
    std::wstring tail = path.substr(path.size() - n);
    for (wchar_t& c : tail) c = static_cast<wchar_t>(towlower(c));
    std::wstring expected(extension);
    for (wchar_t& c : expected) c = static_cast<wchar_t>(towlower(c));
    return tail == expected;
}

std::wstring DataPathForProgram(const std::wstring& programPath) {
    std::filesystem::path path(programPath);
    path.replace_extension(L".32d");
    return path.wstring();
}

uint32_t Color565ToRgb(uint16_t color) {
    const uint32_t r5 = (color >> 11) & 0x1Fu;
    const uint32_t g6 = (color >> 5) & 0x3Fu;
    const uint32_t b5 = color & 0x1Fu;
    const uint32_t r = (r5 * 255u) / 31u;
    const uint32_t g = (g6 * 255u) / 63u;
    const uint32_t b = (b5 * 255u) / 31u;
    return (r << 16) | (g << 8) | b;
}

uint16_t PaletteRgb332To565(uint8_t index) {
    const uint32_t r3 = (index >> 5) & 7u;
    const uint32_t g3 = (index >> 2) & 7u;
    const uint32_t b2 = index & 3u;
    const uint32_t r5 = (r3 * 31u + 3u) / 7u;
    const uint32_t g6 = (g3 * 63u + 3u) / 7u;
    const uint32_t b5 = (b2 * 31u + 1u) / 3u;
    return static_cast<uint16_t>((r5 << 11) | (g6 << 5) | b5);
}

using Glyph = std::array<uint8_t, 5>; // each row uses the low 3 bits

Glyph GetGlyph(uint8_t code) {
    const uint8_t c = (code >= 'a' && code <= 'z') ? static_cast<uint8_t>(code - 'a' + 'A') : code;
    switch (c) {
    case ' ': return {0,0,0,0,0};
    case '0': return {7,5,5,5,7};
    case '1': return {2,6,2,2,7};
    case '2': return {7,1,7,4,7};
    case '3': return {7,1,7,1,7};
    case '4': return {5,5,7,1,1};
    case '5': return {7,4,7,1,7};
    case '6': return {7,4,7,5,7};
    case '7': return {7,1,2,2,2};
    case '8': return {7,5,7,5,7};
    case '9': return {7,5,7,1,7};
    case 'A': return {2,5,7,5,5};
    case 'B': return {6,5,6,5,6};
    case 'C': return {3,4,4,4,3};
    case 'D': return {6,5,5,5,6};
    case 'E': return {7,4,6,4,7};
    case 'F': return {7,4,6,4,4};
    case 'G': return {3,4,5,5,3};
    case 'H': return {5,5,7,5,5};
    case 'I': return {7,2,2,2,7};
    case 'J': return {1,1,1,5,2};
    case 'K': return {5,5,6,5,5};
    case 'L': return {4,4,4,4,7};
    case 'M': return {5,7,7,5,5};
    case 'N': return {5,7,7,7,5};
    case 'O': return {7,5,5,5,7};
    case 'P': return {6,5,6,4,4};
    case 'Q': return {7,5,5,7,1};
    case 'R': return {6,5,6,5,5};
    case 'S': return {3,4,2,1,6};
    case 'T': return {7,2,2,2,2};
    case 'U': return {5,5,5,5,7};
    case 'V': return {5,5,5,5,2};
    case 'W': return {5,5,7,7,5};
    case 'X': return {5,5,2,5,5};
    case 'Y': return {5,5,2,2,2};
    case 'Z': return {7,1,2,4,7};
    case '.': return {0,0,0,0,2};
    case ',': return {0,0,0,2,4};
    case ':': return {0,2,0,2,0};
    case ';': return {0,2,0,2,4};
    case '!': return {2,2,2,0,2};
    case '?': return {6,1,2,0,2};
    case '-': return {0,0,7,0,0};
    case '_': return {0,0,0,0,7};
    case '+': return {0,2,7,2,0};
    case '=': return {0,7,0,7,0};
    case '/': return {1,1,2,4,4};
    case '\\': return {4,4,2,1,1};
    case '(': return {1,2,2,2,1};
    case ')': return {4,2,2,2,4};
    case '[': return {3,2,2,2,3};
    case ']': return {6,2,2,2,6};
    case '<': return {1,2,4,2,1};
    case '>': return {4,2,1,2,4};
    case '*': return {0,5,2,5,0};
    case '#': return {5,7,5,7,5};
    case '%': return {5,1,2,4,5};
    case '\'': return {2,2,0,0,0};
    case '"': return {5,5,0,0,0};
    case '|': return {2,2,2,2,2};
    default: return {7,5,2,0,2}; // unknown glyph: compact question mark
    }
}

struct AutoRegisterFile {
    std::array<uint16_t, kAutoRegisterCount> values{};
    std::bitset<kAutoRegisterCount> inUse{};
    size_t hint = 0;

    int Acquire(uint16_t value) {
        for (size_t n = 0; n < kAutoRegisterCount; ++n) {
            const size_t slot = (hint + n) % kAutoRegisterCount;
            if (!inUse.test(slot)) {
                inUse.set(slot);
                values[slot] = value;
                hint = (slot + 1) % kAutoRegisterCount;
                return static_cast<int>(slot);
            }
        }
        return -1;
    }
    uint16_t Read(int slot) const { return values[static_cast<size_t>(slot)]; }
    void Release(int slot) {
        if (slot >= 0) inUse.reset(static_cast<size_t>(slot));
    }
    void Reset() { values.fill(0); inUse.reset(); hint = 0; }
};

struct WindowEntry {
    uint32_t tag = std::numeric_limits<uint32_t>::max();
    Instruction instruction{};
    bool hasSideEffects = false;
    bool isBranch = false;
};

class InstructionWindow {
public:
    const WindowEntry& Fetch(const std::vector<Instruction>& program, uint32_t pc) {
        if (pc >= base_ && static_cast<uint64_t>(pc - base_) < count_) {
            const WindowEntry& entry = entries_[static_cast<size_t>(pc - base_)];
            if (entry.tag == pc) return entry;
        }
        Fill(program, pc);
        if (count_ == 0) throw std::runtime_error("instruction window fetch beyond program");
        return entries_[0];
    }

    void Prefetch(const std::vector<Instruction>& program, uint32_t pc) {
        if (pc >= program.size()) return;
        if (pc >= base_ && static_cast<uint64_t>(pc - base_) < count_) return;
        Fill(program, pc);
    }

private:
    void Fill(const std::vector<Instruction>& program, uint32_t base) {
        base_ = base;
        count_ = std::min<size_t>(kInstructionWindowSize, program.size() - base);
        for (size_t i = 0; i < kInstructionWindowSize; ++i) {
            entries_[i] = WindowEntry{};
            if (i >= count_) continue;
            const Instruction ins = program[static_cast<size_t>(base) + i];
            WindowEntry entry{};
            entry.tag = base + static_cast<uint32_t>(i);
            entry.instruction = ins;
            entry.hasSideEffects = (ins.op == 0x06 || ins.op == 0x10 || ins.op == 0x19 ||
                                    ins.op == 0x1B || ins.op == 0x18 || ins.op == 0x1C);
            entry.isBranch = (ins.op >= 0x11 && ins.op <= 0x17) || ins.op == 0x1E || ins.op == 0x1F;
            entries_[i] = entry;
        }
    }

    std::array<WindowEntry, kInstructionWindowSize> entries_{};
    uint32_t base_ = std::numeric_limits<uint32_t>::max();
    size_t count_ = 0;
};

class BranchPredictor {
public:
    bool PredictIf(uint32_t pc) const {
        const Entry& e = table_[pc % table_.size()];
        return e.tag == pc && e.counter >= 2;
    }
    void UpdateIf(uint32_t pc, bool taken) {
        Entry& e = table_[pc % table_.size()];
        if (e.tag != pc) { e.tag = pc; e.counter = 1; }
        if (taken) { if (e.counter < 3) ++e.counter; }
        else { if (e.counter > 0) --e.counter; }
    }
    void Reset() { table_.fill(Entry{}); }
private:
    struct Entry { uint32_t tag = std::numeric_limits<uint32_t>::max(); uint8_t counter = 1; };
    std::array<Entry, 256> table_{};
};

class Machine {
public:
    Machine() { ResetAll(); }

    void SetDiskPath(const std::wstring& path) {
        std::lock_guard<std::mutex> lock(mutex_);
        diskPath_ = path;
    }

    ExecutionResult Execute(const Instruction& ins, uint32_t pc, uint32_t programSize,
                            ProgramState* state, bool allowControlFlow) {
        ExecutionResult result{};
        if (ins.op > 0x1F) {
            result.status = kStatusInvalid;
            result.fatal = true;
            return result;
        }

        bool screenChanged = false;
        RECT dirty{0, 0, 0, 0};
        {
            std::lock_guard<std::mutex> lock(mutex_);

            if (ins.op == 0x07) {
                pendingA_ = ins.a;
                pendingB_ = ins.b;
                result.kind = kKindControl;
                return result;
            }

            uint16_t a = ins.a;
            uint16_t b = ins.b;
            bool historyError = false;
            if (pendingA_ != 0 && !ReadHistoryNoLock(pendingA_, a)) {
                a = 0;
                historyError = true;
            }
            if (pendingB_ != 0 && !ReadHistoryNoLock(pendingB_, b)) {
                b = 0;
                historyError = true;
            }
            pendingA_ = pendingB_ = 0;

            // The auto-register bank is an internal temporary allocator. Its
            // slot numbers and values are never visible to 32a programs.
            const int regA = autoRegs_.Acquire(a);
            const int regB = autoRegs_.Acquire(b);
            if (regA < 0 || regB < 0) {
                autoRegs_.Reset();
                result.status |= kStatusInternal;
                result.fatal = true;
                return result;
            }
            a = autoRegs_.Read(regA);
            b = autoRegs_.Read(regB);

            uint16_t out = 0;
            bool saveHistory = false;
            bool numeric = false;
            bool resultReady = false;

            switch (ins.op) {
            // ---------------- GPU: original 16g OPs ----------------
            case 0x00: out = a; saveHistory = true; numeric = true; resultReady = true; break; // PASS
            case 0x01: out = static_cast<uint16_t>(static_cast<uint32_t>(a) + b); saveHistory = true; numeric = true; resultReady = true; break;
            case 0x02: out = static_cast<uint16_t>(static_cast<uint32_t>(a) - b); saveHistory = true; numeric = true; resultReady = true; break;
            case 0x03: out = static_cast<uint16_t>(static_cast<uint32_t>(a) * b); saveHistory = true; numeric = true; resultReady = true; break;
            case 0x04:
                if (b == 0) { out = 0; result.status |= kStatusDivZero; }
                else out = static_cast<uint16_t>(a / b);
                saveHistory = true; numeric = true; resultReady = true; break;
            case 0x05:
                if (b == 0) { out = 0; result.status |= kStatusDivZero; }
                else out = static_cast<uint16_t>(a % b);
                saveHistory = true; numeric = true; resultReady = true; break;
            case 0x06: { // VGA: A=(Y<<8)|X, B=RGB565
                const uint8_t y = static_cast<uint8_t>(a >> 8);
                const uint8_t x = static_cast<uint8_t>(a & 0xFFu);
                SetPixelNoLock(x, y, b);
                dirty = PixelRect(x, y);
                screenChanged = true;
                result.kind = kKindScreen;
                break;
            }
            case 0x08: out = static_cast<uint16_t>(a & b); saveHistory = true; numeric = true; resultReady = true; break;
            case 0x09: out = static_cast<uint16_t>(a | b); saveHistory = true; numeric = true; resultReady = true; break;
            case 0x0A: out = static_cast<uint16_t>(a ^ b); saveHistory = true; numeric = true; resultReady = true; break;
            case 0x0B: out = (b >= 16) ? 0 : static_cast<uint16_t>((static_cast<uint32_t>(a) << b) & 0xFFFFu); saveHistory = true; numeric = true; resultReady = true; break;
            case 0x0C: out = (b >= 16) ? 0 : static_cast<uint16_t>(a >> b); saveHistory = true; numeric = true; resultReady = true; break;
            case 0x0D: { // BLEND RGB565
                const unsigned r = ((((a >> 11) & 31u) + ((b >> 11) & 31u)) / 2u);
                const unsigned g = ((((a >> 5) & 63u) + ((b >> 5) & 63u)) / 2u);
                const unsigned blue = (((a & 31u) + (b & 31u)) / 2u);
                out = static_cast<uint16_t>((r << 11) | (g << 5) | blue);
                saveHistory = true;
                result.kind = kKindColor;
                resultReady = true;
                break;
            }
            case 0x0E: { // BRIGHT
                const uint32_t factor = std::min<uint32_t>(b, 255u);
                const uint32_t r = (((a >> 11) & 31u) * factor) / 255u;
                const uint32_t g = (((a >> 5) & 63u) * factor) / 255u;
                const uint32_t blue = ((a & 31u) * factor) / 255u;
                out = static_cast<uint16_t>((r << 11) | (g << 5) | blue);
                saveHistory = true;
                result.kind = kKindColor;
                resultReady = true;
                break;
            }
            case 0x0F: out = (a < b) ? 0 : ((a == b) ? 1 : 2); saveHistory = true; numeric = true; resultReady = true; break;

            // ---------------- CPU: new 16 OPs ----------------
            case 0x10: { // TEXT: A=(charY<<8)|charX, B=(palette<<8)|character
                const uint8_t charY = static_cast<uint8_t>(a >> 8);
                const uint8_t charX = static_cast<uint8_t>(a & 0xFFu);
                const uint8_t paletteIndex = static_cast<uint8_t>(b >> 8);
                const uint8_t character = static_cast<uint8_t>(b & 0xFFu);
                if (charX > 84 || charY > 50) {
                    result.status |= kStatusInvalid;
                    result.fatal = true;
                    break;
                }
                const Glyph glyph = GetGlyph(character);
                const uint16_t color = PaletteRgb332To565(paletteIndex);
                const int x0 = static_cast<int>(charX) * 3;
                const int y0 = static_cast<int>(charY) * 5;
                for (int gy = 0; gy < 5; ++gy) {
                    for (int gx = 0; gx < 3; ++gx) {
                        if ((glyph[static_cast<size_t>(gy)] & (1u << (2 - gx))) != 0) {
                            SetPixelNoLock(static_cast<uint8_t>(x0 + gx), static_cast<uint8_t>(y0 + gy), color);
                        }
                    }
                }
                dirty = RECT{x0 * kScale, y0 * kScale, (x0 + 3) * kScale, (y0 + 5) * kScale};
                screenChanged = true;
                result.kind = kKindScreen;
                break;
            }
            case 0x11: case 0x12: case 0x13: case 0x14: case 0x15: case 0x16: { // IF family
                if (!allowControlFlow || state == nullptr) {
                    result.status |= kStatusInvalid;
                    result.fatal = true;
                    break;
                }
                bool condition = false;
                switch (ins.op) {
                case 0x11: condition = (a == b); break;
                case 0x12: condition = (a != b); break;
                case 0x13: condition = (a > b); break;
                case 0x14: condition = (a < b); break;
                case 0x15: condition = (a >= b); break;
                case 0x16: condition = (a <= b); break;
                }
                uint64_t target = static_cast<uint64_t>(pc) + (condition ? 2u : 1u);
                // If the skipped slot is the last instruction, reaching EOF is normal.
                if (target > programSize) target = programSize;
                result.hasNextPc = true;
                result.nextPc = static_cast<uint32_t>(target);
                result.kind = kKindControl;
                break;
            }
            case 0x17: { // JUMP: base=pc+1, then +A-B
                if (!allowControlFlow || state == nullptr) {
                    result.status |= kStatusInvalid;
                    result.fatal = true;
                    break;
                }
                const int64_t target = static_cast<int64_t>(pc) + 1 + static_cast<int64_t>(a) - static_cast<int64_t>(b);
                if (target < 0 || static_cast<uint64_t>(target) > programSize) {
                    result.status |= kStatusPc;
                    result.fatal = true;
                } else {
                    result.hasNextPc = true;
                    result.nextPc = static_cast<uint32_t>(target);
                    result.kind = kKindControl;
                }
                break;
            }
            case 0x18: { // DLOAD: word address in A
                uint16_t value = 0;
                if (!ReadDiskWordNoLock(a, value)) {
                    result.status |= kStatusDiskIo;
                    result.fatal = true;
                } else {
                    out = value;
                    saveHistory = true;
                    resultReady = true;
                    result.kind = kKindNumeric;
                }
                break;
            }
            case 0x19: { // DSTORE: word address A, value B
                if (!WriteDiskWordNoLock(a, b)) {
                    result.status |= kStatusDiskIo;
                    result.fatal = true;
                }
                result.kind = kKindControl;
                break;
            }
            case 0x1A: // LOAD RAM[A]
                out = ram_[a];
                saveHistory = true;
                resultReady = true;
                result.kind = kKindNumeric;
                break;
            case 0x1B: // STORE RAM[A] = B
                ram_[a] = b;
                result.kind = kKindControl;
                break;
            case 0x1C: // WAIT milliseconds
                result.waitMilliseconds = a;
                result.kind = kKindControl;
                break;
            case 0x1D: // HALT: in standalone/pipe mode it acknowledges a no-op; in a program it stops that program.
                if (state != nullptr) state->halted = true;
                result.kind = kKindControl;
                break;
            case 0x1E: { // CALL
                if (!allowControlFlow || state == nullptr) {
                    result.status |= kStatusInvalid;
                    result.fatal = true;
                    break;
                }
                const int64_t target = static_cast<int64_t>(pc) + 1 + static_cast<int64_t>(a) - static_cast<int64_t>(b);
                if (target < 0 || static_cast<uint64_t>(target) >= programSize) {
                    result.status |= kStatusPc;
                    result.fatal = true;
                } else if (state->returnStack.size() >= kCallStackCapacity) {
                    result.status |= kStatusStack;
                    result.fatal = true;
                } else {
                    state->returnStack.push_back(pc + 1);
                    result.hasNextPc = true;
                    result.nextPc = static_cast<uint32_t>(target);
                    result.kind = kKindControl;
                }
                break;
            }
            case 0x1F: { // RET
                if (!allowControlFlow || state == nullptr) {
                    result.status |= kStatusInvalid;
                    result.fatal = true;
                } else if (state->returnStack.empty()) {
                    result.status |= kStatusStack;
                    result.fatal = true;
                } else {
                    result.hasNextPc = true;
                    result.nextPc = state->returnStack.back();
                    state->returnStack.pop_back();
                    result.kind = kKindControl;
                }
                break;
            }
            default:
                result.status |= kStatusInvalid;
                result.fatal = true;
                break;
            }

            if (resultReady) {
                result.value = out;
                if (saveHistory) PushHistoryNoLock(out);
                if (numeric) {
                    result.kind = kKindNumeric;
                    result.printNumeric = true;
                }
            }
            if (historyError) result.status |= kStatusBadHistory;

            const int resultReg = autoRegs_.Acquire(out);
            if (resultReg >= 0) autoRegs_.Release(resultReg);
            autoRegs_.Release(regB);
            autoRegs_.Release(regA);
        }

        if (screenChanged) {
            HWND hwnd = g_window.load();
            if (hwnd) InvalidateRect(hwnd, &dirty, FALSE);
        }
        if (result.waitMilliseconds != 0) Sleep(result.waitMilliseconds);
        return result;
    }

    void ClearScreen() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            framebuffer_.fill(0);
            image_.fill(0);
        }
        HWND hwnd = g_window.load();
        if (hwnd) InvalidateRect(hwnd, nullptr, FALSE);
    }

    void Paint(HDC dc, const BITMAPINFO* bmi) {
        std::lock_guard<std::mutex> lock(mutex_);
        StretchDIBits(dc, 0, 0, kWindowSize, kWindowSize,
                      0, 0, kWindowSize, kWindowSize,
                      image_.data(), bmi, DIB_RGB_COLORS, SRCCOPY);
    }

    void ResetAll() {
        std::lock_guard<std::mutex> lock(mutex_);
        framebuffer_.fill(0);
        image_.fill(0);
        history_.fill(0);
        ram_.fill(0);
        historyHead_ = kHistoryCapacity - 1;
        historyCount_ = 0;
        pendingA_ = pendingB_ = 0;
        autoRegs_.Reset();
    }

private:
    RECT PixelRect(uint8_t x, uint8_t y) const {
        const int px = static_cast<int>(x) * kScale;
        const int py = static_cast<int>(y) * kScale;
        return RECT{px, py, px + kScale, py + kScale};
    }

    void SetPixelNoLock(uint8_t x, uint8_t y, uint16_t color) {
        framebuffer_[static_cast<size_t>(y) * kLogicalWidth + x] = color;
        const uint32_t rgb = Color565ToRgb(color);
        const int px = static_cast<int>(x) * kScale;
        const int py = static_cast<int>(y) * kScale;
        for (int dy = 0; dy < kScale; ++dy) {
            for (int dx = 0; dx < kScale; ++dx) {
                image_[static_cast<size_t>(py + dy) * kWindowSize + static_cast<size_t>(px + dx)] = rgb;
            }
        }
    }

    void PushHistoryNoLock(uint16_t value) {
        historyHead_ = (historyHead_ + 1) % kHistoryCapacity;
        history_[historyHead_] = value;
        if (historyCount_ < kHistoryCapacity) ++historyCount_;
    }

    bool ReadHistoryNoLock(uint16_t index, uint16_t& value) const {
        if (index == 0 || index > historyCount_) return false;
        const size_t slot = (historyHead_ + kHistoryCapacity - (static_cast<size_t>(index) - 1)) % kHistoryCapacity;
        value = history_[slot];
        return true;
    }

    bool ReadDiskWordNoLock(uint16_t address, uint16_t& value) const {
        value = 0;
        if (diskPath_.empty()) return false;
        HANDLE file = CreateFileW(diskPath_.c_str(), GENERIC_READ,
                                  FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                                  FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file == INVALID_HANDLE_VALUE) {
            const DWORD error = GetLastError();
            return error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND;
        }
        LARGE_INTEGER size{};
        if (!GetFileSizeEx(file, &size) || size.QuadPart < 0 ||
            (size.QuadPart & 1) != 0 || static_cast<uint64_t>(size.QuadPart) > kRamWords * 2u) {
            CloseHandle(file);
            return false;
        }
        const uint64_t offset = static_cast<uint64_t>(address) * 2u;
        if (offset + 2u > static_cast<uint64_t>(size.QuadPart)) {
            CloseHandle(file);
            return true;
        }
        LARGE_INTEGER pos{};
        pos.QuadPart = static_cast<LONGLONG>(offset);
        if (!SetFilePointerEx(file, pos, nullptr, FILE_BEGIN)) {
            CloseHandle(file);
            return false;
        }
        uint8_t bytes[2]{};
        DWORD read = 0;
        const BOOL ok = ReadFile(file, bytes, 2, &read, nullptr);
        CloseHandle(file);
        if (!ok || read != 2) return false;
        value = static_cast<uint16_t>((static_cast<uint16_t>(bytes[0]) << 8) | bytes[1]);
        return true;
    }

    bool WriteDiskWordNoLock(uint16_t address, uint16_t value) const {
        if (diskPath_.empty()) return false;
        HANDLE file = CreateFileW(diskPath_.c_str(), GENERIC_READ | GENERIC_WRITE,
                                  FILE_SHARE_READ, nullptr, OPEN_ALWAYS,
                                  FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file == INVALID_HANDLE_VALUE) return false;
        LARGE_INTEGER size{};
        if (!GetFileSizeEx(file, &size) || size.QuadPart < 0 ||
            (size.QuadPart & 1) != 0 || static_cast<uint64_t>(size.QuadPart) > kRamWords * 2u) {
            CloseHandle(file);
            return false;
        }
        const uint64_t offset = static_cast<uint64_t>(address) * 2u;
        if (offset > static_cast<uint64_t>(std::numeric_limits<LONGLONG>::max() - 2)) {
            CloseHandle(file);
            return false;
        }
        if (static_cast<uint64_t>(size.QuadPart) < offset) {
            LARGE_INTEGER end{};
            end.QuadPart = static_cast<LONGLONG>(offset);
            if (!SetFilePointerEx(file, end, nullptr, FILE_BEGIN) || !SetEndOfFile(file)) {
                CloseHandle(file);
                return false;
            }
        }
        LARGE_INTEGER pos{};
        pos.QuadPart = static_cast<LONGLONG>(offset);
        if (!SetFilePointerEx(file, pos, nullptr, FILE_BEGIN)) {
            CloseHandle(file);
            return false;
        }
        const uint8_t bytes[2] = {
            static_cast<uint8_t>((value >> 8) & 0xFFu),
            static_cast<uint8_t>(value & 0xFFu)
        };
        DWORD written = 0;
        const BOOL ok = WriteFile(file, bytes, 2, &written, nullptr);
        const BOOL flushed = ok && written == 2 && FlushFileBuffers(file);
        CloseHandle(file);
        return flushed != FALSE;
    }

    mutable std::mutex mutex_;
    std::array<uint16_t, kPixelCount> framebuffer_{};
    std::array<uint32_t, static_cast<size_t>(kWindowSize) * kWindowSize> image_{};
    std::array<uint16_t, kHistoryCapacity> history_{};
    std::array<uint16_t, kRamWords> ram_{};
    size_t historyHead_ = kHistoryCapacity - 1;
    size_t historyCount_ = 0;
    uint16_t pendingA_ = 0;
    uint16_t pendingB_ = 0;
    AutoRegisterFile autoRegs_{};
    std::wstring diskPath_;
};

Machine g_machine;

BITMAPINFO MakeBitmapInfo() {
    BITMAPINFO bmi{};
    bmi.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    bmi.bmiHeader.biWidth = kWindowSize;
    bmi.bmiHeader.biHeight = -kWindowSize;
    bmi.bmiHeader.biPlanes = 1;
    bmi.bmiHeader.biBitCount = 32;
    bmi.bmiHeader.biCompression = BI_RGB;
    return bmi;
}

LRESULT CALLBACK WindowProc(HWND hwnd, UINT message, WPARAM wParam, LPARAM lParam) {
    switch (message) {
    case WM_ERASEBKGND:
        return 1;
    case WM_PAINT: {
        PAINTSTRUCT ps{};
        HDC dc = BeginPaint(hwnd, &ps);
        const BITMAPINFO bmi = MakeBitmapInfo();
        g_machine.Paint(dc, &bmi);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_CLOSE:
        g_running.store(false);
        DestroyWindow(hwnd);
        return 0;
    case WM_DESTROY:
        g_window.store(nullptr);
        g_running.store(false);
        PostQuitMessage(0);
        return 0;
    default:
        return DefWindowProcW(hwnd, message, wParam, lParam);
    }
}

bool CreateVgaWindow(HINSTANCE instance) {
    SetProcessDPIAware();
    WNDCLASSEXW wc{};
    wc.cbSize = sizeof(wc);
    wc.style = CS_HREDRAW | CS_VREDRAW;
    wc.lpfnWndProc = WindowProc;
    wc.hInstance = instance;
    wc.hCursor = LoadCursorW(nullptr, MAKEINTRESOURCEW(32512));
    wc.hbrBackground = (HBRUSH)GetStockObject(BLACK_BRUSH);
    wc.lpszClassName = kWindowClass;
    if (!RegisterClassExW(&wc) && GetLastError() != ERROR_CLASS_ALREADY_EXISTS) return false;

    RECT rect{0, 0, kWindowSize, kWindowSize};
    const DWORD style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX;
    if (!AdjustWindowRect(&rect, style, FALSE)) return false;
    HWND hwnd = CreateWindowExW(0, kWindowClass, kWindowTitle, style,
        CW_USEDEFAULT, CW_USEDEFAULT, rect.right - rect.left, rect.bottom - rect.top,
        nullptr, nullptr, instance, nullptr);
    if (!hwnd) return false;
    g_window.store(hwnd);
    ShowWindow(hwnd, SW_SHOW);
    UpdateWindow(hwnd);
    return true;
}

bool ReadWholeFile(const std::wstring& path, std::vector<uint8_t>& bytes) {
    HANDLE file = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                              FILE_ATTRIBUTE_NORMAL | FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (file == INVALID_HANDLE_VALUE) throw std::runtime_error("cannot open .32a file");
    LARGE_INTEGER size{};
    if (!GetFileSizeEx(file, &size) || size.QuadPart <= 0 ||
        static_cast<unsigned long long>(size.QuadPart) > kMaxProgramBytes) {
        CloseHandle(file);
        throw std::runtime_error(".32a file is empty, too large, or has an invalid size");
    }
    bytes.resize(static_cast<size_t>(size.QuadPart));
    size_t offset = 0;
    while (offset < bytes.size()) {
        const DWORD want = static_cast<DWORD>(std::min<size_t>(bytes.size() - offset, 1u << 20));
        DWORD got = 0;
        if (!ReadFile(file, bytes.data() + offset, want, &got, nullptr) || got == 0) {
            CloseHandle(file);
            throw std::runtime_error("failed while reading .32a file");
        }
        offset += got;
    }
    CloseHandle(file);
    return true;
}

std::vector<Instruction> Load32aFile(const std::wstring& path) {
    std::vector<uint8_t> data;
    ReadWholeFile(path, data);
    std::vector<Instruction> instructions;
    size_t offset = 0;
    while (offset < data.size()) {
        if (data.size() - offset < 5) throw std::runtime_error("truncated .32a instruction record");
        if (data[offset] > 0x1F) throw std::runtime_error(".32a contains an invalid opcode byte");
        Instruction ins{};
        ins.op = data[offset];
        ins.a = static_cast<uint16_t>((static_cast<uint16_t>(data[offset + 1]) << 8) | data[offset + 2]);
        ins.b = static_cast<uint16_t>((static_cast<uint16_t>(data[offset + 3]) << 8) | data[offset + 4]);
        instructions.push_back(ins);
        offset += 5;
        if (offset == data.size()) break;
        if (data[offset] != 0x26) throw std::runtime_error("missing 0x26 (&) command separator in .32a file");
        ++offset;
        if (offset == data.size()) throw std::runtime_error(".32a file ends with a command separator");
    }
    if (instructions.empty()) throw std::runtime_error(".32a file contains no instructions");
    return instructions;
}

unsigned int ParseUnsigned(const std::string& s, unsigned int base, size_t exactLength, unsigned int maxValue) {
    if (s.size() != exactLength) throw std::runtime_error("field has the wrong number of digits");
    unsigned int value = 0;
    for (char c : s) {
        unsigned int digit = 0;
        if (c >= '0' && c <= '9') digit = static_cast<unsigned int>(c - '0');
        else if (base == 16 && c >= 'a' && c <= 'f') digit = static_cast<unsigned int>(c - 'a' + 10);
        else if (base == 16 && c >= 'A' && c <= 'F') digit = static_cast<unsigned int>(c - 'A' + 10);
        else throw std::runtime_error("field contains an invalid digit");
        if (digit >= base) throw std::runtime_error("field contains an invalid digit");
        value = value * base + digit;
        if (value > maxValue) throw std::runtime_error("field is outside its allowed range");
    }
    return value;
}

Instruction ParseTextInstruction(const std::vector<std::string>& fields, bool hex) {
    if (fields.size() != 3) throw std::runtime_error("expected exactly three fields: OP INPUT1 INPUT2");
    Instruction ins{};
    if (hex) {
        ins.op = static_cast<uint8_t>(ParseUnsigned(fields[0], 16, 2, 0x1F));
        ins.a = static_cast<uint16_t>(ParseUnsigned(fields[1], 16, 4, 0xFFFF));
        ins.b = static_cast<uint16_t>(ParseUnsigned(fields[2], 16, 4, 0xFFFF));
    } else {
        ins.op = static_cast<uint8_t>(ParseUnsigned(fields[0], 2, 5, 0x1F));
        ins.a = static_cast<uint16_t>(ParseUnsigned(fields[1], 2, 16, 0xFFFF));
        ins.b = static_cast<uint16_t>(ParseUnsigned(fields[2], 2, 16, 0xFFFF));
    }
    return ins;
}

std::vector<std::string> SplitFields(const std::string& line) {
    std::istringstream in(line);
    std::vector<std::string> fields;
    std::string field;
    while (in >> field) fields.push_back(field);
    return fields;
}

bool IsIfOp(uint8_t op) { return op >= 0x11 && op <= 0x16; }
bool IsControlFlowOp(uint8_t op) { return IsIfOp(op) || op == 0x17 || op == 0x1E || op == 0x1F; }

void ExecuteAndReportStandalone(const Instruction& ins, bool printToStdout, bool printErrors) {
    if (IsControlFlowOp(ins.op)) {
        ExecutionResult invalid{};
        invalid.status = kStatusInvalid;
        invalid.fatal = true;
        if (printErrors) PrintDiagnostics(invalid);
        return;
    }
    ExecutionResult result = g_machine.Execute(ins, 0, 0, nullptr, false);
    if (printToStdout && result.printNumeric) WriteStandard(false, FormatValue(result.value));
    if (printErrors && result.status != 0) PrintDiagnostics(result);
}

class ExecutionCache {
public:
    const WindowEntry& Fetch(const std::vector<Instruction>& program, uint32_t pc) {
        return window_.Fetch(program, pc);
    }
    bool PredictIf(uint32_t pc) const { return predictor_.PredictIf(pc); }
    void UpdateIf(uint32_t pc, bool taken) { predictor_.UpdateIf(pc, taken); }
    void Prefetch(const std::vector<Instruction>& program, uint32_t pc) { window_.Prefetch(program, pc); }
    void Reset() { predictor_.Reset(); }
private:
    InstructionWindow window_;
    BranchPredictor predictor_;
};

void RunProgram(const std::vector<Instruction>& program, bool loop, bool printToStdout, bool printErrors) {
    if (program.empty()) return;
    ExecutionCache cache;
    ProgramState state{};
    const uint32_t programSize = static_cast<uint32_t>(program.size());
    bool finished = false;
    while (g_running.load() && !finished) {
        uint32_t pc = 0;
        while (g_running.load() && !state.halted && pc < programSize) {
            const WindowEntry entry = cache.Fetch(program, pc);
            const Instruction ins = entry.instruction;

            // Predict and prefetch only. No speculative instruction is executed,
            // so VGA/RAM/disk/history side effects can never be committed wrongly.
            if (IsIfOp(ins.op)) {
                const bool predictedTaken = cache.PredictIf(pc);
                const uint32_t predictedPc = pc + (predictedTaken ? 2u : 1u);
                cache.Prefetch(program, predictedPc);
            } else if (ins.op == 0x17 || ins.op == 0x1E) {
                const int64_t predicted = static_cast<int64_t>(pc) + 1 + ins.a - ins.b;
                if (predicted >= 0 && static_cast<uint64_t>(predicted) < program.size())
                    cache.Prefetch(program, static_cast<uint32_t>(predicted));
            } else if (ins.op == 0x1F && !state.returnStack.empty()) {
                cache.Prefetch(program, state.returnStack.back());
            }

            ExecutionResult result = g_machine.Execute(ins, pc, programSize, &state, true);
            if (printToStdout && result.printNumeric) WriteStandard(false, FormatValue(result.value));
            if (printErrors && result.status != 0) PrintDiagnostics(result);
            if (result.fatal) { finished = true; break; }
            if (state.halted) { finished = true; break; }

            if (IsIfOp(ins.op)) {
                const bool taken = result.hasNextPc && result.nextPc == pc + 2u;
                cache.UpdateIf(pc, taken);
            }
            pc = result.hasNextPc ? result.nextPc : pc + 1u;
        }
        if (!loop || !g_running.load() || state.halted || finished) break;
        // -loop preserves RAM, disk, framebuffer, result history and pending SHFL.
        Sleep(0);
    }
}

bool ReadExact(HANDLE pipe, uint8_t* buffer, size_t bytes) {
    size_t offset = 0;
    while (offset < bytes) {
        DWORD got = 0;
        const DWORD wanted = static_cast<DWORD>(std::min<size_t>(bytes - offset, 0x7FFFFFFFu));
        if (!ReadFile(pipe, buffer + offset, wanted, &got, nullptr) || got == 0) return false;
        offset += got;
    }
    return true;
}

bool WriteExact(HANDLE pipe, const uint8_t* buffer, size_t bytes) {
    size_t offset = 0;
    while (offset < bytes) {
        DWORD written = 0;
        const DWORD wanted = static_cast<DWORD>(std::min<size_t>(bytes - offset, 0x7FFFFFFFu));
        if (!WriteFile(pipe, buffer + offset, wanted, &written, nullptr) || written == 0) return false;
        offset += written;
    }
    return true;
}

void PipeClientWorker(HANDLE pipe) {
    for (;;) {
        uint8_t request[5]{};
        if (!ReadExact(pipe, request, sizeof(request))) break;
        Instruction ins{};
        ins.op = request[0];
        ins.a = static_cast<uint16_t>((static_cast<uint16_t>(request[1]) << 8) | request[2]);
        ins.b = static_cast<uint16_t>((static_cast<uint16_t>(request[3]) << 8) | request[4]);
        ExecutionResult result{};
        if (ins.op > 0x1F || IsControlFlowOp(ins.op)) {
            result.status = kStatusInvalid;
            result.fatal = true;
        } else {
            try {
                result = g_machine.Execute(ins, 0, 0, nullptr, false);
            } catch (...) {
                result.status = kStatusInternal;
                result.fatal = true;
            }
        }

        uint8_t wireStatus = 0x00;
        if (result.status & kStatusInvalid) wireStatus = 0x80;
        else if (result.status & (kStatusInternal | kStatusDiskIo | kStatusPc | kStatusStack)) wireStatus = 0x81;
        else if (result.status != 0) wireStatus = 0x01;
        uint8_t response[4] = {wireStatus,
            static_cast<uint8_t>((result.value >> 8) & 0xFFu),
            static_cast<uint8_t>(result.value & 0xFFu), result.kind};
        if (result.status != 0) PrintDiagnostics(result);
        // Machine::Execute performs WAIT after releasing its state lock.
        if (!WriteExact(pipe, response, sizeof(response))) break;
    }
    DisconnectNamedPipe(pipe);
    CloseHandle(pipe);
}

bool MakeCurrentUserPipeSecurity(SECURITY_ATTRIBUTES& sa, PSECURITY_DESCRIPTOR& descriptor) {
    descriptor = nullptr;
    HANDLE token = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return false;
    DWORD bytesNeeded = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &bytesNeeded);
    if (bytesNeeded == 0) { CloseHandle(token); return false; }
    std::vector<uint8_t> tokenInfo(bytesNeeded);
    if (!GetTokenInformation(token, TokenUser, tokenInfo.data(), bytesNeeded, &bytesNeeded)) {
        CloseHandle(token);
        return false;
    }
    CloseHandle(token);
    const auto* user = reinterpret_cast<const TOKEN_USER*>(tokenInfo.data());
    LPWSTR sidText = nullptr;
    if (!ConvertSidToStringSidW(user->User.Sid, &sidText)) return false;
    const std::wstring sddl = std::wstring(L"D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;") + sidText + L")";
    LocalFree(sidText);
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl.c_str(), SDDL_REVISION_1,
                                                                &descriptor, nullptr)) return false;
    sa = {};
    sa.nLength = sizeof(sa);
    sa.lpSecurityDescriptor = descriptor;
    sa.bInheritHandle = FALSE;
    return true;
}

void PostCloseWindow() {
    HWND hwnd = g_window.load();
    if (hwnd) PostMessageW(hwnd, WM_CLOSE, 0, 0);
}

void PipeServerWorker() {
    SetLastError(ERROR_SUCCESS);
    HANDLE mutex = CreateMutexW(nullptr, TRUE, kPipeMutexName);
    const DWORD mutexError = GetLastError();
    if (!mutex) { PrintError("32a: cannot create pipe-server mutex.", true); PostCloseWindow(); return; }
    if (mutexError == ERROR_ALREADY_EXISTS) {
        CloseHandle(mutex);
        PrintError("32a: another -pipe server is already running.", true);
        PostCloseWindow();
        return;
    }

    SECURITY_ATTRIBUTES sa{};
    PSECURITY_DESCRIPTOR descriptor = nullptr;
    if (!MakeCurrentUserPipeSecurity(sa, descriptor)) {
        ReleaseMutex(mutex);
        CloseHandle(mutex);
        PrintError("32a: could not create named-pipe access rules.", true);
        PostCloseWindow();
        return;
    }

    bool firstInstance = true;
    while (g_running.load()) {
        const DWORD openMode = PIPE_ACCESS_DUPLEX | (firstInstance ? FILE_FLAG_FIRST_PIPE_INSTANCE : 0);
        HANDLE pipe = CreateNamedPipeW(kPipePath, openMode,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            PIPE_UNLIMITED_INSTANCES, 4, 5, 0, &sa);
        if (pipe == INVALID_HANDLE_VALUE) {
            PrintError("32a: CreateNamedPipe failed.", true);
            PostCloseWindow();
            break;
        }
        firstInstance = false;
        const BOOL connected = ConnectNamedPipe(pipe, nullptr);
        const DWORD connectError = connected ? ERROR_SUCCESS : GetLastError();
        if (!connected && connectError != ERROR_PIPE_CONNECTED) {
            CloseHandle(pipe);
            if (!g_running.load()) break;
            Sleep(20);
            continue;
        }
        std::thread(PipeClientWorker, pipe).detach();
    }

    LocalFree(descriptor);
    ReleaseMutex(mutex);
    CloseHandle(mutex);
}

void PrintHelp() {
    const char* help =
        "32a APU commands:\r\n"
        "  HELP                    show this help\r\n"
        "  CLEAR                   clear the VGA framebuffer only\r\n"
        "  RUN <file.32a>          execute a binary program\r\n"
        "  EXIT                    close 32a\r\n"
        "  <OP> <INPUT1> <INPUT2>  execute one non-branching instruction\r\n\r\n"
        "GPU OP (hex): 00 PASS 01 ADD 02 SUB 03 MUL 04 DIV 05 MOD 06 VGA 07 SHFL\r\n"
        "              08 AND 09 OR 0A XOR 0B SHL 0C SHR 0D BLEND 0E BRIGHT 0F CMP\r\n"
        "CPU OP (hex): 10 TEXT 11 IF_EQ 12 IF_NE 13 IF_GT 14 IF_LT 15 IF_GE 16 IF_LE\r\n"
        "              17 JUMP 18 DLOAD 19 DSTORE 1A LOAD 1B STORE 1C WAIT 1D HALT 1E CALL 1F RET\r\n"
        "Binary mode fields: 5/16/16 bits; -hex mode: 2/4/4 hex digits.\r\n"
        "TEXT: A=(Y<<8)|X, B=(palette<<8)|ASCII-code; character grid is 85x51.\r\n"
        "Pipe: 32a.exe -pipe  (\\\\.\\pipe\\32a); control-flow OPs require a .32a program.\r\n";
    WriteStandard(false, help);
}

void ConsoleWorker() {
    WriteStandard(false,
        "32a APU\r\n"
        "OP count: 32 (16 GPU + 16 CPU)\r\n"
        "Resolution: 256x256 logical pixels; 2x2 physical scale\r\n"
        "Color: RGB565; TEXT: 3x5 pixels, 256-color palette\r\n"
        "CPU: 256 internal 16-bit auto registers; 256-instruction predecode window\r\n"
        "Type HELP for commands.\r\n\r\n");
    std::string line;
    while (g_running.load()) {
        WriteStandard(false, "32a> ");
        if (!std::getline(std::cin, line)) { PostCloseWindow(); return; }
        if (!line.empty() && line.back() == '\r') line.pop_back();
        line = TrimAscii(line);
        if (line.empty()) continue;
        const std::string upper = UpperAscii(line);
        if (upper == "HELP") { PrintHelp(); continue; }
        if (upper == "CLEAR") { g_machine.ClearScreen(); continue; }
        if (upper == "EXIT" || upper == "QUIT") { PostCloseWindow(); return; }
        if (upper.size() >= 3 && upper.substr(0, 3) == "RUN" &&
            (upper.size() == 3 || std::isspace(static_cast<unsigned char>(upper[3])))) {
            std::string pathText = TrimAscii(line.substr(3));
            if (pathText.size() >= 2 && pathText.front() == '"' && pathText.back() == '"')
                pathText = pathText.substr(1, pathText.size() - 2);
            if (pathText.empty()) { PrintError("Usage: RUN <file.32a>"); continue; }
            try {
                const std::wstring path = Utf8ToWide(pathText);
                if (!HasExtension(path, L".32a")) throw std::runtime_error("RUN accepts only .32a files");
                const auto program = Load32aFile(path);
                g_machine.SetDiskPath(DataPathForProgram(path));
                RunProgram(program, false, true, true);
                g_machine.SetDiskPath(L"32a.32d");
            } catch (const std::exception& e) {
                PrintError(std::string("32a: ") + e.what());
            }
            continue;
        }
        try {
            const Instruction ins = ParseTextInstruction(SplitFields(line), g_hexOutput.load());
            ExecuteAndReportStandalone(ins, true, true);
        } catch (const std::exception& e) {
            PrintError(std::string("32a: ") + e.what());
        }
    }
}

void ShowUsage() {
    WriteStandard(true,
        "Usage:\r\n"
        "  32a.exe                         interactive console + VGA\r\n"
        "  32a.exe -hex                    interactive hexadecimal mode\r\n"
        "  32a.exe <OP> <A> <B>            run one standalone instruction\r\n"
        "  32a.exe -hex <OP> <A> <B>       run one hex instruction\r\n"
        "  32a.exe [ -hex ] <file.32a>     run a binary program\r\n"
        "  32a.exe [ -hex ] -loop <file.32a> repeat a program\r\n"
        "  32a.exe -pipe                   host \\.\\pipe\\32a\r\n");
}

} // namespace

int WINAPI WinMain(HINSTANCE instance, HINSTANCE, LPSTR, int) {
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv || argc <= 0) {
        PrintError("32a: failed to parse command line.", true);
        return 2;
    }

    bool hex = false;
    bool loop = false;
    bool pipeMode = false;
    std::vector<std::wstring> positional;
    for (int i = 1; i < argc; ++i) {
        std::wstring arg = argv[i];
        std::wstring lower = arg;
        for (wchar_t& c : lower) c = static_cast<wchar_t>(towlower(c));
        if (lower == L"-hex") hex = true;
        else if (lower == L"-loop") loop = true;
        else if (lower == L"-pipe") pipeMode = true;
        else positional.push_back(arg);
    }
    LocalFree(argv);
    g_hexOutput.store(hex);

    enum class Mode { Interactive, Single, File, Pipe };
    Mode mode = Mode::Interactive;
    Instruction single{};
    std::vector<Instruction> program;
    std::wstring diskPath = L"32a.32d";

    try {
        if (pipeMode) {
            if (loop || !positional.empty()) throw std::runtime_error("-pipe cannot be combined with a command or file");
            mode = Mode::Pipe;
        } else if (loop) {
            if (positional.size() != 1 || !HasExtension(positional[0], L".32a"))
                throw std::runtime_error("-loop requires exactly one .32a file");
            program = Load32aFile(positional[0]);
            diskPath = DataPathForProgram(positional[0]);
            mode = Mode::File;
        } else if (positional.empty()) {
            mode = Mode::Interactive;
        } else if (positional.size() == 1 && HasExtension(positional[0], L".32a")) {
            program = Load32aFile(positional[0]);
            diskPath = DataPathForProgram(positional[0]);
            mode = Mode::File;
        } else if (positional.size() == 3) {
            std::vector<std::string> fields;
            fields.reserve(3);
            for (const auto& field : positional) {
                std::string narrow;
                for (wchar_t c : field) {
                    if (c > 0x7F) throw std::runtime_error("instruction fields must use ASCII digits");
                    narrow.push_back(static_cast<char>(c));
                }
                fields.push_back(narrow);
            }
            single = ParseTextInstruction(fields, hex);
            mode = Mode::Single;
        } else {
            throw std::runtime_error("unrecognized command-line arguments");
        }
    } catch (const std::exception& e) {
        ShowUsage();
        PrintError(std::string("32a: ") + e.what(), true);
        return 2;
    }

    g_machine.SetDiskPath(diskPath);

    if (mode == Mode::Interactive) {
        if (!AllocConsole()) {
            PrintError("32a: could not allocate interactive console.", true);
            return 3;
        }
        SetConsoleCP(CP_UTF8);
        SetConsoleOutputCP(CP_UTF8);
        (void)freopen("CONIN$", "r", stdin);
        (void)freopen("CONOUT$", "w", stdout);
        (void)freopen("CONOUT$", "w", stderr);
        std::ios::sync_with_stdio(true);
    }

    if (!CreateVgaWindow(instance)) {
        PrintError("32a: could not create VGA window.", true);
        return 4;
    }

    switch (mode) {
    case Mode::Interactive:
        std::thread(ConsoleWorker).detach();
        break;
    case Mode::Single:
        std::thread([single]() { ExecuteAndReportStandalone(single, true, true); }).detach();
        break;
    case Mode::File:
        std::thread([program = std::move(program), loop]() { RunProgram(program, loop, true, true); }).detach();
        break;
    case Mode::Pipe:
        std::thread(PipeServerWorker).detach();
        break;
    }

    MSG msg{};
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
    g_running.store(false);
    return static_cast<int>(msg.wParam);
}
