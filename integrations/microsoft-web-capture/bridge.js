(() => {
  if (window.__workStackCaptureBridge) return;
  window.__workStackCaptureBridge = true;

  let timer = null;
  let attempts = 0;
  let pending = null;

  function stop() {
    if (timer !== null) window.clearInterval(timer);
    timer = null;
  }

  function deliver() {
    if (!pending || attempts >= 40) {
      stop();
      return;
    }
    attempts += 1;
    window.postMessage({
      type: 'workstack.source-capture.v1',
      token: pending.token,
      payload: pending.payload
    }, window.location.origin);
  }

  window.addEventListener('message', event => {
    if (event.source !== window || event.origin !== window.location.origin) return;
    if (event.data?.type !== 'workstack.source-capture.ack.v1' || event.data?.token !== pending?.token) return;
    stop();
    chrome.storage.local.remove('pendingWorkStackCapture');
  });

  chrome.storage.local.get('pendingWorkStackCapture').then(result => {
    pending = result.pendingWorkStackCapture || null;
    if (!pending) return;
    deliver();
    timer = window.setInterval(deliver, 500);
  });
})();
