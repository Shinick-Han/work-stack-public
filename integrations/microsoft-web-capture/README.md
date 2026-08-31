# Work Stack Microsoft Web Capture

This unpacked Chromium extension is the thin source adapter for Outlook, Teams, and OneNote web apps.

## Behavior

- Outlook prefers an explicit text selection.
- Teams and OneNote use the selected text when exposed, otherwise the text the user explicitly copied.
- Clipboard data is read only after the user clicks the Work Stack button.
- The extension opens Source Inbox and transfers a bounded draft through extension-local storage.
- Work Stack acknowledges and deletes the pending transfer before the user reviews and creates the Task.
- The extension never calls the planning API, claims OOB provenance, monitors the clipboard, or stores Microsoft credentials.

## Local installation

1. Open `edge://extensions`.
2. Enable Developer mode.
3. Choose **Load unpacked** and select this directory.
4. Open the extension details and choose **Extension options**.
5. Enter the Work Stack Source Inbox URL. The default is `http://127.0.0.1:8765/?surface=inbox`.

For a remote Linux Work Stack URL, saving the option requests access only to that configured origin and registers the local bridge there.
