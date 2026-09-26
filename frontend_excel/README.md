# Excel task pane — local HTTPS (Mac)

Excel blocks unsigned/self-signed certs (“not signed by a valid security certificate”).

## One-time setup (run in Terminal.app — needs your Mac password)

```bash
brew install mkcert
mkcert -install
cd /Users/oscarferrari/Documents/wealth_blotter_plus/frontend_excel
npm install
npm run certs
```

## Every session

```bash
cd /Users/oscarferrari/Documents/wealth_blotter_plus/frontend_excel
npm run dev
```

Then sideload `manifest.xml` in Excel.

If the error persists after `mkcert -install`, quit Excel fully (Cmd+Q) and reopen.
