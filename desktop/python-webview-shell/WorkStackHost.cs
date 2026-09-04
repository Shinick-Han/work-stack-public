// Work Stack same-process desktop host.
//
// This is the branded x64 WorkStack.exe that lives at the installation root and
// runs the EXISTING CPython desktop entry inside its own STA process through one
// direct Py_Main call.  It is deliberately not a launcher: it never spawns
// pythonw.exe, never falls back to another interpreter, and offers no arbitrary
// -c/-m entrypoint.  The CLI, server, maintenance, backup and
// --check-remote-connection paths keep running on the bundled console Python and
// are not touched by this file.
//
// Everything the host may run is derived from its own image path.  The current
// directory, PATH, an unrelated assembly directory and any configuration file are
// never consulted to choose an executable or a script.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;

internal static class WorkStackHost
{
    // Relative to the installation root, both fixed by the packaging layout.
    private const string HostImageName = "WorkStack.exe";
    private const string DesktopScriptRelative = @"desktop\python-webview-shell\workstack_desktop.py";
    private const string PythonDllRelative = @"runtime\python312.dll";

    private const uint LoadLibrarySearchDllLoadDir = 0x00000100;
    private const uint LoadLibrarySearchSystem32 = 0x00000800;

    private const string DialogTitle = "Work Stack launch failure";
    private const int MessageBoxIconError = 0x00000010;

    // The GUI-only options the existing Python entry already accepts. The host
    // validates their shape and forwards the value unchanged; it never reinterprets
    // what they mean.
    private static readonly Dictionary<string, ArgumentKind> AllowedOptions =
        new Dictionary<string, ArgumentKind>(StringComparer.Ordinal)
        {
            { "--install-root", ArgumentKind.InstallRoot },
            { "--state-root", ArgumentKind.StateRoot },
            { "--url", ArgumentKind.Opaque },
            { "--probe-provider", ArgumentKind.ProbeProvider },
            { "--probe-result", ArgumentKind.AbsolutePath },
            { "--auto-close-seconds", ArgumentKind.Integer },
        };

    private static readonly string[] AllowedProbeProviders = { "outlook", "teams", "onenote" };

    private enum ArgumentKind
    {
        Opaque,
        InstallRoot,
        StateRoot,
        AbsolutePath,
        ProbeProvider,
        Integer,
    }

    [DllImport("python312.dll", CallingConvention = CallingConvention.Cdecl, ExactSpelling = true)]
    private static extern int Py_Main(int argc, IntPtr argv);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern IntPtr LoadLibraryExW(string name, IntPtr reserved, uint flags);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern uint GetModuleFileNameW(IntPtr module, StringBuilder name, int size);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern int MessageBoxW(IntPtr owner, string text, string caption, uint type);

