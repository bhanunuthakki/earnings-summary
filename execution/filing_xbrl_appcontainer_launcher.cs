// Windows AppContainer launcher for the qualified offline filing-XBRL runtime.
// Build with the .NET Framework C# compiler; the resulting executable is hash
// locked in config/filing_xbrl_processor_bundle.json.

using System;
using System.Collections.Generic;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;

internal static class FilingXbrlAppContainerLauncher
{
    private const string Contract = "earnings-xbrl-os-sandbox.v1";
    private const string ProfileName = "EarningsSummaryFilingXbrlV1";
    private const string MutexName = "Local\\EarningsSummaryFilingXbrlLauncherV1";
    private const int ErrorAlreadyExistsHresult = unchecked((int)0x800700B7);
    private const uint ExtendedStartupInfoPresent = 0x00080000;
    private const uint CreateNoWindow = 0x08000000;
    private const uint CreateSuspended = 0x00000004;
    private const uint StartfUseStdHandles = 0x00000100;
    private const int StdInputHandle = -10;
    private const int StdOutputHandle = -11;
    private const int StdErrorHandle = -12;
    private const int TokenIsAppContainer = 29;
    private const uint TokenQuery = 0x0008;
    private const uint Infinite = 0xFFFFFFFF;
    private const uint HandleFlagInherit = 0x00000001;
    private const uint JobObjectLimitKillOnJobClose = 0x00002000;
    private const int MaximumInputBytes = 64 * 1024 * 1024;
    private static readonly IntPtr ProcThreadAttributeHandleList = new IntPtr(0x00020002);
    private static readonly IntPtr ProcThreadAttributeSecurityCapabilities =
        new IntPtr(0x00020009);

    private sealed class Invocation
    {
        internal string RuntimeRoot = string.Empty;
        internal string InputRoot = string.Empty;
        internal readonly List<string> Command = new List<string>();
    }

    private sealed class AccessGrant
    {
        internal string Path = string.Empty;
        internal FileSystemAccessRule Rule = null;
    }

    private sealed class LauncherWin32Exception : System.ComponentModel.Win32Exception
    {
        internal readonly string Stage;

