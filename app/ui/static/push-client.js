/* Small browser-API bridge for the optional Python-configured Web Push feature. */
window.CignalPush = {
  async enable(apiBase, bearerToken, vapidPublicKey) {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) throw new Error('Push is not supported by this browser');
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') throw new Error('Notification permission was not granted');
    const registration = await navigator.serviceWorker.ready;
    const decode = value => {
      const padding = '='.repeat((4 - value.length % 4) % 4);
      const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/');
      return Uint8Array.from(atob(base64), character => character.charCodeAt(0));
    };
    const subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: decode(vapidPublicKey) });
    const base = (apiBase || window.location.origin).replace(/\/$/, '');
    const response = await fetch(base + '/api/push/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + bearerToken },
      body: JSON.stringify({ subscription: subscription.toJSON() })
    });
    if (!response.ok) throw new Error('Could not save the push subscription');
    return true;
  }
};
