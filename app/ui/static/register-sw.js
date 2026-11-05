/* Minimal PWA configuration asset; application logic remains Python/NiceGUI. */
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/service-worker.js', { scope: '/' }).catch(() => {});
  });
}
