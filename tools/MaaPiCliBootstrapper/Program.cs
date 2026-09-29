using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

internal static class Program
{
    private static int Main(string[] args)
    {
        var app = new Bootstrapper(AppDomain.CurrentDomain.BaseDirectory);
        return app.Run(args);
    }
}

internal sealed class Bootstrapper
{
    private readonly string baseDir;
    private readonly string realCli;
    private readonly string configPath;
    private readonly string logPath;

    public Bootstrapper(string baseDir)
    {
        this.baseDir = Path.GetFullPath(baseDir);
        realCli = Path.Combine(this.baseDir, "real", "MaaPiCli.exe");
        configPath = Path.Combine(this.baseDir, "config", "maa_pi_config.json");
        logPath = Path.Combine(this.baseDir, "debug", "maa.log");
    }

    public int Run(string[] args)
    {
        try
        {
            Log("INF", "MaaPiCli bootstrapper started.");
            var config = LoadConfig();
            EnsureEmulator(config);
            return RunRealCli(args);
        }
        catch (Exception ex)
        {
            Log("ERR", "MGA_TASK_FAILED MaaPiCli bootstrapper failed: " + ex.Message);
            return 1;
        }
    }

    private CliConfig LoadConfig()
    {
        if (!File.Exists(realCli))
        {
            throw new FileNotFoundException("Real MaaPiCli is missing.", realCli);
        }
        if (!File.Exists(configPath))
        {
            throw new FileNotFoundException("maa_pi_config.json is missing.", configPath);
        }

        var text = File.ReadAllText(configPath, Encoding.UTF8);
        var adbPath = JsonString(text, "adb_path");
        var address = JsonString(text, "address");
        var mumuRoot = JsonString(text, "path");
        var mumuIndex = JsonInt(text, "index", 0);

        if (string.IsNullOrWhiteSpace(mumuRoot) && !string.IsNullOrWhiteSpace(adbPath))
        {
            var nxMain = Path.GetDirectoryName(adbPath);
            mumuRoot = Path.GetDirectoryName(nxMain ?? string.Empty) ?? string.Empty;
        }

        return new CliConfig(adbPath, address, mumuRoot, mumuIndex);
    }

    private void EnsureEmulator(CliConfig config)
    {
        if (string.IsNullOrWhiteSpace(config.AdbPath) || string.IsNullOrWhiteSpace(config.Address))
        {
            Log("WRN", "ADB path or address is empty; skip emulator bootstrap.");
            return;
        }

        if (IsAdbReady(config) && IsAndroidBootReady(config))
        {
            Log("INF", "ADB device is already ready: " + config.Address);
            return;
        }

        StartMuMu(config);
        WaitForAdb(config, TimeSpan.FromSeconds(180));
        WaitForAndroidBoot(config, TimeSpan.FromSeconds(180));
        Thread.Sleep(TimeSpan.FromSeconds(3));
    }

    private void StartMuMu(CliConfig config)
    {
        var manager = Path.Combine(config.MuMuRoot, "nx_main", "MuMuManager.exe");
        if (!File.Exists(manager))
        {
            manager = Path.Combine(Path.GetDirectoryName(config.AdbPath) ?? string.Empty, "MuMuManager.exe");
        }
        if (!File.Exists(manager))
        {
            throw new FileNotFoundException("MuMuManager.exe is missing.", manager);
        }

        var arguments = "api -v " + config.MuMuIndex + " launch_player";
        Log("INF", "Starting MuMu: " + manager + " " + arguments);
        var result = RunProcess(manager, arguments, Path.GetDirectoryName(manager) ?? baseDir, TimeSpan.FromSeconds(30));
        var combined = (result.Stdout + "\n" + result.Stderr).Trim();
        if (!string.IsNullOrWhiteSpace(combined))
        {
            Log("INF", "MuMuManager output: " + combined.Replace("\r", " ").Replace("\n", " | "));
        }
        if (result.ExitCode != 0 && combined.IndexOf("result=0", StringComparison.OrdinalIgnoreCase) < 0)
        {
            throw new InvalidOperationException("MuMuManager failed with exit code " + result.ExitCode + ".");
        }
    }

