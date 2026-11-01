// <agvisor-desktop gateway="wss://host/path" token="..."></agvisor-desktop>
// Give the element a size with CSS. Fires an `error` CustomEvent (detail = message) on failure.
import Guacamole from 'guacamole-common-js';

// Lets SSR frameworks import this file on the server without crashing; it only does anything in a browser.
const Base = globalThis.HTMLElement ?? class {};

class AgvisorDesktop extends Base {
  static observedAttributes = ['gateway', 'token'];
  #client;
  #ro;
  #queued = false;

  connectedCallback() {
    this.tabIndex = 0; // keyboard events only reach the desktop while it has focus
    this.style.display ||= 'block';
    this.#schedule();
  }
  disconnectedCallback() { this.#stop(); }
  attributeChangedCallback() { this.#schedule(); }

  #schedule() { // coalesce the gateway + token attribute updates into one (re)connect
    if (this.#queued) return;
    this.#queued = true;
    queueMicrotask(() => { this.#queued = false; this.#start(); });
  }

  #stop() {
    this.#ro?.disconnect();
    this.#client?.disconnect();
    this.#client = this.#ro = null;
    this.replaceChildren();
  }

  #start() {
    this.#stop();
    const gateway = this.getAttribute('gateway');
    const token = this.getAttribute('token');
    if (!this.isConnected || !gateway || !token) return;

    const client = (this.#client = new Guacamole.Client(new Guacamole.WebSocketTunnel(gateway)));
    const display = client.getDisplay().getElement();
    this.append(display);

    new Guacamole.Mouse(display).onEach(['mousedown', 'mouseup', 'mousemove'], (e) => {
      this.focus();
      client.sendMouseState(e.state, true);
    });
    const keys = new Guacamole.Keyboard(this);
    keys.onkeydown = (k) => client.sendKeyEvent(1, k);
    keys.onkeyup = (k) => client.sendKeyEvent(0, k);
    this.onblur = () => keys.reset(); // no stuck modifier keys when focus leaves
    client.onerror = (s) => this.dispatchEvent(new CustomEvent('error', { detail: s.message }));

    let t;
    this.#ro = new ResizeObserver(() => {
      clearTimeout(t);
      t = setTimeout(() => client.sendSize(this.clientWidth, this.clientHeight), 200);
    });
    this.#ro.observe(this);

    const w = this.clientWidth || 1280, h = this.clientHeight || 720; // 0 = element has no CSS size
    client.connect(`token=${encodeURIComponent(token)}&width=${w}&height=${h}&dpi=96`);
  }
}

globalThis.customElements?.define('agvisor-desktop', AgvisorDesktop);