    /// <summary>The thread that calls Py_Main is the GUI thread; it must be STA.</summary>
    [STAThread]
    private static int Main(string[] args)
    {
        string phase = "startup";
        IntPtr argvBlock = IntPtr.Zero;
        List<IntPtr> originalStrings = new List<IntPtr>();
        try
        {
            phase = "resolve-image";
            string hostImage = ResolveHostImage();
            string installRoot = Path.GetDirectoryName(hostImage);
            if (string.IsNullOrEmpty(installRoot))
            {
                throw new HostStartupException("the installation root could not be derived from the host image");
            }

            phase = "resolve-payload";
            string desktopScript = RequirePayloadLeaf(
                installRoot, Path.Combine(installRoot, DesktopScriptRelative), "desktop entry");
            string pythonDll = RequirePayloadLeaf(
                installRoot, Path.Combine(installRoot, PythonDllRelative), "Python runtime library");

            phase = "parse-arguments";
            string[] desktopArguments = BuildDesktopArguments(args, installRoot, desktopScript);

            phase = "load-runtime";
            IntPtr loaded = LoadLibraryExW(
                pythonDll, IntPtr.Zero, LoadLibrarySearchDllLoadDir | LoadLibrarySearchSystem32);
            if (loaded == IntPtr.Zero)
            {
                throw new HostStartupException(
                    "the packaged Python runtime could not be loaded", Marshal.GetLastWin32Error());
            }

            phase = "verify-runtime";
            StringBuilder moduleName = new StringBuilder(32768);
            uint copied = GetModuleFileNameW(loaded, moduleName, moduleName.Capacity);
            if (copied == 0 || copied >= moduleName.Capacity)
            {
                throw new HostStartupException(
                    "the loaded Python runtime path could not be verified", Marshal.GetLastWin32Error());
            }
            if (!SamePath(moduleName.ToString(), pythonDll))
            {
                // A different python312.dll answered the load: refuse rather than
                // run the desktop against an unverified runtime.
                throw new HostStartupException("a different Python runtime was loaded than the packaged one");
            }

            phase = "build-argv";
            // argv[0] is the host image, exactly as a Python launcher would report
            // itself; -B keeps the installation free of bytecode writes.
            List<string> pythonArgs = new List<string> { hostImage, "-B", desktopScript };
            pythonArgs.AddRange(desktopArguments);

            argvBlock = Marshal.AllocHGlobal(checked((pythonArgs.Count + 1) * IntPtr.Size));
            // The bookkeeping capacity is reserved BEFORE the first unmanaged
            // allocation. Growing the list between StringToHGlobalUni and Add would
            // allocate, and a failure there would lose the pointer with no owner.
            originalStrings.Capacity = pythonArgs.Count;
            for (int index = 0; index < pythonArgs.Count; index++)
            {
                IntPtr pointer = Marshal.StringToHGlobalUni(pythonArgs[index]);
                originalStrings.Add(pointer);
                Marshal.WriteIntPtr(argvBlock, index * IntPtr.Size, pointer);
            }
            Marshal.WriteIntPtr(argvBlock, pythonArgs.Count * IntPtr.Size, IntPtr.Zero);

            phase = "run";
            // Py_Main owns initialization and finalization. There is no
            // Py_Initialize/Py_Finalize wrapper, no second Py_Main and no manual
            // unload: the real desktop entry ends in raise SystemExit(main()), and a
            // CPython process-exit path may never return here at all. That is
            // expected, and the absence of a managed epilogue is not a failure.
            return Py_Main(pythonArgs.Count, argvBlock);
        }
        catch (HostStartupException error)
        {
            return FailVisibly(phase, error.Message, error.NativeErrorCode);
        }
        catch (Exception error)
        {
            // Only the exception type is reported: messages can carry paths.
            return FailVisibly(phase, error.GetType().Name, 0);
        }
        finally
        {
            // Py_Main may reorder the vector, so free the saved originals rather
            // than reading pointers back out of argvBlock. The runtime library is
            // deliberately not unloaded while CLR consumers may still reference it.
            foreach (IntPtr pointer in originalStrings)
            {
                Marshal.FreeHGlobal(pointer);
            }
            if (argvBlock != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(argvBlock);
            }
        }
    }

    private static string ResolveHostImage()
    {
        // The process image, never the working directory, PATH or a caller-supplied
        // root.
        StringBuilder buffer = new StringBuilder(32768);
        uint copied = GetModuleFileNameW(IntPtr.Zero, buffer, buffer.Capacity);
        if (copied == 0 || copied >= buffer.Capacity)
        {
            throw new HostStartupException("the host image path could not be resolved", Marshal.GetLastWin32Error());
        }
        string image = NormalizeExisting(buffer.ToString(), "host image");
        // The host only ever runs as the installed WorkStack.exe. A renamed or
        // copied image is refused rather than allowed to derive a root of its own.
        if (!string.Equals(Path.GetFileName(image), HostImageName, StringComparison.OrdinalIgnoreCase))
        {
            throw new HostStartupException("the host image is not the installed Work Stack executable");
        }
        string root = Path.GetDirectoryName(image);
        if (string.IsNullOrEmpty(root))
        {
            throw new HostStartupException("the installation root could not be derived from the host image");
        }
        RejectReparseDirectory(root, "installation root");
        return image;
    }

    private static void RejectReparseDirectory(string directory, string description)
    {
        DirectoryInfo info = new DirectoryInfo(directory);
        if (!info.Exists)
        {
            throw new HostStartupException("the " + description + " is missing");
        }
        if ((info.Attributes & FileAttributes.ReparsePoint) == FileAttributes.ReparsePoint)
        {
            throw new HostStartupException("the " + description + " is a reparse point");
        }
    }