    private void WaitForAdb(CliConfig config, TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            if (IsAdbReady(config))
            {
                Log("INF", "ADB device is ready: " + config.Address);
                return;
            }
            Thread.Sleep(TimeSpan.FromSeconds(3));
        }
        throw new TimeoutException("Timed out waiting for ADB device: " + config.Address);
    }

    private void WaitForAndroidBoot(CliConfig config, TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            if (IsAndroidBootReady(config))
            {
                Log("INF", "Android boot is ready.");
                return;
            }
            Thread.Sleep(TimeSpan.FromSeconds(3));
        }
        throw new TimeoutException("Timed out waiting for Android boot.");
    }

    private bool IsAndroidBootReady(CliConfig config)
    {
        try
        {
            var cwd = Path.GetDirectoryName(config.AdbPath) ?? baseDir;
            var boot = RunProcess(config.AdbPath, "-s " + config.Address + " shell getprop sys.boot_completed", cwd, TimeSpan.FromSeconds(10));
            if (boot.Stdout.Trim() != "1")
            {
                return false;
            }
            var size = RunProcess(config.AdbPath, "-s " + config.Address + " shell wm size", cwd, TimeSpan.FromSeconds(10));
            return size.Stdout.IndexOf("Physical size", StringComparison.OrdinalIgnoreCase) >= 0;
        }
        catch
        {
            return false;
        }
    }

    private string GetMuMuCli(CliConfig config)
    {
        var cli = Path.Combine(config.MuMuRoot, "nx_main", "mumu-cli.exe");
        if (File.Exists(cli))
        {
            return cli;
        }

        cli = Path.Combine(Path.GetDirectoryName(config.AdbPath) ?? string.Empty, "mumu-cli.exe");
        return File.Exists(cli) ? cli : string.Empty;
    }

    private bool IsAdbReady(CliConfig config)
    {
        if (!CanConnect(config.Address))
        {
            return false;
        }

        var result = RunProcess(config.AdbPath, "devices -l", Path.GetDirectoryName(config.AdbPath) ?? baseDir, TimeSpan.FromSeconds(10));
        if (!HasAdbDevice(result.Stdout, config.Address))
        {
            RunProcess(config.AdbPath, "connect " + config.Address, Path.GetDirectoryName(config.AdbPath) ?? baseDir, TimeSpan.FromSeconds(10));
            result = RunProcess(config.AdbPath, "devices -l", Path.GetDirectoryName(config.AdbPath) ?? baseDir, TimeSpan.FromSeconds(10));
        }
        return HasAdbDevice(result.Stdout, config.Address);
    }

    private static bool HasAdbDevice(string adbDevicesOutput, string address)
    {
        return adbDevicesOutput
            .Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries)
            .Select(line => line.Trim())
            .Any(line => line.StartsWith(address, StringComparison.OrdinalIgnoreCase)
                && Regex.IsMatch(line, @"\sdevice\b", RegexOptions.IgnoreCase));
    }

    private static bool CanConnect(string address)
    {
        var parts = address.Split(':');
        int port;
        if (parts.Length != 2 || !int.TryParse(parts[1], out port))
        {
            return false;
        }

        try
        {
            using (var client = new TcpClient())
            {
                var task = client.ConnectAsync(parts[0], port);
                return task.Wait(TimeSpan.FromSeconds(1)) && client.Connected;
            }
        }
        catch
        {
            return false;
        }
    }

    private int RunRealCli(string[] args)
    {
        Log("INF", "Starting real MaaPiCli: " + realCli + " " + string.Join(" ", args.Select(QuoteArg).ToArray()));
        using (var job = JobObject.TryCreate())
        using (var process = new Process())
        {
            process.StartInfo = new ProcessStartInfo
            {
                FileName = realCli,
                WorkingDirectory = baseDir,
                UseShellExecute = false,
                Arguments = string.Join(" ", args.Select(QuoteArg).ToArray()),
            };

            process.Start();
            if (job != null)
            {
                job.Assign(process);
            }
            process.WaitForExit();
            Log("INF", "Real MaaPiCli exited: " + process.ExitCode);
            if (process.ExitCode != 0)
            {
                Log("ERR", "MGA_TASK_FAILED Real MaaPiCli exited with code: " + process.ExitCode);
            }
            return process.ExitCode;
        }
    }

    private ProcessResult RunProcess(string fileName, string arguments, string workingDirectory, TimeSpan timeout)
    {
        using (var process = new Process())
        {
            process.StartInfo = new ProcessStartInfo
            {
                FileName = fileName,
                Arguments = arguments,
                WorkingDirectory = workingDirectory,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            process.Start();
            if (!process.WaitForExit((int)timeout.TotalMilliseconds))
            {
                try
                {
                    process.Kill();
                }
                catch
                {
                    // Best effort cleanup.
                }
                throw new TimeoutException("Process timeout: " + fileName);
            }
            return new ProcessResult(process.ExitCode, process.StandardOutput.ReadToEnd(), process.StandardError.ReadToEnd());
        }
    }

    private void Log(string level, string message)
    {
        var dir = Path.GetDirectoryName(logPath);
        if (!string.IsNullOrWhiteSpace(dir))
        {
            Directory.CreateDirectory(dir);
        }
        var timestamp = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.ffffff");
        File.AppendAllText(logPath, "[" + timestamp + "][" + level + "][MaaPiCliBootstrapper] " + message + Environment.NewLine, Encoding.UTF8);
    }

    private static string JsonString(string json, string key)
    {
        var match = Regex.Match(json, "\"" + Regex.Escape(key) + "\"\\s*:\\s*\"((?:\\\\.|[^\"])*)\"", RegexOptions.Singleline);
        if (!match.Success)
        {
            return string.Empty;
        }
        return Regex.Unescape(match.Groups[1].Value);
    }

    private static int JsonInt(string json, string key, int fallback)
    {
        var match = Regex.Match(json, "\"" + Regex.Escape(key) + "\"\\s*:\\s*(-?\\d+)", RegexOptions.Singleline);
        int value;
        return match.Success && int.TryParse(match.Groups[1].Value, out value) ? value : fallback;
    }

    private static string QuoteArg(string value)
    {
        if (string.IsNullOrEmpty(value))
        {
            return "\"\"";
        }
        return value.IndexOfAny(new[] { ' ', '\t', '"' }) >= 0
            ? "\"" + value.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\""
            : value;
    }
}

