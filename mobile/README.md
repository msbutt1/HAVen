# HAVen phone app (Expo)

One app for iPhone and Android. It shows the laptop's HAVen over Wi-Fi: Live, Calibrate, Touch test,
History and Summary, plus a practice mode with made-up data that needs no laptop. The laptop still talks to the
sleeve and keeps every recording; the phone only shows and controls it.

The app runs the web dashboard's own adapters (`../dashboard/js`: contract, mock adapter, backend adapter), so the
phone follows the same rules as the browser dashboard. Metro watches that folder (`metro.config.js`).

## Run it on a phone (Expo Go, no Xcode needed)

1. Install **Expo Go** from the App Store or Google Play.
2. Put the phone and the laptop on the same Wi-Fi. Networks that keep devices apart (eduroam, guest or company
   Wi-Fi) will not work: use the phone's Personal Hotspot or a separate router instead.
3. On the laptop:

   ```bash
   cd mobile
   npm install
   npx expo start
   ```

4. iPhone: open the Camera and point it at the QR code in the terminal, then tap the banner to open in Expo Go.
   Android: scan it from inside Expo Go. Allow **Local Network** when iOS asks.
5. In a second terminal start the backend for phones:

   ```bash
   cd backend
   uv run vg-backend serve --device wifi --host 0.0.0.0
   ```

   It prints a second QR code (`http://<laptop>:<port>/dashboard/`, never the password). In the app tap
   **Scan the QR code** and point it at that one. Or open **Can't connect?** and type the address printed under it.

If `VG_BASIC_AUTH` is set on the laptop, the app asks for the user name and password once and keeps them in the
phone's secure storage. The app sends `X-HAVen-Client`, so the backend answers a missing password with a plain
401 instead of a browser sign-in challenge (iOS would otherwise hold the request open waiting for a prompt).

## Develop

```bash
npm test             # node --test over src/**/*.test.ts (address parsing, formatting, summary maths)
npx tsc --noEmit     # typecheck
```

Jest and ESLint are not wired up: `jest-expo` and `eslint-config-expo` currently clash with React 19.3.0 peer
versions, so the tests use Node's built-in runner on the plain TypeScript helpers in `src/vg/`.

Layout: `src/app/` routes (`index.tsx` connect screen, `scan.tsx` QR camera, `(tabs)/` the five main screens),
`src/state/` the store over the shared adapters, `src/components/` UI and SVG charts, `src/vg/` pure helpers.

## What is verified and what is not

- Verified in the iOS Simulator through Expo Go: bundling, the connect screen, practice mode, every tab, the
  Summary numbers matching the web Overview, switching laptops.
- **Not yet verified on a real phone:** scanning the laptop's QR code (the Simulator has no camera), and the
  password flow after the plain-401 fix.
- Expo Go only: no store build yet. A standalone build needs EAS (`npx eas-cli@latest build`).

Same honesty rules as the dashboard: practice data is striped and labelled, there are no safe or unsafe lines,
grip effort is relative muscle activity (not force), vibration is unweighted, and the score is experimental.
