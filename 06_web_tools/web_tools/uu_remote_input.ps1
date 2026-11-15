param(
    [string]$Nonce,
    [string]$ExpectedComputer,
    [switch]$ProtocolSelfTest,
    [ValidateRange(10, 90)][int]$IdleSeconds = 90
)

# This script is a transient remote GUI endpoint, not a shell API.
# Load its source in memory through an existing authenticated UU terminal.
# Never run actual input actions on the controller computer.
$ErrorActionPreference = 'Stop'
$protocolName = 'xiaoruo1.uu.remote-input.v2'
$framePrefix = 'XIAORUO1_UU_INPUT_V2|'
$requestPrefix = 'XIAORUO1_UU_REQUEST_V2|'

$nativeSource = @'
using System;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Security;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;

namespace Xiaoruo1.UURemoteInputV1 {
    [StructLayout(LayoutKind.Sequential)]
    public struct MouseInput {
        public int dx, dy;
        public uint mouseData, dwFlags, time;
        public UIntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct KeyboardInput {
        public ushort wVk, wScan;
        public uint dwFlags, time;
        public UIntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct HardwareInput {
        public uint uMsg;
        public ushort wParamL, wParamH;
    }
    [StructLayout(LayoutKind.Explicit)]
    public struct InputUnion {
        [FieldOffset(0)] public MouseInput mouse;
        [FieldOffset(0)] public KeyboardInput keyboard;
        [FieldOffset(0)] public HardwareInput hardware;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct Input {
        public uint type;
        public InputUnion data;
    }

    public static class Native {
        [DllImport("user32.dll", SetLastError=true)]
        private static extern uint SendInput(uint count, Input[] inputs, int size);
        [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint pid);
        [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr window);
        [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
        [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr window, uint flags);
        [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr window);
        [DllImport("user32.dll", SetLastError=true)] private static extern bool BringWindowToTop(IntPtr window);
        [DllImport("user32.dll", SetLastError=true)] private static extern bool AttachThreadInput(uint current, uint other, bool attach);
        [DllImport("user32.dll", EntryPoint="PeekMessageW", ExactSpelling=true, SetLastError=true)]
        private static extern bool PeekMessage(IntPtr message, IntPtr window, uint first, uint last, uint flags);
        [DllImport("user32.dll", EntryPoint="GetWindowTextW", ExactSpelling=true, CharSet=CharSet.Unicode)]
        private static extern int GetWindowText(IntPtr window, StringBuilder text, int maximum);
        [DllImport("user32.dll", EntryPoint="GetClassNameW", ExactSpelling=true, CharSet=CharSet.Unicode)]
        private static extern int GetClassName(IntPtr window, StringBuilder text, int maximum);
        [DllImport("user32.dll")] private static extern short GetAsyncKeyState(int virtualKey);
        [DllImport("user32.dll")] private static extern int GetSystemMetrics(int index);
        [DllImport("user32.dll")] private static extern IntPtr WindowFromPoint(Point point);
        [DllImport("user32.dll", SetLastError=true)] private static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
        [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
        [DllImport("kernel32.dll", SetLastError=true)] private static extern bool ProcessIdToSessionId(uint pid, out uint session);
        [DllImport("kernel32.dll")] private static extern uint WTSGetActiveConsoleSessionId();
        [DllImport("user32.dll")] private static extern IntPtr GetProcessWindowStation();
        [DllImport("user32.dll")] private static extern IntPtr GetThreadDesktop(uint threadId);
        [DllImport("user32.dll", SetLastError=true)] private static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
        [DllImport("user32.dll")] private static extern bool CloseDesktop(IntPtr desktop);
        [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        private static extern bool GetUserObjectInformation(IntPtr handle, int index, StringBuilder buffer, int length, out uint needed);
        private delegate bool EnumWindowProc(IntPtr window, IntPtr value);
        [DllImport("user32.dll", SetLastError=true)]
        private static extern bool EnumDesktopWindows(IntPtr desktop, EnumWindowProc callback, IntPtr value);
        private static string expectedComputer;
        private static string captureOrigin;
        public static readonly string ProcessCreatedTicks = Process.GetCurrentProcess().StartTime.ToUniversalTime().Ticks.ToString(CultureInfo.InvariantCulture);
        public static int InputSize { get { return Marshal.SizeOf(typeof(Input)); } }
        public static int UnionOffset { get { return (int)Marshal.OffsetOf(typeof(Input), "data"); } }

        private static Exception Error(string code, string message) {
            return new InvalidOperationException(code + ": " + message);
        }
        private static string ObjectName(IntPtr handle) {
            if (handle == IntPtr.Zero) throw Error("desktop_unavailable", "No desktop/window-station handle");
            uint needed;
            StringBuilder name = new StringBuilder(256);
            if (!GetUserObjectInformation(handle, 2, name, name.Capacity * 2, out needed))
                throw Error("desktop_unavailable", "Cannot inspect desktop/window-station name");
            return name.ToString();
        }
        public static void Initialize(string computer) {
            if (String.IsNullOrWhiteSpace(computer)) throw Error("expected_computer_required", "Supply the exact remote computer name");
            expectedComputer = computer;
            ValidateRuntime();
        }
        public static Dictionary<string, object> ValidateRuntime() {
            if (String.IsNullOrEmpty(expectedComputer) || !String.Equals(Environment.MachineName, expectedComputer, StringComparison.OrdinalIgnoreCase))
                throw Error("remote_computer_mismatch", "This helper is not running on the expected remote computer");
            Process process = Process.GetCurrentProcess();
            int session = process.SessionId;
            uint active = WTSGetActiveConsoleSessionId();
            if (session == 0 || active == 0xFFFFFFFF || session != active)
                throw Error("interactive_session_required", "Helper must run in the current active console session, never Session 0 or a new logon");
            string station = ObjectName(GetProcessWindowStation());
            string threadDesktop = ObjectName(GetThreadDesktop(GetCurrentThreadId()));
            IntPtr input = OpenInputDesktop(0, false, 1);
            string inputDesktop;
            try { inputDesktop = ObjectName(input); }
            finally { if (input != IntPtr.Zero) CloseDesktop(input); }
            if (!String.Equals(station, "WinSta0", StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(threadDesktop, "Default", StringComparison.OrdinalIgnoreCase) ||
                !String.Equals(inputDesktop, "Default", StringComparison.OrdinalIgnoreCase))
                throw Error("interactive_desktop_required", "Only the unlocked remote WinSta0\\Default desktop is supported");
            return new Dictionary<string, object> {
                {"computer", Environment.MachineName}, {"session_id", session}, {"active_session_id", active},
                {"window_station", station}, {"thread_desktop", threadDesktop}, {"input_desktop", inputDesktop},
                {"helper_pid", process.Id}, {"helper_creation_time_utc_ticks", ProcessCreatedTicks}
            };
        }
        private static bool OnCurrentDesktop(IntPtr target) {
            bool found = false;
            EnumWindowProc callback = delegate(IntPtr window, IntPtr _) {
                if (window == target) { found = true; return false; }
                return true;
            };
            EnumDesktopWindows(GetThreadDesktop(GetCurrentThreadId()), callback, IntPtr.Zero);
            GC.KeepAlive(callback);
            return found;
        }
        public static Dictionary<string, object> WindowIdentity(long rawWindow) {
            IntPtr window = new IntPtr(rawWindow);
            uint pid;
            if (window == IntPtr.Zero || !IsWindow(window) || GetWindowThreadProcessId(window, out pid) == 0)
                throw Error("target_window_stale", "Expected top-level window is unavailable");
            Process process = Process.GetProcessById((int)pid);
            StringBuilder title = new StringBuilder(512);
            StringBuilder className = new StringBuilder(256);
            GetWindowText(window, title, title.Capacity);
            GetClassName(window, className, className.Capacity);
            string processName = process.ProcessName;
            if (processName.Length > 64) processName = processName.Substring(0, 64);
            return new Dictionary<string, object> {
                {"hwnd", rawWindow}, {"pid", (int)pid},
                {"creation_time_utc_ticks", process.StartTime.ToUniversalTime().Ticks.ToString(CultureInfo.InvariantCulture)},
                {"session_id", process.SessionId}, {"title", title.ToString()},
                {"class_name", className.ToString()}, {"process_name", processName}
            };
        }
        public static bool IsProtectedTransportWindow(string title, string className, string processName) {
            if (String.IsNullOrEmpty(title) || title.IndexOf("xru_input_", StringComparison.OrdinalIgnoreCase) < 0)
                return false;
            return String.Equals(className, "ConsoleWindowClass", StringComparison.OrdinalIgnoreCase) ||
                   String.Equals(className, "CASCADIA_HOSTING_WINDOW_CLASS", StringComparison.OrdinalIgnoreCase) ||
                   String.Equals(processName, "WindowsTerminal", StringComparison.OrdinalIgnoreCase) ||
                   String.Equals(processName, "WindowsTerminal.exe", StringComparison.OrdinalIgnoreCase);
        }
        public static uint[] BuildAttachThreads(uint current, uint foreground, uint target) {
            if (current == 0 || target == 0) throw Error("input_queue_thread_unavailable", "Remote input thread identity is unavailable");
            List<uint> result = new List<uint>();
            if (foreground != 0 && foreground != current) result.Add(foreground);
            if (target != current && !result.Contains(target)) result.Add(target);
            return result.ToArray();
        }
        private static void ActivateExpected(IntPtr target, int pid, string created) {
            // This method is reachable only after ValidateRuntime and exact
            // target identity/default-desktop validation on the remote host.
            IntPtr foreground = GetForegroundWindow();
            uint foregroundThread = 0;
            if (foreground != IntPtr.Zero) {
                uint foregroundPid, foregroundSession;
                foregroundThread = GetWindowThreadProcessId(foreground, out foregroundPid);
                if (foregroundThread == 0 || !ProcessIdToSessionId(foregroundPid, out foregroundSession) ||
                    foregroundSession != (uint)Process.GetCurrentProcess().SessionId || !OnCurrentDesktop(foreground))
                    throw Error("activation_foreground_unverified", "The remote foreground queue is outside the verified user desktop");
            }
            uint targetPid;
            uint targetThread = GetWindowThreadProcessId(target, out targetPid);
            if (targetThread == 0 || targetPid != (uint)pid)
                throw Error("target_identity_mismatch", "The remote activation target changed");
            uint current = GetCurrentThreadId();
            // PeekMessage(PM_NOREMOVE) creates only this helper thread's queue.
            // The MSG buffer is opaque here and large enough for x86/x64 MSG.
            IntPtr message = Marshal.AllocHGlobal(64);
            try { PeekMessage(message, IntPtr.Zero, 0, 0, 0); }
            finally { Marshal.FreeHGlobal(message); }
            List<uint> attached = new List<uint>();
            bool detachFailed = false;
            try {
                foreach (uint other in BuildAttachThreads(current, foregroundThread, targetThread)) {
                    if (!AttachThreadInput(current, other, true))
                        throw Error("input_queue_attach_failed", "The verified remote GUI input queues could not be attached");
                    attached.Add(other);
                }
                Dictionary<string, object> actual = WindowIdentity(target.ToInt64());
                if ((int)actual["pid"] != pid || !String.Equals((string)actual["creation_time_utc_ticks"], created, StringComparison.Ordinal) || !OnCurrentDesktop(target))
                    throw Error("target_identity_mismatch", "The remote activation target identity changed");
                BringWindowToTop(target);
                SetForegroundWindow(target);
                Stopwatch wait = Stopwatch.StartNew();
                while (GetForegroundWindow() != target && wait.ElapsedMilliseconds < 200) Thread.Sleep(10);
            } finally {
                for (int i = attached.Count - 1; i >= 0; i--)
                    if (!AttachThreadInput(current, attached[i], false)) detachFailed = true;
                // Do not restore the transport window: the caller requested
                // this exact remote application's foreground, not a temporary
                // focus change. A failed detach still prevents any input.
                if (detachFailed) throw Error("input_queue_detach_failed", "The remote input queues did not detach; no input was sent");
            }
        }
        public static Dictionary<string, object> Foreground() {
            IntPtr window = GetForegroundWindow();
            if (window == IntPtr.Zero) return new Dictionary<string, object> { {"available", false} };
            Dictionary<string, object> result = WindowIdentity(window.ToInt64());
            result.Add("available", true);
            return result;
        }
        private static IntPtr ValidateExpected(long rawWindow, int pid, string created, bool activate) {
            ValidateRuntime();
            IntPtr window = new IntPtr(rawWindow);
            Dictionary<string, object> actual = WindowIdentity(rawWindow);
            if (IsProtectedTransportWindow((string)actual["title"], (string)actual["class_name"], (string)actual["process_name"]))
                throw Error("transport_window_protected", "The owned remote input transport window cannot receive GUI input");
            if ((int)actual["pid"] != pid || !String.Equals((string)actual["creation_time_utc_ticks"], created, StringComparison.Ordinal) ||
                (int)actual["session_id"] != Process.GetCurrentProcess().SessionId ||
                GetAncestor(window, 2) != window || !IsWindowVisible(window) || !OnCurrentDesktop(window))
                throw Error("target_identity_mismatch", "Expected window/process identity or desktop no longer matches");
            if (GetForegroundWindow() != window && activate) {
                // The caller supplied this exact remote HWND/PID/create-time.
                // No activation is ever performed without that explicit identity.
                ActivateExpected(window, pid, created);
            }
            if (GetForegroundWindow() != window)
                throw Error("target_not_foreground", "The expected remote window is not foreground; no input was sent");
            return window;
        }
        private static Input Keyboard(ushort key, ushort scan, uint flags) {
            Input result = new Input();
            result.type = 1;
            result.data.keyboard.wVk = key;
            result.data.keyboard.wScan = scan;
            result.data.keyboard.dwFlags = flags;
            result.data.keyboard.dwExtraInfo = UIntPtr.Zero;
            return result;
        }
        public static Input[] BuildUnicode(string text) {
            if (String.IsNullOrEmpty(text) || text.Length > 2000 || text.IndexOf('\0') >= 0)
                throw Error("invalid_text", "Text must contain 1-2000 non-NUL UTF-16 characters");
            // Reject isolated surrogates rather than silently replacing text.
            new UTF8Encoding(false, true).GetBytes(text);
            string normalized = text.Replace("\r\n", "\n").Replace("\r", "\n");
            Input[] result = new Input[normalized.Length * 2];
            for (int i = 0; i < normalized.Length; i++) {
                // Editor canvases such as Monaco route Enter through key
                // events rather than interpreting a Unicode packet as a key.
                // Each unit still has its own down/up pair for partial cleanup.
                if (normalized[i] == '\n') {
                    result[i * 2] = Keyboard(13, 0, 0);
                    result[i * 2 + 1] = Keyboard(13, 0, 2);
                } else {
                    result[i * 2] = Keyboard(0, normalized[i], 4);
                    result[i * 2 + 1] = Keyboard(0, normalized[i], 6);
                }
            }
            return result;
        }
        public static ushort[] ParseKeys(string[] names) {
            if (names == null || names.Length < 1 || names.Length > 5)
                throw Error("invalid_keys", "Use one chord containing 1-5 keys");
            Dictionary<string, ushort> known = new Dictionary<string, ushort>(StringComparer.OrdinalIgnoreCase) {
                {"CTRL",17},{"CONTROL",17},{"ALT",18},{"SHIFT",16},
                {"ENTER",13},{"RETURN",13},{"TAB",9},{"BACKSPACE",8},{"ESC",27},{"ESCAPE",27},
                {"SPACE",32},{"DELETE",46},{"INSERT",45},{"HOME",36},{"END",35},
                {"PAGEUP",33},{"PAGEDOWN",34},{"LEFT",37},{"UP",38},{"RIGHT",39},{"DOWN",40}
            };
            for (int i = 0; i < 26; i++) known.Add(((char)('A'+i)).ToString(), (ushort)('A'+i));
            for (int i = 0; i < 10; i++) known.Add(i.ToString(CultureInfo.InvariantCulture), (ushort)('0'+i));
            for (int i = 1; i <= 24; i++) known.Add("F"+i.ToString(CultureInfo.InvariantCulture), (ushort)(111+i));
            List<ushort> result = new List<ushort>();
            foreach (string raw in names) {
                ushort key;
                string name = raw == null ? "" : raw.Trim();
                if (!known.TryGetValue(name, out key) || result.Contains(key))
                    throw Error("invalid_keys", "Unsupported or duplicate virtual key");
                result.Add(key);
            }
            if (result.Contains(17) && result.Contains(18) && result.Contains(46))
                throw Error("secure_attention_unsupported", "Secure-attention sequences are not synthesized");
            return result.ToArray();
        }
        private static uint Extended(ushort key) {
            return (key >= 33 && key <= 40) || key == 45 || key == 46 ? 1U : 0U;
        }
        public static Input[] BuildChord(ushort[] keys) {
            if (keys == null || keys.Length < 1 || keys.Length > 5) throw Error("invalid_keys", "Invalid key chord");
            Input[] result = new Input[keys.Length * 2];
            for (int i = 0; i < keys.Length; i++) {
                result[i] = Keyboard(keys[i], 0, Extended(keys[i]));
                ushort reverse = keys[keys.Length - i - 1];
                result[keys.Length+i] = Keyboard(reverse, 0, Extended(reverse) | 2);
            }
            return result;
        }
        private static void CheckHeldKeys(ushort[] desired) {
            HashSet<int> inspect = new HashSet<int>(new int[] {16,17,18,0x5B,0x5C});
            if (desired != null) foreach (ushort key in desired) inspect.Add(key);
            foreach (int key in inspect)
                if ((GetAsyncKeyState(key) & 0x8000) != 0)
                    throw Error("remote_key_already_down", "A physical or pre-existing remote key is held; no input was sent");
        }
        private static Dictionary<string, object> Deliver(Input[] inputs, long window, int pid, string created,
                                                         bool activate, ushort[] keys, bool unicode) {
            ValidateExpected(window, pid, created, activate);
            CheckHeldKeys(keys);
            // Revalidate immediately before the one serialized input batch.
            ValidateExpected(window, pid, created, false);
            uint sent = SendInput((uint)inputs.Length, inputs, InputSize);
            int error = Marshal.GetLastWin32Error();
            if (sent != inputs.Length) {
                // Release only key-downs that this call actually inserted.
                // Never reset the remote keyboard or release the user's keys.
                if (unicode && (sent % 2) == 1) {
                    Input release = inputs[(int)sent];
                    SendInput(1, new Input[] {release}, InputSize);
                } else if (!unicode && keys != null) {
                    List<ushort> held = new List<ushort>();
                    for (int i = 0; i < Math.Min((int)sent, inputs.Length); i++) {
                        ushort key = inputs[i].data.keyboard.wVk;
                        if ((inputs[i].data.keyboard.dwFlags & 2) == 0) held.Add(key);
                        else held.Remove(key);
                    }
                    List<Input> releases = new List<Input>();
                    for (int i = held.Count-1; i >= 0; i--) releases.Add(Keyboard(held[i], 0, Extended(held[i]) | 2));
                    if (releases.Count > 0) SendInput((uint)releases.Count, releases.ToArray(), InputSize);
                }
                throw Error("input_delivery_incomplete", "Remote input was rejected or incomplete (including possible UIPI); no retry/fallback. inserted="+
                            sent.ToString(CultureInfo.InvariantCulture)+" requested="+inputs.Length.ToString(CultureInfo.InvariantCulture)+
                            " win32_error="+error.ToString(CultureInfo.InvariantCulture));
            }
            return new Dictionary<string, object> {
                {"inserted_events",sent},{"requested_events",inputs.Length},{"target_hwnd",window},
                {"target_pid",pid},{"remote_effect_verified",false},{"delivery","remote_session_SendInput"}
            };
        }
        public static Dictionary<string, object> TypeText(string text, long window, int pid, string created, bool activate) {
            return Deliver(BuildUnicode(text), window, pid, created, activate, null, true);
        }
        public static Dictionary<string, object> KeyChord(string[] names, long window, int pid, string created, bool activate) {
            ushort[] keys = ParseKeys(names);
            return Deliver(BuildChord(keys), window, pid, created, activate, keys, false);
        }
        // These builders/validators are pure. ProtocolSelfTest may call them on
        // the controller; only the dispatch methods below touch a GUI or wire.
        public static void ValidateBounds(int left, int top, int width, int height) {
            if (width < 1 || height < 1 || width > 8192 || height > 8192 || (long)width * height > 32000000 ||
                (long)left + width - 1 > Int32.MaxValue || (long)top + height - 1 > Int32.MaxValue)
                throw Error("invalid_bounds", "Virtual screen bounds exceed supported physical-pixel limits");
        }
        private static void ValidatePoint(int x, int y, int left, int top, int width, int height) {
            ValidateBounds(left, top, width, height);
            if ((long)x < left || (long)y < top || (long)x >= (long)left + width || (long)y >= (long)top + height)
                throw Error("invalid_coordinates", "Physical mouse point is outside the supplied virtual screen");
        }
        private static Input Mouse(int x, int y, uint flags, uint data, int left, int top, int width, int height) {
            ValidatePoint(x,y,left,top,width,height);
            Input result = new Input();
            result.type = 0;
            result.data.mouse.dx = width == 1 ? 0 : (int)Math.Round(((long)x-left)*65535.0/(width-1), MidpointRounding.AwayFromZero);
            result.data.mouse.dy = height == 1 ? 0 : (int)Math.Round(((long)y-top)*65535.0/(height-1), MidpointRounding.AwayFromZero);
            result.data.mouse.dwFlags = flags | 0x8000U | 0x4000U; // ABSOLUTE | VIRTUALDESK
            result.data.mouse.mouseData = data;
            return result;
        }
        private static uint[] ButtonFlags(string button) {
            if (button == "left") return new uint[] {0x2,0x4};
            if (button == "right") return new uint[] {0x8,0x10};
            if (button == "middle") return new uint[] {0x20,0x40};
            throw Error("invalid_button", "Mouse button must be left, right or middle");
        }
        public static Input[] BuildClick(int x, int y, string button, int count, int left, int top, int width, int height) {
            uint[] flags = ButtonFlags(button);
            if (count < 1 || count > 2) throw Error("invalid_click_count", "Click count must be one or two");
            List<Input> events = new List<Input>();
            events.Add(Mouse(x,y,1,0,left,top,width,height));
            for (int i=0; i<count; i++) {
                events.Add(Mouse(x,y,flags[0],0,left,top,width,height));
                events.Add(Mouse(x,y,flags[1],0,left,top,width,height));
            }
            return events.ToArray();
        }
        public static Input[] BuildScroll(int x, int y, int vertical, int horizontal, int left, int top, int width, int height) {
            if (vertical < -12000 || vertical > 12000 || horizontal < -12000 || horizontal > 12000 || (vertical == 0 && horizontal == 0))
                throw Error("invalid_scroll", "Wheel deltas must be nonzero overall and within plus/minus 12000");
            List<Input> events = new List<Input>();
            events.Add(Mouse(x,y,1,0,left,top,width,height));
            if (vertical != 0) events.Add(Mouse(x,y,0x800,unchecked((uint)vertical),left,top,width,height));
            if (horizontal != 0) events.Add(Mouse(x,y,0x1000,unchecked((uint)horizontal),left,top,width,height));
            return events.ToArray();
        }
        public static Input[] BuildDrag(int x, int y, int toX, int toY, int duration, int left, int top, int width, int height) {
            ValidatePoint(x,y,left,top,width,height);
            ValidatePoint(toX,toY,left,top,width,height);
            if (duration < 50 || duration > 5000) throw Error("invalid_drag_duration", "Drag duration must be 50-5000 milliseconds");
            int steps = Math.Max(2,Math.Min(100,(duration+15)/16));
            List<Input> events = new List<Input>();
            events.Add(Mouse(x,y,1,0,left,top,width,height));
            events.Add(Mouse(x,y,2,0,left,top,width,height));
            for (int i=1;i<=steps;i++) {
                int px=(int)Math.Round(x+((long)toX-x)*(double)i/steps,MidpointRounding.AwayFromZero);
                int py=(int)Math.Round(y+((long)toY-y)*(double)i/steps,MidpointRounding.AwayFromZero);
                events.Add(Mouse(px,py,1,0,left,top,width,height));
            }
            events.Add(Mouse(toX,toY,4,0,left,top,width,height));
            return events.ToArray();
        }
        private sealed class PhysicalPixels : IDisposable {
            private IntPtr previous;
            public PhysicalPixels() {
                previous = SetThreadDpiAwarenessContext(new IntPtr(-4)); // PER_MONITOR_AWARE_V2, this thread only
                if (previous == IntPtr.Zero) throw Error("physical_pixels_unavailable", "Cannot enter physical-pixel DPI context");
            }
            public void Dispose() {
                if (previous != IntPtr.Zero) {
                    IntPtr old=previous; previous=IntPtr.Zero;
                    if (SetThreadDpiAwarenessContext(old) == IntPtr.Zero)
                        throw Error("dpi_context_restore_failed", "Could not restore the helper thread DPI context");
                }
            }
        }
        private static Dictionary<string,object> Bounds(int left,int top,int width,int height) {
            return new Dictionary<string,object> {{"left",left},{"top",top},{"width",width},{"height",height}};
        }
        private static int[] CurrentBounds() {
            int[] result = new int[] {GetSystemMetrics(76),GetSystemMetrics(77),GetSystemMetrics(78),GetSystemMetrics(79)};
            ValidateBounds(result[0],result[1],result[2],result[3]);
            return result;
        }
        private static void ValidateMouseRuntime(int left,int top,int width,int height) {
            ValidateRuntime();
            ValidateBounds(left,top,width,height);
            int[] current=CurrentBounds();
            if (current[0]!=left || current[1]!=top || current[2]!=width || current[3]!=height)
                throw Error("screen_bounds_changed", "Physical virtual screen changed; capture a fresh image before input");
        }
        private static void CheckHeldMouseAndModifiers() {
            CheckHeldKeys(null);
            foreach (int button in new int[] {1,2,4,5,6})
                if ((GetAsyncKeyState(button)&0x8000)!=0)
                    throw Error("remote_mouse_already_down", "A remote mouse button is already held; no input was sent");
        }
        private static void ProtectMousePoint(int x,int y) {
            IntPtr hit=WindowFromPoint(new Point(x,y));
            if (hit==IntPtr.Zero) return; // A real desktop background is a valid mouse target.
            IntPtr root=GetAncestor(hit,2);
            Dictionary<string,object> actual=WindowIdentity((root==IntPtr.Zero?hit:root).ToInt64());
            if (IsProtectedTransportWindow((string)actual["title"],(string)actual["class_name"],(string)actual["process_name"]))
                throw Error("transport_window_protected", "The owned transport console cannot receive mouse input");
            if ((int)actual["session_id"]!=Process.GetCurrentProcess().SessionId)
                throw Error("mouse_target_session_mismatch", "Mouse target is outside the verified remote session");
        }
        private static Dictionary<string,object> SafeForeground() {
            try { return Foreground(); }
            catch { return new Dictionary<string,object> {{"available",false}}; }
        }
        private static Dictionary<string,object> MouseResult(int count) {
            // SendInput reports insertion, not queue consumption. Give the
            // remote GUI a brief chance to apply click-driven focus changes.
            Thread.Sleep(50);
            return new Dictionary<string,object> {{"inserted_events",count},{"requested_events",count},
                {"remote_effect_verified",false},{"delivery","remote_session_SendInput"},{"foreground",SafeForeground()}};
        }
        private static void SendMouseBatch(Input[] inputs) {
            uint sent=SendInput((uint)inputs.Length,inputs,InputSize);
            if (sent==inputs.Length) return;
            // Only release a button whose DOWN event THIS call inserted without
            // its matching UP. Never reset the user's keyboard or mouse state.
            uint held=0;
            for (int i=0;i<(int)sent;i++) {
                uint flags=inputs[i].data.mouse.dwFlags;
                if ((flags&2)!=0) held|=4; if ((flags&4)!=0) held&=~4U;
                if ((flags&8)!=0) held|=16; if ((flags&16)!=0) held&=~16U;
                if ((flags&32)!=0) held|=64; if ((flags&64)!=0) held&=~64U;
            }
            if (held!=0) {
                Input release=new Input(); release.data.mouse.dwFlags=held;
                SendInput(1,new Input[] {release},InputSize);
            }
            throw Error("input_delivery_incomplete", "Remote mouse input was incomplete; no automatic replay or fallback");
        }
        public static Dictionary<string,object> Click(int x,int y,string button,int count,int left,int top,int width,int height) {
            Input[] events=BuildClick(x,y,button,count,left,top,width,height);
            using (new PhysicalPixels()) {
                ValidateMouseRuntime(left,top,width,height); CheckHeldMouseAndModifiers(); ProtectMousePoint(x,y);
                ValidateMouseRuntime(left,top,width,height);
                SendMouseBatch(events); return MouseResult(events.Length);
            }
        }
        public static Dictionary<string,object> Scroll(int x,int y,int vertical,int horizontal,int left,int top,int width,int height) {
            Input[] events=BuildScroll(x,y,vertical,horizontal,left,top,width,height);
            using (new PhysicalPixels()) {
                ValidateMouseRuntime(left,top,width,height); CheckHeldMouseAndModifiers(); ProtectMousePoint(x,y);
                ValidateMouseRuntime(left,top,width,height);
                SendMouseBatch(events); return MouseResult(events.Length);
            }
        }
        public static Dictionary<string,object> Drag(int x,int y,int toX,int toY,int duration,int left,int top,int width,int height) {
            Input[] events=BuildDrag(x,y,toX,toY,duration,left,top,width,height);
            using (new PhysicalPixels()) {
                ValidateMouseRuntime(left,top,width,height); CheckHeldMouseAndModifiers();
                int steps=events.Length-3;
                // Reject a drag crossing the owned transport before any input.
                for (int i=0;i<=steps;i++) ProtectMousePoint((int)Math.Round(x+((long)toX-x)*(double)i/steps),
                                                                           (int)Math.Round(y+((long)toY-y)*(double)i/steps));
                bool down=false;
                try {
                    ValidateMouseRuntime(left,top,width,height);
                    SendMouseBatch(new Input[] {events[0],events[1]}); down=true;
                    Stopwatch clock=Stopwatch.StartNew();
                    for (int i=0;i<steps;i++) {
                        int due=(int)((long)duration*(i+1)/steps);
                        int delay=due-(int)clock.ElapsedMilliseconds; if (delay>0) Thread.Sleep(delay);
                        ValidateMouseRuntime(left,top,width,height);
                        SendMouseBatch(new Input[] {events[i+2]});
                    }
                    SendMouseBatch(new Input[] {events[events.Length-1]}); down=false;
                } finally {
                    if (down) {
                        // This endpoint owns the matching DOWN. No other state
                        // is cleared, even when the remote desktop changes.
                        Input release=new Input(); release.data.mouse.dwFlags=4;
                        SendInput(1,new Input[] {release},InputSize);
                    }
                }
                return MouseResult(events.Length);
            }
        }
        public static string ValidateCaptureOrigin(string origin) {
            Uri uri;
            if (String.IsNullOrEmpty(origin) || origin.Length>2048 || !Uri.TryCreate(origin,UriKind.Absolute,out uri) ||
                uri.Scheme!=Uri.UriSchemeHttps || String.IsNullOrEmpty(uri.Host) || !String.IsNullOrEmpty(uri.UserInfo) ||
                uri.AbsolutePath!="/" || !String.IsNullOrEmpty(uri.Query) || !String.IsNullOrEmpty(uri.Fragment) ||
                !String.Equals(uri.GetLeftPart(UriPartial.Authority),origin,StringComparison.Ordinal))
                throw Error("invalid_capture_origin", "Capture ingress must be one exact HTTPS origin without path or credentials");
            return origin;
        }
        public static void ValidateCaptureArguments(string id,string url,string token) {
            if (id==null || !Regex.IsMatch(id,@"\A[A-Za-z0-9_-]{1,128}\z") || url==null || url.Length>2304 ||
                token==null || !Regex.IsMatch(token,@"\A[A-Za-z0-9._~-]{16,1024}\z"))
                throw Error("invalid_capture_ticket", "Capture id, destination or opaque ticket is invalid");
            Uri uri;
            if (!Uri.TryCreate(url,UriKind.Absolute,out uri)) throw Error("invalid_capture_ticket", "Invalid capture destination");
            string origin=ValidateCaptureOrigin(uri.GetLeftPart(UriPartial.Authority));
            if (!String.Equals(url,origin+"/remote-gui/captures/"+id,StringComparison.Ordinal))
                throw Error("invalid_capture_ticket", "Capture destination must match the exact ticket path");
        }
        public static Dictionary<string,object> ConfigureCapture(string origin) {
            ValidateCaptureOrigin(origin);
            if (captureOrigin!=null && !String.Equals(captureOrigin,origin,StringComparison.Ordinal))
                throw Error("capture_origin_already_configured", "This session already has its fixed capture ingress");
            captureOrigin=origin;
            return new Dictionary<string,object> {{"configured",true}};
        }
        public static string ValidateConfiguredCapture(string id,string url,string token) {
            ValidateCaptureArguments(id,url,token);
            if (captureOrigin==null) throw Error("capture_origin_required", "Configure the trusted session ingress before capture");
            if (!String.Equals(url,captureOrigin+"/remote-gui/captures/"+id,StringComparison.Ordinal))
                throw Error("capture_origin_mismatch", "Capture destination differs from this session's configured ingress");
            return url;
        }
        public static void ValidatePng(byte[] png,int width,int height) {
            ValidateBounds(0,0,width,height);
            byte[] magic=new byte[] {137,80,78,71,13,10,26,10};
            if (png==null || png.Length<33 || png.Length>16*1024*1024)
                throw Error("invalid_capture_png", "Capture is not a bounded PNG image");
            for (int i=0;i<8;i++) if (png[i]!=magic[i]) throw Error("invalid_capture_png", "Invalid PNG signature");
            if (png[8]!=0 || png[9]!=0 || png[10]!=0 || png[11]!=13 || png[12]!=73 || png[13]!=72 || png[14]!=68 || png[15]!=82 ||
                ((long)png[16]<<24 | (long)png[17]<<16 | (long)png[18]<<8 | png[19])!=width ||
                ((long)png[20]<<24 | (long)png[21]<<16 | (long)png[22]<<8 | png[23])!=height)
                throw Error("invalid_capture_png", "PNG header does not match the captured physical bounds");
        }
        public static Dictionary<string,object> EncodeCapture(Bitmap bitmap) {
            // Encoding is pure: no desktop, input or network operation occurs here.
            try {
                if (bitmap==null) throw new InvalidOperationException();
                int width=bitmap.Width,height=bitmap.Height;
                ValidateBounds(0,0,width,height);
                byte[] png;
                using (MemoryStream memory=new MemoryStream()) { bitmap.Save(memory,ImageFormat.Png); png=memory.ToArray(); }
                // The intermediate lossless image may exceed the old PNG wire
                // limit; final PNGs stay <=1 MiB and final JPEGs <=12 MiB.
                byte[] pngHeader=png;
                if (png.Length>16*1024*1024) {
                    if (png.LongLength>(long)width*height*4+1024*1024) throw new InvalidOperationException();
                    pngHeader=new byte[33]; Array.Copy(png,pngHeader,pngHeader.Length);
                }
                ValidatePng(pngHeader,width,height);
                if (png.Length<=1024*1024)
                    return new Dictionary<string,object> {{"bytes",png},{"mime_type","image/png"},{"format","png"}};
                ImageCodecInfo jpegCodec=null;
                foreach (ImageCodecInfo codec in ImageCodecInfo.GetImageEncoders())
                    if (codec.FormatID==ImageFormat.Jpeg.Guid) { jpegCodec=codec; break; }
                if (jpegCodec==null) throw new InvalidOperationException();
                byte[] jpeg;
                using (MemoryStream memory=new MemoryStream())
                using (EncoderParameters parameters=new EncoderParameters(1)) {
                    parameters.Param[0]=new EncoderParameter(System.Drawing.Imaging.Encoder.Quality,92L);
                    bitmap.Save(memory,jpegCodec,parameters); jpeg=memory.ToArray();
                }
                if (jpeg.Length<4 || jpeg.Length>12*1024*1024 || jpeg[0]!=255 || jpeg[1]!=216 ||
                    jpeg[jpeg.Length-2]!=255 || jpeg[jpeg.Length-1]!=217) throw new InvalidOperationException();
                using (MemoryStream memory=new MemoryStream(jpeg,false))
                using (Image decoded=Image.FromStream(memory,false,true)) {
                    if (decoded.RawFormat.Guid!=ImageFormat.Jpeg.Guid || decoded.Width!=width || decoded.Height!=height)
                        throw new InvalidOperationException();
                    using (Bitmap proof=new Bitmap(decoded))
                        if (proof.Width!=width || proof.Height!=height) throw new InvalidOperationException();
                }
                return new Dictionary<string,object> {{"bytes",jpeg},{"mime_type","image/jpeg"},{"format","jpeg"}};
            } catch {
                throw Error("capture_encoding_failed","The receiver capture could not be encoded at its original physical dimensions");
            }
        }
        public static string UploadTimeoutCode(string phase) {
            if (phase=="open") return "capture_upload_timeout_open";
            if (phase=="write") return "capture_upload_timeout_write";
            if (phase=="response") return "capture_upload_timeout_response";
            return "capture_upload_timeout";
        }
        private static void UploadCapture(string url,string token,byte[] bytes,string mimeType) {
            // This receiver has one bounded upload attempt; its ticket is never retried.
            HttpWebRequest request=null;
            System.Threading.Timer deadline=null;
            int timedOut=0;
            string phase="open";
            string failureCode=null;
            Func<int,string> httpCode=delegate(int status) {
                switch(status) {
                    case 401: return "capture_upload_http_401";
                    case 403: return "capture_upload_http_403";
                    case 404: return "capture_upload_http_404";
                    case 409: return "capture_upload_http_409";
                    case 413: return "capture_upload_http_413";
                    case 415: return "capture_upload_http_415";
                    case 429: return "capture_upload_http_429";
                    case 500: return "capture_upload_http_500";
                    case 502: return "capture_upload_http_502";
                    case 503: return "capture_upload_http_503";
                    case 504: return "capture_upload_http_504";
                    default: return "capture_upload_failed";
                }
            };
            try {
                if (mimeType!="image/png" && mimeType!="image/jpeg") throw new InvalidOperationException();
                ServicePointManager.SecurityProtocol=SecurityProtocolType.Tls12;
                request=(HttpWebRequest)WebRequest.Create(url);
                request.Method="POST"; request.AllowAutoRedirect=false;
                request.Timeout=45000; request.ReadWriteTimeout=45000; request.KeepAlive=false;
                request.UserAgent="Xiaoruo1Receiver/1.1";
                request.ServicePoint.Expect100Continue=false;
                request.ContentType=mimeType; request.ContentLength=bytes.Length;
                request.Headers[HttpRequestHeader.Authorization]="Bearer "+token;
                // The ordinary OS certificate chain/name validation MUST succeed.
                request.ServerCertificateValidationCallback=delegate(object sender,System.Security.Cryptography.X509Certificates.X509Certificate certificate,
                    System.Security.Cryptography.X509Certificates.X509Chain chain,SslPolicyErrors errors) { return errors==SslPolicyErrors.None; };
                deadline=new System.Threading.Timer(delegate(object state) {
                    Interlocked.Exchange(ref timedOut,1);
                    try { request.Abort(); } catch { }
                },null,45000,Timeout.Infinite);
                using (Stream stream=request.GetRequestStream()) { phase="write"; stream.Write(bytes,0,bytes.Length); }
                phase="response";
                using (HttpWebResponse response=(HttpWebResponse)request.GetResponse()) {
                    int status=(int)response.StatusCode;
                    if (status<200 || status>=300) failureCode=httpCode(status);
                }
            } catch (WebException failure) {
                HttpWebResponse response=failure.Response as HttpWebResponse;
                try {
                    if (response!=null) failureCode=httpCode((int)response.StatusCode);
                } finally {
                    if (response!=null) { try { response.Close(); } catch { } }
                }
                if (failureCode==null) {
                    if (Interlocked.CompareExchange(ref timedOut,0,0)!=0 || failure.Status==WebExceptionStatus.Timeout)
                        failureCode=UploadTimeoutCode(phase);
                    else if (failure.Status==WebExceptionStatus.TrustFailure || failure.Status==WebExceptionStatus.SecureChannelFailure)
                        failureCode="capture_upload_tls_failed";
                    else if (failure.Status==WebExceptionStatus.NameResolutionFailure || failure.Status==WebExceptionStatus.ProxyNameResolutionFailure)
                        failureCode="capture_upload_dns_failed";
                    else if (failure.Status==WebExceptionStatus.ConnectFailure || failure.Status==WebExceptionStatus.ConnectionClosed ||
                        failure.Status==WebExceptionStatus.ReceiveFailure || failure.Status==WebExceptionStatus.SendFailure)
                        failureCode="capture_upload_connection_failed";
                    else failureCode="capture_upload_failed";
                }
            } catch {
                failureCode=Interlocked.CompareExchange(ref timedOut,0,0)!=0 ? UploadTimeoutCode(phase) : "capture_upload_failed";
            } finally {
                if (deadline!=null) deadline.Dispose();
                if (request!=null) { try { request.Abort(); } catch { } }
            }
            // Never expose an address, ticket, body or native exception message.
            if (failureCode!=null) throw Error(failureCode,"Capture ingress upload failed; the upload was not retried");
        }
        public static Dictionary<string,object> StableCaptureForeground(Dictionary<string,object> before,Dictionary<string,object> after) {
            if (before!=null && after!=null && before.ContainsKey("available") && after.ContainsKey("available") &&
                (bool)before["available"] && (bool)after["available"] &&
                Object.Equals(before["hwnd"],after["hwnd"]) && Object.Equals(before["pid"],after["pid"]) &&
                Object.Equals(before["creation_time_utc_ticks"],after["creation_time_utc_ticks"])) return after;
            return new Dictionary<string,object> {{"available",false},{"target_changed",true}};
        }
        public static Dictionary<string,object> Capture(string id,string url,string token) {
            ValidateConfiguredCapture(id,url,token);
            ValidateRuntime();
            Dictionary<string,object> encoded; int[] bounds;
            Dictionary<string,object> before,after;
            using (new PhysicalPixels()) {
                bounds=CurrentBounds(); before=SafeForeground();
                using (Bitmap bitmap=new Bitmap(bounds[2],bounds[3],PixelFormat.Format32bppArgb)) {
                    using (Graphics graphics=Graphics.FromImage(bitmap))
                        graphics.CopyFromScreen(bounds[0],bounds[1],0,0,new Size(bounds[2],bounds[3]),CopyPixelOperation.SourceCopy);
                    after=SafeForeground();
                    encoded=EncodeCapture(bitmap);
                }
                ValidateMouseRuntime(bounds[0],bounds[1],bounds[2],bounds[3]);
            }
            byte[] bytes=(byte[])encoded["bytes"];
            string mimeType=(string)encoded["mime_type"];
            string digest;
            using (SHA256 sha=SHA256.Create()) digest=BitConverter.ToString(sha.ComputeHash(bytes)).Replace("-","").ToLowerInvariant();
            UploadCapture(url,token,bytes,mimeType);
            return new Dictionary<string,object> {{"capture_id",id},{"width",bounds[2]},{"height",bounds[3]},
                {"bounds",Bounds(bounds[0],bounds[1],bounds[2],bounds[3])},{"remote_bounds",Bounds(bounds[0],bounds[1],bounds[2],bounds[3])},
                {"foreground",StableCaptureForeground(before,after)},{"sha256",digest},
                {"mime_type",mimeType},{"format",(string)encoded["format"]},{"size_bytes",bytes.Length}};
        }
    }

    public sealed class LineReader {
        private readonly BlockingCollection<string> queue = new BlockingCollection<string>(32);
        private volatile bool ended;
        public LineReader() {
            Thread reader = new Thread(delegate() {
                try {
                    string line;
                    while ((line = Console.ReadLine()) != null) {
                        if (line.Length > 65536) line = "!line_too_long";
                        if (!queue.TryAdd(line, 1000)) break;
                    }
                } finally { ended = true; }
            });
            reader.Name = "Xiaoruo1 UU stdin reader";
            reader.IsBackground = true;
            reader.Start();
        }
        public string Next(int timeout) {
            string line;
            if (queue.TryTake(out line, timeout)) return line;
            if (ended) throw new System.IO.EndOfStreamException();
            return null;
        }
    }

    public static class Fixture {
        private static Form form;
        private static TextBox textbox;
        private static Thread thread;
        private static readonly List<string> events = new List<string>();
        private static long hwnd;
        private static object Invoke(Func<object> action) {
            if (form == null || form.IsDisposed) throw new InvalidOperationException("fixture_not_open: No owned test window");
            object value = null;
            Exception failure = null;
            using (ManualResetEventSlim done = new ManualResetEventSlim(false)) {
                form.BeginInvoke(new Action(delegate() {
                    try { value = action(); } catch (Exception error) { failure = error; }
                    finally { done.Set(); }
                }));
                if (!done.Wait(3000)) throw new TimeoutException("fixture_timeout: Owned test window did not respond");
                if (failure != null) throw failure;
            }
            return value;
        }
        public static Dictionary<string, object> Create(string suffix) {
            Native.ValidateRuntime();
            if (form != null && !form.IsDisposed) return State();
            using (ManualResetEventSlim ready = new ManualResetEventSlim(false)) {
                Exception failure = null;
                thread = new Thread(delegate() {
                    try {
                        form = new Form();
                        form.Text = "Xiaoruo1 UU GUI Input Test " + suffix;
                        form.StartPosition = FormStartPosition.CenterScreen;
                        form.Size = new Size(800, 420);
                        form.ShowInTaskbar = true;
                        // The acceptance fixture must be visible above this
                        // endpoint's own console so UU can focus it by mouse.
                        form.TopMost = true;
                        Label label = new Label();
                        label.Text = "Temporary Xiaoruo1 test window. No user document is opened or saved.";
                        label.Dock = DockStyle.Top;
                        label.Height = 48;
                        textbox = new TextBox();
                        textbox.Multiline = true;
                        textbox.AcceptsReturn = true;
                        textbox.AcceptsTab = true;
                        textbox.ScrollBars = ScrollBars.Vertical;
                        textbox.Dock = DockStyle.Fill;
                        textbox.Font = new Font("Segoe UI", 14);
                        events.Clear();
                        textbox.KeyDown += delegate(object sender, KeyEventArgs args) {
                            events.Add("down:"+args.KeyData.ToString());
                            if (events.Count > 100) events.RemoveAt(0);
                        };
                        textbox.KeyUp += delegate(object sender, KeyEventArgs args) {
                            events.Add("up:"+args.KeyData.ToString());
                            if (events.Count > 100) events.RemoveAt(0);
                        };
                        form.Controls.Add(textbox);
                        form.Controls.Add(label);
                        form.Shown += delegate {
                            hwnd = form.Handle.ToInt64();
                            form.Activate();
                            textbox.Focus();
                            ready.Set();
                        };
                        Application.Run(form);
                    } catch (Exception error) { failure = error; ready.Set(); }
                });
                thread.IsBackground = true;
                thread.Name = "Xiaoruo1 UU owned test fixture";
                thread.SetApartmentState(ApartmentState.STA);
                thread.Start();
                if (!ready.Wait(5000)) throw new TimeoutException("fixture_timeout: Owned test window did not start");
                if (failure != null) throw failure;
            }
            return State();
        }
        public static Dictionary<string, object> State() {
            Native.ValidateRuntime();
            if (form == null || form.IsDisposed) return new Dictionary<string, object> { {"exists",false} };
            return (Dictionary<string, object>)Invoke(delegate {
                Dictionary<string, object> result = Native.WindowIdentity(hwnd);
                result.Add("exists",true);
                result.Add("text",textbox.Text);
                result.Add("text_length",textbox.TextLength);
                result.Add("key_events",events.ToArray());
                result.Add("owned_fixture",true);
                return result;
            });
        }
        public static bool Close() {
            Form owned = form;
            if (owned == null || owned.IsDisposed) return false;
            try { owned.BeginInvoke(new Action(delegate { owned.Close(); })); }
            catch (InvalidOperationException) { }
            if (thread != null) thread.Join(3000);
            bool closed = owned.IsDisposed;
            if (closed) { form = null; textbox = null; thread = null; hwnd = 0; }
            return closed;
        }
    }
}
'@

function Initialize-NativeTypes {
    if (-not ([System.Management.Automation.PSTypeName]'Xiaoruo1.UURemoteInputV1.Native').Type) {
        Add-Type -TypeDefinition $nativeSource -ReferencedAssemblies System.Windows.Forms, System.Drawing -ErrorAction Stop
    }
}

function Assert-Properties($Object, [string[]]$Allowed, [string[]]$Required = @()) {
    if ($null -eq $Object -or $Object -is [string] -or $Object -is [array] -or $Object -is [ValueType]) {
        throw 'invalid_request: Expected a JSON object'
    }
    $names = @($Object.PSObject.Properties.Name | Where-Object { $null -ne $_ })
    foreach ($name in $names) { if ($name -notin $Allowed) { throw 'invalid_request: Unknown request property' } }
    foreach ($name in $Required) { if ($name -notin $names) { throw 'invalid_request: Required request property is missing' } }
}

function Get-PositiveInteger($Value, [string]$Name, [long]$Maximum = [long]::MaxValue) {
    if ($Value -is [ValueType] -and $Value -isnot [bool]) {
        try {
            $number = [decimal]$Value
            if ($number -le 0 -or $number -gt $Maximum -or [decimal]::Truncate($number) -ne $number) { throw 'invalid' }
            return [long]$number
        } catch { throw "invalid_request: $Name must be a supported positive integer" }
    }
    if ($Value -isnot [string] -or $Value -notmatch '^[1-9][0-9]*$') {
        throw "invalid_request: $Name must be a positive integer"
    }
    $parsed = 0L
    if (-not [long]::TryParse([string]$Value, [ref]$parsed) -or $parsed -gt $Maximum) {
        throw "invalid_request: $Name exceeds supported limits"
    }
    return $parsed
}

function Get-PhysicalInteger($Value, [string]$Name, [int]$Minimum = [int]::MinValue, [int]$Maximum = [int]::MaxValue) {
    if ($Value -isnot [ValueType] -or $Value -is [bool]) { throw "invalid_request: $Name must be an integer" }
    try {
        $number = [decimal]$Value
        if ([decimal]::Truncate($number) -ne $number -or $number -lt $Minimum -or $number -gt $Maximum) { throw 'invalid' }
        return [int]$number
    } catch { throw "invalid_request: $Name must be a bounded integer" }
}

function Get-MouseBounds($Bounds) {
    Assert-Properties $Bounds @('left','top','width','height') @('left','top','width','height')
    $values = @(
        (Get-PhysicalInteger $Bounds.left 'bounds.left'),
        (Get-PhysicalInteger $Bounds.top 'bounds.top'),
        (Get-PhysicalInteger $Bounds.width 'bounds.width' 1 8192),
        (Get-PhysicalInteger $Bounds.height 'bounds.height' 1 8192)
    )
    [Xiaoruo1.UURemoteInputV1.Native]::ValidateBounds($values[0],$values[1],$values[2],$values[3])
    return ,([int[]]$values)
}

function Get-Request([string]$Line, [string]$ExpectedNonce) {
    if ($null -eq $Line -or $Line.Length -gt 32768 -or $Line -cnotmatch '^XIAORUO1_UU_REQUEST_V2\|([A-Za-z0-9+/]+={0,2})\|([0-9a-f]{64})\|END$') {
        throw 'invalid_request_frame: Expected one complete bounded V2 request line'
    }
    $encoded = $Matches[1]
    $checksum = $Matches[2]
    try { $bytes = [Convert]::FromBase64String($encoded) }
    catch { throw 'invalid_request_frame: Invalid request encoding' }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $actualChecksum = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
    if (-not [string]::Equals($actualChecksum, $checksum, [StringComparison]::Ordinal)) {
        throw 'request_checksum_mismatch: Request bytes did not match their checksum; no action was dispatched'
    }
    try {
        $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $json = $utf8.GetString($bytes)
        $request = ConvertFrom-Json -InputObject $json -ErrorAction Stop
    } catch { throw 'invalid_request: Invalid base64 UTF8 JSON' }
    Assert-Properties $request @('nonce','reqid','action','arguments') @('nonce','reqid','action')
    if ($request.nonce -isnot [string] -or -not [string]::Equals($request.nonce, $ExpectedNonce, [StringComparison]::Ordinal)) { throw 'nonce_mismatch: Request nonce does not match this helper' }
    if ($request.reqid -isnot [string] -or $request.reqid -notmatch '^[A-Za-z0-9._:-]{1,80}$') { throw 'invalid_request: Invalid request id' }
    if ($request.action -isnot [string] -or $request.action -notin @('status','type','key','stop','create_test_window','fixture_state','close_test_window','configure_capture','capture','click','scroll','drag')) { throw 'unsupported_action: Unsupported fixed action' }
    if ($null -eq $request.arguments) { $request | Add-Member -NotePropertyName arguments -NotePropertyValue ([PSCustomObject]@{}) -Force }
    $arguments = $request.arguments
    switch ($request.action) {
        {$_ -in @('status','stop','create_test_window','fixture_state','close_test_window')} { Assert-Properties $arguments @() }
        'type' {
            Assert-Properties $arguments @('text','expect','activate') @('text','expect')
            if ($arguments.text -isnot [string]) { throw 'invalid_text: text must be a string' }
            $null = [Xiaoruo1.UURemoteInputV1.Native]::BuildUnicode($arguments.text)
        }
        'key' {
            Assert-Properties $arguments @('keys','expect','activate') @('keys','expect')
            if ($arguments.keys -is [string]) { $names = @($arguments.keys -split '\+') }
            elseif ($arguments.keys -is [array] -and @($arguments.keys | Where-Object { $_ -isnot [string] }).Count -eq 0) { $names = @($arguments.keys) }
            else { throw 'invalid_keys: keys must be a string chord or string array' }
            $null = [Xiaoruo1.UURemoteInputV1.Native]::ParseKeys([string[]]$names)
        }
        'configure_capture' {
            Assert-Properties $arguments @('origin') @('origin')
            if ($arguments.origin -isnot [string]) { throw 'invalid_capture_origin: origin must be a string' }
            $null = [Xiaoruo1.UURemoteInputV1.Native]::ValidateCaptureOrigin($arguments.origin)
        }
        'capture' {
            Assert-Properties $arguments @('capture_id','upload_url','token') @('capture_id','upload_url','token')
            foreach ($name in @('capture_id','upload_url','token')) {
                if ($arguments.$name -isnot [string]) { throw 'invalid_capture_ticket: Ticket values must be strings' }
            }
            [Xiaoruo1.UURemoteInputV1.Native]::ValidateCaptureArguments($arguments.capture_id,$arguments.upload_url,$arguments.token)
        }
        {$_ -in @('click','scroll','drag')} {
            $allowed = switch ($request.action) {
                'click' { @('x','y','button','count','bounds') }
                'scroll' { @('x','y','vertical','horizontal','bounds') }
                'drag' { @('x','y','to_x','to_y','duration_ms','bounds') }
            }
            Assert-Properties $arguments $allowed $allowed
            $b = Get-MouseBounds $arguments.bounds
            $x = Get-PhysicalInteger $arguments.x 'x'; $y = Get-PhysicalInteger $arguments.y 'y'
            switch ($request.action) {
                'click' {
                    if ($arguments.button -isnot [string]) { throw 'invalid_button: button must be a string' }
                    $count = Get-PhysicalInteger $arguments.count 'count' 1 2
                    $null = [Xiaoruo1.UURemoteInputV1.Native]::BuildClick($x,$y,$arguments.button,$count,$b[0],$b[1],$b[2],$b[3])
                }
                'scroll' {
                    $v = Get-PhysicalInteger $arguments.vertical 'vertical' -12000 12000
                    $h = Get-PhysicalInteger $arguments.horizontal 'horizontal' -12000 12000
                    $null = [Xiaoruo1.UURemoteInputV1.Native]::BuildScroll($x,$y,$v,$h,$b[0],$b[1],$b[2],$b[3])
                }
                'drag' {
                    $toX = Get-PhysicalInteger $arguments.to_x 'to_x'; $toY = Get-PhysicalInteger $arguments.to_y 'to_y'
                    $duration = Get-PhysicalInteger $arguments.duration_ms 'duration_ms' 50 5000
                    $null = [Xiaoruo1.UURemoteInputV1.Native]::BuildDrag($x,$y,$toX,$toY,$duration,$b[0],$b[1],$b[2],$b[3])
                }
            }
        }
    }
    if ($request.action -in @('type','key')) {
        Assert-Properties $arguments.expect @('hwnd','pid','creation_time_utc_ticks') @('hwnd','pid','creation_time_utc_ticks')
        $null = Get-PositiveInteger $arguments.expect.hwnd 'hwnd'
        $null = Get-PositiveInteger $arguments.expect.pid 'pid' ([int]::MaxValue)
        if ($arguments.expect.creation_time_utc_ticks -isnot [string]) { throw 'invalid_request: creation_time_utc_ticks must be a decimal string' }
        $null = Get-PositiveInteger $arguments.expect.creation_time_utc_ticks 'creation_time_utc_ticks'
        if ('activate' -in @($arguments.PSObject.Properties.Name) -and $arguments.activate -isnot [bool]) { throw 'invalid_request: activate must be a boolean' }
    }
    return $request
}

function ConvertTo-CanonicalValue($Value) {
    if ($null -eq $Value) { return $null }
    if ($Value -is [string] -or $Value -is [bool]) { return $Value }
    if ($Value -is [System.Collections.IDictionary] -or $Value -is [PSCustomObject]) {
        $canonical = [ordered]@{}
        [string[]]$names = if ($Value -is [System.Collections.IDictionary]) { @($Value.Keys) } else { @($Value.PSObject.Properties.Name | Where-Object { $null -ne $_ }) }
        if ($names.Count -gt 1) { [Array]::Sort($names, [StringComparer]::Ordinal) }
        foreach ($name in $names) {
            $child = if ($Value -is [System.Collections.IDictionary]) { $Value[$name] } else { $Value.PSObject.Properties[$name].Value }
            $canonical[$name] = ConvertTo-CanonicalValue $child
        }
        return ,$canonical
    }
    if ($Value -is [array]) {
        $canonical = New-Object 'System.Collections.Generic.List[object]'
        foreach ($child in $Value) { $canonical.Add((ConvertTo-CanonicalValue $child)) }
        return ,($canonical.ToArray())
    }
    if ($Value -is [ValueType]) {
        # The validated protocol's only numeric fields are positive HWND/PID
        # integers. Normalize JSON 1 and 1.0 to the same canonical integer.
        try {
            $number = [decimal]$Value
            if ($number -ge [long]::MinValue -and $number -le [long]::MaxValue -and [decimal]::Truncate($number) -eq $number) { return [long]$number }
        } catch { }
        return $Value
    }
    throw 'invalid_request: Unsupported canonical JSON value'
}

function Get-PayloadHash($Request) {
    $canonical = ConvertTo-CanonicalValue $Request
    $json = ConvertTo-Json -InputObject $canonical -Depth 12 -Compress
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($json))
        return ([BitConverter]::ToString($digest)).Replace('-','').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function New-ReceiptCache([ValidateRange(1,256)][int]$Limit = 256) {
    return [PSCustomObject]@{
        Limit = $Limit
        Items = New-Object 'System.Collections.Generic.Dictionary[string,object]' ([StringComparer]::Ordinal)
        Order = New-Object 'System.Collections.Generic.List[string]'
    }
}

function Start-Receipt($Cache, $Request) {
    $key = [string]$Request.nonce + ':' + [string]$Request.reqid
    $hash = Get-PayloadHash $Request
    if ($Cache.Items.ContainsKey($key)) {
        $entry = $Cache.Items[$key]
        if (-not [string]::Equals($entry.Hash, $hash, [StringComparison]::Ordinal)) { throw 'request_id_conflict: This nonce/request id already identifies a different payload' }
        if ($entry.State -eq 'inflight') { throw 'request_inflight: This request is already executing; it was not executed again' }
        return [PSCustomObject]@{Kind='replay';Key=$key;Hash=$hash;Frame=$entry.Frame}
    }
    if ($Cache.Items.Count -ge $Cache.Limit) {
        $evicted = $false
        # Preserve first-arrival FIFO order. Inflight entries stay in place;
        # replays never refresh their position or evict an executing request.
        for ($index=0; $index -lt $Cache.Order.Count; $index++) {
            $candidate = $Cache.Order[$index]
            if ($Cache.Items[$candidate].State -eq 'terminal') {
                $null = $Cache.Items.Remove($candidate)
                $Cache.Order.RemoveAt($index)
                $evicted = $true
                break
            }
        }
        if (-not $evicted) { throw 'receipt_cache_busy: All receipt slots are inflight; the new request was not executed' }
    }
    $Cache.Items.Add($key, [PSCustomObject]@{Hash=$hash;State='inflight';Frame=$null})
    $Cache.Order.Add($key)
    return [PSCustomObject]@{Kind='new';Key=$key;Hash=$hash;Frame=$null}
}

function Complete-Receipt($Cache, $Ticket, [string]$Frame) {
    if ($null -eq $Ticket -or $Ticket.Kind -ne 'new' -or -not $Cache.Items.ContainsKey($Ticket.Key) -or [string]::IsNullOrEmpty($Frame)) { throw 'receipt_state_invalid: Cannot complete an unknown request receipt' }
    $entry = $Cache.Items[$Ticket.Key]
    if ($entry.State -ne 'inflight' -or $entry.Hash -ne $Ticket.Hash) { throw 'receipt_state_invalid: Receipt is no longer the matching inflight request' }
    $entry.Frame = $Frame
    $entry.State = 'terminal'
}

function Wait-EndpointExitFrame {
    # UU samples terminal screen differences. Only exit-boundary frames need
    # a brief opportunity to become visible before the next Clear or exit.
    # Self-test helpers never enter the endpoint and need no timing delay.
    if (-not $ProtocolSelfTest) { Start-Sleep -Milliseconds 350 }
}

function Write-Frame([string]$Reqid, [string]$Action, [bool]$Ok, $Result, $ErrorValue) {
    $payload = [ordered]@{ protocol=$protocolName; nonce=$Nonce; reqid=$Reqid; action=$Action; ok=$Ok }
    if ($Ok) { $payload.result = $Result } else { $payload.error = $ErrorValue }
    $json = ConvertTo-Json -InputObject $payload -Depth 10 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $base64 = [Convert]::ToBase64String($bytes)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $checksum = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
    # UU renders terminal screen differences. The complete JSON and checksum
    # must agree before a controller accepts a possibly repainted response.
    $script:lastProtocolFrame = $framePrefix + $base64 + '|' + $checksum + '|END'
    # This endpoint owns a dedicated, non-windowed UU terminal. Keep each
    # response at its top so UU's long scrollback diff cannot mix old rows.
    # Redirected test/output streams may not have a console buffer to clear.
    try { [Console]::Clear() } catch { }
    [Console]::WriteLine($script:lastProtocolFrame)
    [Console]::Out.Flush()
    if (($Action -eq 'stop' -and $Ok) -or $Action -eq 'stopped' -or ($Action -eq 'ready' -and -not $Ok)) {
        Wait-EndpointExitFrame
    }
}

function Start-EndpointRuntime(
    [scriptblock]$Proof = {
        Initialize-NativeTypes
        $null = [Xiaoruo1.UURemoteInputV1.Native]::Initialize($ExpectedComputer)
        $runtime = [Xiaoruo1.UURemoteInputV1.Native]::ValidateRuntime()
        $runtime.Add('input_size',[Xiaoruo1.UURemoteInputV1.Native]::InputSize)
        $runtime.Add('idle_seconds',$IdleSeconds)
        return $runtime
    },
    [scriptblock]$Cleanup = {
        if (([System.Management.Automation.PSTypeName]'Xiaoruo1.UURemoteInputV1.Fixture').Type) {
            $null = [Xiaoruo1.UURemoteInputV1.Fixture]::Close()
        }
    },
    [scriptblock]$Prepare = { }
) {
    # Proof and Cleanup are internal bootstrap hooks, never request actions.
    # Keeping this boundary separate allows failure-frame tests without loading
    # an interactive desktop or calling any input/GUI API.
    try {
        if ($Nonce -cnotmatch '^[A-Za-z0-9_-]{32,128}$') { throw 'nonce_required: Supply a unique valid nonce' }
        $runtime = & $Proof
        if ($null -eq $runtime) { throw 'runtime_proof_missing: Runtime proof did not return a result' }
        $null = & $Prepare
        Write-Frame '' 'ready' $true $runtime $null
        return $true
    } catch {
        $failure = $_.Exception
        while ($failure.InnerException) { $failure = $failure.InnerException }
        $code = if ($failure.Message -cmatch '^([a-z][a-z0-9_]{0,79}):') { $Matches[1] } else { 'remote_initialization_failed' }
        try { $null = & $Cleanup } catch { }
        # Never expose native/PowerShell exception text, process details or
        # source excerpts during initialization failure.
        try { Write-Frame '' 'ready' $false $null @{code=$code;message='The remote GUI input endpoint could not initialize'} } catch { }
        return $false
    }
}

if ($ProtocolSelfTest) {
    Initialize-NativeTypes
    $testNonce = '0123456789abcdef0123456789abcdef'
    $samples = @(
        @{nonce=$testNonce;reqid='s1';action='status';arguments=@{}},
        @{nonce=$testNonce;reqid='s2';action='type';arguments=@{text='ABC';expect=@{hwnd=10;pid=20;creation_time_utc_ticks='123'}}},
        @{nonce=$testNonce;reqid='s3';action='key';arguments=@{keys=@('CTRL','A');expect=@{hwnd=10;pid=20;creation_time_utc_ticks='123'}}}
    )
    foreach ($sample in $samples) {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($sample | ConvertTo-Json -Depth 8 -Compress))
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $checksum = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() }
        finally { $sha.Dispose() }
        $line = $requestPrefix + [Convert]::ToBase64String($bytes) + '|' + $checksum + '|END'
        $null = Get-Request $line $testNonce
    }
    $rejected = 0
    $badSamples = @(
        @{nonce='wrong';reqid='b1';action='status';arguments=@{}},
        @{nonce=$testNonce;reqid='b2';action='type';arguments=@{text='ABC'}},
        @{nonce=$testNonce;reqid='b3';action='execute';arguments=@{command='forbidden'}},
        @{nonce=$testNonce;reqid='b4';action='key';arguments=@{keys=@('CTRL','CONTROL');expect=@{hwnd=10;pid=20;creation_time_utc_ticks='123'}}},
        @{nonce=$testNonce;reqid='b5';action='type';arguments=@{text='ABC';expect=@{hwnd=10;pid=20;creation_time_utc_ticks='123'};activate='true'}}
    )
    foreach ($sample in $badSamples) {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($sample | ConvertTo-Json -Depth 8 -Compress))
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $checksum = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() }
        finally { $sha.Dispose() }
        $line = $requestPrefix + [Convert]::ToBase64String($bytes) + '|' + $checksum + '|END'
        try { $null = Get-Request $line $testNonce } catch { $rejected++ }
    }
    $expectedSize = if ([IntPtr]::Size -eq 8) { 40 } else { 28 }
    if ([Xiaoruo1.UURemoteInputV1.Native]::InputSize -ne $expectedSize -or $rejected -ne $badSamples.Count) { throw 'protocol_self_test_failed: Input layout or schema rejection mismatch' }
    $unicode = [Xiaoruo1.UURemoteInputV1.Native]::BuildUnicode(('A' + [char]0x4E2D + [char]0xD83D + [char]0xDE00))
    $newline = [Xiaoruo1.UURemoteInputV1.Native]::BuildUnicode("a`nb")
    $crlf = [Xiaoruo1.UURemoteInputV1.Native]::BuildUnicode("a`r`nb")
    $carriage = [Xiaoruo1.UURemoteInputV1.Native]::BuildUnicode("a`rb")
    if ($newline.Length -ne 6 -or $crlf.Length -ne 6 -or $carriage.Length -ne 6 -or
        $newline[2].data.keyboard.wVk -ne 13 -or $newline[3].data.keyboard.wVk -ne 13 -or
        $newline[2].data.keyboard.wScan -ne 0 -or $newline[3].data.keyboard.wScan -ne 0 -or
        $newline[2].data.keyboard.dwFlags -ne 0 -or $newline[3].data.keyboard.dwFlags -ne 2 -or
        $crlf[2].data.keyboard.wVk -ne 13 -or $crlf[3].data.keyboard.wVk -ne 13 -or
        $carriage[2].data.keyboard.wVk -ne 13 -or $carriage[3].data.keyboard.wVk -ne 13) {
        throw 'protocol_self_test_failed: Newline must produce one virtual Return down/up pair'
    }
    $keys = [Xiaoruo1.UURemoteInputV1.Native]::ParseKeys([string[]]@('CTRL','A'))
    $chord = [Xiaoruo1.UURemoteInputV1.Native]::BuildChord($keys)
    $protectedConsole = [Xiaoruo1.UURemoteInputV1.Native]::IsProtectedTransportWindow('xru_input_selftest','ConsoleWindowClass','conhost')
    $allowedFixture = -not [Xiaoruo1.UURemoteInputV1.Native]::IsProtectedTransportWindow('Xiaoruo1 UU GUI Input Test selftest','WindowsForms10.Window','powershell')
    $attachPlan = [Xiaoruo1.UURemoteInputV1.Native]::BuildAttachThreads(11,22,33)
    if (-not $protectedConsole -or -not $allowedFixture -or ($attachPlan -join ',') -ne '22,33') {
        throw 'protocol_self_test_failed: Transport protection or remote queue plan mismatch'
    }
    [ordered]@{ok=$true;protocol=$protocolName;input_size=[Xiaoruo1.UURemoteInputV1.Native]::InputSize;union_offset=[Xiaoruo1.UURemoteInputV1.Native]::UnionOffset;valid_schema_samples=$samples.Count;rejected_schema_samples=$rejected;unicode_events=$unicode.Length;chord_events=$chord.Length;newline_events=$newline.Length;crlf_events=$crlf.Length;newline_virtual_key=[int]$newline[2].data.keyboard.wVk;newline_flags=@([int]$newline[2].data.keyboard.dwFlags,[int]$newline[3].data.keyboard.dwFlags);no_input_sent=$true;no_gui_created=$true} | ConvertTo-Json -Compress
    return
}

if (-not (Start-EndpointRuntime -Prepare {
    $script:reader = New-Object Xiaoruo1.UURemoteInputV1.LineReader
    $script:receipts = New-ReceiptCache
    $script:idle = [Diagnostics.Stopwatch]::StartNew()
    $script:stopping = $false
})) { return }
try {
    while (-not $stopping -and $idle.Elapsed.TotalSeconds -lt $IdleSeconds) {
        try { $line = $reader.Next(500) }
        catch { if ($_.Exception.ToString() -match 'EndOfStreamException') { break }; throw }
        if ($null -eq $line) { continue }
        $request = $null
        $ticket = $null
        try {
            $request = Get-Request $line $Nonce
            $idle.Restart()
            $ticket = Start-Receipt $receipts $request
            if ($ticket.Kind -eq 'replay') {
                try { [Console]::Clear() } catch { }
                [Console]::WriteLine($ticket.Frame)
                [Console]::Out.Flush()
                continue
            }
            $arguments = $request.arguments
            $result = $null
            switch ($request.action) {
                'status' {
                    $result = [Xiaoruo1.UURemoteInputV1.Native]::ValidateRuntime()
                    $result.Add('foreground',[Xiaoruo1.UURemoteInputV1.Native]::Foreground())
                }
                'type' {
                    $result = [Xiaoruo1.UURemoteInputV1.Native]::TypeText($arguments.text,[long]$arguments.expect.hwnd,[int]$arguments.expect.pid,[string]$arguments.expect.creation_time_utc_ticks,[bool]$arguments.activate)
                }
                'key' {
                    $names = if ($arguments.keys -is [string]) { @($arguments.keys -split '\+') } else { @($arguments.keys) }
                    $result = [Xiaoruo1.UURemoteInputV1.Native]::KeyChord([string[]]$names,[long]$arguments.expect.hwnd,[int]$arguments.expect.pid,[string]$arguments.expect.creation_time_utc_ticks,[bool]$arguments.activate)
                }
                'configure_capture' {
                    $null = [Xiaoruo1.UURemoteInputV1.Native]::ValidateRuntime()
                    $result = [Xiaoruo1.UURemoteInputV1.Native]::ConfigureCapture($arguments.origin)
                }
                'capture' { $result = [Xiaoruo1.UURemoteInputV1.Native]::Capture($arguments.capture_id,$arguments.upload_url,$arguments.token) }
                'click' {
                    $b = Get-MouseBounds $arguments.bounds
                    $result = [Xiaoruo1.UURemoteInputV1.Native]::Click($arguments.x,$arguments.y,$arguments.button,$arguments.count,$b[0],$b[1],$b[2],$b[3])
                }
                'scroll' {
                    $b = Get-MouseBounds $arguments.bounds
                    $result = [Xiaoruo1.UURemoteInputV1.Native]::Scroll($arguments.x,$arguments.y,$arguments.vertical,$arguments.horizontal,$b[0],$b[1],$b[2],$b[3])
                }
                'drag' {
                    $b = Get-MouseBounds $arguments.bounds
                    $result = [Xiaoruo1.UURemoteInputV1.Native]::Drag($arguments.x,$arguments.y,$arguments.to_x,$arguments.to_y,$arguments.duration_ms,$b[0],$b[1],$b[2],$b[3])
                }
                'create_test_window' { $result = [Xiaoruo1.UURemoteInputV1.Fixture]::Create($Nonce.Substring(0,8)) }
                'fixture_state' { $result = [Xiaoruo1.UURemoteInputV1.Fixture]::State() }
                'close_test_window' { $result = @{closed=[Xiaoruo1.UURemoteInputV1.Fixture]::Close()} }
                'stop' { $result = @{stopping=$true;fixture_closed=[Xiaoruo1.UURemoteInputV1.Fixture]::Close()}; $stopping=$true }
            }
            Write-Frame $request.reqid $request.action $true $result $null
            Complete-Receipt $receipts $ticket $script:lastProtocolFrame
        } catch {
            $message = $_.Exception.Message
            if ($_.Exception.InnerException) { $message = $_.Exception.InnerException.Message }
            $code = if ($message -match '^([a-z][a-z0-9_]{0,79}):') { $Matches[1] } else { 'remote_input_failed' }
            $reqid = if ($request) { $request.reqid } else { '' }
            $action = if ($request) { $request.action } else { 'protocol_error' }
            Write-Frame $reqid $action $false @{ } @{code=$code;message=$message}
            # Input errors include unknown/partial delivery. Retain that exact
            # failure receipt so retrying its id cannot insert another batch.
            if ($null -ne $ticket -and $ticket.Kind -eq 'new' -and $receipts.Items[$ticket.Key].State -eq 'inflight') {
                Complete-Receipt $receipts $ticket $script:lastProtocolFrame
            }
        }
    }
} finally {
    $closed = [Xiaoruo1.UURemoteInputV1.Fixture]::Close()
    Write-Frame '' 'stopped' $true @{fixture_closed=$closed;reason=$(if($stopping){'stop'}elseif($idle.Elapsed.TotalSeconds -ge $IdleSeconds){'idle_timeout'}else{'stdin_closed'})} $null
}