internal sealed class CliConfig
{
    public CliConfig(string adbPath, string address, string mumuRoot, int mumuIndex)
    {
        AdbPath = adbPath;
        Address = address;
        MuMuRoot = mumuRoot;
        MuMuIndex = mumuIndex;
    }

    public string AdbPath { get; private set; }
    public string Address { get; private set; }
    public string MuMuRoot { get; private set; }
    public int MuMuIndex { get; private set; }
}

internal sealed class ProcessResult
{
    public ProcessResult(int exitCode, string stdout, string stderr)
    {
        ExitCode = exitCode;
        Stdout = stdout;
        Stderr = stderr;
    }

    public int ExitCode { get; private set; }
    public string Stdout { get; private set; }
    public string Stderr { get; private set; }
}

internal sealed class JobObject : IDisposable
{
    private const int JobObjectExtendedLimitInformationClass = 9;
    private const uint JobObjectLimitKillOnJobClose = 0x00002000;
    private readonly IntPtr handle;

    private JobObject(IntPtr handle)
    {
        this.handle = handle;
    }

    public static JobObject TryCreate()
    {
        if (Environment.OSVersion.Platform != PlatformID.Win32NT)
        {
            return null;
        }

        var handle = CreateJobObject(IntPtr.Zero, null);
        if (handle == IntPtr.Zero)
        {
            return null;
        }

        var info = new JobObjectExtendedLimitInformation
        {
            BasicLimitInformation = new JobObjectBasicLimitInformation
            {
                LimitFlags = JobObjectLimitKillOnJobClose,
            },
        };
        var length = Marshal.SizeOf(typeof(JobObjectExtendedLimitInformation));
        var buffer = Marshal.AllocHGlobal(length);
        try
        {
            Marshal.StructureToPtr(info, buffer, false);
            if (!SetInformationJobObject(handle, JobObjectExtendedLimitInformationClass, buffer, (uint)length))
            {
                CloseHandle(handle);
                return null;
            }
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }

        return new JobObject(handle);
    }

    public void Assign(Process process)
    {
        if (handle != IntPtr.Zero)
        {
            AssignProcessToJobObject(handle, process.Handle);
        }
    }

    public void Dispose()
    {
        if (handle != IntPtr.Zero)
        {
            CloseHandle(handle);
        }
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);

    [DllImport("kernel32.dll")]
    private static extern bool SetInformationJobObject(IntPtr hJob, int jobObjectInfoClass, IntPtr lpJobObjectInfo, uint cbJobObjectInfoLength);

    [DllImport("kernel32.dll")]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr hObject);

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectBasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JobObjectExtendedLimitInformation
    {
        public JobObjectBasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }
}
