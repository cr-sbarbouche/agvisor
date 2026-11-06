# agvisor-desktop

`<agvisor-desktop>` web component: shows an [agvisor](https://github.com/cr-sbarbouche/agvisor) GUI sandbox (a GNOME desktop under gVisor) in a web page.

> Alpha. Needs the agvisor gateway and a backend that mints tokens: this package is only the browser half.

```sh
npm install agvisor-desktop
```

```js
import "agvisor-desktop";   // safe to import during SSR (React / Next.js 'use client'): it does nothing on the server
```

```html
<agvisor-desktop gateway="wss://your.host/desktop" token="..." style="width:100%;height:600px"></agvisor-desktop>
```

- `gateway`: the `wss://` URL of your agvisor gateway.
- `token`: minted server-side with `Sandbox.desktop_token()` from the Python package (`pip install 'agvisor[web]'`). Set a new token to reconnect.
- Give the element a size with CSS. It fires an `error` event (`e.detail` = message) when the connection fails or drops, for example when a token has expired.

Setting up the gateway, the token and the sandbox: see the [agvisor README](https://github.com/cr-sbarbouche/agvisor#gui-in-a-web-page-react-nextjs-vue-plain-html). MIT licensed.
