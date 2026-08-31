using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Net;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace WorkStack.Desktop
{
    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new WorkStackShellForm(ShellOptions.Parse(args)));
        }
    }

    internal sealed class ShellOptions
    {
        public string InstallRoot { get; private set; }
        public string StateRoot { get; private set; }
        public string WorkStackUrl { get; private set; }
        public string ProbeProvider { get; private set; }
        public string ProbeResultPath { get; private set; }
        public int AutoCloseSeconds { get; private set; }

        public static ShellOptions Parse(string[] args)
        {
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            return new ShellOptions
            {
                InstallRoot = Value(args, "--install-root", Path.Combine(local, "Programs", "WorkStack")),
                StateRoot = Value(args, "--state-root", Path.Combine(local, "WorkStack")),
                WorkStackUrl = Value(args, "--url", "http://127.0.0.1:8765/"),
                ProbeProvider = Value(args, "--probe-provider", ""),
                ProbeResultPath = Value(args, "--probe-result", ""),
                AutoCloseSeconds = Integer(args, "--auto-close-seconds", 0)
            };
        }

        private static string Value(string[] args, string name, string fallback)
        {
            for (int index = 0; index + 1 < args.Length; index++)
            {
                if (String.Equals(args[index], name, StringComparison.OrdinalIgnoreCase)) return args[index + 1];
            }
            return fallback;
        }

        private static int Integer(string[] args, string name, int fallback)
        {
            int parsed;
            return Int32.TryParse(Value(args, name, fallback.ToString()), out parsed) && parsed >= 0 ? parsed : fallback;
        }
    }

    internal sealed class WorkStackShellForm : Form
    {
        private const string SourceHostPrefix = "workstack-source-host";
        private readonly Dictionary<string, string> providerUrls = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            { "outlook", "https://outlook.office.com/mail/" },
            { "teams", "https://teams.microsoft.com/v2/" },
            { "onenote", "https://www.office.com/launch/onenote" }
        };
        private readonly ShellOptions options;
        private readonly WebView2 workStackWebView;
        private readonly WebView2 sourceWebView;
        private readonly Panel sourceViewport;
        private readonly ToolStripStatusLabel status;
        private bool serverStartedByShell;
        private Timer autoCloseTimer;
        private string runtimeVersion;
        private bool probeRecorded;
        private string activeProvider = "";

        public WorkStackShellForm(ShellOptions options)
        {
            this.options = options;
            Text = "Work Stack";
            Icon = SystemIcons.Application;
            MinimumSize = new Size(1024, 700);
            StartPosition = FormStartPosition.CenterScreen;
            WindowState = FormWindowState.Maximized;

            status = new ToolStripStatusLabel("Starting Work Stack…");
            var statusStrip = new StatusStrip();
            statusStrip.Items.Add(status);

            workStackWebView = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = Color.FromArgb(11, 13, 18) };
            sourceWebView = new WebView2 { DefaultBackgroundColor = Color.White };
            sourceViewport = new Panel { Visible = false, BackColor = Color.White };
            sourceViewport.Controls.Add(sourceWebView);
            Controls.Add(workStackWebView);
            Controls.Add(sourceViewport);
            Controls.Add(statusStrip);

            Shown += async delegate { await InitializeAsync(); };
            FormClosed += delegate { StopOwnedServer(); };
        }

        private async Task InitializeAsync()
        {
            try
            {
                status.Text = "Checking local server…";
                if (!await Task.Run(delegate { return IsReady(options.WorkStackUrl); }))
                {
                    status.Text = "Starting local server…";
                    await Task.Run(delegate { StartServer(); });
                    serverStartedByShell = true;
                }

                runtimeVersion = CoreWebView2Environment.GetAvailableBrowserVersionString();
                string userDataFolder = Path.Combine(options.StateRoot, "desktop-webview-profile");
                var environment = await CoreWebView2Environment.CreateAsync(null, userDataFolder, null);
                await workStackWebView.EnsureCoreWebView2Async(environment);
                await sourceWebView.EnsureCoreWebView2Async(environment);
                ConfigureWorkStackWebView();
                ConfigureSourceWebView();
                status.Text = "Ready · WebView2 " + runtimeVersion;
                workStackWebView.CoreWebView2.Navigate(options.WorkStackUrl);

                if (!String.IsNullOrWhiteSpace(options.ProbeProvider))
                {
                    Rectangle probeBounds = new Rectangle(60, 90, Math.Max(640, ClientSize.Width - 120), Math.Max(480, ClientSize.Height - 170));
                    ShowSource(options.ProbeProvider, probeBounds);
                }
                if (options.AutoCloseSeconds > 0)
                {
                    autoCloseTimer = new Timer { Interval = Math.Min(options.AutoCloseSeconds, 60) * 1000 };
                    autoCloseTimer.Tick += delegate { autoCloseTimer.Stop(); Close(); };
                    autoCloseTimer.Start();
                }
            }
            catch (Exception error)
            {
                status.Text = "Startup failed";
                MessageBox.Show(this, error.Message, "Work Stack could not start", MessageBoxButtons.OK, MessageBoxIcon.Error);
                Close();
            }
        }

        private void ConfigureWorkStackWebView()
        {
            workStackWebView.CoreWebView2.Settings.AreDevToolsEnabled = false;
            workStackWebView.CoreWebView2.Settings.AreDefaultContextMenusEnabled = true;
            workStackWebView.CoreWebView2.Settings.IsPasswordAutosaveEnabled = false;
            workStackWebView.CoreWebView2.Settings.IsGeneralAutofillEnabled = false;
            workStackWebView.CoreWebView2.NavigationStarting += delegate(object sender, CoreWebView2NavigationStartingEventArgs eventArgs)
            {
                Uri target;
                if (!Uri.TryCreate(eventArgs.Uri, UriKind.Absolute, out target) || !IsWorkStackAllowed(target))
                {
                    eventArgs.Cancel = true;
                    status.Text = "Blocked navigation outside local Work Stack";
                }
            };
            workStackWebView.CoreWebView2.NavigationCompleted += delegate(object sender, CoreWebView2NavigationCompletedEventArgs eventArgs)
            {
                status.Text = eventArgs.IsSuccess ? "Work Stack ready" : "Work Stack navigation failed · " + eventArgs.WebErrorStatus;
            };
            workStackWebView.CoreWebView2.WebMessageReceived += OnWorkStackMessage;
            workStackWebView.CoreWebView2.NewWindowRequested += delegate(object sender, CoreWebView2NewWindowRequestedEventArgs eventArgs)
            {
                eventArgs.Handled = true;
                Uri target;
                if (Uri.TryCreate(eventArgs.Uri, UriKind.Absolute, out target) && IsMicrosoftAllowed(target))
                {
                    Process.Start(new ProcessStartInfo(eventArgs.Uri) { UseShellExecute = true });
                }
            };
        }

        private void ConfigureSourceWebView()
        {
            sourceWebView.CoreWebView2.Settings.AreDevToolsEnabled = false;
            sourceWebView.CoreWebView2.Settings.AreDefaultContextMenusEnabled = true;
            sourceWebView.CoreWebView2.Settings.IsPasswordAutosaveEnabled = false;
            sourceWebView.CoreWebView2.Settings.IsGeneralAutofillEnabled = false;
            sourceWebView.CoreWebView2.NavigationStarting += delegate(object sender, CoreWebView2NavigationStartingEventArgs eventArgs)
            {
                Uri target;
                if (!Uri.TryCreate(eventArgs.Uri, UriKind.Absolute, out target) || !IsMicrosoftAllowed(target))
                {
                    eventArgs.Cancel = true;
                    status.Text = "Blocked Microsoft source navigation outside the allowlist";
                }
            };
            sourceWebView.CoreWebView2.NavigationCompleted += delegate(object sender, CoreWebView2NavigationCompletedEventArgs eventArgs)
            {
                status.Text = eventArgs.IsSuccess
                    ? "Source Inbox · " + activeProvider + " · " + Host(sourceWebView.Source)
                    : "Microsoft source navigation failed · " + eventArgs.WebErrorStatus;
                RecordProbe(eventArgs);
            };
            sourceWebView.CoreWebView2.NewWindowRequested += delegate(object sender, CoreWebView2NewWindowRequestedEventArgs eventArgs)
            {
                eventArgs.Handled = true;
                Uri target;
                if (Uri.TryCreate(eventArgs.Uri, UriKind.Absolute, out target) && IsMicrosoftAllowed(target))
                {
                    sourceWebView.CoreWebView2.Navigate(eventArgs.Uri);
                }
            };
        }

        private void OnWorkStackMessage(object sender, CoreWebView2WebMessageReceivedEventArgs eventArgs)
        {
            string message;
            try { message = eventArgs.TryGetWebMessageAsString(); }
            catch { return; }
            if (String.Equals(message, SourceHostPrefix + "|hide", StringComparison.Ordinal))
            {
                HideSource();
                return;
            }
            string[] parts = message.Split('|');
            if (parts.Length != 7 || parts[0] != SourceHostPrefix || parts[1] != "show" || !providerUrls.ContainsKey(parts[2])) return;
            int left, top, width, height;
            if (!Int32.TryParse(parts[3], NumberStyles.Integer, CultureInfo.InvariantCulture, out left)
                || !Int32.TryParse(parts[4], NumberStyles.Integer, CultureInfo.InvariantCulture, out top)
                || !Int32.TryParse(parts[5], NumberStyles.Integer, CultureInfo.InvariantCulture, out width)
                || !Int32.TryParse(parts[6], NumberStyles.Integer, CultureInfo.InvariantCulture, out height)) return;
            if (left < -10000 || top < -10000 || left > 10000 || top > 10000
                || width < 160 || height < 120 || width > 10000 || height > 10000) return;
            ShowSource(parts[2], new Rectangle(left, top, width, height));
        }

        private void ShowSource(string provider, Rectangle requestedBounds)
        {
            string url;
            if (!providerUrls.TryGetValue(provider, out url)) return;
            Rectangle clipped = Rectangle.Intersect(workStackWebView.ClientRectangle, requestedBounds);
            if (clipped.Width < 160 || clipped.Height < 120)
            {
                HideSource();
                return;
            }
            Point screenLocation = workStackWebView.PointToScreen(clipped.Location);
            Point formLocation = PointToClient(screenLocation);
            sourceViewport.Bounds = new Rectangle(formLocation, clipped.Size);
            sourceWebView.Bounds = new Rectangle(
                requestedBounds.Left - clipped.Left,
                requestedBounds.Top - clipped.Top,
                requestedBounds.Width,
                requestedBounds.Height);
            sourceViewport.Visible = true;
            sourceViewport.BringToFront();
            if (!String.Equals(activeProvider, provider, StringComparison.OrdinalIgnoreCase))
            {
                activeProvider = provider;
                sourceWebView.CoreWebView2.Navigate(url);
            }
        }

        private void HideSource()
        {
            sourceViewport.Visible = false;
            activeProvider = "";
        }

        private bool IsWorkStackAllowed(Uri uri)
        {
            Uri configured;
            if (!Uri.TryCreate(options.WorkStackUrl, UriKind.Absolute, out configured)) return false;
            return uri.Scheme == configured.Scheme && uri.Host == configured.Host && uri.Port == configured.Port;
        }

        private static bool IsMicrosoftAllowed(Uri uri)
        {
            if (uri.Scheme != "https") return false;
            string host = uri.Host.ToLowerInvariant();
            string[] suffixes = {
                ".office.com", ".office365.com", ".microsoft.com", ".microsoftonline.com",
                ".sharepoint.com", ".cloud.microsoft", ".live.com", ".onenote.com"
            };
            foreach (string suffix in suffixes)
            {
                string root = suffix.Substring(1);
                if (host == root || host.EndsWith(suffix, StringComparison.Ordinal)) return true;
            }
            return false;
        }

        private void RecordProbe(CoreWebView2NavigationCompletedEventArgs eventArgs)
        {
            if (probeRecorded || String.IsNullOrWhiteSpace(options.ProbeResultPath)) return;
            probeRecorded = true;
            string result = "success=" + eventArgs.IsSuccess.ToString().ToLowerInvariant() + Environment.NewLine
                + "host=" + Host(sourceWebView.Source) + Environment.NewLine
                + "web_error=" + eventArgs.WebErrorStatus + Environment.NewLine
                + "runtime=" + runtimeVersion + Environment.NewLine;
            File.WriteAllText(options.ProbeResultPath, result);
            BeginInvoke(new Action(Close));
        }

        private static string Host(Uri uri)
        {
            return uri == null ? "initializing" : uri.Host;
        }

        private static bool IsReady(string baseUrl)
        {
            try
            {
                var request = (HttpWebRequest)WebRequest.Create(new Uri(new Uri(baseUrl), "/api/v1/health"));
                request.Method = "GET";
                request.Timeout = 1500;
                request.ReadWriteTimeout = 1500;
                using (var response = (HttpWebResponse)request.GetResponse()) return response.StatusCode == HttpStatusCode.OK;
            }
            catch { return false; }
        }

        private void StartServer()
        {
            string launcher = Path.Combine(options.InstallRoot, "scripts", "windows", "Start-WorkStack.ps1");
            if (!File.Exists(launcher)) throw new FileNotFoundException("Installed Work Stack launcher was not found.", launcher);
            string arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File " + Quote(launcher)
                + " -InstallRoot " + Quote(options.InstallRoot)
                + " -StateRoot " + Quote(options.StateRoot) + " -NoBrowser";
            var process = Process.Start(new ProcessStartInfo("powershell.exe", arguments)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden
            });
            process.WaitForExit();
            if (process.ExitCode != 0 || !IsReady(options.WorkStackUrl))
            {
                throw new InvalidOperationException("The installed Work Stack launcher did not produce a ready local server.");
            }
        }

        private void StopOwnedServer()
        {
            if (!serverStartedByShell) return;
            try
            {
                string stopper = Path.Combine(options.InstallRoot, "scripts", "windows", "Stop-WorkStack.ps1");
                if (!File.Exists(stopper)) return;
                string arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File " + Quote(stopper)
                    + " -InstallRoot " + Quote(options.InstallRoot);
                var process = Process.Start(new ProcessStartInfo("powershell.exe", arguments)
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden
                });
                process.WaitForExit(10000);
            }
            catch
            {
                // A shutdown failure must not rewrite planning state or block the window from closing.
            }
        }

        private static string Quote(string value)
        {
            return "\"" + value.Replace("\"", "\"\"") + "\"";
        }
    }
}
