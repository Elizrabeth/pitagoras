// Pitagoras.exe: starts Pitagoras without a console window.
// It runs .venv\Scripts\python.exe run.py from the folder the exe is in, writes everything it prints to
// logs\pitagoras.log, and tells you if it fails to start. Build it with launcher\build.ps1.
using System;
using System.Diagnostics;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Windows.Forms;

static class Launcher
{
    const string Title = "Pitagoras";

    [STAThread]
    static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        string python = Path.Combine(root, @".venv\Scripts\python.exe");
        string script = Path.Combine(root, "run.py");

        if (!File.Exists(script))
            return Fail("run.py isn't next to Pitagoras.exe.\n\nKeep Pitagoras.exe in the Pitagoras folder:\n" + root);
        if (!File.Exists(python))
            return Fail("Pitagoras isn't set up yet (no .venv folder).\n\nRun setup.ps1 in this folder first:\n" + root);

        bool alone;
        using (var once = new Mutex(true, @"Local\PitagorasLauncher", out alone))
        {
            if (!alone || EventsPortBusy())
            {
                MessageBox.Show("Pitagoras is already running.\n\nRight-click the yellow ball to quit it.",
                    Title, MessageBoxButtons.OK, MessageBoxIcon.Information);
                return 0;
            }
            return Run(root, python, args);
        }
    }

    static int Run(string root, string python, string[] args)
    {
        string logs = Path.Combine(root, "logs");
        Directory.CreateDirectory(logs);
        string log = Path.Combine(logs, "pitagoras.log");
        try
        {
            if (File.Exists(log)) File.Copy(log, Path.Combine(logs, "pitagoras.previous.log"), true);
        }
        catch (IOException) { }

        var psi = new ProcessStartInfo(python, "run.py " + string.Join(" ", args))
        {
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        psi.EnvironmentVariables["PYTHONUTF8"] = "1";
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";

        var started = DateTime.Now;
        using (var file = new StreamWriter(log, false, new UTF8Encoding(false)) { AutoFlush = true })
        using (var p = new Process { StartInfo = psi })
        {
            object gate = new object();
            DataReceivedEventHandler write = (s, e) => { if (e.Data != null) lock (gate) file.WriteLine(e.Data); };
            p.OutputDataReceived += write;
            p.ErrorDataReceived += write;
            try
            {
                p.Start();
            }
            catch (Exception e)
            {
                return Fail("Couldn't start Python:\n" + e.Message);
            }
            p.StandardInput.Close(); // (no console to type into)
            p.BeginOutputReadLine();
            p.BeginErrorReadLine();
            p.WaitForExit();
            Thread.Sleep(300);
            lock (gate) file.Flush();

            // crashed (not quit from the menu): show why
            if (p.ExitCode != 0 && (DateTime.Now - started).TotalMinutes < 10)
                return Fail("Pitagoras stopped with an error:\n\n" + Tail(log, 12) + "\n\nFull log: " + log);
            return p.ExitCode;
        }
    }

    static bool EventsPortBusy()
    {
        try
        {
            using (var c = new TcpClient())
            {
                var r = c.BeginConnect("127.0.0.1", 8765, null, null);
                return r.AsyncWaitHandle.WaitOne(300) && c.Connected;
            }
        }
        catch (SocketException) { return false; }
    }

    static string Tail(string path, int lines)
    {
        try
        {
            string[] all;
            using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
            using (var sr = new StreamReader(fs, Encoding.UTF8)) all = sr.ReadToEnd().Split('\n');
            int start = Math.Max(0, all.Length - lines);
            return string.Join("\n", all, start, all.Length - start).Trim();
        }
        catch (IOException) { return "(couldn't read the log)"; }
    }

    static int Fail(string msg)
    {
        MessageBox.Show(msg, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
        return 1;
    }
}