    /// <summary>
    /// True when <paramref name="candidate"/> lies inside <paramref name="root"/> on a
    /// segment boundary. A shared textual prefix such as C:\WorkStackOther is not
    /// containment.
    /// </summary>
    private static bool IsContained(string root, string candidate)
    {
        // TrimTrailingSeparator deliberately keeps a drive root as C:\, so the
        // separator is appended only when the root does not already end with one.
        // Otherwise C:\ would become C:\\ and never match C:\runtime\...
        string trimmed = TrimTrailingSeparator(root);
        string prefix = trimmed.EndsWith(Path.DirectorySeparatorChar.ToString(), StringComparison.Ordinal)
            ? trimmed
            : trimmed + Path.DirectorySeparatorChar;
        return candidate.StartsWith(prefix, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>
    /// Rejects a reparse point anywhere between the installation root and the leaf.
    /// Checking only the leaf leaves a redirected ancestor able to move the desktop
    /// script or the runtime outside the installation.
    /// </summary>
    private static void RejectReparseAncestors(string root, string leaf, string description)
    {
        DirectoryInfo current = new DirectoryInfo(Path.GetDirectoryName(leaf));
        string stop = TrimTrailingSeparator(root);
        while (current != null)
        {
            if ((current.Attributes & FileAttributes.ReparsePoint) == FileAttributes.ReparsePoint)
            {
                throw new HostStartupException("the " + description + " is reached through a reparse point");
            }
            if (string.Equals(TrimTrailingSeparator(current.FullName), stop, StringComparison.OrdinalIgnoreCase))
            {
                return;
            }
            current = current.Parent;
        }
        throw new HostStartupException("the " + description + " is outside the installation");
    }

    private static string[] BuildDesktopArguments(string[] args, string installRoot, string desktopScript)
    {
        List<string> forwarded = new List<string>();
        HashSet<string> seen = new HashSet<string>(StringComparer.Ordinal);
        int index = 0;

        // The installed shortcuts and the updater pass the desktop script first.
        // Zero arguments is equally valid and selects the same entry with defaults.
        if (args.Length > 0 && !args[0].StartsWith("--", StringComparison.Ordinal))
        {
            if (!SamePath(NormalizeShape(args[0], "leading argument"), desktopScript))
            {
                throw new HostStartupException("only the packaged desktop entry may be launched");
            }
            index = 1;
        }

        for (; index < args.Length; index++)
        {
            string option = args[index];
            ArgumentKind kind;
            if (!AllowedOptions.TryGetValue(option, out kind))
            {
                // No alternate positional script, no "--" separator accepting another
                // script, no -c, -m or arbitrary Python switch, and no
                // --check-remote-connection: that stays a console command.
                throw new HostStartupException("an unsupported desktop option was supplied");
            }
            if (!seen.Add(option))
            {
                throw new HostStartupException("a desktop option was supplied more than once");
            }
            if (index + 1 >= args.Length)
            {
                throw new HostStartupException("a desktop option was supplied without a value");
            }
            string value = args[++index];
            forwarded.Add(option);
            forwarded.Add(ValidateOptionValue(kind, value, installRoot));
        }
        return forwarded.ToArray();
    }

    private static string ValidateOptionValue(ArgumentKind kind, string value, string installRoot)
    {
        switch (kind)
        {
            case ArgumentKind.InstallRoot:
                {
                    string supplied = NormalizeShape(value, "install root");
                    if (!SamePath(supplied, installRoot))
                    {
                        // An explicit root must agree with the image-derived root; the
                        // host never searches another installation.
                        throw new HostStartupException("the supplied install root does not match this installation");
                    }
                    return supplied;
                }
            case ArgumentKind.StateRoot:
                // A legitimate separate user state directory that need not exist yet.
                // When absent the existing Python default under LOCALAPPDATA applies.
                return NormalizeShape(value, "state root");
            case ArgumentKind.AbsolutePath:
                return NormalizeShape(value, "probe result path");
            case ArgumentKind.ProbeProvider:
                foreach (string provider in AllowedProbeProviders)
                {
                    if (string.Equals(value, provider, StringComparison.Ordinal))
                    {
                        return value;
                    }
                }
                throw new HostStartupException("an unsupported probe provider was supplied");
            case ArgumentKind.Integer:
                {
                    // Shape only, and deliberately NOT narrowed to ASCII digits:
                    // Python's int() accepts surrounding whitespace, an optional
                    // sign, single underscores between digits, and any Unicode
                    // decimal digit. The admitted string is forwarded verbatim, so
                    // type=int and the existing clamping keep their meaning and a
                    // value beyond Int32 is neither rejected nor rewritten.
                    if (!LooksLikePythonInteger(value))
                    {
                        throw new HostStartupException("a non-integer auto-close value was supplied");
                    }
                    return value;
                }
            default:
                RejectControl(value, "option value");
                return value;
        }
    }

    /// <summary>
    /// Decimal-scalar ranges taken from the PINNED interpreter's own table:
    /// CPython 3.12.10, unicodedata 15.0.0, every code point for which
    /// unicodedata.decimal() is defined, collapsed to inclusive ranges. It is used
    /// instead of the framework's Unicode tables because those are older and
    /// classify by UTF-16 char, which cannot see supplementary digits such as
    /// Adlam U+1E950 or Mathematical U+1D7CE that int() accepts.
    /// </summary>
    private static readonly int[] PythonDecimalScalars =
    {
        0x30, 0x39,
        0x660, 0x669,
        0x6F0, 0x6F9,
        0x7C0, 0x7C9,
        0x966, 0x96F,
        0x9E6, 0x9EF,
        0xA66, 0xA6F,
        0xAE6, 0xAEF,
        0xB66, 0xB6F,
        0xBE6, 0xBEF,
        0xC66, 0xC6F,
        0xCE6, 0xCEF,
        0xD66, 0xD6F,
        0xDE6, 0xDEF,
        0xE50, 0xE59,
        0xED0, 0xED9,
        0xF20, 0xF29,
        0x1040, 0x1049,
        0x1090, 0x1099,
        0x17E0, 0x17E9,
        0x1810, 0x1819,
        0x1946, 0x194F,
        0x19D0, 0x19D9,
        0x1A80, 0x1A89,
        0x1A90, 0x1A99,
        0x1B50, 0x1B59,
        0x1BB0, 0x1BB9,
        0x1C40, 0x1C49,
        0x1C50, 0x1C59,
        0xA620, 0xA629,
        0xA8D0, 0xA8D9,
        0xA900, 0xA909,
        0xA9D0, 0xA9D9,
        0xA9F0, 0xA9F9,
        0xAA50, 0xAA59,
        0xABF0, 0xABF9,
        0xFF10, 0xFF19,
        0x104A0, 0x104A9,
        0x10D30, 0x10D39,
        0x11066, 0x1106F,
        0x110F0, 0x110F9,
        0x11136, 0x1113F,
        0x111D0, 0x111D9,
        0x112F0, 0x112F9,
        0x11450, 0x11459,
        0x114D0, 0x114D9,
        0x11650, 0x11659,
        0x116C0, 0x116C9,
        0x11730, 0x11739,
        0x118E0, 0x118E9,
        0x11950, 0x11959,
        0x11C50, 0x11C59,
        0x11D50, 0x11D59,
        0x11DA0, 0x11DA9,
        0x11F50, 0x11F59,
        0x16A60, 0x16A69,
        0x16AC0, 0x16AC9,
        0x16B50, 0x16B59,
        0x1D7CE, 0x1D7FF,
        0x1E140, 0x1E149,
        0x1E2F0, 0x1E2F9,
        0x1E4F0, 0x1E4F9,
        0x1E950, 0x1E959,
        0x1FBF0, 0x1FBF9
    };

    /// <summary>
    /// Consumes one code point at <paramref name="index"/>, advancing past a
    /// surrogate pair, and reports whether it is a Python decimal scalar.
    /// </summary>
    private static bool IsPythonDecimalScalar(string value, ref int index)
    {
        int scalar = char.ConvertToUtf32(value, index);
        if (char.IsHighSurrogate(value[index]))
        {
            index++;
        }
        for (int pair = 0; pair < PythonDecimalScalars.Length; pair += 2)
        {
            if (scalar >= PythonDecimalScalars[pair] && scalar <= PythonDecimalScalars[pair + 1])
            {
                return true;
            }
        }
        return false;
    }

    /// <summary>
    /// Mirrors the shape CPython's int() accepts for a base-10 string: optional
    /// surrounding whitespace, an optional sign, Unicode decimal digits, and single
    /// underscores only between digits. It deliberately does not convert, so no
    /// Int32 range or ASCII-only rule is imposed on Python's own parser.
    /// </summary>
    private static bool LooksLikePythonInteger(string value)
    {
        if (value == null)
        {
            return false;
        }
        string trimmed = value.Trim();
        for (int scan = 0; scan < trimmed.Length; scan++)
        {
            // An unpaired surrogate is not a scalar value; refuse before decoding.
            if (char.IsHighSurrogate(trimmed[scan]))
            {
                if (scan + 1 >= trimmed.Length || !char.IsLowSurrogate(trimmed[scan + 1]))
                {
                    return false;
                }
                scan++;
            }
            else if (char.IsLowSurrogate(trimmed[scan]))
            {
                return false;
            }
        }
        int index = 0;
        if (index < trimmed.Length && (trimmed[index] == '+' || trimmed[index] == '-'))
        {
            index++;
        }
        bool previousWasDigit = false;
        bool sawDigit = false;
        for (; index < trimmed.Length; index++)
        {
            char current = trimmed[index];
            if (current == '_')
            {
                // An underscore must sit between digits, never lead, trail or double.
                if (!previousWasDigit)
                {
                    return false;
                }
                previousWasDigit = false;
                continue;
            }
            if (!IsPythonDecimalScalar(trimmed, ref index))
            {
                return false;
            }
            previousWasDigit = true;
            sawDigit = true;
        }
        return sawDigit && previousWasDigit;
    }

    private static string NormalizeShape(string value, string description)
    {
        RejectControl(value, description);

        // The INPUT must already be fully absolute. Calling GetFullPath first would
        // silently resolve a relative or drive-relative path against the current
        // directory, after which a rootedness check can never refuse it, so nothing
        // the host runs may depend on the working directory.
        if (!IsFullyAbsolute(value))
        {
            throw new HostStartupException("a supplied " + description + " is not an absolute path");
        }
        string full;
        try
        {
            full = Path.GetFullPath(value);
        }
        catch (Exception)
        {
            throw new HostStartupException("a supplied " + description + " is not a usable path");
        }
        return TrimTrailingSeparator(full);
    }

    /// <summary>
    /// True only for a rooted drive path such as C:\x or a UNC path. A
    /// drive-relative C:foo is rooted by Path.IsPathRooted but resolves against the
    /// process working directory, so it is rejected here.
    /// </summary>
    private static bool IsFullyAbsolute(string value)
    {
        if (string.IsNullOrEmpty(value))
        {
            return false;
        }
        if (value.StartsWith(@"\\", StringComparison.Ordinal))
        {
            return true;
        }
        if (value.Length < 3 || value[1] != ':')
        {
            return false;
        }
        char drive = value[0];
        bool isDriveLetter = (drive >= 'A' && drive <= 'Z') || (drive >= 'a' && drive <= 'z');
        return isDriveLetter && (value[2] == '\\' || value[2] == '/');
    }

    /// <summary>
    /// Trims one trailing separator without turning a drive root into a
    /// drive-relative path: C:\ stays C:\, while C:\x\ becomes C:\x.
    /// </summary>
    private static string TrimTrailingSeparator(string value)
    {
        if (value.Length <= 3)
        {
            return value;
        }
        return value.TrimEnd(Path.DirectorySeparatorChar);
    }

    private static string NormalizeExisting(string value, string description)
    {
        string full = NormalizeShape(value, description);
        FileInfo info = new FileInfo(full);
        if (!info.Exists)
        {
            throw new HostStartupException("the " + description + " is missing");
        }
        // The image leaf is verified the same way a critical payload leaf is: a
        // reparse point here would let the installation root be derived elsewhere.
        if ((info.Attributes & FileAttributes.ReparsePoint) == FileAttributes.ReparsePoint)
        {
            throw new HostStartupException("the " + description + " is a reparse point");
        }
        return full;
    }

    private static string RequirePayloadLeaf(string installRoot, string candidate, string description)
    {
        string full = NormalizeShape(candidate, description);
        if (!IsContained(installRoot, full))
        {
            throw new HostStartupException("the " + description + " is outside the installation");
        }
        FileInfo info = new FileInfo(full);
        if (!info.Exists)
        {
            throw new HostStartupException("the " + description + " is missing");
        }
        if ((info.Attributes & FileAttributes.ReparsePoint) == FileAttributes.ReparsePoint)
        {
            throw new HostStartupException("the " + description + " is a reparse point");
        }
        RejectReparseAncestors(installRoot, full, description);
        return full;
    }

    private static void RejectControl(string value, string description)
    {
        if (value == null)
        {
            throw new HostStartupException("a supplied " + description + " is missing");
        }
        if (value.IndexOf('\0') >= 0)
        {
            throw new HostStartupException("a supplied " + description + " contains a NUL character");
        }
    }

    private static bool SamePath(string left, string right)
    {
        // Segment-aware, case-insensitive comparison: a prefix must end on a
        // separator boundary, never mid-segment.
        string a = TrimTrailingSeparator(left ?? string.Empty);
        string b = TrimTrailingSeparator(right ?? string.Empty);
        return string.Equals(a, b, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>
    /// Pre-Python failures only. The existing Python startup log and dialog remain
    /// the reporting path once CPython is running; this covers the window before
    /// that, and records no command line, state, URL, token or path-bearing message.
    /// </summary>
    private static int FailVisibly(string phase, string reason, int nativeErrorCode)
    {
        string sanitized = "Work Stack could not start (phase: " + phase + "; " + reason
            + (nativeErrorCode != 0 ? "; code " + nativeErrorCode.ToString(CultureInfo.InvariantCulture) : string.Empty)
            + ").";
        string logPath = TryRecordDiagnostic(sanitized);
        string dialog = logPath == null
            ? sanitized
            : sanitized + Environment.NewLine + "Details were written to " + logPath + ".";
        try
        {
            // A logging failure must never suppress the dialog or the exit code.
            MessageBoxW(IntPtr.Zero, dialog, DialogTitle, MessageBoxIconError);
        }
        catch (Exception)
        {
        }
        return 1;
    }

    private static string TryRecordDiagnostic(string sanitized)
    {
        try
        {
            // An explicit contained LOCALAPPDATA is honoured exactly as supplied. If
            // it is present but invalid, logging is skipped rather than escaping the
            // supplied containment through the known folder; the caller still shows
            // the dialog and still exits non-zero. The known folder is the fallback
            // ONLY when the variable is absent.
            string supplied = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            string localAppData;
            if (supplied == null)
            {
                localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            }
            else if (IsFullyAbsolute(supplied))
            {
                localAppData = supplied;
            }
            else
            {
                return null;
            }
            if (string.IsNullOrEmpty(localAppData) || !IsFullyAbsolute(localAppData))
            {
                return null;
            }
            string directory = Path.Combine(Path.Combine(localAppData, "WorkStack"), "logs");
            Directory.CreateDirectory(directory);
            string path = Path.Combine(directory, "desktop-startup.log");
            string line = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture)
                + " host " + sanitized + Environment.NewLine;
            File.AppendAllText(path, line, new UTF8Encoding(false));
            return path;
        }
        catch (Exception)
        {
            // Deliberately swallowed: the caller still shows the dialog and exits
            // non-zero, and no apparent success is reported.
            return null;
        }
    }

    private sealed class HostStartupException : Exception
    {
        internal HostStartupException(string message)
            : this(message, 0)
        {
        }

        internal HostStartupException(string message, int nativeErrorCode)
            : base(message)
        {
            NativeErrorCode = nativeErrorCode;
        }

        internal int NativeErrorCode { get; private set; }
    }
}
