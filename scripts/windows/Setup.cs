// Offline, per-user installer. No admin rights, package managers or downloads.
using System;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Diagnostics;
using System.Threading;
using System.Windows.Forms;

class Setup : Form {
    const string BuildId = "__BUILD_ID__";
    Label status = new Label();
    ProgressBar progress = new ProgressBar();
    Button install = new Button();
    bool installing;
    string destination;

    Setup() {
        Text = "Установка job-bless";
        ClientSize = new Size(520, 260);
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        Font = new Font("Segoe UI", 10);
        Label title = new Label { Text = "job-bless", Font = new Font("Segoe UI", 20, FontStyle.Bold), AutoSize = true, Location = new Point(24, 20) };
        status.Text = "Все компоненты уже входят в установщик.\nПосле установки останется войти в Google AI Studio.\n\nAIStudioToAPI: CC BY-NC 4.0, для некоммерческого использования.";
        status.SetBounds(24, 74, 472, 105);
        progress.SetBounds(24, 185, 472, 8);
        install.Text = "Установить и открыть";
        install.SetBounds(290, 210, 206, 34);
        install.Click += delegate { StartInstall(); };
        Controls.AddRange(new Control[] { title, status, progress, install });
        FormClosing += delegate(object sender, FormClosingEventArgs e) { if (installing) e.Cancel = true; };
    }

    void UpdateStatus(string text, int percent) {
        Invoke((Action)delegate { status.Text = text; progress.Value = percent; });
    }

    void StartInstall() {
        installing = true;
        install.Enabled = false;
        Thread worker = new Thread(delegate() {
            try {
                string parent = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "job-bless", "versions");
                destination = Path.Combine(parent, BuildId);
                Extract(destination, delegate(int value) { UpdateStatus("Распаковываем приложение и встроенные компоненты…", value); });
                CreateShortcut(destination);
                Invoke((Action)delegate {
                    installing = false;
                    status.Text = "Готово. Открываем job-bless в браузере.";
                    Process.Start(new ProcessStartInfo("wscript.exe", Quote(Path.Combine(destination, "Start-job-bless.vbs"))) { UseShellExecute = false, CreateNoWindow = true });
                    Close();
                });
            } catch (Exception error) {
                Invoke((Action)delegate {
                    installing = false;
                    install.Enabled = true;
                    status.Text = "Не удалось установить приложение:\n" + error.Message;
                });
            }
        });
        worker.IsBackground = true;
        worker.Start();
    }

    static string Quote(string value) { return "\"" + value + "\""; }

    static void Extract(string destination, Action<int> report) {
        string complete = Path.Combine(destination, ".complete");
        if (File.Exists(complete) && File.ReadAllText(complete) == BuildId) { report(100); return; }
        Directory.CreateDirectory(destination);
        string root = Path.GetFullPath(destination).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        using (Stream payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
        using (ZipArchive zip = new ZipArchive(payload, ZipArchiveMode.Read)) {
            long total = 0, done = 0;
            foreach (ZipArchiveEntry item in zip.Entries) total += item.Length;
            int lastPercent = -1;
            foreach (ZipArchiveEntry item in zip.Entries) {
                string target = Path.GetFullPath(Path.Combine(destination, item.FullName));
                if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase)) throw new IOException("Недопустимый путь в архиве.");
                if (String.IsNullOrEmpty(item.Name)) { Directory.CreateDirectory(target); continue; }
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                using (Stream input = item.Open())
                using (FileStream output = File.Create(target)) input.CopyTo(output);
                done += item.Length;
                int percent = total == 0 ? 100 : (int)(done * 100 / total);
                if (percent != lastPercent) { report(percent); lastPercent = percent; }
            }
        }
        File.WriteAllText(complete, BuildId);
    }

    static void CreateShortcut(string destination) {
        Type shellType = Type.GetTypeFromProgID("WScript.Shell");
        object shell = Activator.CreateInstance(shellType);
        foreach (string folder in new string[] { Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), Environment.GetFolderPath(Environment.SpecialFolder.Programs) }) {
            object shortcut = shellType.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { Path.Combine(folder, "job-bless.lnk") });
            Type type = shortcut.GetType();
            type.InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "wscript.exe") });
            type.InvokeMember("Arguments", BindingFlags.SetProperty, null, shortcut, new object[] { Quote(Path.Combine(destination, "Start-job-bless.vbs")) });
            type.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, shortcut, new object[] { destination });
            type.InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { "job-bless" });
            type.InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null);
        }
    }

    [STAThread]
    static void Main(string[] args) {
        // Build verification can extract into an isolated directory without
        // installing shortcuts or starting a second user instance.
        if (args.Length == 2 && args[0] == "--extract-to") { Extract(Path.GetFullPath(args[1]), delegate(int value) {}); return; }
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new Setup());
    }
}
