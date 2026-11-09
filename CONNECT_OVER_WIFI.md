# Connect the wearable to your phone over Wi-Fi

A step-by-step guide for setting up the HAVen wearable (board W), the laptop and the Android app on one Wi-Fi
network. Part 1 is done once by whoever builds the firmware. Parts 2 to 4 are what you do every time.

## How it fits together

The phone does **not** talk to the wearable directly. The laptop sits in the middle: it receives the wearable's data,
works out grip effort, vibration and tool use, saves the recordings, and shows its dashboard to the phone.

```
   wearable (board W)  ── Wi-Fi ──►  laptop (HAVen backend)  ◄── Wi-Fi ──  phone (HAVen app)
   streams raw data                  processes and stores it                   shows the dashboard
```

All three must be on **the same Wi-Fi network**.

| What | Talks to | How it finds the other side |
|---|---|---|
| Wearable → laptop | TCP port 8765 | broadcasts on UDP 8766; the laptop answers |
| Phone → laptop | web port 8000 | Android: the app broadcasts on UDP 8767 and the laptop answers. iPhone: the laptop announces itself with Bonjour and the app looks for it |

You never type an address into the wearable. The phone only needs one if its search finds nothing.

## What you need

- The wearable (board W), flashed with the Wi-Fi name and password (Part 1), electrodes, and power (USB power bank or
  cable).
- A laptop with this repository, Python and `uv` (one-time: `cd backend && uv sync`).
- An Android phone with the app: `android/dist/HAVen-0.1.0-debug.apk` (build it with `android/build-apk.sh`).
- A Wi-Fi network that meets all of these:
  - **2.4 GHz.** The wearable's ESP32 cannot see 5 GHz networks.
  - **A normal password** (WPA2-Personal). Networks that ask for a user name and password, like eduroam or most
    company networks, will not work for the wearable.
  - **Lets devices talk to each other.** Guest, campus and company Wi-Fi often block this ("client isolation").

  The easiest choice is a phone hotspot or a small travel router set to 2.4 GHz:
  - **Android hotspot:** Settings › Network › Hotspot › Advanced (or AP band) › choose **2.4 GHz**.
  - **iPhone hotspot:** Settings › Personal Hotspot › turn on **Maximize Compatibility** (this makes it 2.4 GHz).

---

## Part 1 (once): put the Wi-Fi name and password into the wearable

The wearable has no screen, so its Wi-Fi details are built into its firmware. Do this once per network. If you change
networks, repeat it.

