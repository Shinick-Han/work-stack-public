(() => {
  if (window !== window.top || window.__workStackMicrosoftCapture) return;
  window.__workStackMicrosoftCapture = true;

  const hostname = location.hostname.toLowerCase();
  const provider = hostname.includes('outlook')
    ? 'outlook'
    : hostname.includes('teams')
      ? 'teams'
      : hostname.includes('onenote') || hostname.includes('sharepoint') || hostname === 'www.office.com'
        ? 'onenote'
        : null;
  if (!provider) return;

  const DEFAULT_WORK_STACK_URL = 'http://127.0.0.1:8765/?surface=inbox';

  function notice(message) {
    document.getElementById('workstack-source-capture-notice')?.remove();
    const element = document.createElement('div');
    element.id = 'workstack-source-capture-notice';
    element.textContent = message;
    document.documentElement.appendChild(element);
    window.setTimeout(() => element.remove(), 3500);
  }

  function currentSelection() {
    return window.getSelection()?.toString().trim() || '';
  }

  async function copiedText() {
    return (await navigator.clipboard.readText()).trim();
  }

  async function workStackUrl() {
    const stored = await chrome.storage.sync.get({ workStackUrl: DEFAULT_WORK_STACK_URL });
    return stored.workStackUrl || DEFAULT_WORK_STACK_URL;
  }

  async function handoff(text) {
    if (!text) throw new Error('Select or copy a message first.');
    const firstLine = text.split(/\r?\n/).find((line) => line.trim())?.trim() || `New ${provider} task`;
    const pendingCapture = {
      token: crypto.randomUUID(),
      payload: {
        provider,
        title: firstLine.slice(0, 500),
        text: text.slice(0, 4000),
        sourceUrl: location.href.slice(0, 4096),
        capturedAt: new Date().toISOString()
      }
    };
    await chrome.storage.local.set({ pendingWorkStackCapture: pendingCapture });
    window.open(await workStackUrl(), '_blank', 'noopener');
    notice('Sent to Work Stack for review.');
  }

  const button = document.createElement('button');
  button.id = 'workstack-source-capture-button';
  button.type = 'button';
  button.textContent = provider === 'outlook' ? '+ Selection → Work Stack' : '+ Copied text → Work Stack';
  button.setAttribute('aria-label', `Send ${provider} content to Work Stack`);
  document.documentElement.appendChild(button);

  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const selected = currentSelection();
      const text = selected || await copiedText();
      await handoff(text);
    } catch (error) {
      notice(`${error.name || 'CaptureError'}: ${error.message || 'Capture failed.'}`);
    } finally {
      button.disabled = false;
    }
  });
})();