        internal LauncherWin32Exception(int errorCode, string stage)
            : base(errorCode, stage)
        {
            Stage = stage;
        }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct StartupInfo
    {
        internal int cb;
        internal string lpReserved;
        internal string lpDesktop;
        internal string lpTitle;
        internal uint dwX;
        internal uint dwY;
        internal uint dwXSize;
        internal uint dwYSize;
        internal uint dwXCountChars;
        internal uint dwYCountChars;
        internal uint dwFillAttribute;
        internal uint dwFlags;
        internal short wShowWindow;
        internal short cbReserved2;
        internal IntPtr lpReserved2;
        internal IntPtr hStdInput;
        internal IntPtr hStdOutput;
        internal IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct StartupInfoEx
    {
        internal StartupInfo StartupInfo;
        internal IntPtr lpAttributeList;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessInformation
    {
        internal IntPtr hProcess;
        internal IntPtr hThread;
        internal uint dwProcessId;
        internal uint dwThreadId;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct SecurityCapabilities
    {
        internal IntPtr AppContainerSid;
        internal IntPtr Capabilities;
        internal uint CapabilityCount;
        internal uint Reserved;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct SecurityAttributes
    {
        internal int nLength;
        internal IntPtr lpSecurityDescriptor;
        [MarshalAs(UnmanagedType.Bool)]
        internal bool bInheritHandle;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimitInformation
    {
        internal long PerProcessUserTimeLimit;
        internal long PerJobUserTimeLimit;
        internal uint LimitFlags;
        internal UIntPtr MinimumWorkingSetSize;
        internal UIntPtr MaximumWorkingSetSize;
        internal uint ActiveProcessLimit;
        internal UIntPtr Affinity;
        internal uint PriorityClass;
        internal uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        internal ulong ReadOperationCount;
        internal ulong WriteOperationCount;
        internal ulong OtherOperationCount;
        internal ulong ReadTransferCount;
        internal ulong WriteTransferCount;
        internal ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimitInformation
    {
        internal BasicLimitInformation BasicLimitInformation;
        internal IoCounters IoInfo;
        internal UIntPtr ProcessMemoryLimit;
        internal UIntPtr JobMemoryLimit;
        internal UIntPtr PeakProcessMemoryUsed;
        internal UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
    private static extern int CreateAppContainerProfile(
        string appContainerName,
        string displayName,
        string description,
        IntPtr capabilities,
        uint capabilityCount,
        out IntPtr appContainerSid);

    [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
    private static extern int DeriveAppContainerSidFromAppContainerName(
        string appContainerName,
        out IntPtr appContainerSid);

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetStdHandle(int standardHandle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool InitializeProcThreadAttributeList(
        IntPtr attributeList,
        int attributeCount,
        int flags,
        ref IntPtr size);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool UpdateProcThreadAttribute(
        IntPtr attributeList,
        uint flags,
        IntPtr attribute,
        IntPtr value,
        IntPtr size,
        IntPtr previousValue,
        IntPtr returnSize);

    [DllImport("kernel32.dll")]
    private static extern void DeleteProcThreadAttributeList(IntPtr attributeList);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(
        string applicationName,
        StringBuilder commandLine,
        IntPtr processAttributes,
        IntPtr threadAttributes,
        bool inheritHandles,
        uint creationFlags,
        IntPtr environment,
        string currentDirectory,
        ref StartupInfoEx startupInfo,
        out ProcessInformation processInformation);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool OpenProcessToken(
        IntPtr processHandle,
        uint desiredAccess,
        out IntPtr tokenHandle);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetTokenInformation(
        IntPtr tokenHandle,
        int tokenInformationClass,
        out int tokenInformation,
        int tokenInformationLength,
        out int returnLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateProcess(IntPtr processHandle, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CreatePipe(
        out IntPtr readPipe,
        out IntPtr writePipe,
        ref SecurityAttributes pipeAttributes,
        uint size);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetHandleInformation(
        IntPtr handle,
        uint mask,
        uint flags);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool WriteFile(
        IntPtr file,
        byte[] buffer,
        uint bytesToWrite,
        out uint bytesWritten,
        IntPtr overlapped);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(
        IntPtr jobAttributes,
        string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(
        IntPtr job,
        int informationClass,
        IntPtr information,
        uint informationLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint ResumeThread(IntPtr thread);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool GetExitCodeProcess(IntPtr processHandle, out uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern IntPtr FreeSid(IntPtr sid);

    public static int Main(string[] args)
    {
        try
        {
            Invocation invocation = Parse(args);
            using (Mutex mutex = new Mutex(false, MutexName))
            {
                if (!mutex.WaitOne(TimeSpan.FromMinutes(15)))
                {
                    throw new InvalidOperationException("sandbox launcher ownership timed out");
                }

                try
                {
                    return Run(invocation);
                }
                finally
                {
                    mutex.ReleaseMutex();
                }
            }
        }
        catch (Exception exception)
        {
            LauncherWin32Exception win32 = exception as LauncherWin32Exception;
            Console.Error.WriteLine(
                "{\"event\":\"filing_xbrl_sandbox_refused\",\"error_type\":\"" +
                exception.GetType().Name + "\",\"native_error_code\":" +
                (win32 == null ? 0 : win32.NativeErrorCode) +
                ",\"stage\":\"" + (win32 == null ? "managed" : win32.Stage) + "\"}");
            return 125;
        }
    }

    private static Invocation Parse(string[] args)
    {
        Invocation result = new Invocation();
        int index = 0;
        if (args.Length < 8 || args[index++] != "--contract" || args[index++] != Contract)
        {
            throw new ArgumentException("sandbox contract is not exact");
        }
        if (args[index++] != "--deny-network")
        {
            throw new ArgumentException("network-denial flag is required");
        }
        while (index < args.Length && args[index] != "--")
        {
            string option = args[index++];
            if (index >= args.Length)
            {
                throw new ArgumentException("sandbox path option is incomplete");
            }
            string path = Path.GetFullPath(args[index++]);
            if (option == "--read-tree")
            {
                if (!string.IsNullOrEmpty(result.RuntimeRoot))
                {
                    throw new ArgumentException("sandbox runtime root is duplicated");
                }
                result.RuntimeRoot = path;
            }
            else if (option == "--read-input-tree")
            {
                if (!string.IsNullOrEmpty(result.InputRoot))
                {
                    throw new ArgumentException("sandbox input root is duplicated");
                }
                result.InputRoot = path;
            }
            else
            {
                throw new ArgumentException("sandbox option is not closed");
            }
        }
        if (index >= args.Length || args[index++] != "--" || index >= args.Length)
        {
            throw new ArgumentException("sandbox child command is missing");
        }
        while (index < args.Length)
        {
            result.Command.Add(args[index++]);
        }
        result.RuntimeRoot = RequireDirectory(result.RuntimeRoot);
        result.InputRoot = RequireDirectory(result.InputRoot);
        string executable = RequireFile(result.Command[0]);
        if (!IsWithin(executable, result.RuntimeRoot))
        {
            throw new ArgumentException("sandbox executable is outside the runtime root");
        }
        result.Command[0] = executable;
        return result;
    }

    private static int Run(Invocation invocation)
    {
        byte[] payload = ReadInputPayload();
        IntPtr sid = IntPtr.Zero;
        List<AccessGrant> grants = new List<AccessGrant>();
        try
        {
            sid = GetOrCreateProfileSid();
            SecurityIdentifier identity = new SecurityIdentifier(sid);
            grants.Add(GrantDirectory(invocation.RuntimeRoot, identity));
            grants.Add(GrantDirectory(invocation.InputRoot, identity));
            return StartChild(invocation, sid, payload);
        }
        finally
        {
            Exception cleanupFailure = null;
            for (int index = grants.Count - 1; index >= 0; index--)
            {
                try
                {
                    RemoveGrant(grants[index]);
                }
                catch (Exception exception)
                {
                    cleanupFailure = exception;
                }
            }
            if (sid != IntPtr.Zero)
            {
                FreeSid(sid);
            }
            if (cleanupFailure != null)
            {
                throw new InvalidOperationException("sandbox ACL cleanup failed", cleanupFailure);
            }
        }
    }

    private static IntPtr GetOrCreateProfileSid()
    {
        IntPtr sid;
        int result = CreateAppContainerProfile(
            ProfileName,
            "Earnings Summary Filing XBRL",
            "Network-denied processor for captured SEC Inline-XBRL packages",
            IntPtr.Zero,
            0,
            out sid);
        if (result == ErrorAlreadyExistsHresult)
        {
            result = DeriveAppContainerSidFromAppContainerName(ProfileName, out sid);
        }
        if (result != 0 || sid == IntPtr.Zero)
        {
            throw new InvalidOperationException("AppContainer profile is unavailable");
        }
        return sid;
    }

    private static AccessGrant GrantDirectory(string path, SecurityIdentifier identity)
    {
        FileSystemAccessRule rule = new FileSystemAccessRule(
            identity,
            FileSystemRights.ReadAndExecute | FileSystemRights.ListDirectory,
            InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
            PropagationFlags.None,
            AccessControlType.Allow);
        DirectorySecurity security = Directory.GetAccessControl(path, AccessControlSections.Access);
        bool staleRulesRemoved = false;
        AuthorizationRuleCollection rules = security.GetAccessRules(
            true,
            true,
            typeof(SecurityIdentifier));
        foreach (AuthorizationRule item in rules)
        {
            FileSystemAccessRule existing = item as FileSystemAccessRule;
            if (existing == null || !identity.Equals(existing.IdentityReference))
            {
                continue;
            }
            FileSystemRights forbidden =
                FileSystemRights.WriteData |
                FileSystemRights.AppendData |
                FileSystemRights.WriteAttributes |
                FileSystemRights.WriteExtendedAttributes |
                FileSystemRights.DeleteSubdirectoriesAndFiles |
                FileSystemRights.Delete |
                FileSystemRights.ChangePermissions |
                FileSystemRights.TakeOwnership;
            if (
                existing.AccessControlType != AccessControlType.Allow ||
                (existing.FileSystemRights & forbidden) != 0)
            {
                throw new UnauthorizedAccessException(
                    "sandbox root has an unsafe pre-existing AppContainer ACL");
            }
            if (existing.IsInherited)
            {
                continue;
            }
            security.RemoveAccessRuleSpecific(existing);
            staleRulesRemoved = true;
        }
        if (staleRulesRemoved)
        {
            Directory.SetAccessControl(path, security);
            security = Directory.GetAccessControl(path, AccessControlSections.Access);
        }
        security.AddAccessRule(rule);
        Directory.SetAccessControl(path, security);
        return new AccessGrant { Path = path, Rule = rule };
    }

    private static void RemoveGrant(AccessGrant grant)
    {
        DirectorySecurity security = Directory.GetAccessControl(
            grant.Path, AccessControlSections.Access);
        security.RemoveAccessRuleSpecific(grant.Rule);
        Directory.SetAccessControl(grant.Path, security);
    }

    private static int StartChild(Invocation invocation, IntPtr sid, byte[] payload)
    {
        IntPtr attributeList = IntPtr.Zero;
        IntPtr capabilitiesPointer = IntPtr.Zero;
        IntPtr handleListPointer = IntPtr.Zero;
        IntPtr childInput = IntPtr.Zero;
        IntPtr parentInput = IntPtr.Zero;
        IntPtr job = IntPtr.Zero;
        IntPtr jobInformation = IntPtr.Zero;
        bool childTerminal = false;
        ProcessInformation process = new ProcessInformation();
        try
        {
            job = CreateJobObject(IntPtr.Zero, null);
            if (job == IntPtr.Zero)
            {
                throw LastWin32("cannot create child ownership job");
            }
            ExtendedLimitInformation limits = new ExtendedLimitInformation();
            limits.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
            jobInformation = Marshal.AllocHGlobal(Marshal.SizeOf(limits));
            Marshal.StructureToPtr(limits, jobInformation, false);
            if (!SetInformationJobObject(
                job,
                9,
                jobInformation,
                checked((uint)Marshal.SizeOf(limits))))
            {
                throw LastWin32("cannot configure child ownership job");
            }
            SecurityAttributes pipeAttributes = new SecurityAttributes
            {
                nLength = Marshal.SizeOf(typeof(SecurityAttributes)),
                lpSecurityDescriptor = IntPtr.Zero,
                bInheritHandle = true,
            };
            if (!CreatePipe(out childInput, out parentInput, ref pipeAttributes, 0))
            {
                throw LastWin32("cannot create bounded child input pipe");
            }
            if (!SetHandleInformation(parentInput, HandleFlagInherit, 0))
            {
                throw LastWin32("cannot restrict parent input handle");
            }
            IntPtr size = IntPtr.Zero;
            InitializeProcThreadAttributeList(IntPtr.Zero, 2, 0, ref size);
            attributeList = Marshal.AllocHGlobal(size);
            if (!InitializeProcThreadAttributeList(attributeList, 2, 0, ref size))
            {
                throw LastWin32("cannot initialize process attributes");
            }

            SecurityCapabilities capabilities = new SecurityCapabilities
            {
                AppContainerSid = sid,
                Capabilities = IntPtr.Zero,
                CapabilityCount = 0,
                Reserved = 0,
            };
            capabilitiesPointer = Marshal.AllocHGlobal(Marshal.SizeOf(capabilities));
            Marshal.StructureToPtr(capabilities, capabilitiesPointer, false);
            if (!UpdateProcThreadAttribute(
                attributeList,
                0,
                ProcThreadAttributeSecurityCapabilities,
                capabilitiesPointer,
                new IntPtr(Marshal.SizeOf(capabilities)),
                IntPtr.Zero,
                IntPtr.Zero))
            {
                throw LastWin32("cannot apply AppContainer attributes");
            }

            IntPtr stdout = GetStdHandle(StdOutputHandle);
            IntPtr stderr = GetStdHandle(StdErrorHandle);
            handleListPointer = Marshal.AllocHGlobal(IntPtr.Size * 3);
            Marshal.WriteIntPtr(handleListPointer, 0, childInput);
            Marshal.WriteIntPtr(handleListPointer, IntPtr.Size, stdout);
            Marshal.WriteIntPtr(handleListPointer, IntPtr.Size * 2, stderr);
            if (!UpdateProcThreadAttribute(
                attributeList,
                0,
                ProcThreadAttributeHandleList,
                handleListPointer,
                new IntPtr(IntPtr.Size * 3),
                IntPtr.Zero,
                IntPtr.Zero))
            {
                throw LastWin32("cannot restrict inherited handles");
            }

            StartupInfoEx startup = new StartupInfoEx();
            startup.StartupInfo.cb = Marshal.SizeOf(startup);
            startup.StartupInfo.dwFlags = StartfUseStdHandles;
            startup.StartupInfo.hStdInput = childInput;
            startup.StartupInfo.hStdOutput = stdout;
            startup.StartupInfo.hStdError = stderr;
            startup.lpAttributeList = attributeList;
            StringBuilder commandLine = new StringBuilder(BuildCommandLine(invocation.Command));
            if (!CreateProcess(
                null,
                commandLine,
                IntPtr.Zero,
                IntPtr.Zero,
                true,
                ExtendedStartupInfoPresent | CreateNoWindow | CreateSuspended,
                IntPtr.Zero,
                invocation.RuntimeRoot,
                ref startup,
                out process))
            {
                throw LastWin32("cannot create AppContainer process");
            }
            if (!AssignProcessToJobObject(job, process.hProcess))
            {
                throw LastWin32("cannot assign AppContainer child ownership");
            }
            if (!IsAppContainer(process.hProcess))
            {
                throw new InvalidOperationException("child token is not an AppContainer");
            }
            if (ResumeThread(process.hThread) == uint.MaxValue)
            {
                throw LastWin32("cannot resume AppContainer child");
            }
            CloseHandle(process.hThread);
            process.hThread = IntPtr.Zero;
            CloseHandle(childInput);
            childInput = IntPtr.Zero;
            uint written;
            if (!WriteFile(
                parentInput,
                payload,
                checked((uint)payload.Length),
                out written,
                IntPtr.Zero) || written != payload.Length)
            {
                TerminateProcess(process.hProcess, 127);
                throw LastWin32("cannot deliver bounded child input");
            }
            CloseHandle(parentInput);
            parentInput = IntPtr.Zero;
            if (WaitForSingleObject(process.hProcess, Infinite) != 0)
            {
                throw LastWin32("cannot wait for AppContainer process");
            }
            childTerminal = true;
            uint exitCode;
            if (!GetExitCodeProcess(process.hProcess, out exitCode))
            {
                throw LastWin32("cannot read AppContainer exit status");
            }
            return unchecked((int)exitCode);
        }
        finally
        {
            if (process.hProcess != IntPtr.Zero && !childTerminal)
            {
                TerminateProcess(process.hProcess, 127);
                WaitForSingleObject(process.hProcess, Infinite);
                childTerminal = true;
            }
            if (process.hThread != IntPtr.Zero)
            {
                CloseHandle(process.hThread);
            }
            if (process.hProcess != IntPtr.Zero)
            {
                CloseHandle(process.hProcess);
            }
            if (childInput != IntPtr.Zero)
            {
                CloseHandle(childInput);
            }
            if (parentInput != IntPtr.Zero)
            {
                CloseHandle(parentInput);
            }
            if (attributeList != IntPtr.Zero)
            {
                DeleteProcThreadAttributeList(attributeList);
                Marshal.FreeHGlobal(attributeList);
            }
            if (capabilitiesPointer != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(capabilitiesPointer);
            }
            if (handleListPointer != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(handleListPointer);
            }
            if (jobInformation != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(jobInformation);
            }
            if (job != IntPtr.Zero)
            {
                CloseHandle(job);
            }
        }
    }

    private static bool IsAppContainer(IntPtr processHandle)
    {
        IntPtr token;
        if (!OpenProcessToken(processHandle, TokenQuery, out token))
        {
            throw LastWin32("cannot inspect child token");
        }
        try
        {
            int value;
            int returned;
            if (!GetTokenInformation(token, TokenIsAppContainer, out value, 4, out returned))
            {
                throw LastWin32("cannot verify AppContainer token");
            }
            return value == 1;
        }
        finally
        {
            CloseHandle(token);
        }
    }

    private static byte[] ReadInputPayload()
    {
        using (Stream input = Console.OpenStandardInput())
        using (MemoryStream buffer = new MemoryStream())
        {
            byte[] chunk = new byte[64 * 1024];
            while (true)
            {
                int read = input.Read(chunk, 0, chunk.Length);
                if (read == 0)
                {
                    break;
                }
                if (buffer.Length + read > MaximumInputBytes)
                {
                    throw new InvalidOperationException("sandbox input exceeds the hard limit");
                }
                buffer.Write(chunk, 0, read);
            }
            return buffer.ToArray();
        }
    }

    private static string RequireDirectory(string path)
    {
        string full = Path.GetFullPath(path);
        if (!Directory.Exists(full))
        {
            throw new DirectoryNotFoundException("sandbox runtime root is unavailable");
        }
        RequireNoReparsePoints(full);
        return full.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
    }

    private static string RequireFile(string path)
    {
        string full = Path.GetFullPath(path);
        if (!File.Exists(full))
        {
            throw new FileNotFoundException("sandbox file is unavailable");
        }
        RequireNoReparsePoints(full);
        return full;
    }

    private static void RequireNoReparsePoints(string path)
    {
        string current = path;
        while (!string.IsNullOrEmpty(current))
        {
            if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
            {
                throw new IOException("sandbox path contains a reparse point");
            }
            if (File.Exists(current))
            {
                current = Path.GetDirectoryName(current);
            }
            else
            {
                DirectoryInfo parent = Directory.GetParent(current);
                current = parent == null ? null : parent.FullName;
            }
        }
    }

    private static bool IsWithin(string file, string directory)
    {
        return file.StartsWith(directory, StringComparison.OrdinalIgnoreCase);
    }

    private static string BuildCommandLine(List<string> values)
    {
        StringBuilder result = new StringBuilder();
        foreach (string value in values)
        {
            if (result.Length != 0)
            {
                result.Append(' ');
            }
            result.Append(Quote(value));
        }
        return result.ToString();
    }

    private static string Quote(string value)
    {
        if (value.Length != 0 && value.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0)
        {
            return value;
        }
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\')
            {
                slashes++;
            }
            else if (character == '"')
            {
                result.Append('\\', slashes * 2 + 1);
                result.Append('"');
                slashes = 0;
            }
            else
            {
                result.Append('\\', slashes);
                slashes = 0;
                result.Append(character);
            }
        }
        result.Append('\\', slashes * 2);
        result.Append('"');
        return result.ToString();
    }

    private static Exception LastWin32(string message)
    {
        return new LauncherWin32Exception(Marshal.GetLastWin32Error(), message);
    }
}