1. Install ESP-IDF **v6.1** (Espressif's toolkit), following Espressif's install guide, so it ends up in
   `~/esp/esp-idf-v6.1`.
2. Copy the example settings file and fill it in:

   ```bash
   cd firmware/wearable/main
   cp wifi_secrets.example.h wifi_secrets.h
   ```

   Edit `wifi_secrets.h`:

   ```c
   #define VG_WIFI_SSID "TeamHotspot"        // the Wi-Fi name, exactly as spelled (capitals matter)
   #define VG_WIFI_PASSWORD "your-password"
   #define VG_BRIDGE_HOST ""                 // leave empty: the wearable finds the laptop by itself
   ```

   `wifi_secrets.h` is git-ignored, so the password never goes into the repository.
3. Plug the wearable into the laptop with USB and find its port:

   ```bash
   ls /dev/cu.*        # macOS: look for cu.usbserial-… or cu.SLAB_USBtoUART
   ```

4. Build and flash it, then watch its log (replace the port with yours):

   ```bash
   cd firmware
   . ~/esp/esp-idf-v6.1/export.sh
   tools/build.sh wearable esp32_d0wd_v3_devkit30 -p /dev/cu.usbserial-0001 flash monitor
   ```

   Every 5 seconds the wearable prints a status line. The parts that matter here:

   ```
   seq=… wifi=1 tcp=0 bridge= …              joined the Wi-Fi, has not found the laptop yet
   seq=… wifi=1 tcp=1 bridge=192.168.43.20 … connected to the laptop at that address
   ```

   `wifi=0` means it has not joined the Wi-Fi: see Troubleshooting. Leave the monitor with `Ctrl+]`.

---

## Part 2 (every time): start the laptop

1. Join the laptop to the Wi-Fi from Part 1.
2. Start HAVen so the wearable **and** phones can reach it:

   ```bash
   cd backend
   uv run vg-backend serve --device wifi --host 0.0.0.0
   ```

   - `--device wifi` makes it wait for the real wearable. Without it, it plays a **simulated** wearable.
   - `--host 0.0.0.0` lets phones on the Wi-Fi in. On a network you do not control, add a password:
     `VG_BASIC_AUTH=team:something-long uv run vg-backend serve --device wifi --host 0.0.0.0`
     (the app asks for it once).
3. If macOS asks whether **Python** may accept incoming connections, choose **Allow**.
4. You should see these lines:

   ```
   vg-backend: phones on this Wi-Fi: open the HAVen app and tap Find on this Wi-Fi (answered on UDP 8767) …
   INFO:     Uvicorn running on http://0.0.0.0:8000
   ```

Keep this terminal open while you work. `Ctrl+C` stops HAVen.

## Part 3 (every time): turn on the wearable

1. Put the sleeve on, with the electrodes on the skin.
2. Power the wearable. It joins the Wi-Fi, finds the laptop and starts streaming on its own. It keeps trying until it
   succeeds, so the order of Parts 2 and 3 does not matter.
3. Check on the laptop: open `http://localhost:8000/dashboard/`. The band under the header turns green and reads
   **Live data from vg-w-…**, and the Sleeve shows **Ready**.

## Part 4 (every time): connect the phone

1. Join the phone to the **same** Wi-Fi.
2. Open the **HAVen** app. It searches by itself and shows the laptop (for example *Abdujabbors-MacBook-Air*).
   Tap **Open**. Next time it reopens the same laptop by itself.
3. If it finds nothing, tap **Can't connect?** and type the laptop's address. Find it on the laptop with:

   ```bash
   ipconfig getifaddr en0       # macOS, for example 192.168.43.20
   ```

   Type it as `192.168.43.20` (port 8000 is assumed).
4. You are connected when the Live screen says **Updating … times a second** and the band is green (**Live data**).
   A violet striped band means simulated data: the laptop was started without `--device wifi`.
5. **Calibrate** (bottom menu): relax, then squeeze, as the screen says. Do this again after every wearable restart;
   the app will tell you.

On an **iPhone** the HAVen iOS app works the same way (installed from Xcode for now; see `ios/README.md`):
allow **Local Network** when iOS asks, then tap **Open**. Any phone's browser works too:
`http://<laptop address>:8000/dashboard/`.

---

## Troubleshooting

| What you see | Likely cause | What to do |
|---|---|---|
| Wearable log shows `wifi=0` | Wrong name or password, a 5 GHz network, or a network that needs a user name (eduroam) | Check `wifi_secrets.h` (capitals matter), switch the hotspot to 2.4 GHz, then flash again (Part 1, step 4) |
| `wifi=1 tcp=0` and it stays like that | The laptop is not running `--device wifi`, is on another network, or the network blocks broadcasts | Check Part 2; put the laptop on the same Wi-Fi; or set `VG_BRIDGE_HOST` to the laptop's address in `wifi_secrets.h` and flash again |
| The laptop says the Wi-Fi port is already in use | Another HAVen (or `vg-bridge`) is still running | Stop it, then start again. To see who holds the port: `lsof -nP -iTCP:8765` |
| Laptop dashboard shows a violet **Simulated** band | It was started without `--device wifi` | Stop it (`Ctrl+C`) and start it with `--device wifi --host 0.0.0.0` |
| The app finds no laptop, or says **The laptop did not answer** | HAVen is not running, was started without `--host 0.0.0.0`, the phone is on another Wi-Fi, macOS blocked Python, or the network keeps devices apart | Start it as in Part 2; check the phone's Wi-Fi name; allow Python (System Settings › Network › Firewall); use a hotspot; or type the address |
| The app says **The laptop refused this phone** | HAVen was started with `--host` set to one address, and the phone used another | Restart it with `--host 0.0.0.0` as in Part 2 |
| The app opens, but readings are dashes and it says no new reading | The wearable lost power or Wi-Fi | Check its power; it reconnects by itself within seconds |
| Grip effort is a dash and says **Calibrate** | Not calibrated yet, or the wearable restarted | Calibrate (Part 4, step 5) |

**If the phone itself is the hotspot:** the wearable and the laptop join the phone's hotspot. The app's search may
not see the laptop then, because the phone searches its mobile-data side. Type the laptop's address instead (Part 4,
step 3). This setup has not been tested yet.

## Practise without the wearable

To check the laptop and phone side alone, start a **simulated** wearable on the laptop in a second terminal. Its data
is labelled simulated everywhere.

```bash
cd backend
uv run vg-backend fake-wifi
```

Or, with no laptop at all, tap **Practice with made-up data** in the app.

## Safety and honesty notes

- Grip effort is muscle activity relative to this wearer's calibration, not grip force.
- Vibration is unweighted (not ISO 5349), and the exposure score is experimental: neither is a safety limit.
- Traffic is plain `http` on the local network. Use `VG_BASIC_AUTH` on any network you do not control.

## Status

The software chain (wearable protocol → laptop → app) is tested with the simulated Wi-Fi wearable and the Android
emulator. Salaar has run board W over Wi-Fi to the laptop. **Board W with this backend and the app on a real phone has
not been tested end to end yet**: please note anything that differs from this guide.

More detail: `firmware/INTEGRATION_FOR_ABDULJABBAR.md` (the wearable), `backend/README.md` (the laptop),
`android/README.md` (the app).
